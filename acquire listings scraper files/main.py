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


STALE_HOURS = 24            # a source with no fresh row in a day has gone dark
ALERT_COOLDOWN_HOURS = 20   # tell CJ once a day, not once a run


def _parse_ts(value: str):
    """
    Postgres hands back fractional seconds with however many digits it feels
    like ("...03.5356+00:00"), and datetime.fromisoformat on Python 3.9 only
    accepts 3 or 6. Pad it, or the check silently skips that source.
    """
    from datetime import datetime
    import re as _re
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    m = _re.match(r"^(.*\.)(\d{1,6})(.*)$", v)
    if m:
        v = m.group(1) + m.group(2).ljust(6, "0") + m.group(3)
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _setting(key: str):
    try:
        r = db.table("private_settings").select("value").eq("key", key).limit(1).execute()
        return (r.data or [{}])[0].get("value")
    except Exception:
        return None


def alert_on_stale_sources():
    """
    Email CJ when a source stops producing fresh listings.

    Every quality check can pass while one broker quietly goes dark: the run
    still scrapes 15 other sources, the live count stays sane, and the gate
    says ok. BizQuest sat 20 days stale that way and nobody heard about it.
    This looks at the data itself, not at whether the run threw an error.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    stale = []
    for src in ALL_SOURCES:
        try:
            r = (db.table("listings").select("last_seen")
                   .eq("source", src).order("last_seen", desc=True).limit(1).execute())
            rows = r.data or []
            if not rows or not rows[0].get("last_seen"):
                # A source with no rows at all is not a source that went dark.
                # The keyword sweeps file their finds under the parent source.
                continue
            seen = _parse_ts(rows[0]["last_seen"])
            if seen is None:
                log.warning("could not read last_seen for %s: %r", src, rows[0]["last_seen"])
                continue
            hrs = (now - seen).total_seconds() / 3600.0
            if hrs >= STALE_HOURS:
                stale.append((src, int(hrs)))
        except Exception as e:
            log.warning("staleness check failed for %s: %s", src, e)
    if not stale:
        return []

    log.error("STALE SOURCES, no fresh listings in %sh: %s", STALE_HOURS, stale)

    last = _setting("last_stale_alert_at")
    if last:
        when = _parse_ts(last)
        if when is not None and (now - when).total_seconds() / 3600.0 < ALERT_COOLDOWN_HOURS:
            return stale

    key = _setting("resend_api_key")
    if not key or key == "PASTE_KEY_HERE":
        return stale

    items = "".join(
        "<li><b>%s</b>: %s</li>" % (src, "never seen" if hrs is None else "%s hours since a fresh listing" % hrs)
        for src, hrs in stale)
    html_body = (
        "<p>These listing sources have gone quiet. Their listings still show on the site, "
        "but nothing has verified them recently, so any that sold or were pulled still look live.</p>"
        "<ul>" + items + "</ul>"
        "<p>Automatic alert from the Practices scraper.</p>")
    try:
        import requests
        requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"from": "Practices Scraper <feedback@acquireafirm.com>",
                  "to": ["cj@eagleeyeequity.com"],
                  "reply_to": "cj@eagleeyeequity.com",
                  "subject": "Practices: %s listing source(s) went dark" % len(stale),
                  "html": html_body},
            timeout=30)
        db.table("private_settings").upsert(
            {"key": "last_stale_alert_at", "value": now.isoformat()}).execute()
        log.info("stale source alert emailed for %s source(s)", len(stale))
    except Exception as e:
        log.warning("could not send stale source alert: %s", e)
    return stale


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
        total_sources = len(ran) + len(failed)
        checks["enough_sources"] = len(ran) >= 5
        # A few sources returning nothing on a given cycle is normal with 17+
        # sources (rate limits, a slow proxy, or a source that genuinely has no
        # new rows like the keyword sweep). Only treat it as a collapse if a
        # large share fail at once, which signals a real systemic problem.
        checks["no_source_collapse"] = (
            total_sources == 0 or (len(failed) / total_sources) < 0.34
        )
        res = db.table("listings").select("id", count="exact") \
                .in_("status", ["active", "pending"]).execute()
        live = res.count or 0
        checks["live_count_sane"] = 250 <= live <= 4000
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

    try:
        stats["stale_sources"] = alert_on_stale_sources()
    except Exception as e:
        log.warning("stale source check failed: %s", e)

    stats["ok"] = True
    stats["sources_ok"] = ran
    stats["sources_failed"] = failed
    return stats


if __name__ == "__main__":
    # Default to "light" so the frequent cron (every few hours) does cheap fetches
    # only. Deep mode re-reads every detail page through ultra_premium (~25 credits
    # each) and must NOT run on every cron tick or it drains the monthly quota in
    # days. Run deep on its own low-frequency schedule with: python main.py deep
    result = run(sys.argv[1] if len(sys.argv) > 1 else "light")
    # Exit clean whenever any real work happened. Railway should only flag a run
    # as crashed when the scraper genuinely accomplished nothing.
    sys.exit(0 if result.get("ok") else 1)
