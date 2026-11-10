"""Prospect to-do list: each salesperson pastes or uploads the builders they plan to approach.

Statuses move on by themselves:
- To do: added.
- Audited: a finished audit of that website exists (by the list's owner, started after it was added).
- Done: the owner exported a PDF for that builder (Full report or Overview only). Viewing an audit doesn't count.
Done rows stay on the list, ticked, until "Clear completed". Admins can view anyone's list.
"""
import datetime
import secrets
import time
import uuid

from flask import abort, flash, g, redirect, render_template, request, url_for

from aeo import scope as scopes
from . import auth, db, jobs
from .app import PUBLIC_URL, app, login_required, normalize_url
from .bulk import parse_rows

MAX_ROWS = 300


def _root(domain):
    return scopes.root_of((domain or "").lower().replace("www.", "", 1))


def mark_exported(user_id, audit_row, via):
    """Called when someone exports a PDF: tick matching open rows on *their* list."""
    root = _root(audit_row["domain"])
    if not root:
        return 0
    n = 0
    for t in db.q("SELECT id, domain FROM todos WHERE user_id=? AND cleared=0 AND status!='done'", (user_id,)):
        if _root(t["domain"]) == root:
            db.x("UPDATE todos SET status='done', done_via=?, done_at=?, audit_id=? WHERE id=?", (via, time.time(), audit_row["id"], t["id"]))
            n += 1
    return n


def _owner():
    """Whose list we're looking at: your own, or (admins) anyone's via ?user=."""
    uid = g.user["id"]
    if auth.can(g.user, "admin") and request.args.get("user", "").isdigit():
        uid = int(request.args["user"])
    return uid


def _rows(uid, when, show):
    since = {"today": _day_start(), "week": time.time() - 7 * 86400}.get(when)
    sql, args = "SELECT * FROM todos WHERE user_id=? AND cleared=0", [uid]
    if since:
        sql += " AND created_at>=?"; args.append(since)
    rows = [dict(r) for r in db.q(sql + " ORDER BY created_at, id", args)]
    audits = db.q("SELECT id, domain, created_at, builder_name, score FROM audits WHERE user_id=? AND status='done' ORDER BY created_at DESC", (uid,))
    latest = {}
    for a in audits:
        latest.setdefault(_root(a["domain"]), a)
    for r in rows:
        a = latest.get(_root(r["domain"]))
        r["audit"] = dict(a) if a else None
        if r["status"] == "todo" and a and a["created_at"] >= r["created_at"] - 60:
            r["status_view"] = "audited"
        else:
            r["status_view"] = r["status"]
        r["running"] = bool(db.q("SELECT 1 FROM audits WHERE user_id=? AND domain=? AND status IN ('queued','running') LIMIT 1",
                                 (uid, r["domain"]), one=True)) if r["status"] != "done" else False
    if show in ("todo", "done"):
        rows = [r for r in rows if (r["status_view"] == "done") == (show == "done")]
    return rows


def _day_start():
    d = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return d.timestamp()


@app.route("/todo")
@login_required
def todo_home():
    uid = _owner()
    when = request.args.get("when", "all") if request.args.get("when") in ("today", "week", "all") else "all"
    show = request.args.get("show", "all") if request.args.get("show") in ("todo", "done", "all") else "all"
    rows = _rows(uid, when, show)
    allrows = _rows(uid, when, "all")
    counts = {"total": len(allrows), "done": sum(1 for r in allrows if r["status_view"] == "done"),
              "audited": sum(1 for r in allrows if r["status_view"] == "audited")}
    owner = db.q("SELECT id, name FROM users WHERE id=?", (uid,), one=True)
    people = db.q("SELECT id, name FROM users WHERE active=1 AND pending=0 ORDER BY name") if auth.can(g.user, "admin") else []
    return render_template("todo.html", rows=rows, counts=counts, when=when, show=show, owner=owner, people=people,
                           mine=uid == g.user["id"])


