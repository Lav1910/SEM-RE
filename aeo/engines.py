"""One adapter per AI platform. Every adapter returns the same answer record:

  {engine, question_id, status: ok|no_answer|not_configured|error, text, citations: [{url, title, domain}],
   local_pack: [{title, domain}] (Google only), organic: [{domain, url, rank}] (Google only),
   cost_usd (when the provider reports it), error}

Request formats follow each provider's official docs as of November 2026:
  ChatGPT     OpenAI Responses API, web_search tool with approximate user_location
  Gemini      Gemini Interactions API, google_search tool (location given in the prompt)
  Perplexity  Perplexity Agent API (/v1/agent), effort level + web_search tool with user_location
  Google AI Overviews  DataForSEO SERP Google Organic Live Advanced, load_async_ai_overview
  Google AI Mode       DataForSEO SERP Google AI Mode Live Advanced
"""
import base64
import json
import urllib.error
import urllib.parse
import urllib.request

from . import config

ENGINES = ["google_aio", "google_ai_mode", "chatgpt", "gemini", "perplexity"]
ENGINE_LABELS = {
    "google_aio": "Google AI Overviews", "google_ai_mode": "Google AI Mode",
    "chatgpt": "ChatGPT", "gemini": "Gemini", "perplexity": "Perplexity",
}
REQUIRED_KEYS = {
    "google_aio": ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"],
    "google_ai_mode": ["DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD"],
    "chatgpt": ["OPENAI_API_KEY"], "gemini": ["GEMINI_API_KEY"], "perplexity": ["PERPLEXITY_API_KEY"],
}
STATE_CODES = {"Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL",
    "Indiana": "IN", "Iowa": "IA", "Kansas": "KS", "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT",
    "Nebraska": "NE", "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY"}
US_CODES = set(STATE_CODES.values())
# change 58: Canadian provinces and territories work exactly like US states (no code is shared with a US state)
PROVINCE_CODES = {"Alberta": "AB", "British Columbia": "BC", "Manitoba": "MB", "New Brunswick": "NB", "Newfoundland and Labrador": "NL",
                  "Nova Scotia": "NS", "Northwest Territories": "NT", "Nunavut": "NU", "Ontario": "ON", "Prince Edward Island": "PE",
                  "Quebec": "QC", "Saskatchewan": "SK", "Yukon": "YT"}
STATE_CODES.update(PROVINCE_CODES)
COUNTRY_NAMES = {"US": "United States", "CA": "Canada"}


def country_of(market):
    """'US' or 'CA' for a market or location dict (older audits have no country: they're all US)."""
    m = market or {}
    return m.get("country") or ("CA" if m.get("state_code") in PROVINCE_CODES.values() else "US")


def country_name(market):
    return COUNTRY_NAMES[country_of(market)]

ANSWER_INSTRUCTIONS = ("You are answering a local customer's question. Answer the way you normally would for a consumer, "
                       "naming specific businesses where relevant, and cite your sources.")


def domain_of(url):
    try:
        d = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ""
    for p in ("www.", "m."):
        if d.startswith(p):
            d = d[len(p):]
    return d


def _post(url, payload, headers, retries=3):
    """POST JSON; retry rate limits (429) and server errors (5xx) with backoff, but not quota errors."""
    import time
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=config.SETTINGS["HTTP_TIMEOUT"]) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            quota = "insufficient_quota" in body or "credit" in body.lower()
            if attempt < retries and (e.code >= 500 or (e.code == 429 and not quota)):
                time.sleep(2 * (attempt + 1) ** 2)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body}")


def _record(engine, q, **kw):
    rec = {"engine": engine, "question_id": q["id"], "status": "ok", "text": "", "citations": [],
           "local_pack": [], "organic": [], "cost_usd": None, "error": None}
    rec.update(kw)
    return rec


def _cites(pairs):
    out, seen = [], set()
    for url, title in pairs:
        if not url or url in seen:
            continue
        seen.add(url)
        out.append({"url": url, "title": title or "", "domain": domain_of(url)})
    return out


# ---------------- ChatGPT (OpenAI Responses API) ----------------
def parse_openai(resp):
    text, pairs = [], []
    for item in resp.get("output", []):
        if item.get("type") != "message":
            continue
        for c in item.get("content", []):
            if c.get("type") == "output_text":
                text.append(c.get("text", ""))
                for a in c.get("annotations", []) or []:
                    if a.get("type") == "url_citation":
                        pairs.append((a.get("url"), a.get("title")))
    return "\n".join(text), _cites(pairs)


