"""Copy the Command Center setup from one copy of the app to another (change 77): e.g. localhost -> the live site.

What travels: clients (name, website, package, status, go-live, scope doc, ClickUp folder/space, profile and Analytics
choices, owners), people and their roles, versioned scope, special rules, notes, change history, logos (with the
background choice) and the tracking start month.
What doesn't: anything a sync reads again (ClickUp tasks, deliverables, metrics, flags, the sync log) and any secret
(API keys, sign-ins, tokens): those stay in each copy's own settings.

Matching on import: a client by ClickUp folder, else by name; a person by ClickUp id, else email, else name. Existing
records are updated, missing ones added, nothing is deleted except a client's scope rows, which are replaced by the
file's (scope is versioned, so the file carries the whole history). Every change is written to the change history.
"""
import base64
import json

from aeo import kv
from webapp import db
from . import data
from .core import ITEMS, now, root_domain, track_from

KIND = "hbm-command-center-setup"
VERSION = 1
MAX_BYTES = 25_000_000
CLIENT_FIELDS = ["name", "domain", "package", "status", "go_live", "scope_doc", "clickup_folder", "clickup_space",
                 "gbp_how", "gbp_location", "ga4_how", "ga4_property"]
ROLE_FIELDS = {"am": "am_id", "seo": "seo_id", "writer": "writer_id", "dev": "dev_id"}
ITEM_KEYS = {k for k, _ in ITEMS}


class BadFile(Exception):
    pass


def _person_key(p):
    return ("cu:" + str(p["clickup_id"])) if p.get("clickup_id") else ("em:" + p["email"].lower()) if p.get("email") else ("nm:" + p["name"].strip().lower())


# ---------------------------------------------------------------- export
def export(by):
    people = [data.d(p) for p in db.q("SELECT * FROM cc_people ORDER BY id")]
    keys = {p["id"]: _person_key(p) for p in people}
    out_people = [{"key": keys[p["id"]], "name": p["name"], "email": p["email"], "roles": p["roles"] or "", "clickup_id": p["clickup_id"],
                   "active": p["active"]} for p in people]
    clients = []
    for c in db.q("SELECT * FROM cc_clients ORDER BY id"):
        c, cid = data.d(c), c["id"]
        logo = db.q("SELECT * FROM cc_logos WHERE client_id=?", (cid,), one=True)
        clients.append({
            **{f: c.get(f) for f in CLIENT_FIELDS},
            "owners": {role: keys.get(c.get(col)) for role, col in ROLE_FIELDS.items()},
            "scope": [{k: r[k] for k in ("item", "qty", "start_month", "end_month", "set_by", "set_at")}
                      for r in db.q("SELECT * FROM cc_scope WHERE client_id=? ORDER BY item, start_month", (cid,))],
            "rules": [{k: r[k] for k in ("kind", "pattern", "note", "active", "set_by", "set_at")}
                      for r in db.q("SELECT * FROM cc_rules WHERE client_id=? ORDER BY id", (cid,))],
            "notes": [{k: r[k] for k in ("kind", "body", "author", "created_at")}
                      for r in db.q("SELECT * FROM cc_notes WHERE client_id=? ORDER BY id", (cid,))],
            "history": [{k: r[k] for k in ("entity", "entity_id", "field", "old", "new", "by_email", "at")}
                        for r in db.q("SELECT * FROM cc_history WHERE client_id=? ORDER BY id", (cid,))],
            "logo": ({"mime": logo["mime"], "content": base64.b64encode(bytes(logo["content"])).decode() if logo["content"] else None,
                      "source_url": logo["source_url"], "how": logo["how"], "bg": logo["bg"], "note": logo["note"],
                      "fetched_at": logo["fetched_at"]} if logo else None),
        })
    return {"kind": KIND, "version": VERSION, "exported_at": now(), "exported_by": by, "track_from": track_from(),
            "people": out_people, "clients": clients}