@app.route("/todo/add", methods=["POST"])
@login_required
def todo_add():
    f = request.files.get("csv")
    text = ""
    if f and f.filename:
        raw = f.read(2 * 1024 * 1024)
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
    text = text or request.form.get("paste", "")
    rows = parse_rows(text)[:MAX_ROWS]
    have = {_root(r["domain"]) for r in db.q("SELECT domain FROM todos WHERE user_id=? AND cleared=0", (g.user["id"],))}
    added = skipped = bad = 0
    now = time.time()
    for i, r in enumerate(rows):
        u = normalize_url(r["url"])
        if not u:
            bad += 1; continue
        dom = u.split("/")[2].replace("www.", "", 1)
        if _root(dom) in have:
            skipped += 1; continue
        have.add(_root(dom))
        db.x("INSERT INTO todos (user_id, domain, url, name, notes, status, created_at) VALUES (?,?,?,?,?, 'todo', ?)",
             (g.user["id"], dom, u, r["name"] or None, r["notes"] or None, now + i * 0.001))
        added += 1
    if not rows:
        flash("No websites found. Paste one website per line, or upload a CSV with a “website” column.", "bad")
    else:
        flash(f"Added {added} prospect{'s' if added != 1 else ''}"
              + (f" · {skipped} already on your list" if skipped else "") + (f" · {bad} not a website" if bad else "") + ".", "good" if added else "bad")
    return redirect(url_for("todo_home"))


def _mine(tid):
    t = db.q("SELECT * FROM todos WHERE id=?", (tid,), one=True)
    if not t or (t["user_id"] != g.user["id"] and not auth.can(g.user, "admin")):
        abort(404)
    return t


@app.route("/todo/<int:tid>/toggle", methods=["POST"])
@login_required
def todo_toggle(tid):
    t = _mine(tid)
    if t["status"] == "done":
        db.x("UPDATE todos SET status='todo', done_via=NULL, done_at=NULL WHERE id=?", (tid,))
    else:
        db.x("UPDATE todos SET status='done', done_via='manual', done_at=? WHERE id=?", (time.time(), tid))
    return redirect(request.referrer or url_for("todo_home"))


@app.route("/todo/<int:tid>/delete", methods=["POST"])
@login_required
def todo_delete(tid):
    _mine(tid)
    db.x("DELETE FROM todos WHERE id=?", (tid,))
    return redirect(request.referrer or url_for("todo_home"))


@app.route("/todo/clear", methods=["POST"])
@login_required
def todo_clear():
    db.x("UPDATE todos SET cleared=1 WHERE user_id=? AND status='done' AND cleared=0", (g.user["id"],))
    flash("Cleared the completed prospects.", "good")
    return redirect(url_for("todo_home"))


@app.route("/todo/<int:tid>/run", methods=["POST"])
@login_required
def todo_run(tid):
    """Start an audit for this prospect (same rules as the dashboard: daily limit, scope = root domain)."""
    from .app import DAILY_LIMIT
    t = _mine(tid)
    today = db.q("SELECT COUNT(*) AS n FROM audits WHERE user_id=? AND created_at>? AND batch_id IS NULL",
                 (g.user["id"], time.time() - 86400), one=True)["n"]
    if today >= DAILY_LIMIT:
        flash(f"You've run {DAILY_LIMIT} audits in the last 24 hours, the daily limit. Ask an admin to raise it.", "bad")
        return redirect(url_for("todo_home"))
    sc = scopes.make(t["url"], "root")
    aid = uuid.uuid4().hex[:12]
    depth = "full" if request.form.get("depth") == "full" else "quick"
    db.x("""INSERT INTO audits (id, user_id, url, domain, scope, test_mode, depth, status, stage, progress, share_token, created_at)
            VALUES (?,?,?,?, 'root', ?, ?, 'queued', 'Waiting to start', 0, ?, ?)""",
         (aid, g.user["id"], t["url"], sc["domain"], 1 if depth == "quick" else 0, depth, secrets.token_urlsafe(18), time.time()))
    db.x("UPDATE todos SET audit_id=? WHERE id=?", (aid, tid))
    jobs.kick(PUBLIC_URL or request.host_url)
    return redirect(url_for("audit_page", aid=aid))
