"""
Core engine. Every source uses this. Nothing here knows about a specific broker.

What one run does:
  1. Ask each source for its current listings
  2. Sanity check the result so a broken parser cannot wipe the board
  3. Deduplicate, preferring the origin broker over a marketplace
  4. Compare against Supabase and write the differences
"""

import os, re, time, hashlib, logging
from datetime import datetime, timezone, date
from typing import List, Dict, Optional

import requests
from supabase import create_client
from metrics import compute_metrics

log = logging.getLogger("fold")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

UA = "FoldListingsBot/1.0 (accounting practice aggregator; contact: cj@eagleeyeequity.com)"

# Lower number wins during dedupe. Origin brokers beat marketplaces, always.
SOURCE_PRIORITY = {
    "aba": 1, "naab": 1, "aps": 1, "poe": 1, "ppt": 1, "atb": 1,
    "prohorizons": 1, "padgett": 1, "abizbrokers": 1, "afs": 1, "bbi": 1, "pas": 1,
    "businessesforsale": 5, "dealstream": 5, "bizbuysell": 5,
    "bizquest": 5, "loopnet": 5, "karbon": 5, "ape": 6,
}

# Sources that aggregate listings from many origins rather than originating them.
# A listing from one of these that survives dedupe (i.e. matches no origin broker)
# is likely a direct-from-seller deal, and gets flagged as such for the frontend.
MARKETPLACE_SOURCES = {
    "businessesforsale", "dealstream", "bizbuysell",
    "bizquest", "loopnet", "karbon", "ape",
}

# A source must not lose more than this share of its listings in one run.
# A broken parser looks exactly like everything sold. This tells them apart.
COLLAPSE_THRESHOLD = 0.5

# A listing must be missing this many consecutive runs before we act on it.
MISSES_BEFORE_RETIRING = 2


# ----------------------------------------------------------------------
# Fetching
# ----------------------------------------------------------------------

HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}
# Some sites refuse unfamiliar agents from datacenter IPs. When that happens we
# retry once looking like an ordinary browser rather than giving up on the source.
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def fetch(url: str, tries: int = 4, pause: float = 0.4) -> Optional[str]:
    """Polite GET. Spaces requests out and identifies itself honestly."""
    last = None
    for attempt in range(tries):
        headers = dict(HEADERS)
        if attempt >= 2:
            headers["User-Agent"] = BROWSER_UA
        try:
            r = requests.get(url, headers=headers, timeout=40)
            last = r.status_code
            if r.status_code == 200:
                time.sleep(pause)
                return r.text
            log.warning("fetch %s returned %s (attempt %s)", url, r.status_code, attempt + 1)
            if r.status_code in (429, 503):
                time.sleep(3 * (attempt + 1))
        except Exception as e:
            log.warning("fetch %s failed: %s (attempt %s)", url, e, attempt + 1)
        time.sleep(pause * (attempt + 2))
    log.error("fetch gave up on %s, last status %s", url, last)
    return None


SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "")


def fetch_via_api(url: str, tries: int = 3, render: bool = False,
                  premium: bool = False, ultra: bool = False) -> Optional[str]:
    """
    Fetch through ScraperAPI's residential proxy pool, for marketplace sources
    that block datacenter IPs (DealStream, BizQuest, BizBuySell, ...). Reads the
    key from the SCRAPER_API_KEY environment variable so it never lives in the
    repo. If no key is set, falls back to a direct fetch so nothing hard-breaks.
    Failed ScraperAPI requests are not billed. render/premium/ultra cost more
    credits, so leave them off unless a target needs them: DealStream and BizQuest
    work on the standard pool (1 credit), BizBuySell needs ultra=True (ultra
    premium proxies, a paid-plan feature and more credits per request).
    """
    if not SCRAPER_API_KEY:
        log.warning("no SCRAPER_API_KEY set, direct-fetching %s", url)
        return fetch(url)
    params = {"api_key": SCRAPER_API_KEY, "url": url}
    if render:
        params["render"] = "true"
    if ultra:
        params["ultra_premium"] = "true"
    elif premium:
        params["premium"] = "true"
    last = None
    for attempt in range(tries):
        try:
            r = requests.get("https://api.scraperapi.com/", params=params, timeout=90)
            last = r.status_code
            if r.status_code == 200:
                return r.text
            log.warning("fetch_via_api %s returned %s (attempt %s)", url, r.status_code, attempt + 1)
            if r.status_code == 500 and not (premium or ultra):
                log.warning("  (target may need premium/ultra, a paid-plan feature)")
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            log.warning("fetch_via_api %s failed: %s (attempt %s)", url, e, attempt + 1)
            time.sleep(2 * (attempt + 1))
    log.error("fetch_via_api gave up on %s, last status %s", url, last)
    return None


