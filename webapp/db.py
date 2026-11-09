"""SQLite storage for users and audit jobs. One file under the data directory; safe for a single server."""
import os
import sqlite3
import threading
import time

DATA_DIR = os.path.abspath(os.environ.get("HBM_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "appdata")))
DB_PATH = os.path.join(DATA_DIR, "hbm-aeo.sqlite3")
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  url TEXT NOT NULL,
  domain TEXT,
  city TEXT, state TEXT,
  test_mode INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued',
  stage TEXT, progress INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  builder_name TEXT, market TEXT,
  score INTEGER, mentioned INTEGER, answers INTEGER, pack INTEGER, pack_of INTEGER,
  checks_passed INTEGER, checks_total INTEGER, engines_measured INTEGER,
  share_token TEXT UNIQUE,
  out_dir TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS audits_status ON audits(status, created_at);
CREATE INDEX IF NOT EXISTS audits_user ON audits(user_id, created_at);
CREATE INDEX IF NOT EXISTS audits_domain ON audits(domain, created_at);
CREATE TABLE IF NOT EXISTS strategy_status (
  domain TEXT NOT NULL, item_id TEXT NOT NULL, status TEXT NOT NULL, user_id INTEGER, updated_at REAL NOT NULL,
  PRIMARY KEY (domain, item_id)
);
CREATE TABLE IF NOT EXISTS strategy_content (
  audit_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT, status TEXT NOT NULL,
  content TEXT, error TEXT, provider TEXT, cost REAL, user_id INTEGER, updated_at REAL NOT NULL,
  PRIMARY KEY (audit_id, kind, key)
);
CREATE TABLE IF NOT EXISTS login_codes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL REFERENCES users(id), code_hash TEXT NOT NULL, link_hash TEXT NOT NULL,
  expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, ip TEXT
);
CREATE INDEX IF NOT EXISTS login_codes_user ON login_codes(user_id, used, expires);
CREATE TABLE IF NOT EXISTS security_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, user_id INTEGER, email TEXT, ip TEXT, event TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL REFERENCES users(id),
  audit_id TEXT, page TEXT, kind TEXT, message TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


def connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init():
    with connect() as c:
        c.executescript(SCHEMA)
        cols = {r["name"] for r in c.execute("PRAGMA table_info(audits)")}
        for col in ("activity", "engines", "scope", "locations"):  # live progress for the running-audit screen
            if col not in cols:
                c.execute(f"ALTER TABLE audits ADD COLUMN {col} TEXT")
        ucols = {r["name"] for r in c.execute("PRAGMA table_info(users)")}
        for col, ddl in (("role", "TEXT NOT NULL DEFAULT 'sales'"), ("google_sub", "TEXT"), ("pending", "INTEGER NOT NULL DEFAULT 0"),
                         ("last_login", "REAL")):
            if col not in ucols:
                c.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        c.execute("UPDATE users SET role='admin' WHERE is_admin=1 AND role!='admin'")
        # audits keep absolute folder paths; after a move or a backup restore, point them at this data folder
        for r in c.execute("SELECT id, out_dir FROM audits WHERE out_dir IS NOT NULL").fetchall():
            if not os.path.exists(os.path.join(r["out_dir"], "audit.json")):
                cand = os.path.join(DATA_DIR, "audits", os.path.basename(r["out_dir"].rstrip("/")))
                if os.path.exists(os.path.join(cand, "audit.json")):
                    c.execute("UPDATE audits SET out_dir=? WHERE id=?", (cand, r["id"]))


def q(sql, args=(), one=False):
    with connect() as c:
        rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with _lock, connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid


def claim_next():
    """Atomically move the oldest queued audit to running and return it."""
    with _lock, connect() as c:
        row = c.execute("SELECT * FROM audits WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return None
        c.execute("UPDATE audits SET status='running', started_at=?, stage=?, progress=1 WHERE id=?",
                  (time.time(), "Starting", row["id"]))
        return row


def requeue_interrupted():
    x("UPDATE audits SET status='queued', stage='Waiting (server restarted)', progress=0 WHERE status='running'")
