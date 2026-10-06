import json
import tempfile
import unittest
from pathlib import Path

from internship.parser import parse_readme
from internship.scoring import load_profile, score_role
from internship.storage import connect, initialize, ensure_user, ingest, score_and_queue
from internship.worker import crawl
from test_parser import table, row

SEED = Path(__file__).resolve().parents[1] / "profile.yaml"


class StorageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "db.sqlite3")
        initialize(self.path)
        self.roles = parse_readme(table(row()))
        self.profile = SEED.read_text().replace("backends: []", "backends: [webhook]")


class StorageTests(StorageCase):
    def test_scoring(self):
        profile = load_profile(self.profile)
        score = score_role(self.roles[0], profile)
        self.assertEqual(score["score"], 8)
        self.assertEqual(score["matched"], ["firmware", "rust", "AMD", "Santa Clara"])
        self.assertTrue(score["eligible"])
        self.assertFalse(score_role({**self.roles[0], "locations": ["Toronto, ON"]}, profile)["eligible"])
        self.assertTrue(score_role({**self.roles[0], "locations": ["Toronto, ON", "NYC"]}, profile)["eligible"])
        self.assertFalse(score_role({**self.roles[0], "role": "firmware sales"}, profile)["eligible"])
        self.assertFalse(score_role({**self.roles[0], "flags": {"advanced_degree": True}}, profile)["eligible"])
        self.assertEqual(score_role({**self.roles[0], "role": "Trust Intern", "company": "Other", "locations": []}, profile)["score"], 0)

    def test_validation(self):
        for raw in ("!!python/object/apply:os.system ['touch /tmp/not-allowed']", "terms: {x: .nan}", "min_score: true", "require_flags_absent: [unknown]", "locations: []", "notifications: {backends: [bad]}", "resume: /etc/passwd", "a" * 33000):
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                load_profile(raw)

    def test_preserve_gone_tracking_reappearance_and_multisource(self):
        ident = self.roles[0]["id"]
        with connect(self.path) as db:
            ensure_user(db, "a@example.com", self.profile)
            new = ingest(db, "a", self.roles, "2026-01-01")
            score_and_queue(db, new)
            db.execute("INSERT INTO tracking VALUES(?,?, 'applied','2026-01-02','private note')", ("a@example.com", ident))
            ingest(db, "b", self.roles, "2026-01-02")
            ingest(db, "a", [], "2026-01-03")
            self.assertFalse(db.execute("SELECT gone FROM roles").fetchone()[0])
            ingest(db, "b", [], "2026-01-04")
            result = db.execute("SELECT * FROM roles").fetchone()
            self.assertTrue(result["gone"])
            self.assertEqual(result["first_seen"], "2026-01-01")
            self.assertEqual(result["last_seen"], "2026-01-02")
            self.assertEqual(result["closed_at"], "2026-01-04")
            self.assertEqual(result["apply_url"], self.roles[0]["apply_url"])
            new = ingest(db, "a", self.roles, "2026-01-05")
            self.assertFalse(new)
            score_and_queue(db, new)
            self.assertIsNone(db.execute("SELECT closed_at FROM roles").fetchone()[0])
            self.assertEqual(db.execute("SELECT notes FROM tracking").fetchone()[0], "private note")
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)

    def test_closed_cell_preserves_link(self):
        with connect(self.path) as db:
            ingest(db, "a", self.roles)
            ingest(db, "a", parse_readme(table(row(app="🔒")) ))
            self.assertEqual(db.execute("SELECT count(*) FROM roles").fetchone()[0], 1)
            result = db.execute("SELECT * FROM roles").fetchone()
            self.assertEqual(result["apply_url"], self.roles[0]["apply_url"])
            self.assertIsNotNone(result["closed_at"])
            self.assertFalse(result["gone"])

    def test_crawl_failures_keep_existing_rows(self):
        config = {"DATABASE": self.path, "SOURCES": ["a"]}
        crawl(config, lambda _: table(row()))
        crawl(config, lambda _: "Bad gateway")
        with connect(self.path) as db:
            self.assertFalse(db.execute("SELECT gone FROM roles").fetchone()[0])
            self.assertIsNotNone(db.execute("SELECT last_error FROM sources").fetchone()[0])

    def test_per_user_scores_and_only_new_notifications(self):
        with connect(self.path) as db:
            ensure_user(db, "a@example.com", self.profile)
            ensure_user(db, "b@example.com", self.profile.replace("min_score: 4", "min_score: 100"))
            score_and_queue(db, ingest(db, "a", self.roles))
            self.assertEqual(db.execute("SELECT user_id FROM deliveries").fetchone()[0], "a@example.com")
            self.assertEqual(db.execute("SELECT count(*) FROM scores").fetchone()[0], 2)
            ensure_user(db, "late@example.com", self.profile)
            score_and_queue(db, ingest(db, "a", self.roles))
            self.assertEqual(db.execute("SELECT count(*) FROM deliveries").fetchone()[0], 1)