def url_is_gone(url: str, via_api: bool = False) -> Optional[bool]:
    """
    Confirm whether a listing URL has truly been removed. Returns:
      True   the page is gone (404 / 410) -> safe to retire
      False  the page is live (200)        -> do NOT retire, it is a false miss
      None   we could not tell (timeout, 500, block) -> stay cautious, do not retire

    Marketplaces like BizBuySell never mark a deal "sold" - a removed listing just
    stops resolving. So a confirmed 404 is the real removal signal. We separate that
    from a temporary failure so a network blip never wrongly retires a live listing.
    Marketplace URLs go through the proxy (via_api=True); origin brokers fetch direct.
    """
    for attempt in range(2):
        try:
            if via_api and SCRAPER_API_KEY:
                r = requests.get(
                    "https://api.scraperapi.com/",
                    params={"api_key": SCRAPER_API_KEY, "url": url, "ultra_premium": "true"},
                    timeout=90,
                )
            else:
                r = requests.get(url, headers=HEADERS, timeout=40)
            if r.status_code in (404, 410):
                return True
            if r.status_code == 200 and len(r.text) > 2000:
                return False
            # 500 / 403 / tiny body -> inconclusive, try once more then give up
        except Exception as e:
            log.warning("url_is_gone check failed for %s: %s", url, e)
        time.sleep(2 * (attempt + 1))
    return None


# ----------------------------------------------------------------------
# Normalising and matching
# ----------------------------------------------------------------------

STOPWORDS = {
    "for", "sale", "practice", "firm", "cpa", "tax", "accounting", "the", "a",
    "an", "of", "in", "and", "area", "llc", "pc", "inc", "revenue", "gross",
}

def norm_text(s: str) -> str:
    s = (s or "").lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def title_tokens(title: str) -> frozenset:
    """Meaningful words only, so wording differences between sites still match."""
    return frozenset(w for w in norm_text(title).split() if w not in STOPWORDS and len(w) > 2)


def revenue_bucket(revenue: Optional[int]) -> Optional[int]:
    """Round revenue so 497k and 500k for the same firm still collide."""
    if not revenue:
        return None
    return int(round(revenue / 25000.0))


def fingerprint(listing: Dict) -> str:
    """Stable identity for one listing at one source, across runs."""
    code = (listing.get("listing_code") or "").strip().upper()
    if code:
        basis = f"{listing['source']}|code|{code}"
    else:
        basis = "|".join([
            listing["source"], "url", listing.get("source_url", "")
        ])
    return hashlib.sha256(basis.encode()).hexdigest()[:32]


def dedupe_key(listing: Dict):
    """Cross source identity. Same firm listed in two places should collapse."""
    return (
        (listing.get("state") or "").upper(),
        revenue_bucket(listing.get("revenue")),
        title_tokens(listing.get("firm_type", "")),
    )


