"""
One parser per broker. Each returns a list of plain dicts.
The engine handles everything else.

Adding a broker means adding one function here and one line in ALL_SOURCES.
"""

import re, html, logging
from typing import List, Dict, Optional
from engine import fetch, fingerprint

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

        item = _base(
            "businessesforsale", "BusinessesForSale", url,
            firm_type=clean_title(title),
            state=state_deep(title, text[:1500]),
            revenue=num("(?:gross )?revenue|turnover|sales"),
            asking_price=num("asking price"),
            cash_flow=num("cash flow|net profit"),
            description=best_description(text),
            services=services_from(title + " " + text[:600]),
            status=status,
        )
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
