"""Last known result per AI platform, so the dashboard shows live status instead of "key saved = connected".
Stored in ~/.config/hbm-aeo/health.json: {engine: {"t": epoch, "ok": bool, "error": str|None, "via": "semrush"|None}}."""
import json
import os
import threading
import time

PATH = os.path.expanduser("~/.config/hbm-aeo/health.json")
_lock = threading.Lock()


def load():
    from . import kv
    return kv.get("health", None) or {}


def note(engine, ok, error=None, via=None):
    from . import kv
    entry = {"t": time.time(), "ok": bool(ok), "error": (error or "")[:300] or None, "via": via}
    kv.update("health", lambda d: dict(d or {}, **{engine: entry}), {})
