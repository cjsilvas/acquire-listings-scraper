"""
Agent enrichment. Fills agent_name (and phone where published) on listings
that do not have one yet. Runs after sync, touches only rows missing data,
so the first pass does the backlog and later passes only handle new listings.

Two strategies:
  ABA   the listing page publishes brokerFirst / brokerLast in its share link
        and a phone in the deal owner block. One fetch per listing, capped.
  APS   agents are not on listing pages. APS assigns brokers by territory,
        published at /connect-with-your-broker/. We fetch that one page,
        build a state to team map, and stamp listings by state.
        Texas is split by region across teams, so Texas stays unassigned.
"""

import re, logging
from typing import Dict, Optional

from engine import db, fetch

log = logging.getLogger("fold.enrich")

# How many ABA detail pages one run may fetch. Keeps runs short and polite.
ABA_FETCH_CAP = 50

STATE_ABBR = {
    'alabama':'AL','alaska':'AK','arizona':'AZ','arkansas':'AR','california':'CA','colorado':'CO',
    'connecticut':'CT','delaware':'DE','florida':'FL','georgia':'GA','hawaii':'HI','idaho':'ID',
    'illinois':'IL','indiana':'IN','iowa':'IA','kansas':'KS','kentucky':'KY','louisiana':'LA',
    'maine':'ME','maryland':'MD','massachusetts':'MA','michigan':'MI','minnesota':'MN',
    'mississippi':'MS','missouri':'MO','montana':'MT','nebraska':'NE','nevada':'NV',
    'new hampshire':'NH','new jersey':'NJ','new mexico':'NM','new york':'NY',
    'north carolina':'NC','north dakota':'ND','ohio':'OH','oklahoma':'OK','oregon':'OR',
    'pennsylvania':'PA','rhode island':'RI','south carolina':'SC','south dakota':'SD',
    'tennessee':'TN','utah':'UT','vermont':'VT','virginia':'VA',
    'washington':'WA','west virginia':'WV','wisconsin':'WI','wyoming':'WY',
    'district of columbia':'DC',
}

# Fallback map from the APS broker page, used if the live fetch fails.
# Texas is intentionally absent: it is split by region across three teams.
APS_FALLBACK = {
    'CO':'Kevin J. Overberg, CPA/PFS',
    'AK':'Holmes Group','DE':'Holmes Group','HI':'Holmes Group','ID':'Holmes Group',
    'IL':'Holmes Group','IA':'Holmes Group','MD':'Holmes Group','MA':'Holmes Group',
    'MI':'Holmes Group','MT':'Holmes Group','NE':'Holmes Group','NJ':'Holmes Group',
    'NM':'Holmes Group','ND':'Holmes Group','SD':'Holmes Group','UT':'Holmes Group',
    'WV':'Holmes Group','WY':'Holmes Group',
    'IN':'The Weldon Group','KY':'The Weldon Group','MN':'The Weldon Group',
    'OH':'The Weldon Group','WI':'The Weldon Group',
    'AL':'The A Team','AZ':'The A Team','CA':'The A Team','CT':'The A Team',
    'LA':'The A Team','ME':'The A Team','MS':'The A Team','NV':'The A Team',
    'NH':'The A Team','OR':'The A Team','RI':'The A Team','TN':'The A Team',
    'VT':'The A Team','WA':'The A Team',
    'AR':'Team Holmes','DC':'Team Holmes','GA':'Team Holmes','KS':'Team Holmes',
    'MO':'Team Holmes','OK':'Team Holmes','NY':'Team Holmes','NC':'Team Holmes',
    'PA':'Team Holmes','SC':'Team Holmes','VA':'Team Holmes',
    'FL':'Tim E. Price, CPA, PhD',
}


