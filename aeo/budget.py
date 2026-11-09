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
# Conservative per-answer estimates in USD (high end of the published-price ranges).
ESTIMATE = {"chatgpt": 0.06, "gemini": 0.05, "perplexity": 0.05, "google_aio": 0.005, "google_ai_mode": 0.005}
# Typical per-answer cost, for the cost estimate shown before an audit runs (Semrush calls use units, not dollars).
TYPICAL = {"chatgpt": 0.035, "gemini": 0.035, "perplexity": 0.015, "google_aio": 0.003, "google_ai_mode": 0.003}
_lock = threading.Lock()


def per_question(live_engines):
    """Typical dollars to ask one question on every connected platform."""
    return sum(TYPICAL.get(e, 0) for e in live_engines)


def _load():
    try:
        return json.load(open(LEDGER))
    except Exception:
        return {"limit_usd": None, "spent_usd": 0.0, "calls": {}}


def _save(d):
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    tmp = LEDGER + ".tmp"
    json.dump(d, open(tmp, "w"), indent=1)
    os.replace(tmp, LEDGER)


def status():
    return _load()


def set_limit(limit_usd, reset=False):
    with _lock:
        d = _load()
        d["limit_usd"] = limit_usd
        if reset:
            d["spent_usd"], d["calls"] = 0.0, {}
        _save(d)
        return d


def reserve(engine):
    """Reserve the estimate for one call. Returns the reserved amount, or None when the cap would be exceeded."""
    est = ESTIMATE[engine]
    with _lock:
        d = _load()
        if d.get("limit_usd") is not None and d["spent_usd"] + est > d["limit_usd"]:
            return None
        d["spent_usd"] = round(d["spent_usd"] + est, 4)
        d["calls"][engine] = d["calls"].get(engine, 0) + 1
        _save(d)
        return est


def settle(reserved, actual):
    """Swap the reserved estimate for the provider-reported cost when there is one."""
    if reserved is None or actual is None:
        return
    with _lock:
        d = _load()
        d["spent_usd"] = round(max(0.0, d["spent_usd"] - reserved + float(actual)), 4)
        _save(d)
