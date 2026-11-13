"""Seattle-area outbreak monitor, tuned for a household with a young infant.

Polls public-health sources (CDC, WA DOH, Public Health - Seattle & King County,
Snohomish County Health, FDA recalls, Google News), keeps items that look like
disease outbreaks around Seattle, and emails anything new via SNS.

Infant-critical items (measles, whooping cough, RSV, infant formula recalls, ...)
are matched with a wider net (anywhere in Washington, local news outlets even
when the headline has no place name) and are listed first with their own subject.

Pages without feeds (King County + WA DOH measles pages) are watched for changes;
when they change, the email shows the newly added lines (e.g. exposure sites).

Standard library + boto3 only.

Local dry run (no AWS calls, prints what would be sent):
    python3 src/app.py --dry-run [--max-age-days 30]
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

log = logging.getLogger()
log.setLevel(logging.INFO)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
HTTP_TIMEOUT = 20
PACIFIC = timezone(timedelta(hours=-8))

# --------------------------------------------------------------------------
# Matching rules
# --------------------------------------------------------------------------

# Diseases / events that matter most for an infant too young for MMR (12 mo)
# and most other vaccines. These get the wider geographic net and top billing.
INFANT_CRITICAL = re.compile(
    r"\b(measles|whooping cough|pertussis|rsv|respiratory syncytial|meningitis|meningococcal|"
    r"haemophilus|hib|polio|diphtheria|mumps|chickenpox|varicella|rotavirus|"
    r"hand,? foot,? (?:and|&) mouth|cronobacter|botulism|listeri\w*|"
    r"infant formula|baby formula|baby food|daycare|child ?care|nicu)\b",
    re.I,
)

# Terms that on their own indicate an outbreak / notable communicable disease.
STRONG_DISEASE = re.compile(
    r"\b(outbreaks?|exposures?|exposed|measles|mumps|pertussis|whooping cough|"
    r"tuberculosis|norovirus|salmonell\w*|e\.? ?coli|listeri\w*|hepatitis a|"
    r"mpox|monkeypox|avian (?:flu|influenza)|bird flu|h5n1|h5n5|legionell\w*|legionnaires|"
    r"shigell\w*|campylobacter|cholera|meningococcal|meningitis|botulism|cyclospora|"
    r"vibrio|hantavirus|scombroid|food ?borne|food poisoning|gastrointestinal illness|"
    r"cluster of (?:cases|illness)|health advisory|health alert|polio|diphtheria|"
    r"varicella|chickenpox|candida auris|c\. ?auris|cronobacter|rotavirus|"
    r"hand,? foot,? (?:and|&) mouth)\b",
    re.I,
)
# Common seasonal illnesses: only interesting alongside a surge/outbreak word.
WEAK_DISEASE = re.compile(r"\b(flu|influenza|covid(?:-19)?|rsv|respiratory (?:illness|virus|season))\b", re.I)
SURGE = re.compile(
    r"\b(cases?|surg\w*|spik\w*|ris(?:e|ing)|increas\w*|uptick|outbreak|hospitali[sz]\w*|deaths?|"
    r"closed|closure|high levels?|early|severe|spread\w*)\b",
    re.I,
)

GEO_NAMES = re.compile(
    r"\b(seattle|king county|snohomish|pierce county|kitsap|puget sound|tacoma|everett|"
    r"bellevue|redmond|kirkland|renton|federal way|shoreline|burien|sea-?tac|tukwila|"
    r"issaquah|sammamish|bothell|lynnwood|edmonds|mercer island|woodinville|marysville|"
    r"mukilteo|mill creek|monroe|puyallup|bremerton|bainbridge|des moines,? wash\w*|"
    r"kent,? wash\w*|auburn,? wash\w*|south king|east ?side|university of washington|"
    r"washington state|western washington|wash(?:ington)? doh)\b",
    re.I,
)
GEO_ABBREV = re.compile(r"\b(WA|Wash\.)(?=\W|$)")  # case-sensitive
# For infant-critical diseases, anywhere in Washington counts (people travel; Clark,
# Yakima etc. outbreaks often seed Puget Sound cases).
GEO_STATEWIDE = re.compile(r"\bWashington\b(?!,? ?D\.? ?C\.?| (?:or \w+ )?count(?:y|ies)| Co\.)|\bYakima\b|\bSpokane\b|\bClark County\b|"
                           r"\bVancouver,? Wash|\bOlympia\b|\bThurston\b|\bSkagit\b|\bWhatcom\b|\bBellingham\b")
LOCAL_OUTLETS = re.compile(
    r"seattle|king ?5|komo|kiro|fox 13|mynorthwest|heraldnet|everett herald|the stranger|"
    r"publicola|news tribune|thenewstribune|kitsap|shoreline area news|patch|kuow|kcpq|"
    r"bellevue reporter|redmond reporter|kent reporter|my edmonds|lynnwood times|"
    r"south seattle emerald|westside seattle|capitol hill seattle",
    re.I,
)
BABY_PRODUCT = re.compile(r"\b(infant|baby|babies|formula|newborn)\b", re.I)
# A baby-product recall matters here only if it's about contamination / illness
# (not e.g. choking hazards or labeling).
CONTAMINATION = re.compile(
    r"\b(botulism|botulinum|cronobacter|salmonell\w*|listeri\w*|e\.? ?coli|contaminat\w*|lead|"
    r"heavy metals?|arsenic|bacteria\w*|illness\w*|sick|outbreak|toxin\w*|mold)\b", re.I)
# Headlines about other places (wire stories in local outlets).
ELSEWHERE = re.compile(
    r"\b(Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|"
    r"Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|"
    r"Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    r"New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
    r"South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|West Virginia|Wisconsin|Wyoming|"
    r"Canada|Mexico|British Columbia|Ontario|Europe|England|U\.S\.|US|nationwide|national\w*)\b")


def is_disease(text):
    if STRONG_DISEASE.search(text):
        return True
    return bool(WEAK_DISEASE.search(text) and SURGE.search(text))


def is_critical(text):
    return bool(INFANT_CRITICAL.search(text))


def is_local(text):
    return bool(GEO_NAMES.search(text) or GEO_ABBREV.search(text))


def is_relevant_place(text):
    """Seattle area always; anywhere in WA for infant-critical diseases."""
    return is_local(text) or (is_critical(text) and bool(GEO_STATEWIDE.search(text)))


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def http_get(url, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return resp.read()


TAG_RE = re.compile(r"<[^>]+>")


def clean(text):
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", html.unescape(text)))
    return re.sub(r"\s+", " ", text).strip()


def parse_date(value):
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def make_item(source, title, link, summary="", published=None, key=None, critical=None, raw_summary=False):
    title, summary = clean(title), (summary if raw_summary else clean(summary))
    return {
        "source": source,
        "title": title,
        "link": (link or "").strip(),
        "summary": (summary or "")[:1500],
        "published": published,
        "key": key or link or title,
        "critical": is_critical(f"{title} {summary}") if critical is None else critical,
        "commit": None,  # optional callback to persist state after a successful send
    }


def parse_feed(xml_bytes):
    """Yield (title, link, description, pubdate_str, guid) from RSS 2.0 or Atom."""
    root = ET.fromstring(xml_bytes)
    atom = "{http://www.w3.org/2005/Atom}"
    for it in root.iter("item"):
        yield (
            it.findtext("title", ""),
            it.findtext("link", ""),
            it.findtext("description", ""),
            it.findtext("pubDate", ""),
            it.findtext("guid", "") or it.findtext("link", ""),
        )
    for it in root.iter(f"{atom}entry"):
        link_el = it.find(f"{atom}link")
        link = link_el.get("href") if link_el is not None else ""
        yield (
            it.findtext(f"{atom}title", ""),
            link,
            it.findtext(f"{atom}summary", "") or it.findtext(f"{atom}content", ""),
            it.findtext(f"{atom}updated", "") or it.findtext(f"{atom}published", ""),
            it.findtext(f"{atom}id", "") or link,
        )


# --------------------------------------------------------------------------
# State store (DynamoDB in Lambda, in-memory for dry runs)
# --------------------------------------------------------------------------


class MemoryState:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value


class DynamoState:
    def __init__(self, table):
        self.table = table

    def get(self, key):
        item = self.table.get_item(Key={"id": f"state:{key}"}).get("Item")
        return item.get("value") if item else None

    def put(self, key, value):
        self.table.put_item(Item={"id": f"state:{key}", "value": value})


STATE = MemoryState()
PAGE_LINES = {}  # url -> text lines, reused by the dashboard measles panel

# --------------------------------------------------------------------------
# Sources. Each returns a list of items that already passed its filter.
# --------------------------------------------------------------------------


def rss_source(name, url, require_geo):
    """Generic RSS source. Local agencies only need a disease match; broader
    feeds must also mention a relevant place."""

    def fetch():
        out = []
        for title, link, desc, pub, guid in parse_feed(http_get(url)):
            text = f"{clean(title)} {clean(desc)}"
            if not is_disease(text):
                continue
            if require_geo and not is_relevant_place(text):
                continue
            out.append(make_item(name, title, link, desc, parse_date(pub), key=guid))
        return out

    fetch.__name__ = name
    return fetch


def fda_baby_recalls():
    """FDA food recalls involving infant/toddler products, nationwide."""
    url = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/food-safety-recalls/rss.xml"
    try:
        entries = list(parse_feed(http_get(url)))
    except Exception as e:  # noqa: BLE001 - fda.gov often blocks AWS IPs; openFDA doesn't
        log.info("FDA RSS unavailable (%s); using openFDA enforcement API", e)
        return openfda_baby_recalls()
    out = []
    for title, link, desc, pub, guid in entries:
        text = f"{clean(title)} {clean(desc)}"
        if BABY_PRODUCT.search(text) and CONTAMINATION.search(text):
            out.append(make_item("FDA recalls (infant/toddler products, nationwide)", title, link, desc,
                                 parse_date(pub), key=guid, critical=True))
    return out


def openfda_baby_recalls():
    """Fallback: openFDA food enforcement reports (lags the RSS feed by a few weeks)."""
    q = urllib.parse.quote('product_description:("infant formula" OR "baby food" OR infant OR toddler)')
    data = json.loads(http_get(f"https://api.fda.gov/food/enforcement.json?search={q}"
                               "&sort=report_date:desc&limit=50"))
    out = []
    for r in data.get("results", []):
        text = f"{r.get('product_description', '')} {r.get('reason_for_recall', '')}"
        if not (BABY_PRODUCT.search(text) and CONTAMINATION.search(text)):
            continue
        published = datetime.strptime(r["report_date"], "%Y%m%d").replace(hour=20, tzinfo=timezone.utc)
        title = f"{r.get('recalling_firm', 'Recall')}: {r.get('reason_for_recall', '')}"
        summary = f"{r.get('product_description', '')[:300]} (distribution: {r.get('distribution_pattern', 'n/a')[:150]})"
        out.append(make_item("FDA recalls (infant/toddler products, nationwide)", title,
                             "https://www.fda.gov/safety/recalls-market-withdrawals-safety-alerts", summary,
                             published, key=f"openfda:{r.get('recall_number')}", critical=True))
    return out


def cdc_us_outbreaks():
    """CDC's US outbreak feed lists multistate investigations. Keep only the ones
    whose full notice (via CDC's syndication API) mentions Washington, plus any
    involving infant products regardless of state."""
    feed = "https://tools.cdc.gov/api/v2/resources/media/285676.rss"
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days())
    out = []
    for title, link, desc, pub, guid in parse_feed(http_get(feed)):
        published = parse_date(pub)
        if published and published < cutoff:
            continue
        m = re.search(r"[?&]c=(\d+)", html.unescape(link))
        if not m:
            continue
        try:
            data = json.loads(http_get(
                f"https://tools.cdc.gov/api/v2/resources/media/{m.group(1)}/syndicate.json"))
            results = data["results"]
            result = results if isinstance(results, dict) else results[0]
        except Exception as e:  # noqa: BLE001
            log.warning("CDC syndication fetch failed for %s: %s", link, e)
            continue
        text = clean(result["content"])
        in_wa = re.search(r"\bWashington\b(?!,? ?D\.?C\.?)|\bSeattle\b|\bKing County\b", text)
        baby = BABY_PRODUCT.search(clean(title)) and CONTAMINATION.search(text)
        if not (in_wa or baby):
            continue
        cases = re.search(r"Cases\s*:?\s*([\d,]+(?: \(\d+ new\))?)", text)
        where = "Multistate investigation that includes Washington." if in_wa else \
            "Multistate investigation involving an infant/toddler product."
        summary = f"{where} Cases: {cases.group(1)}" if cases else where
        out.append(make_item("CDC outbreak investigations", title, result.get("sourceUrl") or link,
                             summary, published, key=f"cdc:{m.group(1)}:{pub}",
                             critical=bool(baby) or None))
    return out


def parse_kc_date(value):
    """King County uses both '09-24-26' and 'Sept. 25, 2024' styles."""
    value = re.sub(r"\bSept\b", "Sep", value.replace(".", "")).strip()
    for fmt in ("%m-%d-%y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).replace(hour=20, tzinfo=timezone.utc)  # ~noon PT
        except ValueError:
            pass
    return None


def kc_foodborne_outbreaks():
    """Public Health - Seattle & King County foodborne outbreak table."""
    base = "https://kingcounty.gov"
    url = f"{base}/en/dept/dph/health-safety/disease-illness/foodborne-illness-outbreaks"
    page = http_get(url, accept="text/html").decode("utf-8", "replace")
    out = []
    for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 4:
            continue
        posted, updated = clean(cells[0]), clean(cells[1])
        href = re.search(r'href="([^"]+)"', cells[2])
        business, organism = clean(cells[2]), clean(cells[3])
        published = parse_kc_date(updated) or parse_kc_date(posted)
        if published is None:
            continue  # unknown date: don't risk alerting on years-old rows
        link = urllib.parse.urljoin(base, href.group(1)) if href else url
        out.append(make_item(
            "King County foodborne outbreaks",
            f"{organism or 'Illness'} outbreak: {business}",
            link,
            f"Posted {posted}, last updated {updated}.",
            published,
            key=f"kcfood:{link}:{updated}",  # updates to an outbreak re-alert
        ))
    return out


def page_text_lines(page_html):
    """Main-content text of a page, one line per block element."""
    i, j = page_html.find("<main"), page_html.find("</main>")
    body = page_html[i:j] if i >= 0 else page_html
    body = re.sub(r"(?s)<(script|style|form|nav)[^>]*>.*?</\1>", " ", body)
    # Each table row becomes one "cell | cell | cell" line, whatever markup is inside the cells.
    body = re.sub(
        r"(?is)<tr[^>]*>(.*?)</tr>",
        lambda m: "\n" + " | ".join(clean(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", m.group(1)))
        + "\n",
        body)
    body = re.sub(r"(?i)</(p|li|h\d|div|table|ul|ol)>|<br\s*/?>", "\n", body)
    lines = []
    for raw in clean_keep_lines(body).split("\n"):
        line = raw.strip(" |")
        if len(line) > 1 and line not in lines:
            lines.append(line)
    return lines


def clean_keep_lines(text):
    text = html.unescape(TAG_RE.sub(" ", text))
    return "\n".join(re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in text.split("\n"))


def page_watch(name, url, title):
    """Alert when a page without a feed changes; report the newly added lines.
    The first run stores a baseline silently."""

    def fetch():
        lines = page_text_lines(http_get(url, accept="text/html").decode("utf-8", "replace"))
        PAGE_LINES[url] = lines
        digest = hashlib.sha256("\n".join(lines).encode()).hexdigest()
        prev = STATE.get(url)
        if prev is None:
            STATE.put(url, json.dumps({"digest": digest, "lines": lines}))
            log.info("%s: baseline stored (%d lines)", name, len(lines))
            return []
        prev = json.loads(prev)
        if prev["digest"] == digest:
            return []
        old = set(prev["lines"])
        added = [ln for ln in lines if ln not in old]
        if not added:  # only removals / reordering
            STATE.put(url, json.dumps({"digest": digest, "lines": lines}))
            return []
        item = make_item(name, title, url, "New on the page:\n" + "\n".join(f"  + {a}" for a in added[:25]),
                         datetime.now(timezone.utc), key=f"page:{url}:{digest}", critical=True, raw_summary=True)
        item["commit"] = lambda: STATE.put(url, json.dumps({"digest": digest, "lines": lines}))
        return [item]

    fetch.__name__ = name
    return fetch


GOOGLE_NEWS_QUERIES = [
    # (query, require place in headline?)
    ('outbreak (Seattle OR "King County" OR "Snohomish County" OR "Pierce County" OR Tacoma OR Bellevue OR Everett)',
     True),
    ('(measles OR "whooping cough" OR pertussis) (Seattle OR Washington OR "King County" OR Snohomish OR Pierce '
     'OR Kitsap OR Sea-Tac OR Bellevue OR Everett OR Tacoma)', True),
    ('(RSV OR "hand, foot and mouth" OR meningitis OR norovirus OR "E. coli" OR salmonella OR listeria '
     'OR "hepatitis A" OR "bird flu" OR tuberculosis) (Seattle OR "King County" OR "Puget Sound" OR Snohomish)',
     True),
    ('("infant formula" OR "baby formula" OR "baby food" OR "infant botulism" OR cronobacter) '
     '(recall OR outbreak OR illness)', False),
]


def google_news():
    out = []
    for q, needs_place in GOOGLE_NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"{q} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        for title, link, desc, pub, guid in parse_feed(http_get(url)):
            full = clean(title)
            # Google titles end in " - Outlet Name"; split it off so "The Seattle
            # Times" doesn't count as a Seattle-area location by itself.
            headline, _, outlet = full.rpartition(" - ")
            if not headline:
                headline, outlet = full, ""
            if not needs_place:
                ok = BABY_PRODUCT.search(headline) and CONTAMINATION.search(headline)
            else:
                ok = is_disease(headline) and (
                    is_relevant_place(headline)
                    # e.g. "Baby with measles possibly exposed others on public transit - FOX 13 Seattle"
                    or (is_critical(headline) and LOCAL_OUTLETS.search(outlet)
                        and not ELSEWHERE.search(headline)))
            if ok:
                out.append(make_item("News (Google News)", full, link, "", parse_date(pub), key=guid,
                                     critical=True if not needs_place else None))
    return out


KC_MEASLES_URL = "https://kingcounty.gov/measles"
DOH_MEASLES_URL = "https://doh.wa.gov/you-and-your-family/illness-and-disease-z/measles/measles-cases-washington-state"

SOURCES = [
    page_watch("King County measles page", KC_MEASLES_URL,
               "King County measles page updated (cases / exposure locations)"),
    page_watch("WA DOH measles page", DOH_MEASLES_URL,
               "Washington State measles case page updated"),
    cdc_us_outbreaks,
    kc_foodborne_outbreaks,
    fda_baby_recalls,
    rss_source("Public Health - Seattle & King County", "https://publichealthinsider.com/feed/", require_geo=False),
    rss_source("Snohomish County Health Dept", "https://snohd.org/RSSFeed.aspx?ModID=1&CID=All-newsflash.xml",
               require_geo=False),
    rss_source("Washington State DOH", "https://doh.wa.gov/rss.xml", require_geo=False),
    google_news,
]


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def max_age_days():
    return int(os.environ.get("MAX_AGE_DAYS", "7"))


def collect():
    """Run all sources in parallel. Returns (items, errors, status)."""
    items, errors, status = [], [], []
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = {pool.submit(src): src.__name__ for src in SOURCES}
        for fut, name in futures.items():
            try:
                got = fut.result()
                log.info("%s: %d matching items", name, len(got))
                items.extend(got)
                status.append({"name": name, "ok": True, "matches": len(got)})
            except Exception as e:  # noqa: BLE001 - one bad source shouldn't kill the run
                log.exception("Source %s failed", name)
                errors.append(f"{name}: {e}")
                status.append({"name": name, "ok": False, "error": str(e)[:200]})

    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days())
    fresh, seen_titles = [], set()
    for it in sorted(items, key=lambda i: i["published"] or datetime.now(timezone.utc), reverse=True):
        if it["published"] and it["published"] < cutoff:
            continue
        norm = re.sub(r"[^a-z0-9]", "", it["title"].lower().rpartition(" - ")[0] or it["title"].lower())[:80]
        if norm in seen_titles and not it["commit"]:
            continue
        seen_titles.add(norm)
        it["id"] = hashlib.sha256(f"{it['source']}|{it['key']}".encode()).hexdigest()
        fresh.append(it)
    return fresh, errors, status


MEASLES_ADVICE = (
    "MEASLES NOTE: babies under 12 months haven't had their routine MMR dose. If your baby was at a listed "
    "exposure site and time, call your pediatrician right away. Immune globulin (IG) given within 6 days of "
    "exposure can prevent or soften measles in infants. Call ahead; don't walk into a clinic or ER with a "
    "possibly exposed baby."
)


def format_message(items, errors):
    critical = [i for i in items if i["critical"]]
    other = [i for i in items if not i["critical"]]
    lines = []
    if os.environ.get("DASHBOARD_URL"):
        lines += [f"Dashboard: {os.environ['DASHBOARD_URL']}", ""]

    def emit(group):
        by_source = {}
        for it in group:
            by_source.setdefault(it["source"], []).append(it)
        for source, rows in by_source.items():
            lines.append(f"-- {source} --")
            for it in rows:
                date = it["published"].astimezone(PACIFIC).strftime("%b %d") if it["published"] else "n/d"
                lines.append(f"* [{date}] {it['title']}")
                if it["summary"] and it["summary"].lower() not in it["title"].lower():
                    lines.append(f"  {it['summary']}")
                lines.append(f"  {it['link']}")
            lines.append("")

    if critical:
        lines += ["#### RELEVANT FOR YOUR BABY ####", ""]
        emit(critical)
        if any(re.search(r"measles", f"{i['title']} {i['summary']}", re.I) for i in critical):
            lines += [MEASLES_ADVICE, ""]
    if other:
        lines += ["#### Other Seattle-area outbreaks ####", ""]
        emit(other)
    if errors:
        lines.append("Sources that failed this run: " + "; ".join(errors))
    return "\n".join(lines)


def subject_for(items):
    crit = [i for i in items if i["critical"]]
    text = " ".join(f"{i['title']} {i['summary']}" for i in crit)
    if re.search(r"measles", text, re.I):
        return f"MEASLES alert near Seattle ({len(items)} new item(s))"
    if crit:
        return f"Baby-relevant outbreak alert ({len(crit)} urgent, {len(items)} total)"
    return f"Seattle outbreak update: {len(items)} new item(s)"


def filter_unseen(table, items):
    unseen = []
    for i in range(0, len(items), 100):
        chunk = items[i:i + 100]
        # table.meta.client is the resource's client: it (de)serializes values itself.
        request = {table.name: {"Keys": [{"id": it["id"]} for it in chunk], "ProjectionExpression": "id"}}
        found = set()
        while request:
            resp = table.meta.client.batch_get_item(RequestItems=request)
            found |= {r["id"] for r in resp["Responses"].get(table.name, [])}
            request = resp.get("UnprocessedKeys") or None
        unseen.extend(it for it in chunk if it["id"] not in found)
    return unseen


def mark_seen(table, items):
    expires = int(time.time()) + 120 * 86400
    with table.batch_writer() as w:
        for it in items:
            w.put_item(Item={"id": it["id"], "title": it["title"][:500], "expires_at": expires})


def measles_snapshot():
    """Structured measles numbers / exposure sites from the watched pages."""
    out = {"kc_url": KC_MEASLES_URL, "doh_url": DOH_MEASLES_URL}
    doh = PAGE_LINES.get(DOH_MEASLES_URL) or []
    for ln in doh:
        m = re.search(r"Last Update:\s*(.+?\d{4}(?: at [\d:]+ ?[ap]\.?m\.?)?)", ln)
        if m:
            out["doh_updated"] = m.group(1)
            break
    counties, in_county, ages = [], False, []
    for ln in doh:
        if re.search(r"Cases by County", ln):
            in_county = True
            continue
        if in_county:
            m = re.match(r"^([A-Z][A-Za-z .]+?) \| (\d+) \| (\d+)%$", ln)
            if m and not m.group(1).startswith(("Total", "County")):
                counties.append({"county": m.group(1), "cases": int(m.group(2))})
            elif ln.startswith("Total"):
                in_county = False
        m = re.match(r"^Total (\d{4}) Confirmed Cases \| (\d+)", ln)
        if m:
            out["wa_year"], out["wa_total"] = m.group(1), int(m.group(2))
        m = re.match(r"^Hospitalized\*? \| (\d+)", ln)
        if m:
            out["wa_hospitalized"] = int(m.group(1))
        m = re.match(r"^Under 5 years \| (\d+)", ln)
        if m:
            out["wa_under5"] = int(m.group(1))
    out["wa_by_county"] = counties

    kc = PAGE_LINES.get(KC_MEASLES_URL) or []
    exposures = []
    for ln in kc:
        cells = [c.strip() for c in ln.split(" | ")]
        if len(cells) >= 4 and re.match(r"^\d{1,2}/\d{1,2}/\d{2,4}$", cells[0]):
            exposures.append({"date": cells[0], "time": cells[1], "location": cells[2], "watch_until": cells[3]})
    for ln in kc:
        if re.match(r"Measles cases in King County residents", ln):
            continue
        m = re.match(r"^Updated ([\d/]+)", ln)
        if m and "kc_updated" not in out:
            out["kc_updated"] = m.group(1)
        m = re.match(r"^(20\d\d): (\d+)$", ln)
        if m and "kc_cases_this_year" not in out:
            out["kc_year"], out["kc_cases_this_year"] = m.group(1), int(m.group(2))
    out["kc_exposures"] = exposures[:80]
    return out


NNDSS_URL = "https://data.cdc.gov/resource/x9gk-5huc.json"


def nndss_measles_by_state():
    """Measles cases by state from CDC's NNDSS weekly tables (data.cdc.gov).

    Each state has two rows (imported + indigenous); m1 = this week,
    m3 = year to date, m4 = same period last year. Provisional data.
    www.cdc.gov blocks automated clients, data.cdc.gov does not.
    """
    def soql(params):
        return json.loads(http_get(f"{NNDSS_URL}?{urllib.parse.urlencode(params)}"))

    measles = "label like 'Measles%'"
    latest = soql({"$select": "year,week", "$where": measles, "$group": "year,week",
                   "$order": "year DESC, week DESC", "$limit": 1})[0]
    rows = soql({"$where": f"{measles} AND year='{latest['year']}' AND week='{latest['week']}'",
                 "$limit": 500})

    def num(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    states, us = {}, {"ytd": 0, "week": 0, "prev_ytd": 0}
    for r in rows:
        name = r.get("location1")
        if r.get("states") == "U.S. Residents":
            target = us
        elif name:  # states, DC and territories; regions have no location1
            name = "New York" if name == "New York City" else name  # NNDSS splits NYC out
            target = states.setdefault(name, {"state": name, "ytd": 0, "week": 0, "prev_ytd": 0})
        else:
            continue
        target["ytd"] += num(r.get("m3"))
        target["week"] += num(r.get("m1"))
        target["prev_ytd"] += num(r.get("m4"))
    return {
        "year": int(latest["year"]), "week": int(latest["week"]), "us": us,
        "states": sorted(states.values(), key=lambda x: -x["ytd"]),
        "source_url": "https://data.cdc.gov/NNDSS/NNDSS-Weekly-Data/x9gk-5huc",
    }


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _best_effort(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional dashboard panel
        log.exception("%s failed", fn.__name__)
        return None


def publish_dashboard(s3, bucket, items, status):
    """Merge current items into a rolling 90-day history and write data.json."""
    now = datetime.now(timezone.utc)
    try:
        history = json.loads(s3.get_object(Bucket=bucket, Key="history.json")["Body"].read())
    except s3.exceptions.NoSuchKey:
        history = {}
    for it in items:
        rec = history.get(it["id"], {"first_seen": _iso(now)})
        rec.update({
            "source": it["source"], "title": it["title"], "link": it["link"],
            "summary": it["summary"], "published": _iso(it["published"]),
            "critical": bool(it["critical"]),
            "measles": bool(re.search(r"measles", f"{it['title']} {it['summary']}", re.I)),
        })
        history[it["id"]] = rec
    cutoff = (now - timedelta(days=90)).isoformat()
    history = {k: v for k, v in history.items() if (v.get("published") or v["first_seen"]) >= cutoff}
    s3.put_object(Bucket=bucket, Key="history.json", Body=json.dumps(history).encode(),
                  ContentType="application/json")
    data = {
        "generated_at": _iso(now),
        "sources": status,
        "measles": measles_snapshot(),
        "measles_us": _best_effort(nndss_measles_by_state),
        "items": sorted(history.values(), key=lambda v: v.get("published") or v["first_seen"], reverse=True),
    }
    s3.put_object(Bucket=bucket, Key="data.json", Body=json.dumps(data).encode(),
                  ContentType="application/json", CacheControl="max-age=60")


def handler(event, context):
    import boto3

    global STATE
    table = boto3.resource("dynamodb").Table(os.environ["SEEN_TABLE"])
    STATE = DynamoState(table)

    items, errors, status = collect()
    new = filter_unseen(table, items)
    log.info("%d candidate items, %d new, %d source errors", len(items), len(new), len(errors))

    if new:
        boto3.client("sns").publish(
            TopicArn=os.environ["TOPIC_ARN"],
            Subject=subject_for(new)[:100],
            Message=format_message(new, errors),
        )
        mark_seen(table, new)
        for it in new:
            if it["commit"]:
                it["commit"]()
    if os.environ.get("DASHBOARD_BUCKET"):
        try:
            publish_dashboard(boto3.client("s3"), os.environ["DASHBOARD_BUCKET"], items, status)
        except Exception:  # noqa: BLE001 - dashboard is best-effort; alerts already went out
            log.exception("Dashboard publish failed")
    return {"candidates": len(items), "new": len(new), "errors": errors}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--max-age-days" in sys.argv:
        os.environ["MAX_AGE_DAYS"] = sys.argv[sys.argv.index("--max-age-days") + 1]
    if "--dry-run" in sys.argv:
        found, errs, _status = collect()
        print(json.dumps(measles_snapshot(), indent=1)[:2500])
        print("SUBJECT:", subject_for(found))
        print(format_message(found, errs))
    else:
        print(handler({}, None))
