"""Single scheduler process; failed/invalid fetches do not remove postings."""
import fcntl
import logging
import signal
import threading
import time
from urllib.parse import urlsplit

import requests

from .config import settings
from .notifications import deliver
from .parser import parse_readme
from .storage import connect, initialize, ingest, score_and_queue

LOG = logging.getLogger(__name__)
MAX_SOURCE_BYTES = 16 * 1024 * 1024


def fetch(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "raw.githubusercontent.com" or not parsed.path.startswith("/SimplifyJobs/"):
        raise ValueError("Only public SimplifyJobs raw GitHub sources are supported")
    with requests.get(url, timeout=(10, 45), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError(f"Source HTTP {response.status_code}")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_SOURCE_BYTES:
                raise ValueError("Source exceeds 16 MiB")
            chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def crawl(config, fetcher=fetch):
    new_ids = set()
    with connect(config["DATABASE"]) as db:
        # Serialize profile edits and a complete crawl/notification enrollment.
        db.execute("BEGIN IMMEDIATE")
        for source in dict.fromkeys(config["SOURCES"]):
            try:
                roles = parse_readme(fetcher(source))
            except Exception as exc:
                LOG.warning("Crawl failed for %s: %s", source, type(exc).__name__)
                db.execute("INSERT OR IGNORE INTO sources(url) VALUES(?)", (source,))
                db.execute("UPDATE sources SET last_error=? WHERE url=?", (str(exc)[:300], source))
                continue
            new_ids.update(ingest(db, source, roles))
        score_and_queue(db, new_ids)
    return len(new_ids)


def main():
    logging.basicConfig(level=logging.INFO)
    config = settings()
    if not isinstance(config["SOURCES"], list) or not config["SOURCES"] or not 0 <= config["DIGEST_HOUR"] <= 23:
        raise ValueError("Invalid sources or digest hour")
    initialize(config["DATABASE"])
    with open(config["DATABASE"] + ".worker.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        next_crawl = 0
        while not stop.is_set():
            if time.monotonic() >= next_crawl:
                try:
                    LOG.info("Crawl discovered %s new roles", crawl(config))
                except Exception:
                    LOG.exception("Crawl transaction failed")
                next_crawl = time.monotonic() + config["CRAWL_SECONDS"]
            try:
                deliver(config)
            except Exception:
                LOG.exception("Delivery cycle failed")
            stop.wait(min(30, max(1, next_crawl - time.monotonic())))


if __name__ == "__main__":
    main()
