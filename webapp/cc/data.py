"""Command Center storage helpers (change 64): loading a client's picture, saving flags, history, sources and metrics."""
import json

from webapp import db
from . import health
from .core import add_months, month_of, now, root_domain, today, track_from


def d(r):
    return dict(r) if r is not None else None


def people():
    return {r["id"]: d(r) for r in db.q("SELECT * FROM cc_people ORDER BY name")}


def log_change(client_id, entity, entity_id, field, old, new, by):
    if (old or "") == (new or ""):
        return
    db.x("INSERT INTO cc_history (client_id, entity, entity_id, field, old, new, by_email, at) VALUES (?,?,?,?,?,?,?,?)",
         (client_id, entity, str(entity_id) if entity_id is not None else None, field,
          None if old is None else str(old)[:500], None if new is None else str(new)[:500], by, now()))


def set_source(client_id, source, state, detail=None, ok=False):
    t = now()
    prev = db.q("SELECT last_ok FROM cc_sources WHERE client_id=? AND source=?", (client_id, source), one=True)
    db.upsert("cc_sources", {"client_id": client_id, "source": source, "state": state, "detail": (detail or "")[:300] or None,
                             "last_ok": t if ok else (prev["last_ok"] if prev else None), "last_try": t}, ["client_id", "source"])


def set_metric(client_id, source, metric, period, value=None, text=None):
    db.upsert("cc_metrics", {"client_id": client_id, "source": source, "metric": metric, "period": period, "value": value,
                             "text": text, "fetched_at": now()}, ["client_id", "source", "metric", "period"])


def metric(client_id, source, m, period="latest"):
    return db.q("SELECT * FROM cc_metrics WHERE client_id=? AND source=? AND metric=? AND period=?", (client_id, source, m, period), one=True)


def person_for_clickup(user):
    """The cc_people row for a ClickUp assignee (created the first time we see them)."""
    cid = str(user.get("id") or "")
    if not cid:
        return None
    r = db.q("SELECT id FROM cc_people WHERE clickup_id=?", (cid,), one=True)
    if r:
        return r["id"]
    return db.x("INSERT INTO cc_people (name, email, roles, clickup_id, active, created_at) VALUES (?,?,?,?,1,?)",
                ((user.get("username") or user.get("email") or "ClickUp user").strip(), user.get("email"), "", cid, now()))


def aeo_latest(domain):
    """Latest finished AEO Snapshot audit for this domain (any teammate's)."""
    dom = root_domain(domain)
    if not dom:
        return None
    return db.q("""SELECT id, score, finished_at FROM audits WHERE status='done' AND test_mode=0 AND (domain=? OR domain=?)
                   ORDER BY finished_at DESC LIMIT 1""", (dom, "www." + dom), one=True)


# ---------------------------------------------------------------- loading
def _group(rows, key="client_id"):
    out = {}
    for r in rows:
        out.setdefault(r[key], []).append(d(r))
    return out


def contexts(client_ids=None):
    """Everything health and the screens need, for many clients, in a handful of queries (pages read the DB only)."""
    where, args = "", []
    if client_ids is not None:
        if not client_ids:
            return {}
        where = f" WHERE client_id IN ({', '.join('?' for _ in client_ids)})"
        args = list(client_ids)
    cl_where = where.replace("client_id", "id")
    clients = {r["id"]: d(r) for r in db.q("SELECT * FROM cc_clients" + cl_where, args)}
    since = add_months(month_of(today()), -13)
    scope = _group(db.q("SELECT * FROM cc_scope" + where, args))
    # the sitemap baseline rows (month='baseline') only mark what was already there; they're never a deliverable
    dl = _group(db.q("SELECT * FROM cc_deliverables" + (where + " AND" if where else " WHERE") + " month>=? AND month!='baseline'",
                     args + [since]))
    tasks = _group(db.q("SELECT * FROM cc_tasks" + where, args))
    srcs = _group(db.q("SELECT * FROM cc_sources" + where, args))
    mets = _group(db.q("SELECT * FROM cc_metrics" + where, args))
    flags = _group(db.q("SELECT * FROM cc_flags" + (where + " AND" if where else " WHERE") + " resolved_at IS NULL", args))
    out = {}
    t, n, start = today(), now(), track_from()
    for cid, c in clients.items():
        out[cid] = {"client": c, "scope": scope.get(cid, []), "deliverables": dl.get(cid, []), "tasks": tasks.get(cid, []),
                    "sources": {s["source"]: s for s in srcs.get(cid, [])},
                    "metrics": {(m["source"], m["metric"], m["period"]): m for m in mets.get(cid, [])},
                    "flags": flags.get(cid, []), "today": t, "now": n, "track_from": start}
    return out


def context(client_id):
    return contexts([client_id]).get(client_id)


# ---------------------------------------------------------------- flags
def _owner_id(c, role):
    return {"seo": c.get("seo_id"), "am": c.get("am_id"), "dev": c.get("dev_id"), "writer": c.get("writer_id")}.get(role)


def save_flags(client_id, c, flags):
    t = now()
    open_rows = {r["fkey"]: r for r in db.q("SELECT * FROM cc_flags WHERE client_id=? AND resolved_at IS NULL", (client_id,))}
    keep = set()
    for f in flags:
        keep.add(f["fkey"])
        ev = json.dumps(f["evidence"]) if f.get("evidence") else None
        if f["fkey"] in open_rows:
            db.x("UPDATE cc_flags SET code=?, severity=?, reason=?, owner=?, owner_id=?, last_seen=?, evidence=? WHERE id=?",
                 (f["code"], f["severity"], f["reason"], f["owner"], _owner_id(c, f["owner"]), t, ev, open_rows[f["fkey"]]["id"]))
        else:
            db.x("""INSERT INTO cc_flags (client_id, fkey, code, severity, reason, owner, owner_id, first_seen, last_seen, evidence)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (client_id, f["fkey"], f["code"], f["severity"], f["reason"], f["owner"],
                                                     _owner_id(c, f["owner"]), t, t, ev))
    for k, r in open_rows.items():
        if k not in keep:
            db.x("UPDATE cc_flags SET resolved_at=? WHERE id=?", (t, r["id"]))


def recompute(client_id):
    ctx = context(client_id)
    if not ctx:
        return None
    flags = health.sort_flags(health.evaluate(ctx))
    save_flags(client_id, ctx["client"], flags)
    col = health.colour(flags, ctx["client"]["status"])
    top = flags[0]["reason"] if flags else ("Paused" if col == "paused" else "Everything owed is on track and every source is fresh")
    db.x("UPDATE cc_clients SET health=?, health_reason=?, health_at=? WHERE id=?", (col, top[:300], now(), client_id))
    return col


def recompute_all():
    for r in db.q("SELECT id FROM cc_clients"):
        recompute(r["id"])
