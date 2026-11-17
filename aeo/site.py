"""Read a builder's website: business profile (name, market, areas, phone, services, price) and AI-readiness checks.

Uses plain HTTP fetches of the homepage, robots.txt, llms.txt and sitemaps. Most builder sites are
server-rendered (WordPress etc.); for a JavaScript-only site, capture the DOM with REA's
capture_browser_scenario or Playwright and pass the HTML in instead.
"""
import collections
import html as H
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from .engines import COUNTRY_OF_STATE, STATE_CODES, STATE_NAMES, normalize_country, region_name

MCP_CALL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "mcp_call.py")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"
AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "ClaudeBot", "Google-Extended", "CCBot"]
SERVICE_WORDS = {
    "custom homes": r"custom home", "build on your lot": r"(build|building) on your (own )?(lot|land|block)",
    "luxury homes": r"luxury", "ranch homes": r"ranch", "new construction": r"new construction|new homes",
    "townhomes": r"townhome|townhouse", "condos": r"\bcondo", "floor plans": r"floor plan",
    "communities": r"communit(y|ies)", "remodeling": r"remodel|renovat",
    # change 60: Australian builder services
    "knockdown rebuild": r"knock[\s-]?down[\s-]?(?:and[\s-]|&[\s-]?)?re[\s-]?build|\bKDR\b", "house and land": r"house\s*(?:and|&)\s*land",
    "single storey homes": r"single[\s-]stor(?:e)?y", "duplex": r"\bduplex|dual\s+occupancy",
}


LOC_PATH = re.compile(r"/(?:communities|community|neighborhoods?|areas?|service-areas?|areas-we-serve|locations?|where-we-build|cities)/[a-z0-9-]+/?$", re.I)


