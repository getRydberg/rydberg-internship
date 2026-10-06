from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from internship.notifications import deliver
from internship.storage import connect, ensure_user, ingest, score_and_queue
from test_storage import StorageCase


class NotificationTests(StorageCase):
    def setup_delivery(self, mode="immediate"):
        config_path = Path(self.tmp.name) / "notifications.yaml"
        config_path.write_text("users:\n  a@example.com:\n    webhook:\n      url: http://receiver:8080/test\n")
        config = {"DATABASE": self.path, "NOTIFICATION_CONFIG": str(config_path), "DIGEST_HOUR": 18}
        with connect(self.path) as db:
            ensure_user(db, "a@example.com", self.profile.replace("mode: immediate", "mode: " + mode))
            score_and_queue(db, ingest(db, "a", self.roles))
        return config

    def test_deduplication_and_payload(self):
        config = self.setup_delivery()
        sender = Mock()
        deliver(config, sender)
        deliver(config, sender)
        self.assertEqual(sender.call_count, 1)
        payload = sender.call_args.args[3][0]
        self.assertEqual(payload["score"], 8)
        self.assertIn("rust", payload["matched"])
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT state FROM deliveries").fetchone()[0], "sent")

    def test_digest_batches_once_daily(self):
        config = self.setup_delivery("digest")
        sender = Mock()
        deliver(config, sender, datetime(2026, 9, 6, 17, tzinfo=timezone.utc))
        self.assertFalse(sender.called)
        with connect(self.path) as db:
            second = {**self.roles[0], "id": "second", "apply_url": "https://example.com/second"}
            score_and_queue(db, ingest(db, "a", self.roles + [second]))
        deliver(config, sender, datetime(2026, 9, 6, 18, tzinfo=timezone.utc))
        self.assertEqual(len(sender.call_args.args[3]), 2)
        with connect(self.path) as db:
            third = {**self.roles[0], "id": "third", "apply_url": "https://example.com/third"}
            score_and_queue(db, ingest(db, "a", self.roles + [second, third]))
        deliver(config, sender, datetime(2026, 9, 6, 19, tzinfo=timezone.utc))
        self.assertEqual(sender.call_count, 1)
        deliver(config, sender, datetime(2026, 9, 7, 18, tzinfo=timezone.utc))
        self.assertEqual(sender.call_count, 2)

    def test_ambiguous_failure_never_auto_resends(self):
        config = self.setup_delivery()
        sender = Mock(side_effect=TimeoutError("unknown delivery outcome"))
        deliver(config, sender)
        deliver(config, sender)
        self.assertEqual(sender.call_count, 1)
        with connect(self.path) as db:
            result = db.execute("SELECT state,error FROM deliveries").fetchone()
            self.assertEqual(tuple(result), ("failed", "TimeoutError"))
