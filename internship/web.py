"""Resume matching and application tracking with name or dashboard accounts."""
import hashlib
import hmac
import json
import secrets
from pathlib import Path

import requests
from flask import Flask, abort, flash, g, redirect, render_template, request, session

from .config import settings
from .parser import SECTIONS
from .scoring import load_profile
from .storage import connect, decode, ensure_user, initialize, now, rescore
from .metadata import COUNTRIES, SEASONS, STATES
from .resume import extract_pdf, skill_terms
from .applications import FIELDS, LONG_FIELDS, parse_answers, suggestions

STATUSES = ["not_started", "applied", "OA", "interview", "offer", "rejected", "closed"]
PREFIX = "/internship"


def create_app(overrides=None):
    app = Flask(__name__)
    app.config.update(settings())
    app.config.update(MAX_CONTENT_LENGTH=6 * 1024 * 1024, MAX_FORM_MEMORY_SIZE=256 * 1024, MAX_FORM_PARTS=40,
                      SESSION_COOKIE_NAME='internship_session', SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE='Lax')
    app.config.update(overrides or {})
    initialize(app.config["DATABASE"])
    if app.config['AUTH_MODE'] not in ('name', 'dashboard'):
        raise ValueError('AUTH_MODE must be name or dashboard')
    with connect(app.config['DATABASE']) as db:
        db.execute('INSERT OR IGNORE INTO runtime_settings VALUES(?,?)', ('session_secret', secrets.token_hex(32)))
        app.secret_key = app.config.get('SECRET_KEY') or db.execute("SELECT value FROM runtime_settings WHERE key='session_secret'").fetchone()[0]
    seed = Path(app.config["PROFILE_SEED"]).read_text()
    load_profile(seed)

    @app.before_request
    def authenticate():
        if request.mimetype == "multipart/form-data" and request.path != PREFIX + '/resume':
            abort(415, "Upload PDFs on the resume page.")
        if request.path in ("/health", PREFIX + "/style.css") and request.method in ("GET", "HEAD"):
            return None
        if request.path == '/' and request.method == 'GET':
            return redirect(PREFIX + '/')
        if not (request.path == PREFIX or request.path.startswith(PREFIX + "/")):
            abort(404)
        if app.config['AUTH_MODE'] == 'name':
            session.setdefault('csrf', secrets.token_hex(32))
            g.csrf = session['csrf']
            g.user = session.get('user_id')
            if request.method == 'POST':
                check_post()
            if request.path == PREFIX + '/login':
                return None
            if not g.user:
                return render_template('login.html', auth_mode='name'), 401
            with connect(app.config['DATABASE']) as db:
                ensure_user(db, g.user, seed)
                g.display_name = db.execute('SELECT display_name FROM users WHERE id=?', (g.user,)).fetchone()[0]
            return None
        cookie = request.cookies.get("rydberg_session")
        if not cookie:
            return render_template("login.html", login_url=app.config["LOGIN_URL"]), 401
        try:
            response = requests.get(app.config["AUTH_URL"], cookies={"rydberg_session": cookie},
                                    timeout=5, allow_redirects=False)
            if response.status_code in (401, 403):
                return render_template("login.html", login_url=app.config["LOGIN_URL"]), 401
            if response.status_code != 200:
                abort(503, "Dashboard identity service unavailable")
            identity = response.json()
            email = identity.get("email")
            if not isinstance(email, str) or "@" not in email or len(email) > 254 or any(ord(c) < 32 for c in email):
                abort(503, "Invalid dashboard identity")
        except (requests.RequestException, ValueError):
            abort(503, "Dashboard identity service unavailable")
        g.user = email.strip().lower()
        # Bound to the HttpOnly session; no second shared secret or identity header.
        g.csrf = hmac.new(cookie.encode(), b"rydberg-internship-csrf-v1", hashlib.sha256).hexdigest()
        if request.method == "POST":
            if request.mimetype not in ("application/x-www-form-urlencoded", 'multipart/form-data'):
                abort(415, "Only form text is supported")
            if request.headers.get("Origin") not in (None, app.config["PUBLIC_ORIGIN"]):
                abort(403, "Invalid origin")
            if not hmac.compare_digest(request.form.get("csrf", ""), g.csrf):
                abort(403, "Invalid CSRF token")
        with connect(app.config["DATABASE"]) as db:
            ensure_user(db, g.user, seed)

    def check_post():
        if request.mimetype not in ('application/x-www-form-urlencoded', 'multipart/form-data'):
            abort(415, 'Only forms are supported')
        origin = request.headers.get('Origin')
        if origin and origin.rstrip('/') not in (request.host_url.rstrip('/'), app.config['PUBLIC_ORIGIN']):
            abort(403, 'Invalid origin')
        if not hmac.compare_digest(request.form.get('csrf', ''), g.csrf):
            abort(403, 'Invalid CSRF token')

    @app.route(PREFIX + '/login', methods=['GET', 'POST'])
    def login():
        if app.config['AUTH_MODE'] != 'name':
            return redirect(app.config['LOGIN_URL'])
        if request.method == 'POST':
            name = ' '.join(request.form.get('name', '').split())
            if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
                return render_template('login.html', auth_mode='name', error='Enter a name between 1 and 100 characters.'), 400
            user = 'name:' + name.casefold()
            with connect(app.config['DATABASE']) as db:
                ensure_user(db, user, seed)
                db.execute('UPDATE users SET display_name=? WHERE id=?', (name, user))
            session.clear()
            session.update(user_id=user, csrf=secrets.token_hex(32))
            return redirect(PREFIX + '/', code=303)
        return render_template('login.html', auth_mode='name')

    @app.post(PREFIX + '/logout')
    def logout():
        session.clear()
        return redirect(PREFIX + '/login', code=303)

    @app.after_request
    def security_headers(response):
        response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # no-referrer makes Chromium serialize same-site POST origins as "null".
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/health")
    def health():
        try:
            with connect(app.config["DATABASE"]) as db:
                db.execute("SELECT 1 FROM roles LIMIT 1").fetchone()
                sources = [dict(r) for r in db.execute("SELECT * FROM sources")]
            return {"status": "ok", "module": "internship", "sources": sources}
        except Exception:
            return {"status": "error", "module": "internship"}, 503

    @app.get(PREFIX + "/style.css")
    def css():
        return app.response_class((Path(__file__).parent / "style.css").read_text(), mimetype="text/css")

    @app.get(PREFIX)
    def index_redirect():
        return redirect(PREFIX + "/")

    @app.get(PREFIX + "/")
    def index():
        clauses, params = ["s.user_id=?"], [g.user]
        search = request.args.get('q', '').strip()[:200]
        if search:
            clauses.append('(r.company LIKE ? OR r.role LIKE ?)')
            params.extend(['%' + search + '%'] * 2)
        for field in ("section", "company"):
            value = request.args.get(field, "").strip()
            if value:
                clauses.append(f"r.{field}=?")
                params.append(value)
        status = request.args.get("status", "")
        if status:
            if status not in STATUSES:
                abort(400, "Invalid status")
            clauses.append("COALESCE(t.status,'not_started')=?")
            params.append(status)
        try:
            threshold = float(request.args.get("score", "0"))
            page = max(1, int(request.args.get("page", "1")))
            if not -10000 <= threshold <= 10000 or page > 100000:
                raise ValueError()
        except ValueError:
            abort(400, "Invalid score or page")
        clauses.append("s.score>=?")
        params.append(threshold)
        if request.args.get("eligible", "1") != "0":
            clauses.append("s.eligible=1")
        if request.args.get('listing', 'open') != 'all':
            clauses.extend(['r.gone=0', 'r.closed_at IS NULL'])
        loc_clauses = ['l.role_id=r.id']
        for field in ('country', 'state'):
            value = request.args.get(field, '')
            if value:
                if value not in (COUNTRIES if field == 'country' else STATES):
                    abort(400, 'Invalid location filter')
                loc_clauses.append(f'l.{field}=?')
                params.append(value)
        if request.args.get('remote') == '1':
            loc_clauses.append('l.remote=1')
        if len(loc_clauses) > 1:
            clauses.append('EXISTS (SELECT 1 FROM role_locations l WHERE ' + ' AND '.join(loc_clauses) + ')')
        term_clauses = ['rt.role_id=r.id']
        season, year = request.args.get('season', ''), request.args.get('year', '')
        if season:
            if season not in SEASONS:
                abort(400, 'Invalid season')
            term_clauses.append('rt.season=?')
            params.append(season)
        if year:
            if not year.isdigit() or not 2000 <= int(year) <= 2100:
                abort(400, 'Invalid year')
            term_clauses.append('rt.year=?')
            params.append(int(year))
        if len(term_clauses) > 1:
            clauses.append('EXISTS (SELECT 1 FROM role_terms rt WHERE ' + ' AND '.join(term_clauses) + ')')
        coop = request.args.get('coop', '')
        if coop:
            if coop not in ('0', '1'):
                abort(400, 'Invalid co-op filter')
            clauses.append('r.is_coop=?')
            params.append(int(coop))
        orders = {'score': 's.score DESC,r.first_seen DESC,r.id',
                  'first_seen': 'r.first_seen DESC,r.id', 'company': 'r.company,r.role,r.id',
                  'term': "COALESCE((SELECT min(rt.year*10 + CASE rt.season WHEN 'winter' THEN 1 WHEN 'spring' THEN 2 WHEN 'summer' THEN 3 ELSE 4 END) FROM role_terms rt WHERE rt.role_id=r.id),99999),s.score DESC,r.id"}
        sort = request.args.get('sort', 'score')
        if sort not in orders:
            abort(400, 'Invalid sort')
        order = orders[sort]
        with connect(app.config["DATABASE"]) as db:
            rows = db.execute('''SELECT r.*, s.score,s.matched,s.eligible,s.excluded,
              COALESCE(t.status,'not_started') AS status, t.applied_at, COALESCE(t.notes,'') AS notes
              FROM roles r JOIN scores s ON s.role_id=r.id
              LEFT JOIN tracking t ON t.role_id=r.id AND t.user_id=s.user_id WHERE ''' +
              " AND ".join(clauses) + " ORDER BY " + order + " LIMIT 101 OFFSET ?", params + [(page-1)*100]).fetchall()
            companies = [r[0] for r in db.execute("SELECT DISTINCT company FROM roles ORDER BY company")]
            sources = [dict(r) for r in db.execute("SELECT * FROM sources")]
            years = sorted({2027} | {r[0] for r in db.execute('SELECT DISTINCT year FROM role_terms')})
            resume_name = db.execute('SELECT resume_filename FROM users WHERE id=?', (g.user,)).fetchone()[0]
            total = db.execute("SELECT count(*) FROM roles r JOIN scores s ON s.role_id=r.id LEFT JOIN tracking t ON t.role_id=r.id AND t.user_id=s.user_id WHERE " + " AND ".join(clauses), params).fetchone()[0]
        from urllib.parse import urlencode
        def page_url(number):
            return PREFIX + "/?" + urlencode({**request.args.to_dict(), "page": number})
        return render_template("index.html", roles=[decode(r) for r in rows[:100]], statuses=STATUSES,
                               sections=SECTIONS, companies=companies, sources=sources, page=page,
                               countries=COUNTRIES, states=STATES, seasons=SEASONS, years=years,
                               resume_name=resume_name, total=total,
                               prev=page_url(page-1) if page > 1 else None,
                               next=page_url(page+1) if len(rows) > 100 else None)

    @app.post(PREFIX + '/refresh')
    def refresh():
        from .worker import crawl
        discovered = crawl(app.config)
        with connect(app.config['DATABASE']) as db:
            errors = [r[0] for r in db.execute('SELECT last_error FROM sources WHERE last_error IS NOT NULL')]
        flash(f'Refresh completed: {discovered} new postings.' + (' Some sources failed; see source status.' if errors else ''))
        return redirect(PREFIX + '/', code=303)

    @app.post(PREFIX + "/roles/<role_id>")
    def track(role_id):
        if set(request.form) - {"csrf", "status", "notes"}:
            abort(400, "Unknown tracking fields")
        status, notes = request.form.get("status"), request.form.get("notes", "")
        if status not in STATUSES or len(notes) > 10000 or "\x00" in notes:
            abort(400, "Invalid status or notes (maximum 10000 characters)")
        with connect(app.config["DATABASE"]) as db:
            if not db.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
                abort(404)
            db.execute('''INSERT INTO tracking(user_id,role_id,status,applied_at,notes) VALUES(?,?,?,?,?)
              ON CONFLICT(user_id,role_id) DO UPDATE SET status=excluded.status, notes=excluded.notes,
              applied_at=COALESCE(tracking.applied_at,excluded.applied_at)''',
              (g.user, role_id, status, now() if status == "applied" else None, notes))
        return redirect(PREFIX + "/", code=303)

    @app.route(PREFIX + "/profile", methods=["GET", "POST"])
    def profile():
        error = None
        with connect(app.config["DATABASE"]) as db:
            raw = db.execute("SELECT profile FROM users WHERE id=?", (g.user,)).fetchone()[0]
            details = json.loads(db.execute('SELECT details FROM users WHERE id=?', (g.user,)).fetchone()[0])
            if request.method == "POST":
                if request.form.get('action') == 'applicant':
                    try:
                        details = {k: v for k, v in parse_answers(request.form).items() if k in FIELDS}
                    except ValueError as exc:
                        abort(400, str(exc))
                    db.execute('UPDATE users SET details=? WHERE id=?', (json.dumps(details), g.user))
                    flash('Applicant details saved for future forms.')
                    return redirect(PREFIX + '/profile', code=303)
                if set(request.form) - {"csrf", "profile", 'action'}:
                    abort(400, "Unknown profile fields")
                raw = request.form.get("profile", "")
                try:
                    load_profile(raw)
                    db.execute("UPDATE users SET profile=? WHERE id=?", (raw, g.user))
                    rescore(db, g.user, raw)
                except ValueError as exc:
                    error = str(exc)
                else:
                    return redirect(PREFIX + "/profile", code=303)
            deliveries = [dict(r) for r in db.execute('''SELECT r.company,r.role,d.backend,d.state,d.error,d.created_at
              FROM deliveries d JOIN roles r ON r.id=d.role_id WHERE d.user_id=? ORDER BY d.created_at DESC LIMIT 100''', (g.user,))]
        return render_template("profile.html", raw=raw, details=details, fields=FIELDS,
                               long_fields=LONG_FIELDS, error=error, deliveries=deliveries), 400 if error else 200

    @app.route(PREFIX + '/resume', methods=['GET', 'POST'])
    def resume():
        error = None
        with connect(app.config['DATABASE']) as db:
            if request.method == 'POST':
                action = request.form.get('action', 'upload')
                if action == 'remove':
                    db.execute("UPDATE users SET resume_text='',resume_filename='' WHERE id=?", (g.user,))
                    rescore(db, g.user, db.execute('SELECT profile FROM users WHERE id=?', (g.user,)).fetchone()[0])
                    flash('Resume removed and roles rescored.')
                    return redirect(request.path, code=303)
                if action != 'upload' or set(request.files) - {'resume'}:
                    abort(400, 'Invalid upload fields')
                try:
                    upload = request.files.get('resume')
                    text = extract_pdf(upload)
                    filename = upload.filename.replace('\\', '/').rsplit('/', 1)[-1][:200]
                    db.execute('UPDATE users SET resume_text=?,resume_filename=? WHERE id=?', (text, filename, g.user))
                    rescore(db, g.user, db.execute('SELECT profile FROM users WHERE id=?', (g.user,)).fetchone()[0])
                except ValueError as exc:
                    error = str(exc)
                else:
                    flash('Resume uploaded. All internship fit scores have been updated.')
                    return redirect(request.path, code=303)
            row = db.execute('SELECT resume_text,resume_filename FROM users WHERE id=?', (g.user,)).fetchone()
        return render_template('resume.html', resume=row, skills=skill_terms(row['resume_text']), error=error), 400 if error else 200

    @app.route(PREFIX + '/roles/<role_id>/application', methods=['GET', 'POST'])
    def application(role_id):
        with connect(app.config['DATABASE']) as db:
            row = db.execute("SELECT r.*,s.score,s.matched,s.eligible,s.excluded,COALESCE(t.status,'not_started') AS status,COALESCE(t.notes,'') AS notes FROM roles r JOIN scores s ON s.role_id=r.id AND s.user_id=? LEFT JOIN tracking t ON t.role_id=r.id AND t.user_id=s.user_id WHERE r.id=?", (g.user, role_id)).fetchone()
            if not row:
                abort(404)
            role = decode(row)
            saved = db.execute('SELECT * FROM applications WHERE user_id=? AND role_id=?', (g.user, role_id)).fetchone()
            answers = json.loads(saved['answers']) if saved else {}
            status, notes = role['status'], role['notes']
            if request.method == 'POST':
                if set(request.form) - {*FIELDS, 'csrf', 'question', 'answer', 'action', 'status', 'notes'}:
                    abort(400, 'Unknown application fields')
                try:
                    answers = parse_answers(request.form)
                except ValueError as exc:
                    abort(400, str(exc))
                status, notes = request.form.get('status', status), request.form.get('notes', notes)
                if status not in STATUSES or len(notes) > 10000 or '\x00' in notes:
                    abort(400, 'Invalid status or notes')
                action = request.form.get('action', 'save')
                offered = suggestions(db, g.user, role_id, answers.get('question', ''))
                if action == 'save':
                    raw_answers = json.dumps(answers)
                    db.execute('INSERT INTO applications VALUES(?,?,?,?) ON CONFLICT(user_id,role_id) DO UPDATE SET answers=excluded.answers,updated_at=excluded.updated_at', (g.user, role_id, raw_answers, now()))
                    if not saved or saved['answers'] != raw_answers:
                        db.execute('INSERT INTO application_history(user_id,role_id,answers,saved_at) VALUES(?,?,?,?)', (g.user, role_id, raw_answers, now()))
                    db.execute('''INSERT INTO tracking(user_id,role_id,status,applied_at,notes) VALUES(?,?,?,?,?)
                        ON CONFLICT(user_id,role_id) DO UPDATE SET status=excluded.status,notes=excluded.notes,
                        applied_at=COALESCE(tracking.applied_at,excluded.applied_at)''',
                        (g.user, role_id, status, now() if status == 'applied' else None, notes))
                    flash('Application draft and tracking saved.')
                    return redirect(request.path, code=303)
                elif action == 'assist':
                    for key, value in offered.items():
                        if not answers.get(key, '').strip():
                            answers[key] = value['value']
                    flash('Suggestions filled empty fields. Review them, then save your draft.')
                elif action.startswith('reuse:') and action[6:] in offered:
                    answers[action[6:]] = offered[action[6:]]['value']
                elif action != 'lookup':
                    abort(400, 'Invalid assistance action')
            offered = suggestions(db, g.user, role_id, answers.get('question', ''))
            history = [dict(r) for r in db.execute('SELECT saved_at FROM application_history WHERE user_id=? AND role_id=? ORDER BY id DESC LIMIT 10', (g.user, role_id))]
        return render_template('application.html', role=role, answers=answers, suggestions=offered,
            fields=FIELDS, long_fields=LONG_FIELDS, statuses=STATUSES, status=status, notes=notes,
            history=history, updated_at=saved['updated_at'] if saved else None)

    @app.get(PREFIX + "/profile.yaml")
    def download_profile():
        with connect(app.config["DATABASE"]) as db:
            raw = db.execute("SELECT profile FROM users WHERE id=?", (g.user,)).fetchone()[0]
        return app.response_class(raw, mimetype="text/plain", headers={"Content-Disposition": 'attachment; filename="profile.yaml"'})

    return app
