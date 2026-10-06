# Rydberg Internship

A Flask internship workspace with resume PDF matching, source-backed term and
location filters, name-based accounts, application drafts, and reusable answers.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/flask --app 'internship.web:create_app()' run --host 127.0.0.1 --port 8091
```

Open http://localhost:8091/internship/, enter a name, and click **Refresh
internships** to fetch the summer and off-season feeds. Upload a text-based PDF
under **Resume**, then browse by category, country, US state, season, year, co-op,
or remote location. Choose **Internship term** sorting for chronological results.

To refresh on a schedule, run `.venv/bin/python -m internship.worker` in another
terminal. The worker and web process must use the same database path.

Alternatively, run the isolated local Docker stack:

```bash
docker compose -f docker-compose.local.yml up --build -d
```

This publishes the same local port and starts a scheduled worker. The existing
`docker-compose.yml` remains the Rydberg module entry point, joins `rydberg-net`,
and routes `/internship` through Traefik. It defaults to name-based accounts.
Set `INTERNSHIP_AUTH_MODE=dashboard` to retain dashboard session authentication.

## Application workspace

Open any listing's **Application workspace** to fill contact details, experience,
motivation, a cover letter, and an employer question. **Fill empty fields** reuses
the most recent saved answers, applicant profile, and resume contact details.
Each suggestion names its source; **Use this answer** replaces only that field.
Question lookup ignores punctuation and letter case, but requires matching
question wording. Suggestions are deterministic and never invent experience or
authorization details. Review company-specific answers before using them.

Saving a draft records a version and preserves your chosen status. Set it to
`applied` after submitting to the employer. Drafts are prepared here and copied
into the linked employer form; this app does not submit employer applications.

Enter the same name to return to the same workspace, ignoring letter case and
extra spaces. Name-only accounts intentionally allow anyone who knows a name
to open that workspace. Separate names keep separate resumes and applications.

## Data and matching

The implementation follows `/home/seankimjr/Rydberg/README.md`'s module convention:
each module owns its database. The dashboard uses PostgreSQL/SQLAlchemy; this
smaller module retains its existing SQLite store rather than modifying dashboard
tables. By default the local database is `data/internship.sqlite3`; Docker stores
it at `/data/internship.sqlite3` in a persistent named volume. Override with
`INTERNSHIP_DATABASE`. Startup performs additive migrations preserving old data.

Tables: `users` holds matching preferences, applicant details, and extracted
resume text; `roles`, `sources`, and `memberships` hold shared discoveries;
`role_locations` and `role_terms` provide indexed filtering; `scores` and
`tracking` are per account; `applications` holds current drafts;
`application_history` holds saved versions. `runtime_settings` persists the
cookie-signing key. Existing notification tables remain in place. No original
PDF binaries are retained.

Resume extraction uses [pypdf](https://pypdf.readthedocs.io/en/stable/user/extract-text.html).
Upload limits: 5 MiB, 20 pages, 100,000 text characters. Image-only PDFs require
OCR before uploading. Scoring compares detected skills with the listing title
and category, adding two points per matching skill to weighted YAML preferences.
Matched skills are displayed; the score is not a hiring probability or a full
job-description assessment.

Feeds come from [SimplifyJobs](https://github.com/SimplifyJobs/Summer2027-Internships):
the summer README and `README-Off-Season.md`. Explicit terms override the summer
feed's default term. Multiple terms are supported. Unknown terms and locations
stay unspecified, and co-ops require a co-op label in the title. Refresh failures
retain existing listings and display source errors; coverage depends on the feeds.

## Verify

```bash
.venv/bin/python -m unittest discover -s tests -v
```
