"""
One parser per broker. Each returns a list of plain dicts.
The engine handles everything else.

Adding a broker means adding one function here and one line in ALL_SOURCES.
"""

import re, html, logging
from typing import List, Dict, Optional
from engine import fetch, fetch_via_api, fingerprint

log = logging.getLogger("fold.sources")

STATES = {
    'alabama':'AL','alaska':'AK','arizona':'AZ','arkansas':'AR','california':'CA','colorado':'CO',
    'connecticut':'CT','delaware':'DE','florida':'FL','georgia':'GA','hawaii':'HI','idaho':'ID',
    'illinois':'IL','indiana':'IN','iowa':'IA','kansas':'KS','kentucky':'KY','louisiana':'LA',
    'maine':'ME','maryland':'MD','massachusetts':'MA','michigan':'MI','minnesota':'MN',
    'mississippi':'MS','missouri':'MO','montana':'MT','nebraska':'NE','nevada':'NV',
    'new hampshire':'NH','new jersey':'NJ','new mexico':'NM','new york':'NY',
    'north carolina':'NC','north dakota':'ND','ohio':'OH','oklahoma':'OK','oregon':'OR',
    'pennsylvania':'PA','rhode island':'RI','south carolina':'SC','south dakota':'SD',
    'tennessee':'TN','texas':'TX','utah':'UT','vermont':'VT','virginia':'VA',
    'washington':'WA','west virginia':'WV','wisconsin':'WI','wyoming':'WY',
}
ABBR = set(STATES.values())


