import io
import re
from unittest.mock import Mock, patch

from internship.storage import connect, ingest
from internship.web import create_app
from test_storage import StorageCase, SEED


class WebTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.app = create_app({"TESTING": True, "AUTH_MODE": "dashboard", "DATABASE": self.path, "PROFILE_SEED": str(SEED),
                               "PUBLIC_ORIGIN": "https://dashboard.example.com"})
        self.client = self.app.test_client()
        self.client.set_cookie("rydberg_session", "session-a")
        self.auth = patch("internship.web.requests.get")
        self.mock_auth = self.auth.start()
        self.addCleanup(self.auth.stop)
        self.mock_auth.return_value = Mock(status_code=200, json=lambda: {"email": "a@example.com", "role": "guest"})
        with connect(self.path) as db:
            ingest(db, "a", self.roles)

    def token(self, client=None):
        html = (client or self.client).get("/internship/").text
        return re.search(r'name="csrf" value="([a-f0-9]+)"', html)[1]

    def test_user_isolation_and_escaping(self):
        token = self.token()
        url = "/internship/roles/" + self.roles[0]["id"]
        self.assertEqual(self.client.post(url, data={"csrf": token, "status": "applied", "notes": '<script>alert(1)</script>'}).status_code, 303)
        html = self.client.get("/internship/").text
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.mock_auth.return_value.json = lambda: {"email": "b@example.com", "role": "guest"}
        other = self.app.test_client()
        other.set_cookie("rydberg_session", "session-b")
        self.assertNotIn("alert(1)", other.get("/internship/").text)
        self.assertEqual(other.post(url, data={"csrf": token, "status": "offer"}).status_code, 403)
        token_b = self.token(other)
        self.assertEqual(other.post(url, data={"csrf": token_b, "status": "interview", "notes": "B only"}).status_code, 303)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT status FROM tracking WHERE user_id='a@example.com'").fetchone()[0], "applied")
            self.assertEqual(db.execute("SELECT status FROM tracking WHERE user_id='b@example.com'").fetchone()[0], "interview")

    def test_uploads_and_unsafe_profile_rejected(self):
        for filename in ("resume.pdf", "resume.html", "../../app.py", "resume.pdf.exe"):
            response = self.client.post("/internship/profile", data={"resume": (io.BytesIO(b'%PDF-1.7 <script>evil</script>'), filename)})
            self.assertEqual(response.status_code, 415)
        self.assertEqual(self.client.post("/internship/profile", data=b"%PDF test", content_type="application/pdf").status_code, 415)
        token = self.token()
        response = self.client.post("/internship/profile", data={"csrf": token, "profile": "!!python/object/apply:os.system ['id']"})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/internship/profile", data={"csrf": token, "profile": "x" * 50000})
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/internship/profile", data={"csrf": token, "profile": "terms: {rust: 2}"}, headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)

    def test_auth_fails_closed_and_health(self):
        other = self.app.test_client()
        self.assertEqual(other.get("/internship/").status_code, 401)
        self.assertEqual(other.get("/health").status_code, 200)
        self.mock_auth.return_value.status_code = 401
        self.assertEqual(self.client.get("/internship/").status_code, 401)
        self.mock_auth.return_value.status_code = 500
        self.assertEqual(self.client.get("/internship/").status_code, 503)
        self.mock_auth.return_value.status_code = 302
        self.assertEqual(self.client.get("/internship/").status_code, 503)

    def test_profile_private_and_filters_parameterized(self):
        token = self.token()
        self.assertEqual(self.client.post("/internship/profile", data={"csrf": token, "profile": "terms: {rust: 17}\nmin_score: 10"}).status_code, 303)
        self.assertIn("rust: 17", self.client.get("/internship/profile.yaml").text)
        self.assertEqual(self.client.get("/internship/?company=' OR 1=1 --").status_code, 200)
        self.assertNotIn("Firmware Rust Intern", self.client.get("/internship/?company=' OR 1=1 --").text)
        self.assertEqual(self.client.get("/internship/?score=nan").status_code, 400)
        self.assertIn("frame-ancestors 'none'", self.client.get("/internship/").headers["Content-Security-Policy"])
