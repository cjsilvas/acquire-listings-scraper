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

log = logging.getLogger("fold")

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
db = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

UA = "FoldListingsBot/1.0 (accounting practice aggregator; contact: cj@eagleeyeequity.com)"

# Lower number wins during dedupe. Origin brokers beat marketplaces, always.
SOURCE_PRIORITY = {
    "aba": 1, "naab": 1, "aps": 1, "poe": 1, "ppt": 1, "atb": 1,
    "businessesforsale": 5,
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

    for item in listings:
        key = dedupe_key(item)
        if not key[0] or key[1] is None or not key[2]:
            best[("unique", item["fingerprint"])] = item      # not enough to match on
            continue

        incumbent = best.get(key)
        if incumbent is None:
            best[key] = item
            continue

        dropped += 1
        if _wins(item, incumbent):
            item["also_listed_at"] = sorted(
                set(incumbent.get("also_listed_at", []) + [incumbent["source"]])
            )
            best[key] = item
        else:
            incumbent["also_listed_at"] = sorted(
                set(incumbent.get("also_listed_at", []) + [item["source"]])
            )

    if dropped:
        log.info("dedupe collapsed %s duplicate listings", dropped)
    return list(best.values())


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
            "agent_email": item.get("agent_email"),
            "agent_phone": item.get("agent_phone"),
            "services": item.get("services") or [],
            "tags": item.get("tags") or [],
            "available_after": item.get("available_after"),
            "status": item.get("status", "active"),
            "active": item.get("status", "active") in ("active", "pending"),
            "last_seen": now,
            "miss_count": 0,
        }

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

        # Gone for two consecutive runs. We do not know why, so we say so.
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
