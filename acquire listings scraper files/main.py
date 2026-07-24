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

    failed = [n for n in ALL_SOURCES if n not in ran]
    if failed:
        log.error("sources that failed this run: %s", failed)
    if not ran:
        log.error("every source failed. Nothing to sync.")
        return {"ok": False, "reason": "all sources failed"}

    try:
        from engine import deduplicate
        scraped = deduplicate(scraped)
        stats = sync(scraped, ran, first_ever_run=legacy)
    except Exception as e:
        # A failure here must not look like a healthy exit, but it also must not
        # take the container down. The next run will try again on fresh data.
        log.exception("sync failed after scraping %s listings: %s", len(scraped), e)
        return {"ok": False, "reason": "sync failed", "sources_ok": ran}

    log.info("run finished in %.1fs mode=%s %s", time.time() - started, mode, stats)
    if stats.get("skipped_sources"):
        log.error("ATTENTION: sources discarded this run: %s", stats["skipped_sources"])

    # Fill in agent names where brokers publish them. Never blocks the run.
    try:
        from enrich import run_enrichment
        stats["enriched"] = run_enrichment()
    except Exception as e:
        log.exception("enrichment failed, listings are still synced: %s", e)

    stats["ok"] = True
    stats["sources_ok"] = ran
    stats["sources_failed"] = failed
    return stats


if __name__ == "__main__":
    result = run(sys.argv[1] if len(sys.argv) > 1 else "deep")
    # Exit clean whenever any real work happened. Railway should only flag a run
    # as crashed when the scraper genuinely accomplished nothing.
    sys.exit(0 if result.get("ok") else 1)
