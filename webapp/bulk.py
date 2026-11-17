"""Bulk Analysis: audit up to BULK_MAX builder websites from one CSV.

Each row becomes a normal audit (all screens, Strategy, PDF, Loom notes) tagged with the batch. The job runner gives
single audits priority and runs at most BULK_PARALLEL bulk audits at a time. Batches can be paused, resumed,
cancelled and failed rows retried; the person who started it gets an email when it finishes.
"""
import csv
import datetime
import io
import json
import math
import os
import re
import secrets
import time
import uuid

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for

from aeo import budget, engines
from . import auth, db, jobs, status
from .app import PUBLIC_URL, app, normalize_url, permission_required, security_event

BULK_MAX = int(os.environ.get("BULK_MAX", "100"))
MINUTES = {"quick": 1.2, "full": 2.0}           # typical minutes per audit, per parallel slot
QUESTIONS = {"quick": 6, "full": 26}            # questions asked per audit (for the cost estimate)
COLS = {"url": ("website", "url", "domain", "site", "web", "homepage"), "name": ("builder", "name", "company", "business"),
        "city": ("city", "town"), "state": ("state", "st", "region"), "notes": ("notes", "note", "comments")}


# ---------------------------------------------------------------- CSV
def parse_rows(text):
    """Rows from a CSV (with or without a header) or a pasted list of websites. Returns [{url, name, city, state, notes, line}]."""
    text = (text or "").lstrip("﻿").strip()
    if not text:
        return []
    sample = text[:4000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [c.strip().lower() for c in rows[0]]
    idx = {}
    for key, names in COLS.items():
        for i, h in enumerate(head):
            if any(h == n or h.startswith(n) for n in names) and i not in idx.values():
                idx[key] = i
                break
    has_header = "url" in idx and not re.search(r"\.[a-z]{2,}", rows[0][idx["url"]].lower())
    if not has_header:  # no header: the first column that looks like a website is the URL
        idx = {"url": next((i for i, c in enumerate(rows[0]) if re.search(r"[a-z0-9-]+\.[a-z]{2,}", c.lower())), 0)}
        body, start = rows, 1
    else:
        body, start = rows[1:], 2
    out = []
    for n, r in enumerate(body):
        get = lambda k: (r[idx[k]].strip() if k in idx and idx[k] < len(r) else "")  # noqa: E731
        out.append({"url": get("url"), "name": get("name")[:120], "city": get("city")[:60], "state": get("state")[:30],
                    "notes": get("notes")[:300], "line": n + start})
    return out


def check_rows(rows):
    """Flags each row: ok / invalid / duplicate / recent (audited in the last 30 days)."""
    seen = set()
    for r in rows:
        u = normalize_url(r["url"])
        r["norm"] = u
        r["domain"] = re.sub(r"^www\.", "", u.split("/")[2]) if u else ""
        if not u:
            r["flag"] = "invalid"
        elif r["domain"] in seen:
            r["flag"] = "duplicate"
        else:
            seen.add(r["domain"])
            recent = db.q("SELECT id, created_at FROM audits WHERE domain=? AND status='done' AND created_at>? ORDER BY created_at DESC LIMIT 1",
                          (r["domain"], time.time() - 30 * 86400), one=True)
            r["flag"] = "recent" if recent else "ok"
            r["recent_id"] = recent["id"] if recent else None
    return rows


def estimate(n, depth):
    live = [e for e in engines.ENGINES if engines.is_configured(e) and status.current().get(e, {}).get("state") != "bad"]
    minutes = math.ceil(n / max(1, jobs.BULK_PARALLEL)) * MINUTES[depth]
    cost = round(n * budget.per_question(live) * QUESTIONS[depth], 2)
    return {"minutes": int(math.ceil(minutes)), "cost": cost}


def fmt_minutes(m):
    return f"{m} min" if m < 60 else f"{m // 60} h {m % 60:02d} min"


app.jinja_env.filters["mins"] = fmt_minutes


# ---------------------------------------------------------------- pages
def _batch_or_404(bid):
    b = db.q("SELECT b.*, u.name AS owner FROM batches b JOIN users u ON u.id=b.user_id WHERE b.id=?", (bid,), one=True)
    if not b or (b["user_id"] != g.user["id"] and not auth.can(g.user, "team_audits")):
        abort(404)
    return b


def _counts(bid):
    r = db.q("""SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) AS running,
                SUM(CASE WHEN status='queued' THEN 1 ELSE 0 END) AS queued FROM audits WHERE batch_id=?""", (bid,), one=True)
    return {k: (r[k] or 0) for k in ("total", "done", "failed", "running", "queued")}


@app.route("/bulk")
@permission_required("bulk")
def bulk_home():
    rows = db.q("""SELECT b.*, u.name AS owner FROM batches b JOIN users u ON u.id=b.user_id ORDER BY b.created_at DESC LIMIT 50""")
    batches = []
    for b in rows:
        if b["user_id"] != g.user["id"] and not auth.can(g.user, "team_audits"):
            continue
        batches.append(dict(b, **_counts(b["id"])))
    return render_template("bulk.html", batches=batches, bulk_max=BULK_MAX)


@app.route("/bulk/sample.csv")
@permission_required("bulk")
def bulk_sample():
    body = ("website,builder,city,state,notes\n"
            "justindoylehomes.com,Justin Doyle Homes,Cincinnati,OH,Met at home show\n"
            "chrisgormanhomes.com,Chris Gorman Homes,,,\n"
            "examplebuilder.com,,,,City and state are optional\n")
    return app.response_class(body, mimetype="text/csv", headers={"Content-Disposition": 'attachment; filename="bulk-analysis-sample.csv"'})


@app.route("/bulk/preview", methods=["POST"])
@permission_required("bulk")
def bulk_preview():
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
    rows = check_rows(parse_rows(text))
    if not rows:
        flash("No websites found. Upload a CSV with a “website” column, or paste one website per line.", "bad")
        return redirect(url_for("bulk_home"))
    over = max(0, len([r for r in rows if r["flag"] in ("ok", "recent")]) - BULK_MAX)
    usable = [r for r in rows if r["flag"] in ("ok", "recent")][:BULK_MAX]
    est = {d: estimate(len(usable), d) for d in ("quick", "full")}
    est_new = {d: estimate(len([r for r in usable if r["flag"] == "ok"]), d) for d in ("quick", "full")}
    payload = json.dumps([{k: r[k] for k in ("url", "name", "city", "state", "notes", "flag")} for r in usable])
    return render_template("bulk_preview.html", rows=rows, usable=usable, over=over, est=est, est_new=est_new, payload=payload,
                           bulk_max=BULK_MAX, default_name="Bulk analysis · " + datetime.date.today().strftime("%b %d").replace(" 0", " "),
                           counts={k: sum(1 for r in rows if r["flag"] == k) for k in ("ok", "recent", "duplicate", "invalid")})


@app.route("/bulk/start", methods=["POST"])
@permission_required("bulk")
def bulk_start():
    try:
        rows = json.loads(request.form.get("rows") or "[]")
    except ValueError:
        abort(400)
    depth = "quick"  # change 35: Bulk always runs Quick audits (no brand questions, no re-asks), to keep big lists cheap
    skip_recent = bool(request.form.get("skip_recent"))
    rows = check_rows([{"url": str(r.get("url", ""))[:300], "name": str(r.get("name", ""))[:120], "city": str(r.get("city", ""))[:60],
                        "state": str(r.get("state", ""))[:30], "notes": str(r.get("notes", ""))[:300]} for r in rows if isinstance(r, dict)])
    rows = [r for r in rows if r["flag"] == "ok" or (r["flag"] == "recent" and not skip_recent)][:BULK_MAX]
    if not rows:
        flash("Nothing to run: every website was invalid, a duplicate, or audited recently and skipped.", "bad")
        return redirect(url_for("bulk_home"))
    est = estimate(len(rows), depth)
    cap = budget.status()
    if cap.get("limit_usd") is not None and est["cost"] > max(0.0, cap["limit_usd"] - cap["spent_usd"]):
        flash(("This batch would go over the spending cap " + (f"(about ${est['cost']:.2f} needed, ${max(0.0, cap['limit_usd'] - cap['spent_usd']):.2f} left). "
               "Raise the cap on the Admin page or run fewer sites." if auth.can(g.user, "see_costs") else
               "Ask an admin to raise the spending cap, or run fewer sites.")), "bad")
        return redirect(url_for("bulk_home"))
    bid = "b" + uuid.uuid4().hex[:11]
    name = (request.form.get("name") or "").strip()[:80] or "Bulk analysis"
    now = time.time()
    db.x("INSERT INTO batches (id, user_id, name, depth, status, total, created_at) VALUES (?,?,?,?, 'running', ?, ?)",
         (bid, g.user["id"], name, depth, len(rows), now))
    from aeo.engines import region_name
    for i, r in enumerate(rows):
        st = r["state"].strip()
        hint = "AU" if r["domain"].endswith(".au") else ("CA" if r["domain"].endswith(".ca") else None)  # change 60: WA / NT
        st_name = region_name(st, hint) or None if st else None
        db.x("""INSERT INTO audits (id, user_id, url, domain, scope, city, state, test_mode, depth, status, stage, progress, share_token,
                created_at, batch_id) VALUES (?,?,?,?, 'root', ?, ?, ?, 'quick', 'queued', 'Waiting to start', 0, ?, ?, ?)""",
             (uuid.uuid4().hex[:12], g.user["id"], r["norm"], r["domain"], r["city"] or None, st_name if r["city"] else None,
              1 if depth == "quick" else 0, secrets.token_urlsafe(18), now + i * 0.001, bid))
    security_event("bulk_started", g.user["email"], f"{len(rows)} sites, {depth}")
    jobs.kick(PUBLIC_URL or request.host_url, times=jobs.BULK_PARALLEL)
    return redirect(url_for("bulk_run", bid=bid))


@app.route("/bulk/<bid>")
@permission_required("bulk")
def bulk_run(bid):
    b = _batch_or_404(bid)
    can_control = b["user_id"] == g.user["id"] or auth.can(g.user, "admin")
    return render_template("bulk_run.html", b=b, c=_counts(bid), can_control=can_control)


def _rows(bid):
    out = []
    for a in db.q("SELECT * FROM audits WHERE batch_id=? ORDER BY created_at", (bid,)):
        s = json.loads(a["summary"]) if a["summary"] else {}
        out.append({"id": a["id"], "domain": a["domain"], "name": a["builder_name"], "status": a["status"], "progress": a["progress"],
                    "stage": a["stage"], "error": a["error"], "score": a["score"], "mentioned": a["mentioned"], "answers": a["answers"],
                    "pack": a["pack"], "pack_of": a["pack_of"], "market": a["market"], "checks": f"{a['checks_passed']}/{a['checks_total']}"
                    if a["checks_total"] else None, "opportunity": s.get("opportunity"), "top": s.get("top_competitor"),
                    "demand": s.get("demand"), "url": url_for("audit_view", aid=a["id"], view="overview") if a["status"] == "done"
                    else url_for("audit_page", aid=a["id"])})
    return out


@app.route("/api/bulk/<bid>")
@permission_required("bulk")
def bulk_status(bid):
    b = _batch_or_404(bid)
    c = _counts(bid)
    left = c["queued"] + c["running"]
    eta = math.ceil(left / max(1, jobs.BULK_PARALLEL)) * MINUTES[b["depth"]] if left and b["status"] == "running" else 0
    if b["status"] == "running" and left:  # serverless: nothing working on this batch right now -> start ticks
        busy = db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id=? AND status='running' AND lease_until>?", (bid, time.time()), one=True)["n"]
        if not busy:
            jobs.kick(PUBLIC_URL or request.host_url, times=jobs.BULK_PARALLEL)
    return jsonify({"status": b["status"], "counts": c, "eta": fmt_minutes(int(eta)) if eta else None, "rows": _rows(bid)})


@app.route("/bulk/<bid>/action", methods=["POST"])
@permission_required("bulk")
def bulk_action(bid):
    b = _batch_or_404(bid)
    if b["user_id"] != g.user["id"] and not auth.can(g.user, "admin"):
        abort(403)
    act = request.form.get("action")
    if act == "pause" and b["status"] == "running":
        db.x("UPDATE batches SET status='paused' WHERE id=?", (bid,))
        flash("Paused. Audits already running finish; the rest wait until you resume.", "good")
    elif act == "resume" and b["status"] == "paused":
        db.x("UPDATE batches SET status='running' WHERE id=?", (bid,))
        jobs.kick(PUBLIC_URL or request.host_url, times=jobs.BULK_PARALLEL)
        flash("Resumed.", "good")
    elif act == "cancel" and b["status"] in ("running", "paused"):
        db.x("UPDATE audits SET status='failed', error='Cancelled with the bulk analysis.', lease_until=NULL, finished_at=? "
             "WHERE batch_id=? AND status IN ('queued','running')", (time.time(), bid))
        db.x("UPDATE batches SET status='done', finished_at=? WHERE id=?", (time.time(), bid))
        flash("Cancelled. Finished audits are kept.", "good")
    elif act == "retry":
        n = db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id=? AND status='failed'", (bid,), one=True)["n"]
        if n:
            db.x("""UPDATE audits SET status='queued', stage='Waiting to start', progress=0, error=NULL, phase=NULL, attempts=0,
                    lease_until=NULL, activity=NULL, engines=NULL WHERE batch_id=? AND status='failed'""", (bid,))
            db.x("UPDATE batches SET status='running', finished_at=NULL, notified=0 WHERE id=?", (bid,))
            jobs.kick(PUBLIC_URL or request.host_url, times=jobs.BULK_PARALLEL)
            flash(f"Retrying {n} site{'s' if n != 1 else ''}.", "good")
    return redirect(url_for("bulk_run", bid=bid))


@app.route("/bulk/<bid>/export.csv")
@permission_required("bulk")
def bulk_export(bid):
    b = _batch_or_404(bid)
    base = (PUBLIC_URL or request.host_url).rstrip("/")
    rows = sorted(_rows(bid), key=lambda r: -(r["opportunity"] if r["opportunity"] is not None else -1))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Builder", "Website", "Market", "Status", "AI visibility (0-100)", "Named in AI answers", "Maps 3-pack",
                "Site checks passed", "Opportunity (0-100)", "Top competitor named instead", "Monthly local searches", "Report"])
    for r in rows:
        w.writerow([r["name"] or "", r["domain"], r["market"] or "", r["status"], "" if r["score"] is None else r["score"],
                    f"{r['mentioned']} of {r['answers']}" if r["answers"] else "", f"{r['pack']} of {r['pack_of']}" if r["pack_of"] else "",
                    r["checks"] or "", "" if r["opportunity"] is None else r["opportunity"], r["top"] or "", r["demand"] or "",
                    base + r["url"]])
    name = re.sub(r"[^A-Za-z0-9]+", "-", b["name"] or "bulk-analysis").strip("-") or "bulk-analysis"
    return app.response_class("﻿" + buf.getvalue(), mimetype="text/csv",
                              headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
