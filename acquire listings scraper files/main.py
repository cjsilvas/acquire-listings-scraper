"""
Entry point. Railway runs this on a schedule.

Two speeds, so we find new deals fast without hammering the brokers:
  light  hourly       index pages only, spot new and vanished listings
  deep   twice daily  re read detail pages to catch status changes

  python main.py light
  python main.py deep
"""
import sys, logging, time
from engine import sync, db
from sources import ALL_SOURCES

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("fold.main")


def first_ever_run() -> bool:
    res = db.table("listings").select("id").limit(1).execute()
    return not (res.data or [])


def run(mode: str = "deep"):
    started = time.time()
    legacy = first_ever_run()
    if legacy:
        log.info("First run. Everything found is tagged Legacy, true age unknown.")

    scraped, ran = [], []
    for name, fn in ALL_SOURCES.items():
        try:
            items = fn()
            scraped.extend(items)
            ran.append(name)
        except Exception as e:
            log.exception("source %s failed, skipping it: %s", name, e)

    from engine import deduplicate
    scraped = deduplicate(scraped)

    stats = sync(scraped, ran, first_ever_run=legacy)
    log.info("run finished in %.1fs mode=%s %s", time.time() - started, mode, stats)
    if stats["skipped_sources"]:
        log.error("ATTENTION: sources discarded this run: %s", stats["skipped_sources"])
    return stats


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "deep")