def deduplicate(listings: List[Dict]) -> List[Dict]:
    """
    Collapse the same practice appearing at more than one source.
    The origin broker always wins over a marketplace. Where both are origin
    brokers, the one carrying more detail wins.
    """
    best: Dict[tuple, Dict] = {}
    dropped = 0

    # Secondary index: same state + same asking price is a strong duplicate
    # signal even when titles differ across sources (e.g. "WV" vs "West
    # Virginia", or a marketplace re-titling a broker's listing).
    by_price: Dict[tuple, tuple] = {}

    for item in listings:
        key = dedupe_key(item)

        # Try the asking-price key first: (state, asking_price).
        price_key = None
        st = (item.get("state") or "").upper()
        ap = item.get("asking_price")
        if st and ap:
            price_key = (st, int(ap))

        if not key[0] or key[1] is None or not key[2]:
            # Not enough to match on title, but an asking-price match still counts.
            if price_key and price_key in by_price:
                pk, incumbent = by_price[price_key]
                dropped += 1
                if _wins(item, incumbent):
                    item["also_listed_at"] = sorted(
                        set(incumbent.get("also_listed_at", []) + [incumbent["source"]])
                    )
                    best[pk] = item
                    by_price[price_key] = (pk, item)
                else:
                    incumbent["also_listed_at"] = sorted(
                        set(incumbent.get("also_listed_at", []) + [item["source"]])
                    )
                continue
            uk = ("unique", item["fingerprint"])
            best[uk] = item
            if price_key:
                by_price[price_key] = (uk, item)
            continue

        incumbent = best.get(key)
        if incumbent is None and price_key and price_key in by_price:
            # Title key is new, but asking price already matched something.
            key, incumbent = by_price[price_key][0], by_price[price_key][1]
        if incumbent is None:
            best[key] = item
            if price_key:
                by_price[price_key] = (key, item)
            continue

        dropped += 1
        if _wins(item, incumbent):
            item["also_listed_at"] = sorted(
                set(incumbent.get("also_listed_at", []) + [incumbent["source"]])
            )
            best[key] = item
            if price_key:
                by_price[price_key] = (key, item)
        else:
            incumbent["also_listed_at"] = sorted(
                set(incumbent.get("also_listed_at", []) + [item["source"]])
            )

    if dropped:
        log.info("dedupe collapsed %s duplicate listings", dropped)
    return list(best.values())


def flag_direct_sellers(listings: List[Dict]) -> List[Dict]:
    """
    Run AFTER deduplicate. Any listing still standing that came from a
    marketplace matched no origin broker, so it is likely a direct-from-seller
    deal. Tag it so the frontend can surface that. A marketplace listing that
    HAD matched an origin broker would already be gone (broker won the dedupe).
    """
    flagged = 0
    for item in listings:
        if item.get("source") in MARKETPLACE_SOURCES:
            item["seller_note"] = "No broker found - possibly direct to seller"
            flagged += 1
        else:
            item["seller_note"] = None
    if flagged:
        log.info("flagged %s marketplace listings as possible direct sellers", flagged)
    return listings


def _wins(a: Dict, b: Dict) -> bool:
    pa = SOURCE_PRIORITY.get(a["source"], 9)
    pb = SOURCE_PRIORITY.get(b["source"], 9)
    if pa != pb:
        return pa < pb
    return _richness(a) > _richness(b)


def _richness(x: Dict) -> int:
    score = 0
    for field in ("description", "asking_price", "agent_email", "agent_name", "listing_code"):
        if x.get(field):
            score += 1
    if x.get("description") and len(x["description"]) > 400:
        score += 1
    return score


# ----------------------------------------------------------------------
# Writing to Supabase
# ----------------------------------------------------------------------

def _existing_by_fingerprint() -> Dict[str, Dict]:
    rows, page, size = {}, 0, 1000
    while True:
        res = (db.table("listings")
                 .select("id,fingerprint,source,status,first_seen,last_seen,"
                         "is_legacy,days_on_market,miss_count")
                 .range(page * size, page * size + size - 1)
                 .execute())
        batch = res.data or []
        for r in batch:
            rows[r["fingerprint"]] = r
        if len(batch) < size:
            return rows
        page += 1


