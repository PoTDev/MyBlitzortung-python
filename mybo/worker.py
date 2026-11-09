# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import sys
import time
from datetime import timedelta

import httpx
from sqlalchemy import delete
from sqlalchemy.exc import SQLAlchemyError

from .config import Config
from .db import engine_for, exclusive_writer, sessions, utcnow
from .feed import Feed, mark, state

log = logging.getLogger(__name__)


def run(config, engine):
    next_stations = 0.0
    with httpx.Client(auth=(config.feed_user, config.feed_password), timeout=15, follow_redirects=False,
                      headers={"User-Agent": "MyBlitzortung-Python/1.0"}, trust_env=False) as client:
        feed = Feed(config, client)
        while True:
            now = utcnow().replace(microsecond=0)
            failed = False
            try:
                with exclusive_writer(engine) as conn:
                    if config.import_enabled:
                        # Station and stroke failures are isolated from each other.
                        try:
                            feed.strike_cycle(conn, now)
                        except (httpx.HTTPError, ValueError, OSError, EOFError) as exc:
                            log_feed_error("Strike", exc)
                            failed = True
                        if time.monotonic() >= next_stations:
                            try:
                                with conn.begin():
                                    feed.fetch(conn, "stations", "stations.txt.gz", now)
                            except (httpx.HTTPError, ValueError, OSError, EOFError) as exc:
                                log_feed_error("Station", exc)
                                failed = True
                            else:
                                next_stations = time.monotonic() + config.station_seconds
                    with conn.begin():
                        conn.execute(delete(sessions).where(sessions.c.expires <= now))
                        if not failed:
                            mark(conn, "worker", now)
            except (SQLAlchemyError, RuntimeError) as exc:
                log.error("Worker database operation failed (%s)", type(exc).__name__)
            time.sleep(config.poll_seconds)


def log_feed_error(kind, exc):
    if isinstance(exc, httpx.HTTPStatusError):
        log.warning("%s feed returned HTTP %d", kind, exc.response.status_code)
    else:
        log.warning("%s feed update failed (%s)", kind, type(exc).__name__)


def main():
    config = Config.from_env()
    engine = engine_for(config.database)
    if sys.argv[1:] == ["--health"]:
        try:
            with engine.connect() as conn:
                last = state(conn, "worker")
            threshold = timedelta(seconds=max(3 * config.poll_seconds, 600))
            return 0 if last and utcnow() - last["checked"] < threshold else 1
        except SQLAlchemyError:
            return 1
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Blitzortung import %s", "enabled" if config.import_enabled else "disabled")
    run(config, engine)


if __name__ == "__main__":
    raise SystemExit(main())
