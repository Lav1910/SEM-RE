"""Last known result per AI platform, so the dashboard shows live status instead of "key saved = connected".
Stored in ~/.config/hbm-aeo/health.json: {engine: {"t": epoch, "ok": bool, "error": str|None, "via": "semrush"|None}}."""
import json
import os
import threading
import time

PATH = os.path.expanduser("~/.config/hbm-aeo/health.json")
_lock = threading.Lock()


def load():
    try:
        with open(PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def note(engine, ok, error=None, via=None):
    with _lock:
        d = load()
        d[engine] = {"t": time.time(), "ok": bool(ok), "error": (error or "")[:300] or None, "via": via}
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
        tmp = PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, PATH)