def strip_tags(h: str) -> str:
    h = re.sub(r"<script.*?</script>", " ", h, flags=re.S | re.I)
    h = re.sub(r"<style.*?</style>", " ", h, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", h)))


def money(text: str) -> Optional[int]:
    m = re.search(r"\$\s?([\d,]{4,})", text or "")
    return int(m.group(1).replace(",", "")) if m else None


def state_from(text: str) -> Optional[str]:
    t = (text or "").lower()
    for name, ab in STATES.items():
        if name in t:
            return ab
    m = re.search(r",\s*([A-Z]{2})\b", text or "")
    return m.group(1) if m and m.group(1) in ABBR else None


def services_from(text: str) -> List[str]:
    t = (text or "").lower()
    out = []
    if "tax" in t: out.append("Tax")
    if "bookkeep" in t: out.append("Bookkeeping")
    if "payroll" in t: out.append("Payroll")
    if "audit" in t or "attest" in t: out.append("Audit")
    if any(w in t for w in ("advisory", "consult", "cas", "wealth")): out.append("Advisory")
    if "account" in t or "cpa" in t: out.append("Accounting")
    return out or ["Accounting"]


def clean_title(t: str) -> str:
    t = html.unescape(t or "")
    t = re.sub(r"[\u2013\u2014]", " ", t)
    t = re.sub(r"\(?\b[A-Z]{2}[- ]?\d{3,}\)?", " ", t)
    t = re.sub(r"^\s*\$[\d,]+\s*(mm|m|k)?\s*\(?\s*revenue\s*\)?\s*[-,:]?", " ", t, flags=re.I)
    t = re.sub(r"\$[\d,]{4,}", " ", t)
    t = re.sub(r"\b\d+(mm|m|k)\b\s*\(?\s*revenue\s*\)?", " ", t, flags=re.I)
    t = re.sub(r"\bfor sale\b", " ", t, flags=re.I)
    t = re.sub(r"\(\s*\)", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ,.-|:")
    # collapse a phrase repeated back to back
    w = t.split()
    for n in range(len(w)//2, 1, -1):
        if w[:n] == w[n:2*n]:
            t = " ".join(w[n:]); break
    letters=[c for c in t if c.isalpha()]
    if letters and sum(c.isupper() for c in letters)/len(letters) > 0.6:
        t = t.title()
    t = re.sub(r"\bCpa\b","CPA",t)
    t = re.sub(r"\b(Nw|Ne|Sw|Se)\b", lambda m: m.group(1).upper(), t)
    return t.strip(" ,.-|:") or "Accounting Practice"



CITY_STATE = {
    "los angeles":"CA","san francisco":"CA","san diego":"CA","sacramento":"CA","orange county":"CA",
    "seattle":"WA","portland":"OR","denver":"CO","phoenix":"AZ","las vegas":"NV","austin":"TX",
    "dallas":"TX","houston":"TX","san antonio":"TX","atlanta":"GA","miami":"FL","tampa":"FL",
    "orlando":"FL","charlotte":"NC","raleigh":"NC","nashville":"TN","chicago":"IL","detroit":"MI",
    "minneapolis":"MN","milwaukee":"WI","boston":"MA","new york":"NY","brooklyn":"NY",
    "philadelphia":"PA","pittsburgh":"PA","baltimore":"MD","richmond":"VA","columbus":"OH",
    "cleveland":"OH","cincinnati":"OH","indianapolis":"IN","kansas city":"MO","st louis":"MO",
    "salt lake":"UT","boise":"ID","honolulu":"HI","new orleans":"LA","toledo":"OH",
}

def state_deep(*texts):
    precise = [t for t in texts[:2] if t]
    for t in precise:
        st = state_from(t)
        if st:
            return st
    joined = " ".join(precise).lower()
    for city, ab in CITY_STATE.items():
        if city in joined:
            return ab
    for t in texts[2:]:
        if not t:
            continue
        m = re.search(r"(?:Location|State)\s*:?\s*([A-Za-z ]{4,20})", t)
        if m:
            st = state_from(m.group(1))
            if st:
                return st
    return None


def best_description(text):
    chunks = re.split(r"(?:\s{3,}|\|)", text or "")
    good = [c.strip() for c in chunks
            if len(c.strip()) > 220 and c.count(".") >= 2
            and not re.search(r"cookie|privacy|newsletter|subscribe|copyright", c, re.I)]
    return max(good, key=len)[:4000] if good else None


def _base(source, label, url, **kw) -> Dict:
    d = {"source": source, "source_label": label, "source_url": url,
         "status": "active", **kw}
    d["fingerprint"] = fingerprint(d)
    return d


# ----------------------------------------------------------------------
# ABA Advisors  (acctsales.com)   plain HTML, detail page per listing
# ----------------------------------------------------------------------

def scrape_aba() -> List[Dict]:
    index = fetch("https://acctsales.com/practices-for-sale/")
    if not index:
        return []
    urls = sorted(set(re.findall(r'href="(https://acctsales\.com/listing/[^"#?]+)"', index)))
    out = []
    for url in urls:
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split(" - ABA Advisors")[0] if title else ""

        rev = re.search(r"Gross Revenue:\s*\$?([\d,]+)", text, re.I)
        loc = re.search(r"Location:\s*([A-Za-z .,&-]{3,45})", text, re.I)
        ask = re.search(r"Asking Price:\s*\$?([\d,]+)", text, re.I)

        status = "active"
        if re.search(r"sale pending", text, re.I):
            status = "pending"
        elif re.search(r"has been sold|practice has sold", text, re.I):
            status = "sold"

        desc = ""
        m = re.search(r"(OVERVIEW.{200,4000}?)(?:Business ID|Contact|Share this)", text, re.I | re.S)
        if m:
            desc = m.group(1).strip()

        code = re.search(r"\b([A-Z]{2}-\d{3,})\b", text)
        emails = re.findall(r"[\w.+-]+@acctsales\.com", text)
        agent = next((e for e in emails if not e.startswith("info@")), None)

        out.append(_base(
            "aba", "ABA Advisors", url,
            firm_type=clean_title(title),
            state=state_deep(loc.group(1) if loc else None, title, text),
            city=(loc.group(1).strip() if loc else None),
            revenue=int(rev.group(1).replace(",", "")) if rev else None,
            asking_price=int(ask.group(1).replace(",", "")) if ask else None,
            description=(desc or best_description(text)) or None,
            listing_code=code.group(1) if code else None,
            agent_email=agent,
            services=services_from(title + " " + desc),
            status=status,
        ))
    log.info("aba: %s listings", len(out))
    return out


# ----------------------------------------------------------------------
# Naab Consulting   plain HTML, status printed on the page
# ----------------------------------------------------------------------

def scrape_naab() -> List[Dict]:
    index = fetch("https://www.naabconsulting.com/practices-for-sale/")
    if not index:
        return []
    urls = sorted(set(re.findall(
        r'href="(https://www\.naabconsulting\.com/practice-listing/[^"#?]+)"', index)))
    out = []
    for url in urls:
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split(" - Naab")[0] if title else ""

        if "portfolio_category-sold" in page:
            status = "sold"
        elif "portfolio_category-pending" in page:
            status = "pending"
        else:
            m = re.search(r"Status:\s*(Sold|Pending|Under Agreement|Available)", text, re.I)
            v = m.group(1).lower() if m else "available"
            status = "sold" if v == "sold" else ("active" if v == "available" else "pending")

        slug = url.rstrip("/").split("/")[-1]
        code = slug.upper() if re.match(r"^[a-z]{2}[-]?\d{3,}", slug) else None
        rev = money(text)

        desc = ""
        m = re.search(r"(?:Overview|Description)(.{200,4000}?)(?:Contact|Inquire|Status)",
                      text, re.I | re.S)
        if m:
            desc = m.group(1).strip()

        out.append(_base(
            "naab", "Naab Consulting", url,
            firm_type=clean_title(title),
            state=state_deep(slug.replace("-", " "), title, text),
            revenue=rev,
            description=(desc or best_description(text)) or None,
            listing_code=code,
            services=services_from(title + " " + desc),
            status=status,
        ))
    log.info("naab: %s listings", len(out))
    return out


# ----------------------------------------------------------------------
# Accounting Practice Sales
# Sorted New, Available, Pending, On Hold, Sold. We stop at the sold line.
# Everything lives on the index, so no detail fetches at all.
# ----------------------------------------------------------------------

APS_REGIONS = [
    "united-states/northeast", "united-states/southeast",
    "united-states/midwest-us", "united-states/southwest-us",
    "united-states/west", "canada/any", "Worldwide/all",
]

APS_STATUS = {
    "new": "active", "available": "active", "sale pending": "pending",
    "on hold": "on_hold", "sold": "sold",
}

def _aps_parse_page(page_html: str) -> List[Dict]:
    out = []
    for block in re.findall(
        r'<a class="apslistingitem[^"]*"\s+href="([^"]+)"(.*?)</a>', page_html, re.S):
        url, body = block
        def field(label):
            m = re.search(
                rf'listingstattitle">{label}[^<]*</div><div class="listingstatinfo">([^<]*)',
                body)
            return html.unescape(m.group(1)).strip() if m else None

        status_raw = re.search(r'apslistingitem_lstatus">([^<]*)', body)
        status_raw = html.unescape(status_raw.group(1)).strip() if status_raw else "Available"
        name = re.search(r'apslistingitem_lname">([^<]*)', body)
        name = html.unescape(name.group(1)).strip() if name else ""

        avail_after = None
        low = status_raw.lower()
        status = APS_STATUS.get(low)
        if status is None:
            m = re.match(r"available after\s+(\d{1,2}/\d{1,2})", low)
            if m:
                status, avail_after = "active", m.group(1)
            else:
                status = "active"

        out.append(_base(
            "aps", "Accounting Practice Sales", html.unescape(url),
            firm_type=clean_title(name),
            listing_code=field("Listing"),
            state=state_from(field("Location") or name),
            city=field("Location"),
            revenue=money(field("Annual") or ""),
            asking_price=money(field("Asking") or ""),
            listing_type=field("Type"),
            services=services_from(name + " " + (field("Type") or "")),
            status=status,
            _raw_status=status_raw,
            available_after_raw=avail_after,
        ))
    return out


def scrape_aps(max_pages: int = 30) -> List[Dict]:
    out = []
    for region in APS_REGIONS:
        all_sold_streak = 0
        for page in range(0, max_pages):
            url = (f"https://accountingpracticesales.com/{region}/"
                   if page == 0 else
                   f"https://accountingpracticesales.com/{region}/{page}/")
            html_doc = fetch(url)
            if not html_doc:
                break
            items = _aps_parse_page(html_doc)
            if not items:
                break

            live = [i for i in items if i["status"] != "sold"]
            out.extend(live)

            if len(live) == 0:
                all_sold_streak += 1
                # Two consecutive all sold pages means we are past the live inventory.
                if all_sold_streak >= 2:
                    log.info("aps %s: stopped at page %s, into sold archive", region, page)
                    break
            else:
                all_sold_streak = 0
    log.info("aps: %s live listings", len(out))
    return out


# ----------------------------------------------------------------------
# Poe Group Advisors   index at practice-search, detail at /practice/{code}/
# ----------------------------------------------------------------------

def scrape_poe() -> List[Dict]:
    index = fetch("https://poegroupadvisors.com/buying/practice-search/")
    if not index:
        return []
    urls = sorted(set(re.findall(r'href="(https://poegroupadvisors\.com/practice/[^"#?]+)"', index)))
    out = []
    for url in urls:
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split(" | Poe Group")[0] if title else ""

        def val(cls):
            m = re.search(rf'{cls}-value">([^<]*)', page)
            return html.unescape(m.group(1)).strip() if m else None

        ask = money(val("asking-price") or "")
        # Status renders as a label/value spec row, with the value often inside a
        # styled span: <p class="label">Status:</p> <p class="value"><span>SOLD</span></p>
        status_raw = ""
        m = re.search(r'>\s*Status:\s*</(?:p|td)>\s*<(?:p|td)[^>]*>(.*?)</(?:p|td)>',
                      page, re.I | re.S)
        if m:
            status_raw = re.sub(r"<[^>]+>", " ", m.group(1)).strip().lower()
        if not status_raw:
            m = re.search(r"Status:\s*([A-Za-z ]{3,20}?)\s*(?:Designation|Listing|Asking|Location|$)",
                          text, re.I)
            status_raw = m.group(1).strip().lower() if m else ""
        if not status_raw:
            status_raw = (val("status") or "for sale").lower()
        status = ("sold" if "sold" in status_raw else
                  "pending" if "contract" in status_raw or "pending" in status_raw else
                  "active")
        loc = val("location")
        rev = None
        m = re.search(r"Annual Gross:?\s*\$?([\d,]{4,})", text, re.I)
        if m:
            rev = int(m.group(1).replace(",", ""))

        desc = ""
        m = re.search(r"(?:Overview|Description)(.{200,4000}?)(?:Contact|Inquire|Request)",
                      text, re.I | re.S)
        if m:
            desc = m.group(1).strip()

        code = url.rstrip("/").split("/")[-1].upper()

        out.append(_base(
            "poe", "Poe Group Advisors", url,
            firm_type=clean_title(title),
            listing_code=code,
            state=state_deep(loc, title, text),
            city=loc,
            revenue=rev,
            asking_price=ask,
            description=(desc or best_description(text)) or None,
            services=services_from(title + " " + desc),
            status=status,
        ))
    log.info("poe: %s listings", len(out))
    return out


ALL_SOURCES = {
    "aba": scrape_aba,
    "naab": scrape_naab,
    "aps": scrape_aps,
    "poe": scrape_poe,
}


# ----------------------------------------------------------------------
# Private Practice Transitions   /business-listing/{slug}/
# Publishes gross revenue, SDE, EBITDA and asking price on the card
# ----------------------------------------------------------------------

PPT_PAGES = [
    "https://privatepracticetransitions.com/business-industry/accounting-tax/accounting/",
    "https://privatepracticetransitions.com/business-industry/accounting-tax/",
    "https://privatepracticetransitions.com/listings/",
]

def scrape_ppt() -> List[Dict]:
    urls = set()
    for idx in PPT_PAGES:
        page = fetch(idx)
        if page:
            urls |= set(re.findall(
                r'href="(https://privatepracticetransitions\.com/business-listing/[^"#?]+)"', page))
    out = []
    for url in sorted(urls):
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split("|")[0].split(" - Private Practice")[0] if title else ""

        def num(label):
            m = re.search(rf"{label}\s*:?\s*\$?\s*([\d,]{{4,}})", text, re.I)
            return int(m.group(1).replace(",", "")) if m else None

        low = text.lower()
        status = ("sold" if re.search(r"\bsold\b", low[:4000]) else
                  "pending" if re.search(r"under contract|sale pending|pending", low[:4000]) else
                  "active")

        code = re.search(r"\b(\d{4})\s*[\u2013\-]", title)
        out.append(_base(
            "ppt", "Private Practice Transitions", url,
            firm_type=clean_title(title),
            listing_code=code.group(1) if code else None,
            state=state_deep(title, text[:1500], text),
            revenue=num("Gross Revenue"),
            asking_price=num("Asking Price"),
            cash_flow=num("SDE") or num("EBITDA"),
            description=best_description(text),
            services=services_from(title + " " + text[:600]),
            status=status,
        ))
    log.info("ppt: %s listings", len(out))
    return out


# ----------------------------------------------------------------------
# Accounting and Tax Brokerage (atbcal.com)   California focused
# ----------------------------------------------------------------------

ATB_INDEXES = [
    "https://atbcal.com/category/listing-posts/california/northern-california/",
    "https://atbcal.com/category/listing-posts/california/central-california/",
    "https://atbcal.com/category/listing-posts/california/southern-california/",
    "https://www.atbcal.com/listings_detail/",
]

def scrape_atb() -> List[Dict]:
    urls = set()
    for idx in ATB_INDEXES:
        page = fetch(idx)
        if not page:
            continue
        for u in re.findall(r'href="(https?://(?:www\.)?atbcal\.com/[^"#?]+)"', page):
            path = re.sub(r"https?://(www\.)?atbcal\.com", "", u).strip("/")
            # listings sit at the root as city-code, e.g. folsom-fol226
            if re.fullmatch(r"[a-z0-9-]+-[a-z]{2,5}\d{2,5}", path):
                urls.add(u)
    out = []
    for url in sorted(urls):
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split("|")[0].split(" - ATB")[0] if title else ""
        tl = title.lower()
        head = text[:600].lower()
        status = ("sold" if re.search(r"\bsold\b", tl) else
                  "pending" if re.search(r"pending|under contract|in escrow", tl)
                  else "active")
        rev = None
        m = re.search(r"(?:gross|annual)\s+(?:revenue|receipts|billings)\D{0,12}\$?([\d,]{4,})",
                      text, re.I)
        if m:
            rev = int(m.group(1).replace(",", ""))
        elif money(text):
            rev = money(text)
        ask = re.search(r"asking(?:\s+price)?\D{0,12}\$?([\d,]{4,})", text, re.I)

        out.append(_base(
            "atb", "Accounting and Tax Brokerage", url,
            firm_type=clean_title(title),
            state=state_deep(title, text[:1200]) or "CA",
            revenue=rev,
            asking_price=int(ask.group(1).replace(",", "")) if ask else None,
            description=best_description(text),
            services=services_from(title + " " + text[:600]),
            status=status,
        ))
    log.info("atb: %s listings", len(out))
    return out


# ----------------------------------------------------------------------
# BusinessesForSale US   marketplace, loses every dedupe tie to an origin broker
# ----------------------------------------------------------------------

# Site pages that match the detail URL shape but are not listings.
BFS_NOT_LISTINGS = ("emailalerts", "contact", "advice", "sell-your-business",
                    "brokers", "franchises", "login", "register")

def scrape_bfs(max_pages: int = 6) -> List[Dict]:
    urls = set()
    for p in range(1, max_pages + 1):
        # Pagination on this site is a suffix: ...for-sale, ...for-sale-2, ...for-sale-3
        idx = ("https://us.businessesforsale.com/us/search/accountancy-practices-for-sale"
               + ("" if p == 1 else f"-{p}"))
        page = fetch(idx)
        if not page:
            break
        found = set(re.findall(
            r'href="(https://us\.businessesforsale\.com/us/[a-z0-9-]+\.aspx)"', page))
        found = {u for u in found
                 if not any(bad in u.lower() for bad in BFS_NOT_LISTINGS)}
        if not found:
            break
        urls |= found
    out = []
    for url in sorted(urls):
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)

        # BusinessesForSale renders the real listing data (full description,
        # country/region, prices) inside JSON-LD script blocks, not visible
        # HTML. Parse those first; fall back to page text where absent.
        ld = {}
        for blk in re.findall(
                r'<script[^>]*type=["\']application/(?:ld\+json|json)["\'][^>]*>(.*?)</script>',
                page, re.S):
            try:
                obj = json.loads(blk.strip())
            except Exception:
                continue
            cands = obj if isinstance(obj, list) else [obj]
            for c in cands:
                if isinstance(c, dict) and (c.get("description") or c.get("offers") or c.get("makesOffer")):
                    for k, v in c.items():
                        ld.setdefault(k, v)
        ld_desc = ld.get("description") if isinstance(ld.get("description"), str) else None
        ld_region = None
        addr = ld.get("address")
        if isinstance(addr, dict):
            ld_region = addr.get("addressRegion") or addr.get("addressLocality")
        ld_price = None
        offers = ld.get("offers") or ld.get("makesOffer")
        if isinstance(offers, dict):
            p = offers.get("price") or (offers.get("priceSpecification") or {}).get("price")
            if p:
                try:
                    ld_price = int(re.sub(r"[^\d]", "", str(p)))
                except (ValueError, TypeError):
                    ld_price = None

        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split("|")[0] if title else ""
        low = text.lower()
        if "under offer" in low[:3000] or "sale pending" in low[:3000]:
            status = "pending"
        elif re.search(r"\bsold\b", low[:2000]):
            status = "sold"
        else:
            status = "active"

        def num(label):
            m = re.search(rf"(?:{label})\D{{0,15}}\$?\s*([\d,]{{4,}})", text, re.I)
            if not m:
                return None
            try:
                return int(m.group(1).replace(",", ""))
            except (AttributeError, ValueError):
                return None

        listed_by = None
        m = re.search(r"Listed by:?\s*([A-Z][A-Za-z0-9 .,&'()/-]{2,60}?)\s*(?:\.|Listing ID|Seller ref|Asking|Sales Revenue|$)", text)
        if m:
            listed_by = m.group(1).strip(" .,")

        item = _base(
            "businessesforsale", "BusinessesForSale", url,
            listing_brokerage=listed_by,
            firm_type=clean_title(title),
            state=state_deep(title, text[:1500]),
            revenue=num("(?:gross )?revenue|turnover|sales"),
            asking_price=num("asking price") or ld_price,
            cash_flow=num("cash flow|net profit"),
            description=(ld_desc.strip()[:1500] if ld_desc else best_description(text)),
            services=services_from(title + " " + (ld_desc or text[:600])),
            status=status,
        )
        # Prefer the JSON-LD region for state when the text parse missed it.
        if not item.get("state") and ld_region:
            st = _bbs_state(ld_region) or state_from(ld_region)
            if st:
                item["state"] = st
        # The marketplace names the actual listing firm: "Listed by X"
        lb = re.search(r"Listed by:?\s+([A-Z][A-Za-z0-9&.,' -]{2,60}?)(?:\s{2,}|\.\s|$|Asking|Sales Revenue|Contact)", text)
        if lb:
            item["brokerage"] = lb.group(1).strip(" .,")
        # A real listing carries at least one financial figure or a state.
        if not (item.get("state") or item.get("revenue") or item.get("asking_price")):
            log.info("bfs: skipping non listing page %s", url)
            continue
        out.append(item)
    log.info("bfs: %s listings", len(out))
    return out


ALL_SOURCES.update({
    "ppt": scrape_ppt,
    "atb": scrape_atb,
    "businessesforsale": scrape_bfs,
})


# ----------------------------------------------------------------------
# ProHorizons   West coast broker. Status table right on the page,
# including Sold, so this source publishes ground truth directly.
# ----------------------------------------------------------------------

PH_PAGES = [
    "https://www.prohorizons.com/pacific/",       # California
    "https://www.prohorizons.com/oregon/",
    "https://www.prohorizons.com/washington/",
]

def scrape_prohorizons() -> List[Dict]:
    out = []
    for idx in PH_PAGES:
        page = fetch(idx)
        if not page:
            continue
        body = page[page.find("<tbody"):page.find("</table")]
        for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if len(cells) < 6:
                continue
            def cl(c):
                return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", c))).strip()
            opp, city, county, state_name, status_raw, gross = [cl(c) for c in cells[:6]]
            if not city or not state_name:
                continue
            link = re.search(r'href="([^"]+)"', cells[1])
            url = link.group(1) if link else idx
            st = state_from(state_name) or state_from(city)
            sr = status_raw.lower()
            status = ("sold" if "sold" in sr else
                      "pending" if "pending" in sr else "active")
            rev = money(gross)
            code_id = "PH-" + re.sub(r"[^A-Z0-9]", "", (st or "XX") + city.upper()) + "-" + str(rev or 0)
            out.append(_base(
                "prohorizons", "ProHorizons", url,
                firm_type=clean_title(f"{city}, {state_name} Accounting Practice"),
                listing_code=code_id,
                city=city,
                state=st,
                revenue=rev,
                services=["Accounting", "Tax"],
                status=status,
            ))
    log.info("prohorizons: %s listings", len(out))
    return out


# ----------------------------------------------------------------------
# Padgett Advisors   franchise firm resales, detail page per listing
# ----------------------------------------------------------------------

def scrape_padgett() -> List[Dict]:
    idx = fetch("https://www.padgettadvisors.com/join/firms-for-sale/")
    if not idx:
        return []
    urls = sorted(set(re.findall(
        r'href="(https://www\.padgettadvisors\.com/blog/portfolio/[^"#?]+)"', idx)))
    out = []
    for url in urls:
        page = fetch(url)
        if not page:
            continue
        text = strip_tags(page)
        title = re.search(r"<title>([^<]*)", page)
        title = title.group(1).split("|")[0].split(" - Padgett")[0] if title else ""
        m = re.search(r"Annual Revenue:?\s*\$([\d,]+)", text)
        rev = int(m.group(1).replace(",", "")) if m else None
        desc = None
        m = re.search(r"Firm Description:\s*(.{200,4000}?)(?:Inquire|Contact|Interested|Find an office|$)",
                      text, re.S)
        if m:
            desc = m.group(1).strip()
        low = text[:3000].lower()
        status = ("sold" if re.search(r"\bsold\b", title.lower()) or "no longer available" in low
                  else "pending" if re.search(r"sale pending|under contract", low)
                  else "active")
        out.append(_base(
            "padgett", "Padgett Advisors", url,
            firm_type=clean_title(title),
            state=state_deep(title, text[:1500]),
            revenue=rev,
            description=desc or best_description(text),
            services=services_from(title + " " + (desc or "")[:400]),
            status=status,
        ))
    log.info("padgett: %s listings", len(out))
    return out


ALL_SOURCES.update({
    "prohorizons": scrape_prohorizons,
    "padgett": scrape_padgett,
})


# ----------------------------------------------------------------------
# Accounting Biz Brokers   WordPress site. The listing custom post type
# is exposed at /wp-json/wp/v2/listing, and each detail page carries the
# authoritative status and state in its body class (status-sold,
# status-sale-pending, state-tennessee), plus Annual Gross and Asking
# Price in the page text. No rendering or proxy needed.
# ----------------------------------------------------------------------

ABB_STATUS = {
    "status-sold": "sold",
    "status-sale-pending": "pending",
    "status-new": "active",
    "status-publish": "active",
}

def scrape_abizbrokers() -> List[Dict]:
    feed = fetch("https://accountingbizbrokers.com/wp-json/wp/v2/listing?per_page=100")
    if not feed:
        return []
    try:
        import json as _json
        rows = _json.loads(feed)
    except Exception:
        log.error("abizbrokers: feed was not valid JSON")
        return []

    out = []
    for r in rows:
        url = r.get("link")
        title = html.unescape((r.get("title") or {}).get("rendered", "")).strip()
        if not url or not title:
            continue
        page = fetch(url)
        if not page:
            continue

        # Body class holds the ground-truth status and state.
        cls = re.search(r'post-\d+ listing type-listing ([^"]+)', page)
        classes = cls.group(1).split() if cls else []
        status = "active"
        state = None
        for c in classes:
            if c in ABB_STATUS:
                status = ABB_STATUS[c]
            if c.startswith("state-"):
                nm = c[len("state-"):].replace("-", " ")
                if nm != "virtual":
                    state = state_from(nm)

        text = strip_tags(page)
        m = re.search(r"Annual Gross\s*\$?([\d,]+)", text, re.I)
        rev = int(m.group(1).replace(",", "")) if m else None
        m = re.search(r"Asking Price\s*\$?([\d,]+)", text, re.I)
        ask = int(m.group(1).replace(",", "")) if m else None

        # Body text is prefixed by the nav menu; the real copy starts after
        # the status word (NEW / SOLD / SALE PENDING / AVAILABLE) that follows
        # the firm name, and ends at the inquiry form.
        desc = None
        body = re.split(r"View Listings Free Market Analysis", text, 1)
        tail = body[-1]
        m = re.search(r"\b(?:NEW|SOLD|SALE PENDING|AVAILABLE)\b(.{150,4000}?)"
                      r"(?:I Want to Know More|First Name Last Name|Asking Price\b)",
                      tail, re.S)
        if m:
            desc = re.sub(r"\s+", " ", m.group(1)).strip(" .-")

        slug = url.rstrip("/").split("/")[-1]
        out.append(_base(
            "abizbrokers", "Accounting Biz Brokers", url,
            firm_type=clean_title(title),
            listing_code="ABB-" + slug[:40],
            state=state or state_deep(title, text[:1500]),
            revenue=rev,
            asking_price=ask,
            description=desc or best_description(text),
            services=services_from(title + " " + text[:600]),
            status=status,
        ))
    log.info("abizbrokers: %s listings", len(out))
    return out


ALL_SOURCES.update({
    "abizbrokers": scrape_abizbrokers,
})


# ----------------------------------------------------------------------
# DealStream   general marketplace. Listings sit in the page's ld+json
# SearchResultsPage "about" array; each detail page carries a Product
# with an Offer (price + availability = status). A marketplace, so the
# engine ranks it below origin brokers and the match logic hides any
# listing that an origin broker already carries.
# ----------------------------------------------------------------------

DS_CITY_STATE = {
    "detroit": "MI", "grand rapids": "MI", "chicago": "IL", "philadelphia": "PA",
    "marin": "CA", "san francisco": "CA", "sarasota": "FL", "montgomery county": "PA",
    "bucks county": "PA", "oakland county": "MI", "palm beach": "FL", "hernando": "FL",
    "indianapolis": "IN", "hartford": "CT", "albuquerque": "NM", "new orleans": "LA",
    "union county": "NJ", "houston": "TX", "dallas": "TX", "austin": "TX",
    "atlanta": "GA", "denver": "CO", "seattle": "WA", "portland": "OR",
    "boston": "MA", "nashville": "TN", "tampa": "FL", "orlando": "FL", "miami": "FL",
    "phoenix": "AZ", "tucson": "AZ", "charlotte": "NC", "raleigh": "NC",
    "columbus": "OH", "cleveland": "OH", "cincinnati": "OH", "kansas city": "MO",
    "st louis": "MO", "st. louis": "MO", "minneapolis": "MN", "milwaukee": "WI",
    "brooklyn": "NY", "manhattan": "NY", "long island": "NY", "westchester": "NY",
}

DS_AVAIL = {
    "InStock": "active", "PreOrder": "pending",
    "SoldOut": "sold", "OutOfStock": "sold", "Discontinued": "sold",
}

DS_NON_ACCT = re.compile(
    r"\b(POS|kiosk|point-of-sale|restaurant|retail|manufactur|construction|"
    r"dental|medical|law firm|hvac|plumbing|landscap|salon|gym|fitness|"
    r"liquor|laundr|car wash|franchise resale)\b", re.I)


def _ds_state(text):
    if not text:
        return None
    st = state_from(text)
    if st:
        return st
    low = text.lower()
    for city, ab in DS_CITY_STATE.items():
        if city in low:
            return ab
    return None


def _ds_money(text):
    if not text:
        return None
    m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*(mm|m|k)\b", text, re.I)
    if m:
        n = float(m.group(1).replace(",", ""))
        unit = m.group(2).lower()
        return int(n * (1_000_000 if unit in ("mm", "m") else 1_000))
    m = re.search(r"\$([\d,]{4,})", text)
    return int(m.group(1).replace(",", "")) if m else None


def _ds_ldjson(page, want_type):
    for blk in re.findall(r'<script type="application/ld\+json">(.*?)</script>', page, re.S):
        try:
            import json as _json
            j = _json.loads(blk)
            if j.get("@type") == want_type:
                return j
        except Exception:
            continue
    return None


def scrape_dealstream(deep: bool = False) -> List[Dict]:
    """
    Index-only by default. DealStream throttles a datacenter IP that fetches
    many detail pages quickly, so the routine light pass reads only the index
    (which already carries title, description, status words, and revenue where
    published). Pass deep=True on an infrequent schedule to fill price/status
    gaps from detail pages, and even then we go slowly and cap the count.
    """
    out, seen = [], set()
    import time as _time
    for pg in range(1, 10):
        idx = "https://dealstream.com/accounting-practices-for-sale" + (f"/{pg}" if pg > 1 else "")
        page = fetch_via_api(idx)
        if not page:
            break
        srp = _ds_ldjson(page, "SearchResultsPage")
        if not srp or not srp.get("about"):
            break
        new = 0
        for a in srp["about"]:
            it = a.get("item", {})
            url = it.get("url", "")
            if not url or url in seen:
                continue
            seen.add(url)
            new += 1
            name = html.unescape((it.get("name") or "").replace(" - DealStream", "")).strip()
            desc = html.unescape((it.get("description") or "")).strip()
            blob = name + " | " + desc
            if DS_NON_ACCT.search(blob) and not re.search(r"\b(CPA|accounting|tax|bookkeep|audit)\b", name, re.I):
                continue

            rev = _ds_money(name) or _ds_money(desc)
            ask = None
            low = blob.lower()
            status = ("sold" if re.search(r"\bsold\b", low)
                      else "pending" if re.search(r"sale pending|under contract", low)
                      else "active")

            out.append(_base(
                "dealstream", "DealStream", url,
                firm_type=clean_title(name),
                state=_ds_state(name) or _ds_state(desc),
                revenue=rev,
                asking_price=ask,
                description=desc[:1500] or None,
                services=services_from(blob),
                status=status,
                listing_code="DS-" + url.rstrip("/").split("/")[-1],
            ))
        if new == 0:
            break

    # Optional gentle deep pass: fill gaps from detail pages, slowly, capped.
    if deep:
        filled = 0
        for item in out:
            if filled >= 150:
                break
            if item["revenue"] is not None and item["asking_price"] is not None:
                continue
            detail = fetch_via_api(item["source_url"])
            _time.sleep(1.5)
            if not detail:
                continue
            filled += 1
            prod = _ds_ldjson(detail, "Product")
            if prod:
                offer = prod.get("offers", {}) or {}
                if offer.get("price") and not item["asking_price"]:
                    item["asking_price"] = int(offer["price"])
                avail = (offer.get("availability") or "").rsplit("/", 1)[-1]
                if avail in DS_AVAIL:
                    item["status"] = DS_AVAIL[avail]
            if item["revenue"] is None:
                m = re.search(r"(?:Revenue|Sales|Gross)\D{0,12}\$([\d,]{4,})", strip_tags(detail))
                if m:
                    item["revenue"] = int(m.group(1).replace(",", ""))

    log.info("dealstream: %s listings (deep=%s)", len(out), deep)
    return out


ALL_SOURCES.update({
    "dealstream": scrape_dealstream,
})


# ----------------------------------------------------------------------
# BizBuySell   the largest general marketplace. Blocks datacenter IPs hard;
# reachable only through ScraperAPI's ultra premium pool. Listings sit in the
# same ld+json SearchResultsPage "about" array pattern as DealStream, 50-57 per
# page across ~12 pages (~577 total). A marketplace, so ranked below origin
# brokers; the match logic hides any listing a broker already carries and flags
# the rest as possible direct sellers (this is where owner-listed FSBO deals
# turn up). Detail pages carry asking price, cash flow, and gross revenue.
# ----------------------------------------------------------------------

def _bbs_about(page):
    for b in re.findall(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
        try:
            import json as _json
            j = _json.loads(b.strip())
            if isinstance(j, dict) and j.get("about"):
                return j["about"]
        except Exception:
            continue
    return None


def _bbs_state(text):
    if not text:
        return None
    m = re.search(r",\s*([A-Z]{2})\b", text)
    if m and m.group(1) in ABBR:
        return m.group(1)
    return state_from(text)


# Current year used to judge staleness. BizBuySell never publishes a listing date,
# so the only age signal is the content itself: a listing that quotes financial
# figures from two or more years ago is very likely old (sold, expired, or stale).
_CURRENT_YEAR = 2026

# A year tied to FINANCIAL/reporting language (revenue, cash flow, gross, etc).
# This is the age signal. We deliberately do NOT treat "established in 2018" or
# "serving since 2005" as staleness - those are founding dates, not listing age.
_FIN_YEAR = re.compile(
    r'(20[12]\d)\s*(?:gross|revenue|revenues|sales|cash\s*flow|billings|sde|net|profit|income|trailing|projected|expected|estimated)'
    r'|(?:gross|revenue|revenues|sales|cash\s*flow|billings|sde|net|profit|income|trailing|projected|expected|estimated|for|in|through|fy)\s*'
    r'(?:of\s*|around\s*|about\s*|approximately\s*)?(?:\$?[\d,]+\s*(?:in|for)?\s*)?(20[12]\d)',
    re.I)
_FOUNDING = re.compile(
    r'(?:established|since|founded|inception|operating since|in business since|serving since|est\.?)\s*(?:in\s*)?(20[12]\d)',
    re.I)


def stale_data_note(text: str) -> Optional[str]:
    """
    If the description cites financial data from two or more years ago, return a
    plain-language caution. Returns None when the newest financial year is recent
    or absent. Founding years are excluded so 'established 2018' never trips it.
    """
    if not text:
        return None
    founding = set(re.findall(_FOUNDING, text))
    years = []
    for m in _FIN_YEAR.finditer(text):
        y = m.group(1) or m.group(2)
        if y and y not in founding and 2015 <= int(y) <= _CURRENT_YEAR:
            years.append(int(y))
    if not years:
        return None
    newest = max(years)
    if newest <= _CURRENT_YEAR - 2:
        return (f"This listing references financial data from {newest} or earlier "
                f"and may be outdated. Verify it is still available.")
    return None


# First-person seller language -> likely the owner is listing directly.
_OWNER_LANG = re.compile(
    r"\b(i am selling|i'm selling|i am retiring|i'm retiring|my practice|my firm|"
    r"my clients|my book of business|owner is selling|selling my|i have (?:built|owned|run)|"
    r"i built|i started|reach out to me|contact me directly|i will assist|i am the owner|"
    r"by owner|for sale by owner|fsbo|direct from (?:the )?owner)\b", re.I)
# Third-party representation -> a broker/advisor, which is our default assumption.
_BROKER_LANG = re.compile(
    r'\b(broker|brokerage|listing agent|we are pleased to (?:present|offer)|'
    r'our (?:client|firm) is|represented by|confidential listing|advisor is pleased|'
    r'is pleased to (?:present|announce|offer)|contact (?:the )?(?:broker|advisor|agent)|'
    r'business advisors?|transworld|sunbelt|murphy business)\b', re.I)


def seller_flag(text: str) -> str:
    """
    Decide the seller note. Default is broker-assumed ('possibly direct to seller');
    only clear first-person owner language (with no competing broker language) upgrades
    it to a stronger direct-owner note. Broker language keeps the default.
    """
    default = "No broker found - possibly direct to seller"
    if not text:
        return default
    owner = bool(_OWNER_LANG.search(text))
    broker = bool(_BROKER_LANG.search(text))
    if owner and not broker:
        return "Language suggests direct owner, not broker represented"
    return default


def scrape_bizbuysell(deep: bool = False) -> List[Dict]:
    """
    Index pulls title, description, url, and (where the card renders it) location.
    Routed through ScraperAPI ultra premium. deep=True re-reads detail pages to
    fill asking price, cash flow, gross revenue, and status; capped and paced to
    keep credit spend sane.
    """
    out, seen = [], set()
    import time as _time
    for pg in range(1, 14):
        idx = "https://www.bizbuysell.com/accounting-businesses-and-tax-practices-for-sale/" + (f"{pg}/" if pg > 1 else "")
        page = fetch_via_api(idx, ultra=True)
        if not page:
            break
        about = _bbs_about(page)
        if not about:
            break
        locs = re.findall(r'<p class="location[^"]*">([^<]+)</p>', page)
        new = 0
        for i, entry in enumerate(about):
            p = entry.get("item", {})
            url = p.get("url", "")
            if not url or url in seen:
                continue
            # Skip non-listing pages: broker profiles, franchises, asset/real-estate
            # sales, and start-ups. These are not accounting practices for sale and
            # were polluting the feed.
            low_url = url.lower()
            if any(j in low_url for j in (
                    "/business-broker/", "/franchise-for-sale/", "/business-asset/",
                    "/business-real-estate", "/start-up-business/")):
                seen.add(url)
                continue
            seen.add(url)
            new += 1
            name = html.unescape(p.get("name") or "").strip()
            desc = html.unescape(p.get("description") or "").strip()
            loc = (locs[i].strip() if i < len(locs) else "")
            blob = name + " | " + desc
            rev = _ds_money(name) or _ds_money(desc)
            low = blob.lower()
            status = ("sold" if re.search(r"\bsold\b", low)
                      else "pending" if re.search(r"under contract|sale pending", low)
                      else "active")
            note = seller_flag(blob)
            stale = stale_data_note(desc)
            if stale:
                note = note + " | " + stale
            out.append(_base(
                "bizbuysell", "BizBuySell", url,
                firm_type=clean_title(name),
                city=loc.split(",")[0].strip() if loc else None,
                state=_bbs_state(loc) or _bbs_state(desc),
                revenue=rev,
                description=desc[:1500] or None,
                services=services_from(blob),
                status=status,
                seller_note=note,
                listing_code="BBS-" + url.rstrip("/").split("/")[-1],
            ))
        if new == 0:
            break
        _time.sleep(1)

    if deep:
        filled = 0
        for item in out:
            if filled >= 250:
                break
            if (item["revenue"] is not None and item["state"] is not None
                    and item.get("asking_price") is not None):
                continue
            detail = fetch_via_api(item["source_url"], ultra=True)
            _time.sleep(1)
            if not detail:
                continue
            filled += 1
            dtext = strip_tags(detail)
            if item["revenue"] is None:
                m = re.search(r"Gross (?:Revenue|Income)\D{0,8}\$?([\d,]{4,})", dtext, re.I)
                if m:
                    item["revenue"] = int(m.group(1).replace(",", ""))
            if item.get("asking_price") is None:
                m = re.search(r"Asking Price\D{0,8}\$?([\d,]{4,})", dtext, re.I)
                if m:
                    item["asking_price"] = int(m.group(1).replace(",", ""))
            if item["state"] is None:
                m = re.search(r",\s*([A-Z]{2})\b", dtext)
                if m and m.group(1) in ABBR:
                    item["state"] = m.group(1)

            # "Listed By" is BizBuySell's own broker/agent field, the
            # authoritative signal for who is selling: a broker hyperlink whose
            # URL carries the brokerage slug, or an owner sale. Far more reliable
            # than guessing from the description.
            mb = re.search(
                r'ContactBrokerNameHyperLink"[^>]*href="(/business-broker/[^"]+)"[^>]*>([^<]+)</a>',
                detail)
            if mb:
                href, agent = mb.group(1), html.unescape(mb.group(2)).strip()
                item["agent_name"] = agent[:120]
                parts = [p for p in href.split("/") if p]
                if len(parts) >= 3:
                    item["listing_brokerage"] = parts[2].replace("-", " ").title()[:120]
            elif re.search(r"for sale by owner|listed by owner|sale by the owner", dtext, re.I):
                item["seller_note"] = "Listed by owner (direct seller)"
    return out


ALL_SOURCES.update({
    "bizbuysell": scrape_bizbuysell,
})


# ----------------------------------------------------------------------
# BizQuest  (bizquest.com)  BizBuySell's sibling site, same parent + same
# ld+json "about" structure. Works on the standard proxy pool (1 credit),
# so cheaper than BizBuySell. deep=True re-reads detail pages for asking
# price, gross revenue, and state, same as BizBuySell.
# ----------------------------------------------------------------------

def scrape_bizquest(deep: bool = False) -> List[Dict]:
    out, seen = [], set()
    import time as _time
    for pg in range(1, 12):
        idx = "https://www.bizquest.com/cpa-firms-for-sale/" + (f"page-{pg}/" if pg > 1 else "")
        page = fetch_via_api(idx)
        if not page:
            break
        about = _bbs_about(page)
        if not about:
            break
        new = 0
        for entry in about:
            p = entry.get("item", {})
            url = p.get("url", "")
            if not url or url in seen:
                continue
            low_url = url.lower()
            if any(j in low_url for j in (
                    "/business-broker/", "/franchise-for-sale/", "/business-asset/",
                    "/business-real-estate", "/start-up-business/")):
                seen.add(url)
                continue
            seen.add(url)
            new += 1
            name = html.unescape(p.get("name") or "").strip()
            desc = html.unescape(p.get("description") or "").strip()
            blob = name + " | " + desc
            rev = _ds_money(name) or _ds_money(desc)
            low = blob.lower()
            status = ("sold" if re.search(r"\bsold\b", low)
                      else "pending" if re.search(r"under contract|sale pending|\[sale pending\]", low)
                      else "active")
            note = seller_flag(blob)
            stale = stale_data_note(desc)
            if stale:
                note = note + " | " + stale
            out.append(_base(
                "bizquest", "BizQuest", url,
                firm_type=clean_title(name),
                state=_bbs_state(name) or _bbs_state(desc),
                revenue=rev,
                description=desc[:1500] or None,
                services=services_from(blob),
                status=status,
                seller_note=note,
                listing_code="BQ-" + url.rstrip("/").split("/")[-1],
            ))
        if new == 0:
            break
        _time.sleep(1)

    if deep:
        filled = 0
        for item in out:
            if filled >= 250:
                break
            if (item["revenue"] is not None and item["state"] is not None
                    and item.get("asking_price") is not None):
                continue
            detail = fetch_via_api(item["source_url"])
            _time.sleep(1)
            if not detail:
                continue
            filled += 1
            dtext = strip_tags(detail)
            if item["revenue"] is None:
                m = re.search(r"Gross (?:Revenue|Income)\D{0,8}\$?([\d,]{4,})", dtext, re.I)
                if m:
                    item["revenue"] = int(m.group(1).replace(",", ""))
            if item.get("asking_price") is None:
                m = re.search(r"Asking Price\D{0,8}\$?([\d,]{4,})", dtext, re.I)
                if m:
                    item["asking_price"] = int(m.group(1).replace(",", ""))
            if item["state"] is None:
                m = re.search(r",\s*([A-Z]{2})\b", dtext)
                if m and m.group(1) in ABBR:
                    item["state"] = m.group(1)

    log.info("bizquest: %s listings (deep=%s)", len(out), deep)
    return out


ALL_SOURCES.update({
    "bizquest": scrape_bizquest,
})


# ----------------------------------------------------------------------
# AccountingFirmSold  (accountingfirmsold.com)  national broker, 35+ yrs.
# Single listings page with a clean HTML table: Listing Number, Location,
# Annual Gross, Asking Price, Description, Status. All data is in the index
# so no deep pass is needed. Works on the standard proxy pool.
# ----------------------------------------------------------------------

def scrape_afs() -> List[Dict]:
    out = []
    page = fetch_via_api("https://accountingfirmsold.com/listings/")
    if not page:
        log.info("afs: no page")
        return out
    rows = re.findall(r'<tr>(.*?)</tr>', page, re.S)
    for r in rows:
        cells = dict(re.findall(r'data-th="([^"]+)">\s*(.*?)\s*</td>', r, re.S))
        if not cells:
            continue
        cells = {k: re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', v))).strip()
                 for k, v in cells.items()}
        code = cells.get("Listing Number")
        if not code:
            continue
        loc = cells.get("Location", "")
        desc = cells.get("Description", "")
        gross = cells.get("Annual Gross", "")
        asking = cells.get("Asking Price", "")
        raw_status = (cells.get("Status", "") or "").lower()
        status = ("sold" if "sold" in raw_status
                  else "pending" if ("pending" in raw_status or "under contract" in raw_status)
                  else "active")
        rev = None
        m = re.search(r'\$?([\d,]{4,})', gross)
        if m:
            rev = int(m.group(1).replace(",", ""))
        ask = None
        m = re.search(r'\$\s*([\d,]{4,})', asking)
        if m:
            ask = int(m.group(1).replace(",", ""))
        blob = loc + " | " + desc
        out.append(_base(
            "afs", "Accounting Firm Sold",
            "https://accountingfirmsold.com/listings/",
            firm_type=clean_title(desc.split(".")[0][:80]) if desc else "Accounting Practice",
            city=loc.split(",")[0].strip() if "," in loc else (loc if loc and loc != "United States" else None),
            state=_bbs_state(loc) or state_from(loc) or state_deep(desc),
            revenue=rev,
            asking_price=ask,
            description=desc[:1500] or None,
            services=services_from(blob),
            status=status,
            seller_note=seller_flag(blob),
            listing_code="AFS-" + code,
        ))
    log.info("afs: %s listings", len(out))
    return out


ALL_SOURCES.update({
    "afs": scrape_afs,
})


# ----------------------------------------------------------------------
# Business Brokerage Inc  (go2bbi.com)  California-focused accounting/tax
# practice broker, 40+ years. Current listings render on the homepage in a
# text format: "PRACTICE#[code] [LOCATION] $[gross] GROSS. [details]".
# All CA. Works on the standard proxy pool.
# ----------------------------------------------------------------------

def scrape_bbi() -> List[Dict]:
    out = []
    page = fetch_via_api("https://go2bbi.com/")
    if not page:
        log.info("bbi: no page")
        return out
    txt = re.sub(r'\s+', ' ', html.unescape(strip_tags(page)))
    parts = re.split(r'(PRACTICE\s*#\s*\d+)', txt)
    seen = set()
    for i in range(1, len(parts), 2):
        mcode = re.search(r'(\d+)', parts[i])
        if not mcode:
            continue
        code = mcode.group(1)
        if code in seen:
            continue
        body = parts[i + 1] if i + 1 < len(parts) else ""
        m = re.search(r'^\s*([A-Z][A-Za-z .\-]+?)\s+\$([\d,]{4,})\s+GROSS', body)
        if not m:
            continue
        seen.add(code)
        loc = m.group(1).strip()
        gross = int(m.group(2).replace(",", ""))
        desc = re.sub(r'\s+', ' ', body[:600]).strip()
        low = desc.lower()
        status = ("sold" if re.search(r"\bsold\b", low)
                  else "pending" if re.search(r"under contract|sale pending|in escrow", low)
                  else "active")
        out.append(_base(
            "bbi", "Business Brokerage Inc",
            "https://go2bbi.com/",
            firm_type=clean_title((loc.title() + " Accounting Practice")),
            city=loc.title(),
            state="CA",
            revenue=gross,
            description=desc[:1500] or None,
            services=services_from(desc),
            status=status,
            seller_note=seller_flag(desc),
            listing_code="BBI-" + code,
        ))
    log.info("bbi: %s listings", len(out))
    return out


ALL_SOURCES.update({
    "bbi": scrape_bbi,
})





# ----------------------------------------------------------------------
# Accounting Practice Exchange  (accountingpracticeexchange.com)  aggregator
# carrying broker and private-seller (FSBO) deals. Each state has its own
# server rendered page at /Sales/<STATE-NAME> listing that state's ENTIRE
# history, with a status tag per row: new / available / sale pending /
# expired / sold. We keep only the live ones (new, available, sale pending)
# and drop expired/sold. Needs render=True. Priced lower than other
# marketplaces in dedupe, so origin brokers (ABA, Poe, ProHorizons) win and
# only APE's exclusive private-seller rows survive.
# ----------------------------------------------------------------------

_APE_STATES = [
    "ALABAMA", "ALASKA", "ARIZONA", "ARKANSAS", "CALIFORNIA", "COLORADO",
    "CONNECTICUT", "DELAWARE", "DISTRICT-OF-COLUMBIA", "FLORIDA", "GEORGIA",
    "HAWAII", "IDAHO", "ILLINOIS", "INDIANA", "IOWA", "KANSAS", "KENTUCKY",
    "LOUISIANA", "MAINE", "MARYLAND", "MASSACHUSETTS", "MICHIGAN", "MINNESOTA",
    "MISSISSIPPI", "MISSOURI", "MONTANA", "NEBRASKA", "NEVADA", "NEW-HAMPSHIRE",
    "NEW-JERSEY", "NEW-MEXICO", "NEW-YORK", "NORTH-CAROLINA", "NORTH-DAKOTA",
    "OHIO", "OKLAHOMA", "OREGON", "PENNSYLVANIA", "RHODE-ISLAND",
    "SOUTH-CAROLINA", "SOUTH-DAKOTA", "TENNESSEE", "TEXAS", "UTAH", "VERMONT",
    "VIRGINIA", "WASHINGTON", "WEST-VIRGINIA", "WISCONSIN", "WYOMING", "VIRTUAL",
]

# State display name -> 2 letter abbreviation for the state field.
_APE_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
    "DISTRICT-OF-COLUMBIA": "DC", "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW-HAMPSHIRE": "NH", "NEW-JERSEY": "NJ", "NEW-MEXICO": "NM", "NEW-YORK": "NY",
    "NORTH-CAROLINA": "NC", "NORTH-DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE-ISLAND": "RI",
    "SOUTH-CAROLINA": "SC", "SOUTH-DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WEST-VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY", "VIRTUAL": None,
}

_APE_LIVE = {"new", "available", "sale pending", "under offer"}


def scrape_ape() -> List[Dict]:
    import time as _time
    out, seen = [], set()
    for st in _APE_STATES:
        url = f"https://www.accountingpracticeexchange.com/Sales/{st}"
        page = fetch_via_api(url, render=True)
        if not page:
            continue
        rows = re.findall(r'<tr class="ant-table-row[^"]*"[^>]*>(.*?)</tr>', page, re.S)

        def clean(x):
            return re.sub(r'\s+', ' ', html.unescape(re.sub(r'<[^>]+>', '', x))).strip()

        kept = 0
        for r in rows:
            cells = re.findall(r'<td class="ant-table-cell">(.*?)</td>', r, re.S)
            if len(cells) < 5:
                continue
            ref = clean(cells[0])
            if not ref.isdigit() or ref in seen:
                continue
            status_txt = clean(cells[4]).lower()
            if status_txt not in _APE_LIVE:
                continue                      # drop expired / sold
            seen.add(ref)
            kept += 1
            row_state = clean(cells[1])
            loc = clean(cells[2])
            gross_txt = clean(cells[3])
            seller = clean(cells[5]) if len(cells) > 5 else ""
            href = re.search(r'href="([^"]+)"', cells[0])
            href = href.group(1) if href else ""

            rev = None
            m = re.search(r'([\d,]{4,})', gross_txt)
            if m:
                rev = int(m.group(1).replace(",", ""))

            status = "pending" if "pending" in status_txt or "offer" in status_txt else "active"
            state_abbr = (row_state.upper() if row_state and len(row_state) == 2
                          else _APE_ABBR.get(st))
            is_private = "private" in seller.lower()
            note = "Private seller (FSBO)" if is_private else (("Listed via " + seller) if seller else "")
            full_url = ("https://www.accountingpracticeexchange.com" + href) if href.startswith("/") \
                else (href or f"https://www.accountingpracticeexchange.com/Sales/{st}/{ref}")

            out.append(_base(
                "ape", "Accounting Practice Exchange", full_url,
                firm_type=clean_title((loc.title() + " Accounting Practice") if loc else "Accounting Practice"),
                city=loc.title() if loc and loc.lower() != "virtual" else None,
                state=state_abbr,
                revenue=rev,
                description=(f"{loc}. {note}".strip(". ") or None),
                services=services_from(loc + " " + seller),
                status=status,
                seller_note=note,
                listing_code="APE-" + ref,
            ))
        log.info("ape %s: %s live listings", st, kept)
        _time.sleep(1)
    log.info("ape: %s total live listings", len(out))
    return out


ALL_SOURCES.update({
    "ape": scrape_ape,
})


# ----------------------------------------------------------------------
# BizBuySell keyword sweep. The main bizbuysell scraper reads the official
# "Accounting & Tax Practices" category, which is complete for correctly
# categorized listings. This sweep catches practices a broker mis-filed under
# a different category (Financial Services, Business Services, etc.) by running
# targeted keyword searches and keeping only rows whose text is clearly an
# accounting/tax/bookkeeping practice. Dedupe collapses anything already in the
# main category, so this only nets genuine strays. Ultra premium proxy.
# ----------------------------------------------------------------------

_BBS_KW_TERMS = [
    "CPA", "accounting", "tax practice", "bookkeeping",
    "CPA firm", "accounting practice", "tax preparation", "enrolled agent",
]
_BBS_ACCT_RE = re.compile(
    r'\b(CPA|accounting|tax practice|tax prep|bookkeep|enrolled agent|\bEA\b|accountant|payroll service|audit)\b',
    re.I)


def scrape_bizbuysell_keywords() -> List[Dict]:
    import time as _time
    out, seen = [], set()
    for term in _BBS_KW_TERMS:
        q = term.replace(" ", "+")
        for pg in range(1, 4):  # first few pages per term; strays are rare
            idx = f"https://www.bizbuysell.com/businesses-for-sale/?q={q}" + (f"&page={pg}" if pg > 1 else "")
            page = fetch_via_api(idx, ultra=True)
            if not page:
                break
            about = _bbs_about(page)
            if not about:
                break
            added = 0
            for entry in about:
                p = entry.get("item", {})
                url = p.get("url", "")
                if not url or url in seen:
                    continue
                low_url = url.lower()
                if "/business-opportunity/" not in low_url:
                    continue
                if any(j in low_url for j in (
                        "/business-broker/", "/franchise-for-sale/", "/business-asset/",
                        "/business-real-estate", "/start-up-business/")):
                    continue
                name = html.unescape(p.get("name") or "").strip()
                desc = html.unescape(p.get("description") or "").strip()
                if not _BBS_ACCT_RE.search(name + " " + desc):
                    continue                       # not an accounting practice, skip
                seen.add(url)
                added += 1
                blob = name + " | " + desc
                rev = _ds_money(name) or _ds_money(desc)
                low = blob.lower()
                status = ("sold" if re.search(r"\bsold\b", low)
                          else "pending" if re.search(r"under contract|sale pending", low)
                          else "active")
                out.append(_base(
                    "bizbuysell", "BizBuySell", url,
                    firm_type=clean_title(name),
                    state=_bbs_state(name) or _bbs_state(desc),
                    revenue=rev,
                    description=desc[:1500] or None,
                    services=services_from(blob),
                    status=status,
                    seller_note=seller_flag(blob),
                    listing_code="BBS-" + url.rstrip("/").split("/")[-1],
                ))
            if added == 0:
                break
            _time.sleep(1)
    log.info("bizbuysell_keywords: %s stray accounting listings", len(out))
    return out


ALL_SOURCES.update({
    "bizbuysell_keywords": scrape_bizbuysell_keywords,
})