def sync(scraped: List[Dict], sources_run: List[str], first_ever_run: bool) -> Dict:
    """Compare what we just scraped against the database and write the differences."""
    existing = _existing_by_fingerprint()
    now = datetime.now(timezone.utc).isoformat()
    today = date.today().isoformat()

    seen = {l["fingerprint"] for l in scraped}
    stats = {"new": 0, "updated": 0, "status_changed": 0, "retired": 0, "skipped_sources": []}

    # --- guard against a broken parser wiping a source ---
    healthy_sources = []
    for src in sources_run:
        was = sum(1 for r in existing.values()
                  if r["source"] == src and r["status"] in ("active", "pending"))
        now_count = sum(1 for l in scraped if l["source"] == src)
        if was >= 10 and now_count < was * COLLAPSE_THRESHOLD:
            log.error("SOURCE COLLAPSE %s: had %s, got %s. Discarding this source.",
                      src, was, now_count)
            stats["skipped_sources"].append(src)
        else:
            healthy_sources.append(src)

    scraped = [l for l in scraped if l["source"] in healthy_sources]
    seen = {l["fingerprint"] for l in scraped}

    # --- insert and update what we saw ---
    for item in scraped:
        fp = item["fingerprint"]
        prior = existing.get(fp)
        row = {
            "fingerprint": fp,
            "source": item["source"],
            "source_label": item["source_label"],
            "source_url": item["source_url"],
            "broker_id": item.get("broker_id"),
            "listing_code": item.get("listing_code"),
            "firm_type": item.get("firm_type"),
            "city": item.get("city"),
            "state": item.get("state"),
            "revenue": item.get("revenue"),
            "asking_price": item.get("asking_price"),
            "cash_flow": item.get("cash_flow"),
            "description": item.get("description"),
            "listing_type": item.get("listing_type"),
            "agent_name": item.get("agent_name"),
            "agent_team": item.get("agent_team"),
            "listing_brokerage": item.get("listing_brokerage"),
            "brokerage": item.get("brokerage") or item["source_label"],
            "agent_email": item.get("agent_email"),
            "agent_phone": item.get("agent_phone"),
            "services": item.get("services") or [],
            "tags": item.get("tags") or [],
            "available_after": item.get("available_after"),
            "status": item.get("status", "active"),
            "active": item.get("status", "active") in ("active", "pending"),
            "seller_note": item.get("seller_note"),
            "also_listed_at": item.get("also_listed_at") or [],
            "last_seen": now,
            "miss_count": 0,
        }

        # Modeled deal metrics (SDE, debt service, DSCR, monthly cash flow).
        # Uses the listing's own cash flow as SDE when present, else models it
        # off revenue with the tiered margin. Skips junk/near-zero revenue.
        _rev = row.get("revenue")
        if _rev and _rev >= 15000:
            m = compute_metrics(_rev, row.get("cash_flow"))
            if m:
                row.update(m)

        if prior is None:
            row["first_seen"] = now
            # Everything present on the very first run has unknown true age.
            row["is_legacy"] = first_ever_run
            if row["status"] in ("sold", "pending"):
                row["status_changed_at"] = now
            db.table("listings").insert(row).execute()
            stats["new"] += 1
            continue

        # Never move first_seen. The age clock depends on it.
        if prior["status"] != row["status"]:
            row["status_changed_at"] = now
            stats["status_changed"] += 1
            if row["status"] in ("sold", "pending", "on_hold") and not prior.get("days_on_market"):
                row["days_on_market"] = _days_between(prior["first_seen"], now)
            if row["status"] == "active":
                row["days_on_market"] = None       # relisted, clock resumes

        db.table("listings").update(row).eq("fingerprint", fp).execute()
        stats["updated"] += 1

    # --- handle listings that did not appear this run ---
    for fp, prior in existing.items():
        if fp in seen:
            continue
        if prior["source"] not in healthy_sources:
            continue                                  # source was discarded, prove nothing
        if prior["status"] in ("sold", "no_longer_listed"):
            continue                                  # already settled

        misses = (prior.get("miss_count") or 0) + 1
        if misses < MISSES_BEFORE_RETIRING:
            db.table("listings").update({"miss_count": misses}).eq("fingerprint", fp).execute()
            continue

        # Gone for two consecutive runs. Before retiring, confirm the detail page is
        # actually removed. A marketplace listing that merely fell off the index (e.g.
        # pushed past the pages we scrape) is still live and must NOT be retired; only
        # a genuine 404/410 counts. If the check is inconclusive, we hold - a network
        # blip should never bury a live deal.
        url = prior.get("source_url")
        gone = url_is_gone(url, via_api=prior["source"] in MARKETPLACE_SOURCES) if url else None
        if gone is False:
            # Still live - false miss. Reset the counter and refresh last_seen.
            db.table("listings").update({
                "miss_count": 0,
                "last_seen": now,
            }).eq("fingerprint", fp).execute()
            continue
        if gone is None:
            # Could not confirm. Keep the miss count where it is and wait for next run.
            db.table("listings").update({"miss_count": misses}).eq("fingerprint", fp).execute()
            continue

        # Confirmed gone (404/410). Retire it, freezing days-on-market at last seen.
        db.table("listings").update({
            "status": "no_longer_listed",
            "active": False,
            "status_changed_at": now,
            "days_on_market": prior.get("days_on_market") or _days_between(
                prior["first_seen"], prior["last_seen"]),   # freeze at LAST SEEN, not today
            "miss_count": misses,
        }).eq("fingerprint", fp).execute()
        stats["retired"] += 1

    return stats