def ask_chatgpt(q, market):
    payload = {
        "model": config.SETTINGS["OPENAI_MODEL"],
        "instructions": ANSWER_INSTRUCTIONS,
        "input": q["prompt"],
        "tools": [{"type": "web_search", "user_location": {
            "type": "approximate", "country": country_of(market), "city": market["city"], "region": market["state"]}}],
    }
    resp = _post("https://api.openai.com/v1/responses", payload,
                 {"Authorization": "Bearer " + config.key("OPENAI_API_KEY")})
    text, cites = parse_openai(resp)
    return _record("chatgpt", q, text=text, citations=cites, status="ok" if text else "no_answer")


# ---------------- Gemini (Interactions API + google_search) ----------------
def parse_gemini(resp):
    text, pairs = [], []
    for step in resp.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for c in step.get("content", []):
            if c.get("type") == "text":
                text.append(c.get("text", ""))
                for a in c.get("annotations", []) or []:
                    if a.get("type") == "url_citation":
                        pairs.append((a.get("url"), a.get("title")))
    cites = _cites(pairs)
    for c in cites:  # grounding links can be Google redirect URLs; the title carries the real domain
        if "vertexaisearch" in c["domain"] or "google.com" in c["domain"]:
            t = (c["title"] or "").lower().strip()
            if "." in t and " " not in t:
                c["domain"] = t[4:] if t.startswith("www.") else t
    return "\n".join(text), cites


def ask_gemini(q, market):
    prompt = f"I live in {market['city']}, {market['state']}" + (", Canada" if country_of(market) == "CA" else "") + f". {q['prompt']}"
    payload = {"model": config.SETTINGS["GEMINI_MODEL"], "input": prompt, "tools": [{"type": "google_search"}]}
    resp = _post("https://generativelanguage.googleapis.com/v1beta/interactions", payload,
                 {"x-goog-api-key": config.key("GEMINI_API_KEY"), "Api-Revision": config.SETTINGS["GEMINI_API_REVISION"]})
    text, cites = parse_gemini(resp)
    return _record("gemini", q, text=text, citations=cites, status="ok" if text else "no_answer")


