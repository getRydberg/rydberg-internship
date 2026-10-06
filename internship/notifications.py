"""Operator-controlled, per-user destinations. No third-party API credentials."""
import hashlib
import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
import yaml

from .storage import connect, now


def message(payloads):
    return "\n\n".join(f"{p['company']} — {p['role']}\nscore {p['score']:g} — {', '.join(p['matched'])}\n{p['apply_url']}" for p in payloads)


def send(backend, config, user, payloads, delivery_id):
    text = message(payloads)
    if backend == "smtp":
        mail = EmailMessage()
        mail["Subject"] = f"Rydberg Internship: {len(payloads)} new matches"
        mail["From"] = config["from"]
        mail["To"] = user
        mail["Message-ID"] = f"<{delivery_id}@rydberg-internship>"
        mail.set_content(text)
        with smtplib.SMTP(config["host"], int(config.get("port", 25)), timeout=20) as smtp:
            if config.get("starttls", True):
                smtp.starttls()
            smtp.send_message(mail)
    elif backend in ("ntfy", "webhook"):
        url = config["url"]
        if not url.startswith(("http://", "https://")):
            raise ValueError("Notification URL must be HTTP(S)")
        kwargs = {"data": text.encode("utf-8")} if backend == "ntfy" else {"json": {"id": delivery_id, "user": user, "text": text, "roles": payloads}}
        response = requests.post(url, **kwargs, headers={"Idempotency-Key": delivery_id}, timeout=20, allow_redirects=False)
        if not 200 <= response.status_code < 300:
            raise ValueError(f"Notification HTTP {response.status_code}")
    else:
        raise ValueError("Unknown backend")


def deliver(settings, sender=send, clock=None):
    clock = clock or datetime.now(timezone.utc)
    config_file = Path(settings["NOTIFICATION_CONFIG"])
    config = yaml.safe_load(config_file.read_text()) if config_file.exists() else {}
    destinations = (config or {}).get("users", {})
    with connect(settings["DATABASE"]) as db:
        rows = db.execute("SELECT * FROM deliveries WHERE state='pending' ORDER BY created_at, role_id").fetchall()
    groups = {}
    for row in rows:
        if row["mode"] == "digest" and clock.hour < settings["DIGEST_HOUR"]:
            continue
        key = (row["user_id"], row["backend"], row["mode"], row["role_id"] if row["mode"] == "immediate" else "daily")
        groups.setdefault(key, []).append(row)
    for (user, backend, mode, _), batch in groups.items():
        destination = destinations.get(user, {}).get(backend)
        if not destination:
            with connect(settings["DATABASE"]) as db:
                db.executemany("UPDATE deliveries SET error='Backend destination not configured' WHERE user_id=? AND role_id=? AND backend=?",
                               [(user, row["role_id"], backend) for row in batch])
            continue
        day = clock.date().isoformat()
        with connect(settings["DATABASE"]) as db:
            db.execute("BEGIN IMMEDIATE")
            if mode == "digest":
                if not db.execute("INSERT OR IGNORE INTO digests VALUES(?,?,?)", (user, backend, day)).rowcount:
                    continue
            # Claim before side effects: ambiguous failures never auto-resend.
            claimed = []
            for row in batch:
                if db.execute("UPDATE deliveries SET state='sending',error=NULL WHERE user_id=? AND role_id=? AND backend=? AND state='pending'",
                              (user, row["role_id"], backend)).rowcount:
                    claimed.append(row)
        if not claimed:
            continue
        key = hashlib.sha256(json.dumps([user, backend, [r["role_id"] for r in claimed]]).encode()).hexdigest()
        state, error = "sent", None
        try:
            sender(backend, destination, user, [json.loads(row["payload"]) for row in claimed], key)
        except Exception as exc:
            state, error = "failed", type(exc).__name__
        with connect(settings["DATABASE"]) as db:
            db.executemany("UPDATE deliveries SET state=?,sent_at=?,error=? WHERE user_id=? AND role_id=? AND backend=?",
                           [(state, now() if state == "sent" else None, error, user, row["role_id"], backend) for row in claimed])
