"""Small shared key-value store for app-wide state (spending ledger, platform status).

- With DATABASE_URL / POSTGRES_URL (Vercel, or any multi-instance host): a `settings` table in Postgres, updated
  inside a row-locked transaction so concurrent serverless instances never lose an update.
- Otherwise: JSON files under ~/.config/hbm-aeo/ (one machine; a process lock keeps updates whole).
"""
import json
import os
import threading
import time

URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
PG = URL.startswith(("postgres://", "postgresql://"))
DIR = os.environ.get("HBM_KV_DIR") or os.path.expanduser("~/.config/hbm-aeo")  # tests point this at a temp folder
_lock = threading.Lock()
_local = threading.local()


def _conn():
    import psycopg
    c = getattr(_local, "c", None)
    if c is None or c.closed:
        c = _local.c = psycopg.connect(URL, autocommit=True, prepare_threshold=None, connect_timeout=10)
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at DOUBLE PRECISION NOT NULL)")
    return c


def _path(key):
    return os.path.join(DIR, f"{key}.json")


def get(key, default=None):
    if PG:
        row = _conn().execute("SELECT value FROM settings WHERE key=%s", (key,)).fetchone()
        return json.loads(row[0]) if row else default
    try:
        with open(_path(key)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def update(key, fn, default=None):
    """Atomically replace the value with fn(current) and return the new value. fn may return None to leave it unchanged."""
    if PG:
        c = _conn()
        with c.transaction():
            c.execute("INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, %s) ON CONFLICT (key) DO NOTHING",
                      (key, json.dumps(default), time.time()))
            cur = json.loads(c.execute("SELECT value FROM settings WHERE key=%s FOR UPDATE", (key,)).fetchone()[0])
            new = fn(cur)
            if new is not None:
                c.execute("UPDATE settings SET value=%s, updated_at=%s WHERE key=%s", (json.dumps(new), time.time(), key))
            return new if new is not None else cur
    with _lock:
        cur = get(key, default)
        new = fn(cur)
        if new is not None:
            os.makedirs(DIR, exist_ok=True)
            tmp = _path(key) + ".tmp"
            with open(tmp, "w") as f:
                json.dump(new, f, indent=1)
            os.replace(tmp, _path(key))
        return new if new is not None else cur