def fetch(url, timeout=25):
    from . import netsafe
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xml,*/*"})
    try:
        with netsafe.urlopen(req, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(3_000_000).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), "", url
    except Exception as e:
        return 0, "", "", url


def visible_text(dom):
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<noscript.*?</noscript>", " ", dom, flags=re.S | re.I)
    return re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


def jsonld(dom):
    items = []
    for b in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', dom, re.S | re.I):
        try:
            j = json.loads(b.strip())
        except Exception:
            continue
        for node in (j if isinstance(j, list) else [j]):
            if isinstance(node, dict):
                items += node.get("@graph", [node]) if "@graph" in node else [node]
    return [i for i in items if isinstance(i, dict)]


def _types(item):
    t = item.get("@type", [])
    return t if isinstance(t, list) else [t]


def digits(phone):
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("61") and len(d) == 11:  # change 60: +61 2 9876 5432 -> 0298765432, the way Australians write it
        return "0" + d[2:]
    return d[-10:] if len(d) >= 10 else d


# North American numbers, and (change 60) Australian landlines, mobiles and 1300/1800 numbers
PHONE_RX = re.compile(r"\(?\b\d{3}\)?[-. ]?\d{3}[-. ]\d{4}\b"
                      r"|(?:\+61[\s.-]?(?:\(0\)\s?|0)?[2378][\s.-]?|\(0[2378]\)\s?|\b0[2378][\s.-]?)\d{4}[\s.-]?\d{4}\b"
                      r"|(?:\+61[\s.-]?4|\b04)\d{2}[\s.-]?\d{3}[\s.-]?\d{3}\b"
                      r"|\b1[38]00[\s.-]?\d{3}[\s.-]?\d{3}\b")


def phone_numbers(text):
    return sorted({digits(x) for x in PHONE_RX.findall(text or "")})


def format_phone(d):
    """(555) 123-4567 for North America; 02 9876 5432 / 0412 345 678 / 1300 123 456 for Australia."""
    if len(d) == 10 and d[0] in "01":
        return f"{d[:4]} {d[4:7]} {d[7:]}" if d[:2] in ("04", "13", "18") else f"{d[:2]} {d[2:6]} {d[6:]}"
    return f"({d[:3]}) {d[3:6]}-{d[6:]}" if len(d) == 10 else d


def country_hint(domain, addr=None, phones=()):
    """'AU' / 'CA' / 'US' when the site gives it away (address country, .com.au / .ca domain, Australian phone format)."""
    c = normalize_country((addr or {}).get("addressCountry") if isinstance((addr or {}).get("addressCountry"), str)
                          else ((addr or {}).get("addressCountry") or {}).get("name") if isinstance(addr, dict) else None)
    if c:
        return c
    d = (domain or "").lower()
    if d.endswith(".au"):
        return "AU"
    if d.endswith(".ca"):
        return "CA"
    if any(p[:2] in ("02", "03", "04", "07", "08", "13", "18") for p in phones or ()):
        return "AU"
    return None


def sitemap_urls(base, limit=25):
    """All page URLs from the first sitemap root that exists, following sitemap indexes (children fetched in parallel)."""
    from concurrent.futures import ThreadPoolExecutor
    for root in (base + "/sitemap_index.xml", base + "/sitemap.xml", base + "/wp-sitemap.xml"):
        code, _, body, _ = fetch(root)
        if code != 200 or "<loc>" not in body:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", body)
        if "<sitemapindex" not in body:
            return list(dict.fromkeys(locs))
        with ThreadPoolExecutor(max_workers=8) as pool:
            docs = list(pool.map(lambda u: fetch(u, timeout=15), locs[:limit]))
        urls = []
        for c, _, b, _ in docs:
            if c == 200:
                urls += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", b)
        return list(dict.fromkeys(urls))
    return []


def robots_blocks(robots_txt):
    """Return AI bots fully blocked by robots.txt (Disallow: / under their user-agent or *)."""
    blocked, groups, agents, rules = set(), [], [], []
    for line in robots_txt.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        k, v = [x.strip() for x in line.split(":", 1)]
        if k.lower() == "user-agent":
            if rules:
                groups.append((agents, rules)); agents, rules = [], []
            agents.append(v.lower())
        elif k.lower() in ("disallow", "allow"):
            rules.append((k.lower(), v))
    if agents:
        groups.append((agents, rules))
    for bot in AI_BOTS:
        specific = [r for a, r in groups if bot.lower() in a]
        rs = specific[0] if specific else next((r for a, r in groups if "*" in a), [])
        if ("disallow", "/") in rs and ("allow", "/") not in rs:
            blocked.add(bot)
    return sorted(blocked)


def rea_available():
    if os.environ.get("HBM_USE_REA", "1") == "0":
        return False
    try:
        return "rea" in json.load(open(os.path.expanduser("~/.claude.json"))).get("mcpServers", {})
    except Exception:
        return False


def rendered_dom(url, timeout=35):
    """Load the page in a real Chrome through REA (capture_browser_scenario) and return the rendered DOM, or None."""
    chrome = os.environ.get("HBM_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not os.path.exists(chrome):
        return None
    args = {"browser": {"mode": "launch", "executable_path": chrome, "headless": True},
            "start_url": {"url": url},
            "actions": [{"step_id": "open", "action": "goto", "destination": {"url": url}, "wait_until": "domcontentloaded", "timeout_ms": 20000},
                        {"step_id": "settle", "action": "wait_for_timeout", "duration_ms": 2500}],
            "capture": {"at_end": ["dom"]}, "command_timeout_ms": 30000}
    try:
        out = subprocess.run([sys.executable, "-I", MCP_CALL, "rea", "capture_browser_scenario", json.dumps(args)],
                             capture_output=True, text=True, timeout=timeout)
        d = json.loads(out.stdout)
        art = d["normalized_result"]["steps"][-1]["artifacts"]
        return art["dom"]["value"]["text"]
    except Exception:
        return None


_NAME_WORDS = ("custom", "homes", "home", "builders", "builder", "building", "construction", "contracting", "design", "designs",
               "development", "developments", "group", "remodeling", "renovations", "properties", "communities", "residential", "co")


def name_from_domain(domain):
    """A readable business name from a web address, for sites we can't read: tourmalinebuilders.com -> Tourmaline Builders."""
    label = re.sub(r"^www\.", "", (domain or "").lower()).split(".")[0]
    words = [w for w in re.split(r"[-_]+", label) if w]
    out = []
    for w in words:
        parts, rest = [], w
        while rest:
            hit = next((k for k in _NAME_WORDS if rest.endswith(k) and len(rest) > len(k)), None)
            if not hit:
                break
            parts.insert(0, hit)
            rest = rest[: -len(hit)]
        out += [rest] + parts if rest else parts
    return " ".join(x.capitalize() for x in out) or domain


GENERIC_TAIL = (r"(?:development|developments|homes|homebuilders|builders|builder|construction|group|inc|co|company|custom|design|"
                r"designbuild|build|contracting|contractors|renovations|remodeling|remodelers|ltd|llc|corp|studio|properties)+")


# change 60: CDN / firewall / bot-check pages ("ERROR: The request could not be satisfied", "Just a moment...") are
# not the business's site, so their title must never become the business name
ERROR_PAGE = re.compile(r"request could not be satisfied|access denied|attention required|just a moment|forbidden|"
                        r"not acceptable|are you a robot|verify you are human|security check|bad gateway|service unavailable|"
                        r"temporarily unavailable|error \d{3}|\b40[0-9]\b|\b50[0-9]\b", re.I)


# titles that only a block or challenge page has, whatever status code it comes with (CloudFront sends 202)
BLOCK_PAGE = re.compile(r"request could not be satisfied|access denied|attention required|just a moment|are you a robot|"
                        r"verify you are human|security check|request blocked", re.I)


def name_in_text(text, domain):
    """The business name as the page writes it, when its words spell out the domain: 'Infinity West Development Group'
    for infinitywestdevelopment.com. None when the page doesn't contain such a phrase."""
    label = re.sub(r"[^a-z0-9]", "", (domain or "").split(".")[0].lower())
    if len(label) < 6:
        return None
    best = None
    for m in re.finditer(r"(?:[A-Z][\w'’.-]*(?:\s+|&|\s*&\s*)){0,5}[A-Z][\w'’.-]*", text or ""):
        parts = re.split(r"\s+", m.group(0).strip())
        for i in range(len(parts)):
            acc = ""
            for j in range(i, len(parts)):
                acc += re.sub(r"[^a-z0-9]", "", parts[j].lower())
                rest = label[len(acc):] if label.startswith(acc) else None
                if acc == label or (len(acc) >= 8 and rest is not None and (len(rest) <= 4 or re.fullmatch(GENERIC_TAIL, rest))):
                    tail = parts[j + 1] if j + 1 < len(parts) else ""
                    extra = [tail] if re.fullmatch(r"(?:Group|Inc\.?|Ltd\.?|LLC|Homes|Builders|Construction|Company|Co\.?|Corp\.?)", tail) else []
                    cand = " ".join(parts[i:j + 1] + extra).strip(" .,")
                    if not best or len(cand) > len(best):
                        best = cand
                if len(acc) > len(label):
                    break
    return best


def read_site(url, html_override=None, use_browser=True, start=None):
    """start: the page to read first (a subfolder or exact URL scope); defaults to the homepage."""
    if not url.startswith("http"):
        url = "https://" + url
    p = urllib.parse.urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    domain = re.sub(r"^www\.", "", p.netloc.lower())
    # fetch everything at once: the REA browser capture is the slow part, so robots/llms/sitemap load alongside it
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=5)
    from . import netsafe
    try:
        netsafe.check(start or base + "/")
        safe = True
    except netsafe.BlockedURL:
        safe = False
    f_rendered = pool.submit(rendered_dom, start or base + "/") if (safe and use_browser and not html_override and rea_available()) else None
    f_robots = pool.submit(fetch, base + "/robots.txt")
    f_llms = pool.submit(fetch, base + "/llms.txt")
    f_sitemap = pool.submit(sitemap_urls, base)
    code, _, dom, final = (200, "", html_override, url) if html_override else fetch(start or base + "/")
    raw_phones = phone_numbers(visible_text(dom))
    rendered = f_rendered.result() if f_rendered else None
    raw_text = visible_text(dom)  # change 58: the page before scripts run can name the business more fully
    if rendered and len(rendered) > len(dom) * 0.3:
        dom = rendered  # what a visitor's browser shows, after scripts run
    text = visible_text(dom)
    ld = jsonld(dom)
    biz = next((i for i in ld if set(_types(i)) & {"HomeAndConstructionBusiness", "GeneralContractor", "LocalBusiness",
                                                    "HousePainter", "RealEstateAgent", "ProfessionalService"}), None)
    org = next((i for i in ld if "Organization" in _types(i)), None)
    title = (re.findall(r"<title[^>]*>(.*?)</title>", dom, re.S | re.I) or [""])[0]
    og_name = re.findall(r'<meta[^>]+property="og:site_name"[^>]+content="([^"]+)"', dom)
    name = (biz or {}).get("name") or (org or {}).get("name") or (og_name[0] if og_name else "") \
        or H.unescape(re.split(r"[|\-–—]", title)[-1]).strip()

    # change 58: a name in the markup that the page never shows is usually a template leftover ("Artego" on
    # infinitywestdevelopment.com); the name the page itself uses that spells out the domain wins
    seen = max([n for n in (name_in_text(text, domain), name_in_text(raw_text, domain)) if n], key=len, default=None)
    flat = re.sub(r"[^a-z0-9]", "", H.unescape(name or "").lower())
    label = re.sub(r"[^a-z0-9]", "", domain.split(".")[0])
    if seen and (not flat or (flat[:5] not in label and label[:5] not in flat)):  # the markup name has nothing to do with the domain
        name = seen
    addr = (biz or {}).get("address") or {}
    if isinstance(addr, list):
        addr = addr[0] if addr else {}
    areas = []
    for a in (biz or {}).get("areaServed") or []:
        areas.append(a.get("name") if isinstance(a, dict) else str(a))
    state_raw = (addr.get("addressRegion") or "").strip() if isinstance(addr, dict) else ""
    page_phones = phone_numbers(text)
    hint = country_hint(domain, addr if isinstance(addr, dict) else {}, page_phones)
    state = region_name(state_raw, hint) or state_raw

    schema_phones = sorted({digits(i.get("telephone")) for i in ld if i.get("telephone")})
    tracking = sorted({v for v in re.findall(r"(callrail|calltrackingmetrics|whatconverts|invoca|marchex|ringba|dialogtech)", dom, re.I)})
    prices = re.findall(r"\$\s?\d[\d,.]*\s?(?:k|K|M|million|s)?", text)
    services = [s for s, rx in SERVICE_WORDS.items() if re.search(rx, text, re.I)]
    faq_q = [q for q in re.findall(r"<(?:h[2-4]|summary|button|dt)[^>]*>(.*?)</(?:h[2-4]|summary|button|dt)>", dom, re.S | re.I)
             if visible_text(q).endswith("?")]
    # change 53: headings and menu labels say what the business sells (aeo.business reads them)
    headings = list(dict.fromkeys(h for h in (visible_text(x)[:90] for x in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", dom, re.S | re.I)) if h))[:40]
    nav_links = list(dict.fromkeys(t for t in (visible_text(x) for x in re.findall(r"<a[^>]*>(.*?)</a>", dom, re.S | re.I))
                                   if 2 < len(t) <= 40 and not re.search(r"@|\d{3}.\d{4}", t)))[:60]

    # links and phrases that name places the builder serves (used by aeo.locations)
    loc_links = []
    for href, inner in re.findall(r'<a[^>]+href="([^"#]+)"[^>]*>(.*?)</a>', dom, re.S | re.I):
        path = urllib.parse.urlparse(href).path
        if LOC_PATH.search(path):
            loc_links.append([path, visible_text(inner)[:60]])
    area_phrases = [m.group(0)[:240] for m in re.finditer(
        r"(?:proudly\s+)?(?:serving|serve|building in|build in|homes in|communities in|service areas?:?|areas we serve:?|where we build:?)"
        r"\s+(?:the\s+)?(?:[A-Z][\w.'-]+(?:\s+[A-Z][\w.'-]+)*(?:,\s*[A-Z]{2})?(?:,\s*|\s+and\s+|\s*&\s*|\s+or\s+)?){2,}", text)][:6]

    robots_code, _, robots, _ = f_robots.result()
    llms_code, llms_type, llms, _ = f_llms.result()
    has_llms = llms_code == 200 and "html" not in llms_type.lower() and not llms.lstrip().lower().startswith("<!doctype")
    try:  # change 29: a huge or slow sitemap can't hold the audit up; whatever loaded is enough for the checks
        urls = f_sitemap.result(timeout=12)
    except Exception:
        urls = []
    pool.shutdown(wait=False)

    profile = {
        "name": H.unescape(name).strip(), "domain": domain, "url": start or base + "/", "homepage_status": code,
        "title": H.unescape(title).strip(), "address": addr if isinstance(addr, dict) else {},
        "error_page": bool(BLOCK_PAGE.search(H.unescape(title or "")) or ((code or 0) >= 400 and ERROR_PAGE.search(H.unescape(title or "") + " " + text[:300]))),
        "city": (addr.get("addressLocality") if isinstance(addr, dict) else "") or "", "state": state,
        "areas_served": areas, "services": services, "prices_found": prices[:6],
        "page_phones": page_phones, "schema_phones": schema_phones, "call_tracking": tracking,
        "raw_phones": raw_phones, "rendered_with_browser": bool(rendered), "country_hint": hint,
        "schema_types": sorted({t for i in ld for t in _types(i)}), "faq_questions": len(faq_q),
        "robots_status": robots_code, "ai_bots_blocked": robots_blocks(robots) if robots_code == 200 else [],
        "has_llms_txt": has_llms, "sitemap_count": len(urls), "sitemap_urls": urls[:400],
        "word_count": len(text.split()), "same_as": (biz or org or {}).get("sameAs") or [],
        "meta_description": H.unescape((re.findall(r'<meta[^>]+name="description"[^>]+content="([^"]*)"', dom) or [""])[0]),
        "opening_text": text[:2000],
        "location_links": loc_links[:80], "area_phrases": area_phrases, "headings": headings, "nav_links": nav_links,
    }
    return profile


def lookup_market(name, domain):
    """Change 30: when the website doesn't say (or won't let us read it), ask an AI platform with live web search
    where the builder is based. Returns (city, state, how) or (None, None, None). Tries Gemini (Google Search), then
    ChatGPT (web search)."""
    import json as _json
    from . import budget, config, engines
    prompt = (f"Where is the business \"{name}\" (website {domain}) based? Use their Google Business Profile, website or "
              "business listings. Reply with JSON only, no other words: {\"city\": \"...\", \"state\": \"state, province or territory code\", "
              "\"country\": \"US, CA or AU\"}. If you can't tell with confidence, reply {\"city\": null, \"state\": null, \"country\": null}.")
    tries = []
    if config.key("GEMINI_API_KEY"):
        tries.append(("gemini", "a Google search", lambda: engines.parse_gemini(engines._post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            {"model": config.SETTINGS["GEMINI_MODEL"], "input": prompt, "tools": [{"type": "google_search"}]},
            {"x-goog-api-key": config.key("GEMINI_API_KEY"), "Api-Revision": config.SETTINGS["GEMINI_API_REVISION"]}))[0]))
    if config.key("OPENAI_API_KEY"):
        tries.append(("chatgpt", "a web search", lambda: engines.parse_openai(engines._post(
            "https://api.openai.com/v1/responses",
            {"model": config.SETTINGS["OPENAI_MODEL"], "input": prompt, "tools": [{"type": "web_search"}]},
            {"Authorization": "Bearer " + config.key("OPENAI_API_KEY")}))[0]))
    for engine, how, call in tries:
        reserved = budget.reserve(engine)
        if reserved is None:
            continue
        try:
            text = call()
            budget.settle(reserved, None)
        except Exception:
            budget.settle(reserved, 0.0)
            continue
        m = re.search(r"\{.*?\}", text or "", re.S)
        try:
            d = _json.loads(m.group(0)) if m else {}
        except ValueError:
            d = {}
        city = (d.get("city") or "").strip()
        st = region_name(d.get("state"), normalize_country(d.get("country")))  # the full name: unambiguous (WA, NT)
        if city and st:
            return city, st, how
    return None, None, None


def market_from(profile, city=None, state=None):
    """Primary market for questions: CLI override, else first areaServed ('Greater X' -> X), else address city.
    change 60: the site's country (address, .com.au / .ca, phone format) decides shared codes like WA and NT."""
    hint = profile.get("country_hint")
    c = city
    if not c and not state:
        # "Myers Park, Charlotte, NC" / "Parramatta, NSW" style entries: the most common "City, ST" pair is the market
        pairs = collections.Counter()
        for a in profile["areas_served"]:
            m = re.search(r"([A-Z][A-Za-z .'-]+),\s*([A-Z]{2,3})\s*$", a or "")
            if m and region_name(m.group(2), hint):
                pairs[(m.group(1).strip(), region_name(m.group(2), hint))] += 1
        if pairs:
            (pc, ps), _ = pairs.most_common(1)[0]
            return _validated(pc, ps, "website markup", hint)
    if not c:
        for a in profile["areas_served"]:
            m = re.match(r"(?:Greater\s+)?([A-Z][A-Za-z .'-]+?)(?:\s+(?:Area|Metro|Region))?$", a or "")
            if m and not any(st in a for st in STATE_CODES):
                c = m.group(1).strip(); break
    c = c or profile["city"]
    s = state or profile["state"]
    source = "website markup" if (c and s and not city) else ("entered" if city else None)
    v = _validated(c, s, source, hint)
    if v["city"] and v["state"]:
        return v
    guess = infer_market_llm(profile)  # markup missing or invalid (e.g. "san diego, UN"): read the page instead
    if guess:
        g = _validated(guess["city"], guess["state"], "inferred from the website", guess.get("country") or hint)
        if g["city"] and g["state"]:
            if guess["areas"] and not profile["areas_served"]:
                profile["areas_served"] = [g["city"]] + guess["areas"]
            return g
    return v


def _validated(city, state, source, country=None):
    """Only accept a real US state, Canadian province (change 58) or Australian state/territory (change 60); tidy the
    city's capitalisation. Anything else means 'ask the salesperson'. country settles shared codes (WA, NT)."""
    st = region_name(state, country)
    c = (city or "").strip(" ,")
    if c and (c.islower() or c.isupper()):
        c = " ".join(w.capitalize() for w in c.split())
    ctry = COUNTRY_OF_STATE.get(st, "US")
    if not (c and st):
        return {"city": c if st else "", "state": st, "state_code": STATE_CODES.get(st, ""), "source": source, "country": ctry}
    return {"city": c, "state": st, "state_code": STATE_CODES[st], "source": source, "country": ctry}


def infer_market_llm(profile):
    """When the site's markup doesn't say where the business works, ask an AI helper (no web search, tiny prompt) to
    read the title, description, opening text and phone numbers and name the main city, state/province and country.
    Gemini first, ChatGPT if Gemini isn't available (change 60)."""
    from . import llm
    clue = {"title": profile.get("title"), "description": profile.get("meta_description"), "phones": profile.get("page_phones"),
            "domain": profile.get("domain"), "text": (profile.get("opening_text") or "")[:1500]}
    prompt = ("From this business's website, which single city is their main market, in which state, province or territory, "
              "and which country (US, Canada or Australia)? Reply with JSON only, like "
              '{"city": "Charlotte", "state": "North Carolina", "country": "US", "areas": ["Lake Norman"]}. If unknown use null.\n'
              + json.dumps(clue))
    for provider in ("gemini", "chatgpt"):
        if not llm.available(provider):
            continue
        try:
            j, _ = llm.json_answer(prompt, provider, timeout=45)
        except Exception:
            continue
        if isinstance(j, dict) and j.get("city") and j.get("state"):
            return {"city": j["city"], "state": j["state"], "country": normalize_country(j.get("country")),
                    "areas": [a for a in (j.get("areas") or []) if a][:3], "source": provider}
    return None


def checks(profile, market):
    """Website AI-readiness checks. Each: (status good|warn|bad, title, detail)."""
    out, P = [], profile
    biz_types = set(P["schema_types"]) & {"HomeAndConstructionBusiness", "GeneralContractor", "LocalBusiness"}
    if biz_types and P["address"]:
        out.append(("good", "Business details markup", f"{', '.join(sorted(biz_types))} markup with address"
                    + (", service areas" if P["areas_served"] else "") + "."))
    elif biz_types:
        out.append(("warn", "Business details markup", "Business markup exists but has no street address."))
    else:
        out.append(("bad", "Business details markup", "No local business markup, so AI tools must guess the address, phone and service area."))

    if P["schema_phones"] and P["page_phones"] and not set(P["schema_phones"]) & set(P["page_phones"]):
        fmt = format_phone
        note = (f" A {P['call_tracking'][0]} call-tracking script may also swap the number for visitors." if P.get("call_tracking") else "")
        swapped = P.get("rendered_with_browser") and P.get("raw_phones") and set(P["raw_phones"]) != set(P["page_phones"])
        if swapped:
            note = (f" The page's code also contains {fmt(P['raw_phones'][0])}, which a script swaps after loading,"
                    " so crawlers and visitors see different numbers.")
        out.append(("bad", "Phone number consistency", f"The page shows {fmt(P['page_phones'][0])}, but the business markup lists "
                    f"{fmt(P['schema_phones'][0])}. AI answers and Google can repeat the wrong number.{note}"))
    elif P["page_phones"]:
        out.append(("good", "Phone number consistency", "The phone number on the page matches the business markup."
                    if P["schema_phones"] else "A phone number is on the page."))
    else:
        out.append(("bad", "Phone number consistency", "No phone number found on the homepage."))

    if P["prices_found"]:
        out.append(("good", "Prices published", f"Price information on the homepage (for example {P['prices_found'][0]}). AI answers can quote this."))
    else:
        out.append(("warn", "Prices published", "No prices or price ranges on the homepage. Cost questions are among the most common buyers ask AI."))

    if P["faq_questions"] >= 3:
        out.append(("good", "Answers to common questions", f"{P['faq_questions']} question-style headings answer buyer questions on the homepage."))
    else:
        out.append(("warn", "Answers to common questions", "No FAQ-style questions on the homepage for AI tools to quote."))

    if "FAQPage" in P["schema_types"]:
        out.append(("good", "FAQ markup", "FAQPage markup is present."))
    elif P["faq_questions"] >= 3:
        out.append(("warn", "FAQ markup", "The questions have no FAQPage markup, so machines have to guess where each answer starts and ends."))

    if set(P["schema_types"]) & {"AggregateRating", "Review"}:
        out.append(("good", "Review markup", "Review or rating markup is present."))
    else:
        has_t = any(re.search(r"testimonial|review", u, re.I) for u in P["sitemap_urls"])
        out.append(("warn", "Review markup", ("A testimonials page exists, but there is" if has_t else "There is")
                    + " no review or star-rating markup on the site."))

    areas = [a for a in P["areas_served"] if a] or [market["city"]]
    slugs = " ".join(P["sitemap_urls"]).lower()
    missing = [a for a in areas if re.sub(r"^greater\s+", "", a.lower()).replace(" ", "-") not in slugs]
    if missing:
        listed = " and ".join(missing) if len(missing) <= 2 else ", ".join(missing[:-1]) + " and " + missing[-1]
        out.append(("warn", "Service-area pages", f"No dedicated page found for {listed}"
                    + ((", though the markup lists " + ("it" if len(missing) == 1 else "them") + " as service areas.") if P["areas_served"] else ".")))
    else:
        out.append(("good", "Service-area pages", "Each listed service area has its own page."))

    g = P.get("gbp")
    if P.get("gbp_checked"):
        if not g:
            out.append(("warn", "Google Business Profile", "The Google Business Profile couldn't be retrieved this time (no match or the lookup timed out). Check it by hand in Google Maps."))
        else:
            low = sum(v for k, v in (g.get("distribution") or {}).items() if int(k) <= 2)
            parts = [f"{g['rating']}★ from {g['reviews']} reviews" + (f" ({low} rated 1–2★)" if low else "")]
            if g.get("phone") and P["schema_phones"] and g["phone"] not in P["schema_phones"]:
                parts.append(f"its phone ({g['phone'][:3]}) {g['phone'][3:6]}-{g['phone'][6:]} doesn't match the site's markup")
            if not g.get("claimed"):
                parts.append("the profile isn't claimed")
            good = (g.get("rating") or 0) >= 4.6 and (g.get("reviews") or 0) >= 50 and g.get("claimed") and len(parts) == 1
            out.append(("good" if good else "warn", "Google Business Profile", "Google shows " + "; ".join(parts) + "."))
    if P["ai_bots_blocked"]:
        out.append(("bad", "AI crawler access", f"robots.txt blocks {', '.join(P['ai_bots_blocked'])}, so those AI tools can't read the site."))
    else:
        out.append(("good", "AI crawler access", "Nothing blocks AI crawlers" + (" (no robots.txt file, so all crawlers are allowed)." if P["robots_status"] != 200 else ".")))

    if P["sitemap_count"]:
        out.append(("good", "Sitemap", f"A sitemap lists {P['sitemap_count']:,} pages."))
    else:
        out.append(("warn", "Sitemap", "No XML sitemap found."))
    return out
