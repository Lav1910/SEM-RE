"""Credit left on the paid services the app uses (change 86), for the Admin page.

Only some providers let an API key read its own balance:
- DataForSEO: GET /v3/appendix/user_data (free) -> money left on the account.
- Semrush: the free API-units counter, when the app holds a plain Semrush API key (not a sign-in token).
OpenAI, Gemini, Perplexity and DataWise don't expose a balance to an API key, so they show a link to their billing page
and the app's own count of answers instead. Nothing is guessed: no reading means "not available", never zero.

Readings are cached in the shared settings store for CACHE_SECONDS, so opening Admin doesn't call the providers each time.
Errors are reduced to a short reason; keys never appear in them.
"""
import base64
import json
import time
import urllib.parse
import urllib.request

from . import config

CACHE_SECONDS = 600
TIMEOUT = 8
DFS_WARN, DFS_LOW = 15.0, 5.0            # USD
SEMRUSH_WARN, SEMRUSH_LOW = 10000, 2000  # API units

BILLING = {
    "dataforseo": "https://app.dataforseo.com/",
    "semrush": "https://www.semrush.com/accounts/subscription-info/",
    "openai": "https://platform.openai.com/settings/organization/billing/overview",
    "gemini": "https://console.cloud.google.com/billing",
    "perplexity": "https://www.perplexity.ai/settings/api",
}


def _get(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "HBM-AEO-Snapshot/1.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _reason(e):
    code = getattr(e, "code", None)
    if code in (401, 403):
        return "The provider rejected the key"
    if code == 402:
        return "The account is out of credit"
    if code:
        return f"The provider answered HTTP {code}"
    return "Couldn't reach the provider"


def dataforseo():
    login, pw = config.key("DATAFORSEO_LOGIN"), config.key("DATAFORSEO_PASSWORD")
    if not (login and pw):
        return {"state": "off", "value": None, "note": "Not connected"}
    try:
        tok = base64.b64encode(f"{login}:{pw}".encode()).decode()
        d = json.loads(_get("https://api.dataforseo.com/v3/appendix/user_data", {"Authorization": "Basic " + tok}))
        money = ((d.get("tasks") or [{}])[0].get("result") or [{}])[0].get("money") or {}
        bal = money.get("balance")
        if bal is None:
            return {"state": "mute", "value": None, "note": "DataForSEO didn't return a balance"}
        bal = float(bal)
        state = "bad" if bal < DFS_LOW else "warn" if bal < DFS_WARN else "good"
        return {"state": state, "value": round(bal, 2), "unit": "usd",
                "note": f"${float(money.get('total') or 0):,.2f} spent on this account in total"}
    except Exception as e:  # noqa: BLE001  a balance is information only; never break the Admin page
        return {"state": "bad", "value": None, "note": _reason(e)}


def semrush():
    k = config.key("SEMRUSH_API_KEY")
    if not k:
        from . import semrush as sr
        if sr.available():
            return {"state": "mute", "value": None, "note": "Connected by sign-in, which can't read the unit balance"}
        return {"state": "off", "value": None, "note": "Not connected"}
    try:
        raw = _get("https://www.semrush.com/users/countapiunits.html?" + urllib.parse.urlencode({"key": k})).strip()
        units = int(raw)
    except ValueError:
        return {"state": "mute", "value": None, "note": "Semrush didn't return a unit count"}
    except Exception as e:  # noqa: BLE001
        return {"state": "bad", "value": None, "note": _reason(e)}
    state = "bad" if units < SEMRUSH_LOW else "warn" if units < SEMRUSH_WARN else "good"
    return {"state": state, "value": units, "unit": "units", "note": "API units left"}


def _read():
    return {"t": time.time(), "dataforseo": dataforseo(), "semrush": semrush()}


def readings(force=False, cached_only=False):
    """{t, dataforseo, semrush}, from the cache when it is fresh enough. cached_only never calls a provider (tests)."""
    from . import kv
    cached = kv.get("balances", None)
    if cached_only:
        return cached or {"t": None, "dataforseo": {"state": "mute", "value": None, "note": "Not checked yet"},
                          "semrush": {"state": "mute", "value": None, "note": "Not checked yet"}}
    if not force and cached and time.time() - float(cached.get("t") or 0) < CACHE_SECONDS:
        return cached
    fresh = _read()
    kv.update("balances", lambda _o: fresh, {})
    return fresh


def rows(force=False, cached_only=False):
    """The Admin card: one row per paid service, in the order they matter for audits."""
    from . import budget
    r = readings(force, cached_only)
    calls = (budget.status() or {}).get("calls") or {}
    connected = {n: bool(config.key(n)) for n in ("OPENAI_API_KEY", "GEMINI_API_KEY", "PERPLEXITY_API_KEY", "DATAWISE_TOKEN")}

    def no_balance(name, used_for, key, engine, link):
        if not connected[key]:
            return {"name": name, "used_for": used_for, "state": "off", "value": None, "note": "Not connected", "link": None}
        n = calls.get(engine, 0) if engine else None
        note = "The provider doesn't share a balance with API keys"
        if n:
            note += f" · {n:,} answers since the spend was last reset"
        return {"name": name, "used_for": used_for, "state": "mute", "value": None, "note": note, "link": link}

    out = [
        {"name": "DataForSEO", "used_for": "Google AI Overviews, AI Mode, Maps pack, ChatGPT and Gemini app answers",
         **r["dataforseo"], "link": BILLING["dataforseo"]},
        {"name": "Semrush", "used_for": "AI Overview sources and keyword data", **r["semrush"], "link": BILLING["semrush"]},
        no_balance("OpenAI", "ChatGPT answers through the official API", "OPENAI_API_KEY", "chatgpt", BILLING["openai"]),
        no_balance("Google Gemini", "Gemini answers through the official API", "GEMINI_API_KEY", "gemini", BILLING["gemini"]),
        no_balance("Perplexity", "Perplexity answers", "PERPLEXITY_API_KEY", "perplexity", BILLING["perplexity"]),
        no_balance("DataWise", "Search Console data for Delivery", "DATAWISE_TOKEN", None, None),
    ]
    return out, r.get("t")
