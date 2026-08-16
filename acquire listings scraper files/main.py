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
            items = fn(deep=(mode=="deep")) if "deep" in fn.__code__.co_varnames else fn()
            scraped.extend(items)
            ran.append(name)
        except Exception as e:
            log.exception("source %s failed, skipping it: %s", name, e)

    failed = [n for n in ALL_SOURCES if n not in ran]
    # A source that raised no error but produced nothing is failed, not collapsed.
    # This keeps a chronically blocked source from tripping the quality gate.
    empty = [n for n in ran if not any(i.get("source") == n for i in scraped)]
    if empty:
        log.error("sources that returned nothing this run: %s", empty)
        failed = failed + empty
        ran = [n for n in ran if n not in empty]
    if failed:
        log.error("sources that failed this run: %s", failed)
    if not ran:
        log.error("every source failed. Nothing to sync.")
        return {"ok": False, "reason": "all sources failed"}

    try:
        from engine import deduplicate, flag_direct_sellers, flag_db_duplicates
        scraped = deduplicate(scraped)
        scraped = flag_direct_sellers(scraped)
        stats = sync(scraped, ran, first_ever_run=legacy)
        # Cross source dedupe at the DB level, so the same firm from two
        # sources shows once. Hides, never deletes. Never blocks the run.
        try:
            stats.update(flag_db_duplicates())
        except Exception as e:
            log.exception("db dedupe failed, listings still synced: %s", e)
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

    # Quality gate. The site only advances its "listings last updated"
    # stamp when a run passes every check here.
    try:
        checks = {}
        checks["enough_sources"] = len(ran) >= 5
        checks["no_source_collapse"] = not stats.get("skipped_sources")
        res = db.table("listings").select("id", count="exact") \
                .in_("status", ["active", "pending"]).execute()
        live = res.count or 0
        checks["live_count_sane"] = 250 <= live <= 3000
        checks["work_happened"] = (stats.get("new", 0) + stats.get("updated", 0)) > 0
        gate_ok = all(checks.values())
        db.table("sync_health").insert({
            "ok": gate_ok,
            "sources_ok": len(ran),
            "sources_failed": len(failed),
            "live_count": live,
            "details": {"checks": checks, "failed_sources": failed},
        }).execute()
        if not gate_ok:
            log.error("QUALITY GATE FAILED: %s", checks)
    except Exception as e:
        log.exception("could not record sync health: %s", e)

    stats["ok"] = True
    stats["sources_ok"] = ran
    stats["sources_failed"] = failed
    return stats


if __name__ == "__main__":
    result = run(sys.argv[1] if len(sys.argv) > 1 else "deep")
    # Exit clean whenever any real work happened. Railway should only flag a run
    # as crashed when the scraper genuinely accomplished nothing.
    sys.exit(0 if result.get("ok") else 1)