# ---------------- Perplexity (Agent API) ----------------
def parse_perplexity(resp):
    text, pairs = [], []
    for item in resp.get("output", []):
        if item.get("type") == "search_results":
            pairs += [(r.get("url"), r.get("title")) for r in item.get("results", [])]
        elif item.get("type") == "message":
            text += [c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"]
    cost = ((resp.get("usage") or {}).get("cost") or {}).get("total_cost")
    return "\n".join(text), _cites(pairs), cost


def ask_perplexity(q, market):
    payload = {
        "effort": config.SETTINGS["PERPLEXITY_EFFORT"],
        "input": q["prompt"],
        "tools": [{"type": "web_search", "user_location": {
            "country": country_of(market), "region": market["state_code"], "city": market["city"]}}],
    }
    resp = _post("https://api.perplexity.ai/v1/agent", payload,
                 {"Authorization": "Bearer " + config.key("PERPLEXITY_API_KEY")})
    text, cites, cost = parse_perplexity(resp)
    return _record("perplexity", q, text=text, citations=cites, cost_usd=cost, status="ok" if text else "no_answer")


# ---------------- Google via DataForSEO ----------------
def _dfs_headers():
    tok = base64.b64encode(f"{config.key('DATAFORSEO_LOGIN')}:{config.key('DATAFORSEO_PASSWORD')}".encode()).decode()
    return {"Authorization": "Basic " + tok}


def _dfs_location(market):
    return f"{market['city']},{market['state']},{country_name(market)}"


def parse_dataforseo(resp):
    """Return (ai_text, ai_citations, local_pack, organic, cost) from a DataForSEO SERP response."""
    cost = resp.get("cost")
    tasks = resp.get("tasks") or []
    if not tasks:
        raise RuntimeError("DataForSEO returned no task")
    t = tasks[0]
    if t.get("status_code") not in (20000, None):
        raise RuntimeError(f"DataForSEO {t.get('status_code')}: {t.get('status_message')}")
    items = []
    for res in t.get("result") or []:
        items += res.get("items") or []
    ai_text, pairs, pack, organic = "", [], [], []
    for it in items:
        typ = it.get("type")
        if typ == "ai_overview":
            ai_text = it.get("markdown") or it.get("text") or ""
            refs = list(it.get("references") or [])
            for el in it.get("items") or []:
                refs += el.get("references") or []
            pairs += [(r.get("url"), r.get("title") or r.get("source")) for r in refs]
        elif typ == "local_pack":
            pack.append({"title": it.get("title", ""), "domain": domain_of(it.get("url") or "") or (it.get("domain") or "")})
        elif typ == "organic":
            organic.append({"domain": domain_of(it.get("url", "")), "url": it.get("url", ""), "rank": it.get("rank_group")})
    return ai_text, _cites(pairs), pack, organic, cost


def ask_google_aio(q, market):
    payload = [{"keyword": q["keyword"], "location_name": _dfs_location(market), "language_code": "en",
                "device": "desktop", "load_async_ai_overview": True, "depth": 20}]
    resp = _post("https://api.dataforseo.com/v3/serp/google/organic/live/advanced", payload, _dfs_headers())
    text, cites, pack, organic, cost = parse_dataforseo(resp)
    return _record("google_aio", q, text=text, citations=cites, local_pack=pack, organic=organic, cost_usd=cost,
                   status="ok" if text else "no_answer")


def ask_google_ai_mode(q, market):
    payload = [{"keyword": q["prompt"], "location_name": _dfs_location(market), "language_code": "en", "device": "desktop"}]
    resp = _post("https://api.dataforseo.com/v3/serp/google/ai_mode/live/advanced", payload, _dfs_headers())
    text, cites, _, _, cost = parse_dataforseo(resp)
    return _record("google_ai_mode", q, text=text, citations=cites, cost_usd=cost, status="ok" if text else "no_answer")


def ask_google_aio_semrush(q, market):
    """Fallback when DataForSEO isn't connected: Google AI Overview sources and the Maps pack from Semrush's
    phrase_organic report (all result types). Semrush gives the cited sources, not the AI Overview text,
    and its snapshot is national (US database), not city-specific."""
    from . import semrush
    try:
        rows, _, units = semrush._report("phrase_organic", {"database": semrush.database(market), "phrase": q["keyword"], "positions_type": "all",
                                                            "display_limit": 15, "export_columns": ["position", "position_type", "domain", "url", "triggered_serp_features"]})
    except RuntimeError as e:
        if "no data" in str(e).lower():  # Semrush doesn't track this search; not measured rather than an error
            return _record("google_aio", q, status="not_measured", source="semrush", error="Semrush has no data for this search")
        raise
    feats = set((rows[0].get("Keywords SERP Features") or "").split(",")) if rows else set()
    pairs = [(r.get("Url"), r.get("Domain")) for r in rows if r.get("Position Type") == "52"]
    pack = [{"title": r.get("Domain", ""), "domain": domain_of(r.get("Url", "")) or r.get("Domain", "")} for r in rows if r.get("Position Type") == "3"]
    organic = [{"domain": domain_of(r.get("Url", "")), "url": r.get("Url", ""), "rank": int(r.get("Position") or 0)}
               for r in rows if r.get("Position Type") == "-1"]
    cites = _cites(pairs)
    if "52" not in feats and not cites:
        status = "no_answer"
    else:
        status = "ok"
    text = ("Google showed an AI Overview citing: " + ", ".join(c["domain"] for c in cites) + ".") if cites else ""
    return _record("google_aio", q, text=text, citations=cites, local_pack=pack, organic=organic, status=status,
                   source="semrush", semrush_units=units)


ASK = {"chatgpt": ask_chatgpt, "gemini": ask_gemini, "perplexity": ask_perplexity,
       "google_aio": ask_google_aio, "google_ai_mode": ask_google_ai_mode}


# ---------------- change 59: ChatGPT and Gemini as people see them, via DataForSEO ----------------
# DataForSEO's LLM Scraper returns the answer the consumer apps (chatgpt.com, the Gemini app) give, with the sources
# they cite and, for ChatGPT, the local business cards they show. About $0.004 an answer against ~$0.11 for an
# OpenAI web-search answer. If it fails, the official APIs answer instead, so an audit never loses the platform.
CAPTURED = {"chatgpt": "chat_gpt", "gemini": "gemini"}


def captured_on():
    """On unless an admin turned it off (Admin page) or HBM_CAPTURED_ANSWERS=0."""
    import os
    if os.environ.get("HBM_CAPTURED_ANSWERS", "1") == "0":
        return False
    try:
        from . import kv
        return (kv.get("answers") or {}).get("captured", True) is not False
    except Exception:
        return True


def _dfs_ready():
    return bool(config.key("DATAFORSEO_LOGIN") and config.key("DATAFORSEO_PASSWORD"))


def _captured_locations(engine, market):
    """chatgpt.com can only be set to a country; the Gemini app can be set to the city (DataForSEO's naming)."""
    country = country_name(market)
    if engine == "gemini" and market.get("city"):
        if country_of(market) == "US":
            return [f"{market['city']}, {market['state_code']},{market['state']},{country}", country]
        return [f"{market['city']},{market['state']},{country}", country]
    return [country]


def parse_captured(resp):
    """(text, citations, cards, cost) from a DataForSEO LLM Scraper response."""
    t = (resp.get("tasks") or [{}])[0]
    if t.get("status_code") not in (20000, None):
        raise RuntimeError(f"DataForSEO {t.get('status_code')}: {t.get('status_message')}")
    r = (t.get("result") or [None])[0] or {}
    items = r.get("items") or []
    text = r.get("markdown") or "\n".join(it.get("markdown") or it.get("text") or "" for it in items if "text" in (it.get("type") or ""))
    cites = _cites([(x.get("url"), x.get("title")) for x in r.get("sources") or []])
    cards = []
    for it in items:
        if "local_business" in (it.get("type") or ""):
            for b in it.get("items") or [it]:
                if b.get("title"):
                    rating = b.get("rating") or {}
                    cards.append({"title": b["title"], "domain": domain_of(b.get("url") or "") or (b.get("domain") or ""),
                                  "rating": rating.get("value") if isinstance(rating, dict) else rating, "reviews": b.get("reviews_count")})
    return text, cites, cards, t.get("cost"), r.get("model")


def ask_captured(engine, q, market):
    last = None
    for loc in _captured_locations(engine, market):
        body = {"keyword": q["prompt"][:2000], "location_name": loc, "language_code": "en"}
        if engine == "chatgpt":
            body["force_web_search"] = True
        try:
            resp = _post(f"https://api.dataforseo.com/v3/ai_optimization/{CAPTURED[engine]}/llm_scraper/live/advanced", [body], _dfs_headers())
            text, cites, cards, cost, model = parse_captured(resp)
        except RuntimeError as e:
            last = e
            if "location" in str(e).lower():
                continue  # a city DataForSEO doesn't know: ask from the country instead
            raise
        if not text.strip():
            raise RuntimeError("DataForSEO returned an empty answer")
        return _record(engine, q, text=text, citations=cites, cards=cards, cost_usd=cost, source="dataforseo",
                       model=model, location=loc)
    raise last or RuntimeError("DataForSEO couldn't ask from this location")


def _semrush_ok():
    from . import semrush
    return semrush.available()


def _dfs_auth_problem(e):
    t = str(e)
    return "HTTP 401" in t or "HTTP 403" in t or "40100" in t or "40104" in t


def is_configured(engine):
    if all(config.key(k) for k in REQUIRED_KEYS[engine]):
        return True
    if engine in CAPTURED and captured_on() and _dfs_ready():
        return True
    return engine == "google_aio" and _semrush_ok()  # Semrush fallback for AI Overviews + Maps pack


def ask(engine, q, market):
    rec = _ask(engine, q, market)
    try:  # remember the platform's live state for the dashboard
        from . import health
        if rec["status"] in ("ok", "no_answer"):
            health.note(engine, True, via=rec.get("source"), error=rec.get("dfs_error"))
        elif rec["status"] == "error":
            health.note(engine, False, rec.get("error"))
    except Exception:
        pass
    return rec


def _ask(engine, q, market):
    from . import budget
    if not is_configured(engine):
        return _record(engine, q, status="not_configured")
    use_semrush = engine == "google_aio" and not all(config.key(k) for k in REQUIRED_KEYS[engine])
    reserved = 0.0 if use_semrush else budget.reserve(engine)  # Semrush uses API units, not the dollar cap
    if reserved is None:
        return _record(engine, q, status="budget_reached", error="Spending cap reached (see spend.py)")
    if engine in CAPTURED and captured_on() and _dfs_ready():  # change 59
        try:
            rec = ask_captured(engine, q, market)
            budget.settle(reserved, rec.get("cost_usd"))
            return rec
        except Exception as e:
            if not all(config.key(k) for k in REQUIRED_KEYS[engine]):
                if reserved:
                    budget.settle(reserved, 0.0)
                return _record(engine, q, status="error", error=str(e)[:300], source="dataforseo")
            captured_error = str(e)[:200]  # fall back to the official API below
    else:
        captured_error = None
    try:
        rec = ask_google_aio_semrush(q, market) if use_semrush else ASK[engine](q, market)
        budget.settle(reserved, rec.get("cost_usd"))
        if captured_error:
            rec.update(source="api (fallback)", dfs_error=captured_error)
        return rec
    except Exception as e:  # keep the audit going; the report shows "—" for this cell
        if engine == "google_aio" and not use_semrush and _dfs_auth_problem(e) and _semrush_ok():
            # DataForSEO login rejected or account not verified yet: use Semrush for AI Overviews instead
            if reserved:
                budget.settle(reserved, 0.0)
            try:
                rec = ask_google_aio_semrush(q, market)
                rec["dfs_error"] = str(e)[:200]
                return rec
            except Exception as e2:
                return _record(engine, q, status="error", error=str(e2)[:300])
        if reserved:
            budget.settle(reserved, 0.0)  # a failed call isn't billed, so release its reservation
        return _record(engine, q, status="error", error=str(e)[:300])
