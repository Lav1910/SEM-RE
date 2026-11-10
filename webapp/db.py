"""Storage for users, audits, jobs and audit files.

Two backends with the same calls (q, x, upsert, tx):
- Postgres when DATABASE_URL / POSTGRES_URL is set: Vercel (Neon via the Vercel Marketplace), or any multi-instance host.
- SQLite otherwise: one file under HBM_DATA_DIR. Good for a laptop, a single server or Render with a disk.

SQL is written once with `?` placeholders; for Postgres they're translated to `%s`.
"""
import os
import sqlite3
import threading
import time

DATA_DIR = os.path.abspath(os.environ.get("HBM_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "appdata")))
DB_PATH = os.path.join(DATA_DIR, "hbm-aeo.sqlite3")
URL = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
PG = URL.startswith(("postgres://", "postgresql://"))
_lock = threading.Lock()
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id {pk},
  email TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at {real} NOT NULL,
  role TEXT NOT NULL DEFAULT 'sales', google_sub TEXT, pending INTEGER NOT NULL DEFAULT 0, last_login {real},
  avatar_at {real}
);
CREATE TABLE IF NOT EXISTS user_avatars (
  user_id INTEGER PRIMARY KEY REFERENCES users(id), content {blob} NOT NULL, mime TEXT NOT NULL, updated_at {real} NOT NULL
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
  created_at {real} NOT NULL, started_at {real}, finished_at {real},
  activity TEXT, engines TEXT, scope TEXT, locations TEXT,
  phase TEXT, lease_until {real}, attempts INTEGER NOT NULL DEFAULT 0,
  batch_id TEXT, summary TEXT
);
CREATE INDEX IF NOT EXISTS audits_status ON audits(status, created_at);
CREATE INDEX IF NOT EXISTS audits_user ON audits(user_id, created_at);
CREATE INDEX IF NOT EXISTS audits_domain ON audits(domain, created_at);
CREATE TABLE IF NOT EXISTS batches (
  id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), name TEXT, depth TEXT NOT NULL DEFAULT 'quick',
  status TEXT NOT NULL DEFAULT 'running', total INTEGER NOT NULL DEFAULT 0, created_at {real} NOT NULL, finished_at {real},
  notified INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS todos (
  id {pk}, user_id INTEGER NOT NULL REFERENCES users(id), domain TEXT NOT NULL, url TEXT NOT NULL, name TEXT, notes TEXT,
  status TEXT NOT NULL DEFAULT 'todo', audit_id TEXT, done_via TEXT, cleared INTEGER NOT NULL DEFAULT 0,
  created_at {real} NOT NULL, done_at {real}
);
CREATE INDEX IF NOT EXISTS todos_user ON todos(user_id, cleared, created_at);
CREATE TABLE IF NOT EXISTS client_visits (
  id {pk}, audit_id TEXT NOT NULL, token TEXT, started_at {real} NOT NULL, last_at {real} NOT NULL, seconds INTEGER NOT NULL DEFAULT 0,
  device TEXT, city TEXT, region TEXT, country TEXT, sections TEXT, actions TEXT, pages INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS client_visits_audit ON client_visits(audit_id, started_at);
CREATE TABLE IF NOT EXISTS audit_files (
  audit_id TEXT NOT NULL, name TEXT NOT NULL, content {blob} NOT NULL, updated_at {real} NOT NULL,
  PRIMARY KEY (audit_id, name)
);
CREATE TABLE IF NOT EXISTS strategy_status (
  domain TEXT NOT NULL, item_id TEXT NOT NULL, status TEXT NOT NULL, user_id INTEGER, updated_at {real} NOT NULL,
  PRIMARY KEY (domain, item_id)
);
CREATE TABLE IF NOT EXISTS strategy_content (
  audit_id TEXT NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT, status TEXT NOT NULL,
  content TEXT, error TEXT, provider TEXT, cost {real}, user_id INTEGER, updated_at {real} NOT NULL,
  lease_until {real},
  PRIMARY KEY (audit_id, kind, key)
);
CREATE TABLE IF NOT EXISTS login_codes (
  id {pk}, user_id INTEGER NOT NULL REFERENCES users(id), code_hash TEXT NOT NULL, link_hash TEXT NOT NULL,
  expires {real} NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, created_at {real} NOT NULL, ip TEXT
);
CREATE INDEX IF NOT EXISTS login_codes_user ON login_codes(user_id, used, expires);
CREATE TABLE IF NOT EXISTS security_log (
  id {pk}, t {real} NOT NULL, user_id INTEGER, email TEXT, ip TEXT, event TEXT NOT NULL, detail TEXT
);
CREATE INDEX IF NOT EXISTS security_log_t ON security_log(event, t);
CREATE TABLE IF NOT EXISTS api_tokens (
  id {pk}, user_id INTEGER NOT NULL REFERENCES users(id), kind TEXT NOT NULL, name TEXT NOT NULL, prefix TEXT,
  token_hash TEXT UNIQUE NOT NULL, refresh_hash TEXT UNIQUE, client_id TEXT, scope TEXT, created_at {real} NOT NULL,
  expires_at {real}, refresh_expires_at {real}, last_used_at {real}, revoked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS api_tokens_user ON api_tokens(user_id, revoked);
CREATE TABLE IF NOT EXISTS oauth_clients (
  client_id TEXT PRIMARY KEY, name TEXT NOT NULL, redirect_uris TEXT NOT NULL, secret_hash TEXT,
  auth_method TEXT NOT NULL DEFAULT 'none', created_at {real} NOT NULL, ip TEXT
);
CREATE TABLE IF NOT EXISTS oauth_codes (
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, user_id INTEGER NOT NULL, redirect_uri TEXT NOT NULL,
  challenge TEXT NOT NULL, scope TEXT, expires {real} NOT NULL, used INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS agent_log (
  id {pk}, t {real} NOT NULL, user_id INTEGER NOT NULL, token_id INTEGER, client TEXT, tool TEXT NOT NULL, detail TEXT,
  audit_id TEXT, ok INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS agent_log_user ON agent_log(user_id, t);
CREATE TABLE IF NOT EXISTS feedback (
  id {pk},
  user_id INTEGER NOT NULL REFERENCES users(id),
  audit_id TEXT, page TEXT, kind TEXT, message TEXT NOT NULL,
  created_at {real} NOT NULL
);
"""
# columns added after the first release, for SQLite files created by older versions
LATER_COLUMNS = {
    "audits": [("activity", "TEXT"), ("engines", "TEXT"), ("scope", "TEXT"), ("locations", "TEXT"), ("phase", "TEXT"),
               ("lease_until", "REAL"), ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("batch_id", "TEXT"), ("summary", "TEXT"),
               ("cost_usd", "REAL"), ("client_token", "TEXT"), ("client_expires", "REAL"), ("client_views", "INTEGER NOT NULL DEFAULT 0"),
               ("client_last_view", "REAL"), ("depth", "TEXT"), ("client_by", "INTEGER"), ("client_created_at", "REAL"),
               ("client_full", "INTEGER NOT NULL DEFAULT 0"), ("client_alerted_at", "REAL"),
               ("via", "TEXT")],
    "users": [("role", "TEXT NOT NULL DEFAULT 'sales'"), ("google_sub", "TEXT"), ("pending", "INTEGER NOT NULL DEFAULT 0"),
              ("last_login", "REAL"), ("avatar_at", "REAL")],
    "strategy_content": [("lease_until", "REAL")],
}


# ---------------------------------------------------------------- connections
def _pg():
    import psycopg
    from psycopg.rows import dict_row
    c = getattr(_local, "pg", None)
    if c is None or c.closed or c.broken:
        # prepare_threshold=None: works through connection poolers (Neon's pooled URL, PgBouncer)
        c = _local.pg = psycopg.connect(URL, autocommit=True, row_factory=dict_row, prepare_threshold=None, connect_timeout=10)
    return c


def connect():
    """SQLite connection (used by the SQLite backend and the CLI tools)."""
    os.makedirs(DATA_DIR, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def _sql(sql):
    return sql.replace("%", "%%").replace("?", "%s") if PG else sql


def _run_pg(sql, args, fetch):
    for attempt in (1, 2):  # one reconnect if the serverless instance's connection went stale
        try:
            cur = _pg().execute(_sql(sql), tuple(args))
            return cur.fetchall() if fetch and cur.description else None
        except Exception as e:
            import psycopg
            if attempt == 1 and isinstance(e, (psycopg.OperationalError, psycopg.InterfaceError)):
                _local.pg = None
                continue
            raise


# ---------------------------------------------------------------- public API
def init():
    if PG:
        ddl = SCHEMA.format(pk="SERIAL PRIMARY KEY", real="DOUBLE PRECISION", blob="BYTEA")
        for stmt in [s.strip() for s in ddl.split(";") if s.strip()]:
            _run_pg(stmt, (), False)
        for table, cols in LATER_COLUMNS.items():
            for col, typ in cols:
                _run_pg(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ.replace('REAL', 'DOUBLE PRECISION')}", (), False)
        return
    with connect() as c:
        c.executescript(SCHEMA.format(pk="INTEGER PRIMARY KEY AUTOINCREMENT", real="REAL", blob="BLOB"))
        for table, cols in LATER_COLUMNS.items():
            have = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            for col, typ in cols:
                if col not in have:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        c.execute("UPDATE users SET role='admin' WHERE is_admin=1 AND role!='admin'")
        # audits keep absolute folder paths; after a move or a backup restore, point them at this data folder
        for r in c.execute("SELECT id, out_dir FROM audits WHERE out_dir IS NOT NULL").fetchall():
            if not os.path.exists(os.path.join(r["out_dir"], "audit.json")):
                cand = os.path.join(DATA_DIR, "audits", os.path.basename(r["out_dir"].rstrip("/")))
                if os.path.exists(os.path.join(cand, "audit.json")):
                    c.execute("UPDATE audits SET out_dir=? WHERE id=?", (cand, r["id"]))


def q(sql, args=(), one=False):
    if PG:
        rows = _run_pg(sql, args, True) or []
    else:
        with connect() as c:
            rows = c.execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    """Run a write. Returns the new row id for an INSERT into a table with an id column (both backends)."""
    if PG:
        if sql.lstrip().upper().startswith("INSERT") and "RETURNING" not in sql.upper() and _has_serial(sql):
            rows = _run_pg(sql.rstrip().rstrip(";") + " RETURNING id", args, True)
            return rows[0]["id"] if rows else None
        _run_pg(sql, args, False)
        return None
    with _lock, connect() as c:
        return c.execute(sql, args).lastrowid


def _has_serial(sql):
    t = sql.split("INTO", 1)[1].strip().split()[0].lower() if "INTO" in sql.upper() else ""
    return t in ("users", "login_codes", "security_log", "feedback", "todos", "api_tokens", "agent_log")


def upsert(table, data, keys):
    """Insert or replace one row identified by `keys` (both backends)."""
    cols = list(data)
    ph = ", ".join("?" for _ in cols)
    if PG:
        sets = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in keys) or f"{keys[0]}=EXCLUDED.{keys[0]}"
        sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({ph}) ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {sets}"
    else:
        sql = f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) VALUES ({ph})"
    if PG:
        _run_pg(sql, [data[c] for c in cols], False)
    else:
        with _lock, connect() as c:
            c.execute(sql, [data[c] for c in cols])


def claim(table, where, args, order, lease_seconds, key="id", extra_set="", extra_args=()):
    """Atomically take one row that nobody holds (lease expired) and lease it. Returns the row or None.
    Works across processes and serverless instances: Postgres uses FOR UPDATE SKIP LOCKED, SQLite a write lock."""
    now = time.time()
    if PG:
        keys = key.split(",")
        sql = (f"UPDATE {table} SET lease_until=%s{_sql(extra_set)} WHERE ({key}) IN (SELECT {key} FROM {table} WHERE {_sql(where)} "
               f"AND (lease_until IS NULL OR lease_until<%s) ORDER BY {order} LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *")
        rows = _pg().execute(sql, (now + lease_seconds, *extra_args, *args, now)).fetchall()
        return rows[0] if rows else None
    with _lock, connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(f"SELECT * FROM {table} WHERE {where} AND (lease_until IS NULL OR lease_until<?) ORDER BY {order} LIMIT 1",
                        (*args, now)).fetchone()
        if not row:
            c.execute("COMMIT")
            return None
        kcols = key.split(",")
        c.execute(f"UPDATE {table} SET lease_until=?{extra_set} WHERE " + " AND ".join(f"{k}=?" for k in kcols),
                  (now + lease_seconds, *extra_args, *[row[k] for k in kcols]))
        row = c.execute(f"SELECT * FROM {table} WHERE " + " AND ".join(f"{k}=?" for k in kcols), [row[k] for k in kcols]).fetchone()
        c.execute("COMMIT")
        return row


def recent_count(event_like, since, email=None, ip=None):
    """How many security_log events (comma list) happened since `since` for an email and/or IP: shared rate limits."""
    evs = [e.strip() for e in event_like.split(",")]
    sql = f"SELECT COUNT(*) AS n FROM security_log WHERE t>? AND event IN ({', '.join('?' for _ in evs)})"
    args = [since, *evs]
    if email is not None:
        sql += " AND email=?"; args.append(email)
    if ip is not None:
        sql += " AND ip=?"; args.append(ip)
    return q(sql, args, one=True)["n"]