# ---------------------------------------------------------------- import
def parse(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise BadFile("That file is empty or too large.")
    try:
        payload = json.loads(raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeDecodeError):
        raise BadFile("That isn't a Command Center setup file (it isn't readable JSON).")
    if not isinstance(payload, dict) or payload.get("kind") != KIND:
        raise BadFile("That isn't a Command Center setup file.")
    if payload.get("version") != VERSION:
        raise BadFile(f"This file is from a different version of the Command Center (version {payload.get('version')}).")
    if not isinstance(payload.get("clients"), list) or not isinstance(payload.get("people"), list):
        raise BadFile("The setup file is incomplete.")
    for c in payload["clients"]:
        if not isinstance(c, dict) or not (c.get("name") or "").strip():
            raise BadFile("A client in the file has no name.")
        for s in c.get("scope") or []:
            if s.get("item") not in ITEM_KEYS or not isinstance(s.get("qty"), int):
                raise BadFile(f"Scope for {c['name']} has an unknown item or quantity.")
    return payload


def _match_client(c):
    if c.get("clickup_folder"):
        r = db.q("SELECT * FROM cc_clients WHERE clickup_folder=?", (str(c["clickup_folder"]),), one=True)
        if r:
            return data.d(r)
    r = db.q("SELECT * FROM cc_clients WHERE LOWER(name)=?", (c["name"].strip().lower(),), one=True)
    return data.d(r) if r else None


def _match_person(p):
    if p.get("clickup_id"):
        r = db.q("SELECT * FROM cc_people WHERE clickup_id=?", (str(p["clickup_id"]),), one=True)
        if r:
            return data.d(r)
    if p.get("email"):
        r = db.q("SELECT * FROM cc_people WHERE LOWER(email)=?", (p["email"].lower(),), one=True)
        if r:
            return data.d(r)
    r = db.q("SELECT * FROM cc_people WHERE LOWER(name)=?", ((p.get("name") or "").strip().lower(),), one=True)
    return data.d(r) if r else None


def preview(payload):
    """What an import would do, without changing anything."""
    new_c, upd_c, new_p = [], [], 0
    for c in payload["clients"]:
        (upd_c if _match_client(c) else new_c).append(c["name"])
    for p in payload["people"]:
        new_p += not _match_person(p)
    return {"clients": len(payload["clients"]), "new_clients": new_c, "updated_clients": upd_c,
            "people": len(payload["people"]), "new_people": new_p,
            "scope_rows": sum(len(c.get("scope") or []) for c in payload["clients"]),
            "rules": sum(len(c.get("rules") or []) for c in payload["clients"]),
            "notes": sum(len(c.get("notes") or []) for c in payload["clients"]),
            "logos": sum(1 for c in payload["clients"] if (c.get("logo") or {}).get("content")),
            "track_from": payload.get("track_from"), "exported_at": payload.get("exported_at"), "exported_by": payload.get("exported_by")}


def apply(payload, by):
    t = now()
    label = f"Setup import by {by}"
    pid = {}
    for p in payload["people"]:
        ex = _match_person(p)
        if ex:
            roles = sorted({x for x in ((ex["roles"] or "") + "," + (p.get("roles") or "")).split(",") if x})
            sets = {"roles": ",".join(r for r in ("am", "seo", "writer", "dev") if r in roles)}
            if not ex["email"] and p.get("email"):
                sets["email"] = p["email"]
            if not ex["clickup_id"] and p.get("clickup_id"):
                sets["clickup_id"] = str(p["clickup_id"])
            db.x(f"UPDATE cc_people SET {', '.join(k + '=?' for k in sets)} WHERE id=?", (*sets.values(), ex["id"]))
            pid[p["key"]] = ex["id"]
        else:
            pid[p["key"]] = db.x("INSERT INTO cc_people (name, email, roles, clickup_id, active, created_at) VALUES (?,?,?,?,?,?)",
                                 (p["name"], p.get("email"), p.get("roles") or "", str(p["clickup_id"]) if p.get("clickup_id") else None,
                                  1 if p.get("active", 1) else 0, t))
    done = {"clients_added": 0, "clients_updated": 0, "scope_rows": 0, "rules": 0, "notes": 0, "logos": 0, "history": 0}
    for c in payload["clients"]:
        ex = _match_client(c)
        vals = {f: c.get(f) for f in CLIENT_FIELDS}
        vals["domain"] = root_domain(vals["domain"]) or None
        vals["gbp_how"] = vals["gbp_how"] or "auto"
        vals["ga4_how"] = vals["ga4_how"] or "auto"
        for role, col in ROLE_FIELDS.items():
            vals[col] = pid.get((c.get("owners") or {}).get(role))
        if ex:
            cid = ex["id"]
            changed = {k: v for k, v in vals.items() if (ex.get(k) or None) != (v or None)}
            for k, v in changed.items():
                data.log_change(cid, "client", cid, k, ex.get(k), v, label)
            if changed:
                db.x(f"UPDATE cc_clients SET {', '.join(k + '=?' for k in changed)}, updated_at=? WHERE id=?", (*changed.values(), t, cid))
            done["clients_updated"] += 1
        else:
            cols = list(vals) + ["created_at", "updated_at"]
            cid = db.x(f"INSERT INTO cc_clients ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", (*vals.values(), t, t))
            data.log_change(cid, "client", cid, "added", None, vals["name"], label)
            done["clients_added"] += 1
        # scope: the file carries the full versioned history, so it replaces this client's rows
        if c.get("scope") is not None:
            db.x("DELETE FROM cc_scope WHERE client_id=?", (cid,))
            for s in c["scope"]:
                db.x("INSERT INTO cc_scope (client_id, item, qty, start_month, end_month, set_by, set_at) VALUES (?,?,?,?,?,?,?)",
                     (cid, s["item"], s["qty"], s["start_month"], s.get("end_month"), s.get("set_by"), s.get("set_at") or t))
                done["scope_rows"] += 1
        have_rules = {(r["kind"], r["note"], r["pattern"]) for r in db.q("SELECT kind, note, pattern FROM cc_rules WHERE client_id=?", (cid,))}
        for r in c.get("rules") or []:
            if (r.get("kind"), r.get("note"), r.get("pattern")) in have_rules:
                continue
            db.x("INSERT INTO cc_rules (client_id, kind, pattern, note, active, set_by, set_at) VALUES (?,?,?,?,?,?,?)",
                 (cid, r.get("kind") or "custom", r.get("pattern"), r.get("note"), 1 if r.get("active", 1) else 0, r.get("set_by"), r.get("set_at") or t))
            done["rules"] += 1
        have_notes = {(n["body"], round(n["created_at"] or 0)) for n in db.q("SELECT body, created_at FROM cc_notes WHERE client_id=?", (cid,))}
        for n in c.get("notes") or []:
            if (n.get("body"), round(n.get("created_at") or 0)) in have_notes or not n.get("body"):
                continue
            db.x("INSERT INTO cc_notes (client_id, kind, body, author, created_at) VALUES (?,?,?,?,?)",
                 (cid, n.get("kind") or "note", n["body"], n.get("author"), n.get("created_at") or t))
            done["notes"] += 1
        have_hist = {(h["field"], h["new"], round(h["at"] or 0)) for h in db.q("SELECT field, new, at FROM cc_history WHERE client_id=?", (cid,))}
        for h in c.get("history") or []:
            if (h.get("field"), h.get("new"), round(h.get("at") or 0)) in have_hist:
                continue
            db.x("INSERT INTO cc_history (client_id, entity, entity_id, field, old, new, by_email, at) VALUES (?,?,?,?,?,?,?,?)",
                 (cid, h.get("entity") or "client", h.get("entity_id"), h.get("field"), h.get("old"), h.get("new"), h.get("by_email"), h.get("at") or t))
            done["history"] += 1
        lg = c.get("logo") or {}
        cur = db.q("SELECT how FROM cc_logos WHERE client_id=?", (cid,), one=True)
        if lg.get("content") and not (cur and cur["how"] == "upload"):  # a logo someone uploaded here is never replaced
            from .sources import logo as logo_src
            blob = base64.b64decode(lg["content"])
            if logo_src.sniff(blob) == lg.get("mime"):
                db.upsert("cc_logos", {"client_id": cid, "content": blob, "mime": lg["mime"], "source_url": lg.get("source_url"),
                                       "how": lg.get("how") or "auto", "bg": lg.get("bg") or "auto", "note": lg.get("note"),
                                       "fetched_at": lg.get("fetched_at") or t}, ["client_id"])
                done["logos"] += 1
    if payload.get("track_from") and payload["track_from"] != track_from():
        old = track_from()
        kv.update("cc_track_from", lambda _o: payload["track_from"], "")
        data.log_change(None, "setting", "track_from", "tracking starts", old, payload["track_from"], label)
    data.log_change(None, "setup", "import", "imported", None,
                    f"{done['clients_added']} added, {done['clients_updated']} updated", label)
    data.recompute_all()
    return done
