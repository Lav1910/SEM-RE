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


def key_print(engine):
    """A short one-way fingerprint of the key(s) a platform uses right now (change 27). It can't be turned back into
    the key; it only tells whether the key changed since the platform's last result was recorded."""
    import hashlib
    from . import config, engines
    names = engines.REQUIRED_KEYS.get(engine, [])
    raw = "|".join(config.key(n) or "" for n in names)
    return hashlib.sha256(("hbm-health|" + raw).encode()).hexdigest()[:12] if raw.strip("|") else None


def note(engine, ok, error=None, via=None):
    from . import kv
    entry = {"t": time.time(), "ok": bool(ok), "error": (error or "")[:300] or None, "via": via, "kp": key_print(engine)}
    kv.update("health", lambda d: dict(d or {}, **{engine: entry}), {})