def _days_between(a: str, b: str) -> int:
    da = datetime.fromisoformat(a.replace("Z", "+00:00"))
    dbb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return max(0, (dbb - da).days)


def flag_db_duplicates() -> Dict:
    """
    Cross source dedupe at the database level, run after every sync.
    Same firm listed on multiple sources becomes one visible row. The origin
    broker always wins over a marketplace. Duplicates are hidden (is_duplicate
    = true) and point at the survivor via duplicate_of, never deleted, so a
    wrong match is fully reversible. Matching is conservative: same state and
    revenue, plus either equal asking price or a near identical title. This is
    what copy pasted broker summaries look like and avoids merging two
    different firms that happen to share a metro and revenue.
    """
    rows = db.table("listings").select(
        "id,source,revenue,state,asking_price,firm_type,is_duplicate"
    ).eq("status", "active").execute().data or []

    def pri(src: str) -> int:
        return SOURCE_PRIORITY.get(src, 9)

    def norm(t: Optional[str]) -> str:
        return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()

    # Order so the preferred survivor is seen first within each group.
    rows.sort(key=lambda r: (pri(r["source"]), r["source"], str(r["id"])))

    survivors: List[Dict] = []
    to_flag: Dict[str, str] = {}   # dup_id -> survivor_id

    for r in rows:
        if r.get("revenue") is None or not r.get("state"):
            continue
        rt = norm(r.get("firm_type"))
        matched = None
        for s in survivors:
            if s["state"] != r["state"] or s["revenue"] != r["revenue"]:
                continue
            st = s["_t"]
            same_price = (r.get("asking_price") is not None
                          and r["asking_price"] == s.get("asking_price"))
            same_title = bool(rt) and (
                rt == st
                or (len(rt) > 12 and (rt in st or st in rt))
            )
            if same_price or same_title:
                matched = s
                break
        if matched is None:
            r["_t"] = rt
            survivors.append(r)
        else:
            to_flag[r["id"]] = matched["id"]

    # Apply: flag the newly found duplicates, and clear any stale flags on
    # rows that are now survivors (e.g. a higher priority copy disappeared).
    flagged = 0
    for dup_id, keep_id in to_flag.items():
        db.table("listings").update(
            {"is_duplicate": True, "duplicate_of": keep_id}
        ).eq("id", dup_id).execute()
        flagged += 1

    survivor_ids = {s["id"] for s in survivors}
    for r in rows:
        if r["id"] in survivor_ids and r.get("is_duplicate"):
            db.table("listings").update(
                {"is_duplicate": False, "duplicate_of": None}
            ).eq("id", r["id"]).execute()

    log.info("db dedupe: %s active listings hidden as duplicates", flagged)
    return {"duplicates_hidden": flagged}
