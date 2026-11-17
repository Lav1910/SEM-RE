"""Running spend ledger with a hard cap, so test runs can't exceed a set amount across all audits.

Ledger: ~/.config/hbm-aeo/spend.json  {"limit_usd": 20.0, "spent_usd": 3.12, "calls": {...}}
Each paid call reserves a conservative estimate before it runs; when the provider reports the real cost
(Perplexity, DataForSEO) that figure replaces the estimate. Once the cap is reached, calls are skipped and
show as "—" in the report. Set or check the cap with spend.py.
"""
import json
import os
import threading

LEDGER = os.path.expanduser("~/.config/hbm-aeo/spend.json")
# Conservative per-answer reservations in USD; the provider-reported cost replaces them where there is one.
ESTIMATE = {"chatgpt": 0.15, "gemini": 0.06, "perplexity": 0.01, "google_aio": 0.005, "google_ai_mode": 0.005}
# Typical per-answer cost, measured Nov 17 2026 (change 59): an OpenAI web-search answer ~$0.11 (2-6 searches at $0.01
# plus 19-38k tokens of results), a Gemini grounded answer ~$0.05, Perplexity $0.0037, DataForSEO Google $0.004.
TYPICAL = {"chatgpt": 0.11, "gemini": 0.05, "perplexity": 0.004, "google_aio": 0.004, "google_ai_mode": 0.004}
CAPTURED_TYPICAL = 0.004  # chatgpt.com / Gemini app answers through DataForSEO's LLM Scraper


def typical(engine):
    from . import engines
    if engine in engines.CAPTURED and engines.captured_on() and engines._dfs_ready():
        return CAPTURED_TYPICAL
    return TYPICAL.get(engine, 0)
_lock = threading.Lock()


def audit_cost(A):
    """Dollar cost of one audit's AI answers (change 25). Perplexity and DataForSEO report their exact charge on each
    answer; ChatGPT and Gemini don't, so those use the typical per-answer price. Failed or skipped calls cost nothing,
    and Semrush lookups use Semrush units, not dollars."""
    total = 0.0
    for a in A.get("answers", []):
        if a.get("status") not in ("ok", "no_answer") or a.get("source") == "semrush":
            continue
        c = a.get("cost_usd")
        total += float(c) if c else TYPICAL.get(a.get("engine"), 0)
    return round(total, 4)


def per_question(live_engines):
    """Typical dollars to ask one question on every connected platform."""
    return sum(typical(e) for e in live_engines)


EMPTY = {"limit_usd": None, "spent_usd": 0.0, "calls": {}}


def _load():
    from . import kv
    return kv.get("spend", None) or dict(EMPTY, calls={})


def status():
    return _load()


def set_limit(limit_usd, reset=False):
    from . import kv

    def fn(d):
        d = d or dict(EMPTY, calls={})
        d["limit_usd"] = limit_usd
        if reset:
            d["spent_usd"], d["calls"] = 0.0, {}
        return d
    return kv.update("spend", fn, dict(EMPTY, calls={}))


def reserve(engine):
    """Reserve the estimate for one call. Returns the reserved amount, or None when the cap would be exceeded."""
    from . import kv
    est = ESTIMATE[engine]
    ok = [False]

    def fn(d):
        d = d or dict(EMPTY, calls={})
        if d.get("limit_usd") is not None and d["spent_usd"] + est > d["limit_usd"]:
            return None
        d["spent_usd"] = round(d["spent_usd"] + est, 4)
        d.setdefault("calls", {})[engine] = d["calls"].get(engine, 0) + 1
        ok[0] = True
        return d
    kv.update("spend", fn, dict(EMPTY, calls={}))
    return est if ok[0] else None


def settle(reserved, actual):
    """Swap the reserved estimate for the provider-reported cost when there is one."""
    if reserved is None or actual is None:
        return
    from . import kv

    def fn(d):
        d = d or dict(EMPTY, calls={})
        d["spent_usd"] = round(max(0.0, d["spent_usd"] - reserved + float(actual)), 4)
        return d
    kv.update("spend", fn, dict(EMPTY, calls={}))
