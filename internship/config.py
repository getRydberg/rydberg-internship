import json
import os
from pathlib import Path

DEFAULT_SOURCE = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README.md"
OFFSEASON_SOURCE = "https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/README-Off-Season.md"
ROOT = Path(__file__).resolve().parent.parent


def settings():
    return dict(
        DATABASE=os.getenv("INTERNSHIP_DATABASE", str(ROOT / 'data' / 'internship.sqlite3')),
        AUTH_MODE=os.getenv('INTERNSHIP_AUTH_MODE', 'name'),
        PROFILE_SEED=os.getenv("INTERNSHIP_PROFILE_SEED", str(ROOT / "profile.yaml")),
        AUTH_URL=os.getenv("INTERNSHIP_AUTH_URL", "http://backend-dashboard:8080/auth/me"),
        LOGIN_URL=os.getenv("INTERNSHIP_LOGIN_URL", "/auth/login"),
        PUBLIC_ORIGIN=os.getenv("INTERNSHIP_PUBLIC_ORIGIN", "https://dashboard.rydberg.app").rstrip("/"),
        SOURCES=json.loads(os.getenv("INTERNSHIP_SOURCES", json.dumps([DEFAULT_SOURCE, OFFSEASON_SOURCE]))),
        CRAWL_SECONDS=max(60, int(os.getenv("INTERNSHIP_CRAWL_SECONDS", "3600"))),
        DIGEST_HOUR=int(os.getenv("INTERNSHIP_DIGEST_HOUR", "18")),
        NOTIFICATION_CONFIG=os.getenv("INTERNSHIP_NOTIFICATION_CONFIG", "/config/notifications.yaml"),
    )
