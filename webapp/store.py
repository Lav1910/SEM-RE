"""Audit files (audit.json, report.html, loom.txt, PDFs, run state) in one place.

- Postgres backend (Vercel): rows in the audit_files table. No disk needed.
- SQLite backend: files in HBM_DATA_DIR/audits/<audit id>/, as before (the audit row's out_dir when it has one).
"""
import json
import os
import time

from . import db

USE_DB = db.PG or os.environ.get("HBM_STORAGE") == "db"


def _dir(aid):
    r = db.q("SELECT out_dir FROM audits WHERE id=?", (aid,), one=True)
    d = (r["out_dir"] if r and r["out_dir"] else None) or os.path.join(db.DATA_DIR, "audits", aid)
    return d


def put(aid, name, data):
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if USE_DB:
        db.upsert("audit_files", {"audit_id": aid, "name": name, "content": raw, "updated_at": time.time()}, ("audit_id", "name"))
        return
    d = _dir(aid)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, os.path.join(d, name))


def get_bytes(aid, name):
    if USE_DB:
        r = db.q("SELECT content FROM audit_files WHERE audit_id=? AND name=?", (aid, name), one=True)
        return bytes(r["content"]) if r else None
    p = os.path.join(_dir(aid), name)
    if not os.path.exists(p):
        return None
    with open(p, "rb") as f:
        return f.read()


def get_text(aid, name):
    b = get_bytes(aid, name)
    return b.decode("utf-8") if b is not None else None


def exists(aid, name):
    if USE_DB:
        return bool(db.q("SELECT 1 FROM audit_files WHERE audit_id=? AND name=?", (aid, name), one=True))
    return os.path.exists(os.path.join(_dir(aid), name))


def updated_at(aid, name):
    if USE_DB:
        r = db.q("SELECT updated_at FROM audit_files WHERE audit_id=? AND name=?", (aid, name), one=True)
        return r["updated_at"] if r else None
    p = os.path.join(_dir(aid), name)
    return os.path.getmtime(p) if os.path.exists(p) else None


def delete(aid, name):
    if USE_DB:
        db.x("DELETE FROM audit_files WHERE audit_id=? AND name=?", (aid, name))
    else:
        p = os.path.join(_dir(aid), name)
        if os.path.exists(p):
            os.remove(p)


def load_json(aid, name="audit.json"):
    t = get_text(aid, name)
    return json.loads(t) if t else None


def save_json(aid, name, obj):
    put(aid, name, json.dumps(obj, separators=(",", ":"), default=str))


def sizes(aids):
    """change 48: {(audit_id, name): bytes} for the files already saved, for the Inventory."""
    out = {}
    aids = list(aids)
    if not aids:
        return out
    if USE_DB:
        for i in range(0, len(aids), 200):
            chunk = aids[i:i + 200]
            for r in db.q(f"SELECT audit_id, name, LENGTH(content) AS n FROM audit_files WHERE audit_id IN ({','.join('?' * len(chunk))})", tuple(chunk)):
                out[(r["audit_id"], r["name"])] = int(r["n"] or 0)
        return out
    for aid in aids:
        d = _dir(aid)
        if os.path.isdir(d):
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if os.path.isfile(p):
                    out[(aid, name)] = os.path.getsize(p)
    return out


def total_bytes():
    """change 48: storage used by every saved file (Inventory footer)."""
    if USE_DB:
        r = db.q("SELECT COALESCE(SUM(LENGTH(content)), 0) AS n FROM audit_files", one=True)
        return int(r["n"] or 0)
    base = os.path.join(db.DATA_DIR, "audits")
    total = 0
    for root, _, files in os.walk(base):
        total += sum(os.path.getsize(os.path.join(root, f)) for f in files)
    return total
