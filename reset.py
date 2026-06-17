"""Reset UptimeBot to a clean slate.

Deletes the monitors this bot created from Uptime Kuma and clears the bot's
database, so everyone starts fresh. This is destructive and irreversible — it
runs as a DRY RUN unless you pass --yes.

Usage:
    python reset.py                  # dry run: show what would be deleted
    python reset.py --yes            # delete tracked Kuma monitors, then clear the DB
    python reset.py --yes --keep-kuma  # clear the DB only, leave Kuma monitors running

In Docker, run it inside the service so it sees the same env and database:
    docker compose run --rm uptimebot python reset.py --yes
"""
import argparse
import collections
import logging
import sys

import config
import database as db
from kuma_client import KumaClient, KumaError


def _summary(sites):
    by_type = collections.Counter(s["site_type"] for s in sites)
    parts = ", ".join(f"{n} {t}" for t, n in sorted(by_type.items()))
    return f"{len(sites)} site(s){' — ' + parts if parts else ''}"


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    log = logging.getLogger("uptimebot.reset")

    parser = argparse.ArgumentParser(description="Reset UptimeBot to a clean slate.")
    parser.add_argument("--yes", action="store_true",
                        help="actually perform the reset (without this it's a dry run)")
    parser.add_argument("--keep-kuma", action="store_true",
                        help="clear the bot database only; leave Kuma monitors running")
    args = parser.parse_args(argv)

    db.init_db()
    sites = db.all_sites()
    log.info("Database: %s", config.DB_PATH)
    log.info("Tracked: %s", _summary(sites))

    if not sites:
        log.info("Nothing to reset — the database is already empty.")
        return 0

    if not args.yes:
        log.info("DRY RUN — nothing changed. Re-run with --yes to apply%s.",
                 " (and --keep-kuma to leave Kuma monitors)" if not args.keep_kuma else "")
        return 0

    # 1) Delete the monitors we created from Kuma (unless told to keep them).
    if args.keep_kuma:
        log.info("--keep-kuma set: leaving Kuma monitors in place.")
    else:
        deleted = skipped = failed = 0
        try:
            with KumaClient() as kc:
                live = kc.list_monitor_ids()
                for s in sites:
                    mid = s["monitor_id"]
                    if mid not in live:
                        skipped += 1  # already gone from Kuma
                        continue
                    try:
                        kc.delete_monitor(mid)
                        deleted += 1
                    except KumaError as exc:
                        failed += 1
                        log.warning("couldn't delete monitor %s (%s): %s",
                                    mid, s["name"], exc)
        except KumaError as exc:
            log.error("Couldn't reach Uptime Kuma, so no monitors were deleted: %s", exc)
            log.error("Database left untouched. Fix the connection and retry, "
                      "or use --keep-kuma to clear the DB anyway.")
            return 1
        log.info("Kuma monitors: %d deleted, %d already gone, %d failed.",
                 deleted, skipped, failed)

    # 2) Clear the database.
    removed = db.clear_all()
    log.info("Database cleared: %d row(s) removed.", removed)
    log.info("Done. Open Home tabs will refresh on next open or Refresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
