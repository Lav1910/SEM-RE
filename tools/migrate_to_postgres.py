#!/usr/bin/env python3
"""Copy everything from the SQLite version (users, audits and their files, strategy work, feedback, security log)
into the Postgres database used on Vercel. Safe to run more than once: rows that already exist are skipped.

  DATABASE_URL='postgresql://...' python tools/migrate_to_postgres.py --from /path/to/appdata

Get DATABASE_URL from Vercel -> Storage -> your Neon database -> .env.local tab (use the non-pooled URL for this).
"""
import argparse
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", default=os.path.join(ROOT, "appdata"), help="the SQLite version's data folder")
    a = ap.parse_args()
    if not (os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or "").startswith("postgres"):
        raise SystemExit("Set DATABASE_URL to the Postgres database first.")
    from webapp import db, store
    db.init()
    src = sqlite3.connect(os.path.join(a.src, "hbm-aeo.sqlite3"))
    src.row_factory = sqlite3.Row

    def cols(table):
        return [r[1] for r in src.execute(f"PRAGMA table_info({table})")]

    def copy(table, keys, skip=()):
        have = set(cols(table))
        dest = {r["column_name"] for r in db.q("SELECT column_name FROM information_schema.columns WHERE table_name=?", (table,))}
        use = [c for c in cols(table) if c in dest and c not in skip]
        n = 0
        for row in src.execute(f"SELECT * FROM {table}"):
            where = " AND ".join(f"{k}=?" for k in keys)
            if db.q(f"SELECT 1 FROM {table} WHERE {where}", [row[k] for k in keys], one=True):
                continue
            db.x(f"INSERT INTO {table} ({', '.join(use)}) VALUES ({', '.join('?' for _ in use)})", [row[c] for c in use])
            n += 1
        return n

    # users keep their ids so audits still point at the right person
    print("users:", copy("users", ("email",)))
    db.x("SELECT setval(pg_get_serial_sequence('users','id'), (SELECT COALESCE(MAX(id), 1) FROM users))")
    print("audits:", copy("audits", ("id",), skip=("out_dir",)))
    files = 0
    for row in src.execute("SELECT id, out_dir FROM audits WHERE status='done'"):
        d = row["out_dir"] if row["out_dir"] and os.path.isdir(row["out_dir"]) else os.path.join(a.src, "audits", row["id"])
        for name in ("audit.json", "report.html", "loom.txt"):
            p = os.path.join(d, name)
            if os.path.exists(p) and not store.exists(row["id"], name):
                with open(p, "rb") as f:
                    store.put(row["id"], name, f.read())
                files += 1
    print("audit files:", files)
    for table, keys in (("strategy_status", ("domain", "item_id")), ("strategy_content", ("audit_id", "kind", "key")),
                        ("feedback", ("id",)), ("security_log", ("id",))):
        if table in [r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
            print(f"{table}:", copy(table, keys))
    for t in ("feedback", "security_log", "login_codes"):
        db.x(f"SELECT setval(pg_get_serial_sequence('{t}','id'), (SELECT COALESCE(MAX(id), 1) FROM {t}))")
    print("Done at", time.strftime("%H:%M:%S"))


if __name__ == "__main__":
    main()