def aps_territory_map() -> Dict[str, str]:
    """Read the live APS broker page. Falls back to the shipped map."""
    page = fetch("https://accountingpracticesales.com/connect-with-your-broker/")
    if not page:
        return dict(APS_FALLBACK)
    text = re.sub(r"<script.*?</script>", " ", page, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    seg = text[text.find("Connect With Your Broker"):]

    # Entries read as: <Broker or team name> <comma list of territories>
    # Broker names are short; territory lists are state names.
    mapping: Dict[str, str] = {}
    pattern = re.compile(
        r"([A-Z][A-Za-z .,/']{2,40}?)\s+((?:(?:" +
        "|".join(re.escape(s.title()) for s in STATE_ABBR) +
        r"|Texas [\u2013-] North|Central Texas|West Texas|Southeast Texas)[,\s]*(?:and\s+)?)+)")
    for m in pattern.finditer(seg):
        broker = m.group(1).strip(" ,")
        broker = re.sub(r"^(?:Your\s+Broker|Connect\s+With\s+Your\s+Broker)\s*", "", broker, flags=re.I).strip()
        if broker.lower().startswith(("connect", "available", "seller", "buyer")):
            continue
        # A state name is never a broker. Rejecting these kills greedy matches.
        if broker.lower() in STATE_ABBR:
            continue
        # Neither is a compass direction or a stray piece of a territory list.
        junk = ("north","south","east","west","central","southeast","northeast",
                "southwest","northwest")
        provinces = ("saskatchewan","yukon","quebec","ontario","manitoba","alberta",
                     "nunavut","labrador","columbia","brunswick","newfoundland","scotia")
        bl = broker.lower()
        if bl in junk or any(p in bl for p in provinces) or \
           any(re.search(r"\b"+re.escape(s)+r"\b", bl) for s in STATE_ABBR):
            continue
        territories = m.group(2)
        for state_name, ab in STATE_ABBR.items():
            if re.search(r"\b" + re.escape(state_name.title()) + r"\b", territories):
                mapping[ab] = broker
    # Anything the live parse missed or mangled falls back to the shipped map.
    for ab, broker in APS_FALLBACK.items():
        cur = mapping.get(ab)
        if not cur or cur.lower() in STATE_ABBR:
            mapping[ab] = broker
    if len(mapping) < 20:
        log.warning("aps territory parse looked thin (%s states), using fallback", len(mapping))
        return dict(APS_FALLBACK)
    mapping.pop("TX", None)          # split territory, never assign by state alone
    return mapping


def enrich_aps() -> int:
    """Stamp APS listings with their territory team."""
    tmap = aps_territory_map()
    res = (db.table("listings")
             .select("fingerprint,state")
             .eq("source", "aps").is_("agent_name", "null")
             .execute())
    updated = 0
    for row in (res.data or []):
        team = tmap.get((row.get("state") or "").upper())
        if not team:
            continue
        db.table("listings").update({"agent_name": team}) \
          .eq("fingerprint", row["fingerprint"]).execute()
        updated += 1
    log.info("enrich aps: stamped %s listings with territory teams", updated)
    return updated


def _aba_agent(page: str):
    name = None
    m = re.search(r"brokerFirst=([A-Za-z .'-]{1,30})&(?:amp;|#0?38;)?brokerLast=([A-Za-z .'-]{1,30})", page)
    if m:
        name = f"{m.group(1).strip()} {m.group(2).strip()}"
    phone = None
    m = re.search(r'deal-owner-phone">.*?tel:([\d() .+-]{7,20})', page, re.S)
    if m:
        phone = m.group(1).strip()
    return name, phone


def enrich_aba() -> int:
    """Fetch ABA pages missing an agent name and parse it out."""
    res = (db.table("listings")
             .select("fingerprint,source_url")
             .eq("source", "aba").is_("agent_name", "null")
             .limit(ABA_FETCH_CAP)
             .execute())
    updated = 0
    for row in (res.data or []):
        page = fetch(row["source_url"])
        if not page:
            continue
        name, phone = _aba_agent(page)
        if not (name or phone):
            continue
        patch = {}
        if name:
            patch["agent_name"] = name
        if phone:
            patch["agent_phone"] = phone
        db.table("listings").update(patch).eq("fingerprint", row["fingerprint"]).execute()
        updated += 1
    log.info("enrich aba: filled agent details on %s listings", updated)
    return updated


def run_enrichment() -> Dict[str, int]:
    out = {}
    try:
        out["aps"] = enrich_aps()
    except Exception as e:
        log.exception("aps enrichment failed: %s", e)
        out["aps"] = -1
    try:
        out["aba"] = enrich_aba()
    except Exception as e:
        log.exception("aba enrichment failed: %s", e)
        out["aba"] = -1
    return out
