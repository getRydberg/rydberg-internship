"""SQLite persistence; source membership is separate from durable role identity."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .scoring import load_profile, score_role
from .metadata import location_metadata, role_metadata, SEASONS


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users (
          id TEXT PRIMARY KEY, profile TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS roles (
          id TEXT PRIMARY KEY, company TEXT NOT NULL, role TEXT NOT NULL,
          section TEXT NOT NULL, locations TEXT NOT NULL, flags TEXT NOT NULL,
          apply_url TEXT NOT NULL, source_age TEXT NOT NULL,
          first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, closed_at TEXT,
          gone INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS sources (
          url TEXT PRIMARY KEY, last_success TEXT, last_error TEXT, role_count INTEGER);
        CREATE TABLE IF NOT EXISTS memberships (
          source TEXT NOT NULL REFERENCES sources(url), role_id TEXT NOT NULL REFERENCES roles(id),
          present INTEGER NOT NULL, closed INTEGER NOT NULL,
          PRIMARY KEY(source, role_id));
        CREATE TABLE IF NOT EXISTS tracking (
          user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
          status TEXT NOT NULL CHECK(status IN ('not_started','applied','OA','interview','offer','rejected','closed')),
          applied_at TEXT, notes TEXT NOT NULL DEFAULT '', PRIMARY KEY(user_id, role_id));
        CREATE TABLE IF NOT EXISTS scores (
          user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
          score REAL NOT NULL, matched TEXT NOT NULL, eligible INTEGER NOT NULL, excluded TEXT NOT NULL,
          PRIMARY KEY(user_id, role_id));
        CREATE TABLE IF NOT EXISTS deliveries (
          user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
          backend TEXT NOT NULL, mode TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending', sent_at TEXT, error TEXT,
          PRIMARY KEY(user_id, role_id, backend));
        CREATE TABLE IF NOT EXISTS digests (
          user_id TEXT NOT NULL, backend TEXT NOT NULL, day TEXT NOT NULL,
          PRIMARY KEY(user_id, backend, day));
        CREATE TABLE IF NOT EXISTS runtime_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS role_locations (
          role_id TEXT NOT NULL REFERENCES roles(id), location TEXT NOT NULL,
          country TEXT NOT NULL, state TEXT NOT NULL, remote INTEGER NOT NULL,
          PRIMARY KEY(role_id,location));
        CREATE TABLE IF NOT EXISTS role_terms (
          role_id TEXT NOT NULL REFERENCES roles(id), season TEXT NOT NULL, year INTEGER NOT NULL,
          PRIMARY KEY(role_id,season,year));
        CREATE TABLE IF NOT EXISTS applications (
          user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
          answers TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL,
          PRIMARY KEY(user_id,role_id));
        CREATE TABLE IF NOT EXISTS application_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id TEXT NOT NULL REFERENCES users(id), role_id TEXT NOT NULL REFERENCES roles(id),
          answers TEXT NOT NULL, saved_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS locations_filter ON role_locations(country,state,role_id);
        CREATE INDEX IF NOT EXISTS terms_filter ON role_terms(year,season,role_id);
        CREATE INDEX IF NOT EXISTS history_user ON application_history(user_id,id DESC);
        ''')
        # Web workers and the scheduler may start together against the same volume.
        db.execute('BEGIN IMMEDIATE')
        # Additive migration: preserve existing accounts, tracking, and notification records.
        for table, columns in {
            'users': {'display_name': "TEXT NOT NULL DEFAULT ''", 'resume_text': "TEXT NOT NULL DEFAULT ''",
                      'resume_filename': "TEXT NOT NULL DEFAULT ''", 'details': "TEXT NOT NULL DEFAULT '{}'"},
            'roles': {'terms': "TEXT NOT NULL DEFAULT '[]'", 'is_coop': 'INTEGER NOT NULL DEFAULT 0'},
        }.items():
            existing = {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}
            for name, definition in columns.items():
                if name not in existing:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
        for row in db.execute('SELECT * FROM roles WHERE NOT EXISTS (SELECT 1 FROM role_locations l WHERE l.role_id=roles.id)').fetchall():
            role = decode(row)
            source = db.execute('SELECT source FROM memberships WHERE role_id=? LIMIT 1', (role['id'],)).fetchone()
            save_metadata(db, role, source[0] if source else '')


def decode(row):
    result = dict(row)
    for key in ("locations", "flags", "matched", "excluded", "terms"):
        if key in result:
            result[key] = json.loads(result[key])
    return result


def save_score(db, user, role, profile):
    row = db.execute('SELECT resume_text FROM users WHERE id=?', (user,)).fetchone()
    result = score_role(role, profile, row['resume_text'] if row else '')
    db.execute("INSERT OR REPLACE INTO scores VALUES(?,?,?,?,?,?)",
               (user, role["id"], result["score"], json.dumps(result["matched"]),
                result["eligible"], json.dumps(result["excluded"])))
    return result


def rescore(db, user, raw):
    profile = load_profile(raw)
    for row in db.execute("SELECT * FROM roles").fetchall():
        save_score(db, user, decode(row), profile)


def ensure_user(db, user, seed):
    load_profile(seed)
    inserted = db.execute("INSERT OR IGNORE INTO users(id,profile,created_at) VALUES(?,?,?)", (user, seed, now())).rowcount
    if inserted:
        rescore(db, user, seed)


def ingest(db, source, roles, timestamp=None):
    timestamp = timestamp or now()
    db.execute("INSERT OR IGNORE INTO sources(url) VALUES(?)", (source,))
    db.execute("UPDATE memberships SET present=0 WHERE source=?", (source,))
    new_ids = set()
    for role in roles:
        role = dict(role)
        if not role["apply_url"] and role["closed"]:
            # Closed upstream cells sometimes drop both URLs. Keep the historical URL
            # only when company/title/location identifies exactly one known posting.
            candidates = db.execute('''SELECT r.id FROM roles r JOIN memberships m ON m.role_id=r.id
              WHERE m.source=? AND r.company=? AND r.role=? AND r.locations=?''',
              (source, role["company"], role["role"], json.dumps(role["locations"]))).fetchall()
            if len(candidates) == 1:
                role["id"] = candidates[0]["id"]
        inserted = db.execute('''INSERT OR IGNORE INTO roles
          (id,company,role,section,locations,flags,apply_url,source_age,first_seen,last_seen)
          VALUES(?,?,?,?,?,?,?,?,?,?)''',
          (role["id"], role["company"], role["role"], role["section"], json.dumps(role["locations"]),
           json.dumps(role["flags"]), role["apply_url"], role["source_age"], timestamp, timestamp)).rowcount
        if inserted:
            new_ids.add(role["id"])
        db.execute('''UPDATE roles SET section=?, locations=?, flags=?, source_age=?, last_seen=? WHERE id=?''',
                   (role["section"], json.dumps(role["locations"]), json.dumps(role["flags"]),
                    role["source_age"], timestamp, role["id"]))
        db.execute("INSERT OR REPLACE INTO memberships VALUES(?,?,1,?)", (source, role["id"], role["closed"]))
        save_metadata(db, role, source)
    for row in db.execute("SELECT role_id FROM memberships WHERE source=?", (source,)).fetchall():
        counts = db.execute("SELECT count(*) AS present, sum(NOT closed) AS open FROM memberships WHERE role_id=? AND present=1", (row[0],)).fetchone()
        closed = not counts["open"]
        db.execute("UPDATE roles SET gone=?, closed_at=CASE WHEN ? THEN COALESCE(closed_at,?) ELSE NULL END WHERE id=?",
                   (not counts["present"], closed, timestamp, row[0]))
    db.execute("UPDATE sources SET last_success=?, last_error=NULL, role_count=? WHERE url=?", (timestamp, len(roles), source))
    return new_ids


def save_metadata(db, role, source=''):
    terms, coop = role_metadata(role, source)
    # Use fresh source terms, preserving known terms only when none are supplied.
    previous = db.execute('SELECT terms FROM roles WHERE id=?', (role['id'],)).fetchone()
    if not terms and previous:
        terms = json.loads(previous[0])
    db.execute('UPDATE roles SET terms=?, is_coop=? WHERE id=?', (json.dumps(terms), coop, role['id']))
    db.execute('DELETE FROM role_locations WHERE role_id=?', (role['id'],))
    for location in dict.fromkeys(role['locations']):
        meta = location_metadata(location)
        db.execute('INSERT INTO role_locations VALUES(?,?,?,?,?)',
                   (role['id'], location, meta['country'], meta['state'], meta['remote']))
    db.execute('DELETE FROM role_terms WHERE role_id=?', (role['id'],))
    for term in terms:
        season, year = term.split()
        db.execute('INSERT INTO role_terms VALUES(?,?,?)', (role['id'], season.lower(), int(year)))


def score_and_queue(db, new_ids, timestamp=None):
    timestamp = timestamp or now()
    for user in db.execute("SELECT * FROM users").fetchall():
        profile = load_profile(user["profile"])
        for row in db.execute("SELECT * FROM roles").fetchall():
            role = decode(row)
            result = save_score(db, user["id"], role, profile)
            if role["id"] not in new_ids or role["closed_at"] or role["gone"] or not result["eligible"] or result["score"] < profile["min_score"]:
                continue
            payload = {**role, **result}
            for backend in set(profile["notifications"]["backends"]):
                db.execute('''INSERT OR IGNORE INTO deliveries
                  (user_id,role_id,backend,mode,payload,created_at) VALUES(?,?,?,?,?,?)''',
                  (user["id"], role["id"], backend, profile["notifications"]["mode"], json.dumps(payload), timestamp))
