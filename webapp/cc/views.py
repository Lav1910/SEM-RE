"""Command Center pages (change 64). Every route checks access server-side and answers "not found" to anyone else.
Read-only towards ClickUp, client sites and GBP: the only outside calls here are the explicit "Load folders from ClickUp"
button (GET requests) and sync jobs queued by the "Sync now" buttons.
"""
import functools
import json
import re
import secrets
import time

from flask import abort, flash, g, redirect, render_template, request, session, url_for

from aeo import kv
from webapp import db, jobs
from webapp.app import PUBLIC_URL, app
from . import data, health, ledger, sync
from .core import (EVERY, SYNC_SOURCES, CLIENT_STATUSES, GRID_ITEMS, ITEM_LABEL, ITEM_SHORT, ITEMS, PACKAGES, ROLES, SOURCES, STATUS_LABEL, STATUSES,
                   access_emails, add_months, can_edit, can_open, enabled, gbp_live, month_label, reply_days, track_from, month_of, now, parse_date, root_domain, today, ts_date)
from .sources import clickup


def cc_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not can_open(getattr(g, "user", None)):
            abort(404)
        return f(*a, **k)
    return w


app.jinja_env.globals["cc_can_open"] = lambda: can_open(getattr(g, "user", None))

# change 78: viewers see everything and may refresh data; only owners change anything
VIEWER_POSTS = {"cc_sync", "cc_client_sync"}  # "Sync now" only reads the sources again
OWNER_GETS = {"cc_gbp_connect", "cc_gbp_callback", "cc_ga4_connect", "cc_ga4_callback", "cc_export", "cc_transfer"}


@app.before_request
def _cc_view_only():
    ep = request.endpoint or ""
    if not ep.startswith("cc_"):
        return None
    user = getattr(g, "user", None)
    if not can_open(user) or can_edit(user):
        return None  # no access at all is answered by cc_required (404); owners may do anything
    if (request.method == "POST" and ep not in VIEWER_POSTS) or ep in OWNER_GETS:
        flash("You have view-only access to the Command Center, so nothing was changed. Ask Lav if this needs doing.", "bad")
        back = request.referrer or ""
        return redirect(back if back.startswith(request.host_url) else url_for("cc_portfolio"))
    return None


def _me():
    return g.user["email"]


def _kick():
    if jobs.SERVERLESS:
        jobs.kick(PUBLIC_URL or request.host_url)


@app.context_processor
def _cc_ctx():
    if not request.path.startswith("/delivery"):
        return {}
    return {"ITEMS": ITEMS, "ITEM_LABEL": ITEM_LABEL, "ITEM_SHORT": ITEM_SHORT, "GRID_ITEMS": GRID_ITEMS, "STATUS_LABEL": STATUS_LABEL,
            "STATUSES": STATUSES, "PACKAGES": PACKAGES, "CLIENT_STATUSES": CLIENT_STATUSES, "ROLES": ROLES, "SOURCES": SOURCES,
            "month_label": month_label, "fmt_date": _fmt_date, "fmt_ago": _fmt_ago, "cc_tab": request.endpoint,
            "cc_nav": _nav, "cc_track_from": track_from, "cc_edit": can_edit(getattr(g, "user", None))}


SPACE_ORDER = ["Growth", "Organic", "Scale"]  # as the team reads them in ClickUp; any other space follows, A to Z
# change 84: package badges are outlined in the package's colour (ring and letter), on the dark side menu
SPACE_COLOUR = {"Growth": "#F0B429", "Organic": "#EC6A62", "Scale": "#45C487", "Essentials": "#6AA8F0", "One Off": "#9FB7B9"}
SPACE_DEFAULT = "#9FB7B9"


def _space_name(raw):
    name = re.sub(r"\s+package$", "", (raw or "").strip(), flags=re.I)
    return name or "No ClickUp space"


def _nav():
    """The Delivery side menu: clients grouped by their ClickUp space (Growth, Organic, Scale ...), like ClickUp's sidebar."""
    rows = [dict(r) for r in db.q("SELECT id, name, health, clickup_space FROM cc_clients WHERE status='active' ORDER BY LOWER(name)")]
    groups = {}
    for r in rows:
        groups.setdefault(_space_name(r["clickup_space"]), []).append(r)
    order = sorted(groups, key=lambda g: (g == "No ClickUp space", SPACE_ORDER.index(g) if g in SPACE_ORDER else 99, g.lower()))
    spaces = [{"name": g, "key": re.sub(r"[^a-z0-9]+", "-", g.lower()).strip("-"), "colour": SPACE_COLOUR.get(g, SPACE_DEFAULT),
               "clients": groups[g], "red": sum(1 for c in groups[g] if c["health"] == "red")} for g in order]
    return {"clients": rows, "spaces": spaces, "red": sum(1 for r in rows if r["health"] == "red")}


def _fmt_date(ts_or_date, year=False):
    if not ts_or_date:
        return "—"
    if isinstance(ts_or_date, str):
        d = parse_date(ts_or_date)
        if not d:
            return "—"
    else:
        d = ts_date(ts_or_date) if isinstance(ts_or_date, (int, float)) else ts_or_date
    return f"{d:%b} {d.day}, {d.year}" if year or d.year != today().year else f"{d:%b} {d.day}"


def _fmt_ago(ts):
    if not ts:
        return "never"
    s = now() - ts
    if s < 3600:
        return f"{max(1, int(s / 60))} min ago"
    if s < 86400:
        return f"{int(s / 3600)} h ago"
    return f"{int(s / 86400)} days ago"


# ---------------------------------------------------------------- shaping data for the screens
def _person(P, pid):
    return (P.get(pid) or {}).get("name") if pid else None


def _row(ctx, P):
    c, t = ctx["client"], ctx["today"]
    cur = month_of(t)
    gbp_ok = gbp_live(ctx["sources"])
    cells = ledger.month_row(c["go_live"], ctx["scope"], ctx["deliverables"], cur, t, GRID_ITEMS, gbp_ok, ctx.get("track_from"))
    flags = health.sort_flags(ctx["flags"])
    blogs = [x for x in ctx["deliverables"] if x["item"] == "blog" and x["verified_at"] and not x["not_ours"] and not x["merged_into"]]
    last_blog = max((x["published_at"] or x["verified_at"] for x in blogs), default=None)
    home = ctx["metrics"].get(("wordpress", "home_status", "latest"))
    fails = ctx["metrics"].get(("wordpress", "home_fails", "latest"))
    aeo = data.aeo_latest(c["domain"])
    src_off = [k for k, _, _ in SOURCES if (ctx["sources"].get(k) or {}).get("state") in (None, "not_connected", "error", "blocked")]
    setup = setup_checklist(c, ctx["scope"], cur)
    status = status_line(flags, setup)
    return {"c": c, "cells": cells, "flags": flags, "top": ({**flags[0], "reason": flag_view(flags[0])["title"]} if flags else None),
            "health": c["health"] or "amber",
            "setup": setup, "status": status, "initials": initials(c["name"]),
            "am": _person(P, c["am_id"]), "seo": _person(P, c["seo_id"]), "last_blog": last_blog,
            "home": int(home["value"]) if home and home["value"] is not None else None, "home_fails": int(fails["value"] or 0) if fails else 0,
            "home_at": home["fetched_at"] if home else None, "aeo": aeo, "src_off": src_off,
            "wp_state": (ctx["sources"].get("wordpress") or {}).get("state"),
            "reds": sum(1 for f in flags if f["severity"] == "red"), "ambers": sum(1 for f in flags if f["severity"] == "amber")}


SETUP_CODES = {"scope_missing", "owner_missing", "am_missing", "golive_missing", "domain_missing"}
DATA_CODES = {"source_off", "source_error", "source_blocked", "source_partial", "stale"}


def setup_checklist(c, scope_rows, cur):
    scope_ok = all(ledger.scope_qty(scope_rows, i, cur) is not None for i, _ in ITEMS)
    items = [("Website", bool(c["domain"])), ("Package", bool(c["package"])),
             ("Account manager", bool(c["am_id"])), ("SEO owner", bool(c["seo_id"])), ("Monthly scope", scope_ok)]
    # the go-live date is optional (change 76): shown, never counted as missing
    return {"checks": items, "done": sum(ok for _, ok in items), "total": len(items),
            "missing": [n for n, ok in items if not ok], "go_live": bool(c["go_live"])}


def status_line(flags, setup):
    """One plain line for a tile: what kind of problem comes first (delivery, then setup, then data)."""
    delivery = [f for f in flags if f["code"] not in SETUP_CODES | DATA_CODES]
    if delivery:
        sev = "red" if any(f["severity"] == "red" for f in delivery) else "amber"
        return {"sev": sev, "text": f"Delivery: {len(delivery)} issue{'s' if len(delivery) != 1 else ''}", "kind": "delivery",
                "reason": flag_view(delivery[0])["title"] if delivery[0].get("fkey") else (delivery[0].get("reason") or "")}
    if setup["missing"]:
        return {"sev": "red" if any(f["severity"] == "red" for f in flags if f["code"] in SETUP_CODES) else "amber",
                "text": f"Setup {setup['done']}/{setup['total']}", "kind": "setup", "reason": "Missing: " + ", ".join(setup["missing"]).lower()}
    data_ = [f for f in flags if f["code"] in DATA_CODES]
    if data_:
        return {"sev": "amber", "text": f"Data: {len(data_)} source{'s' if len(data_) != 1 else ''} off", "kind": "data",
                "reason": flag_view(data_[0])["title"] if data_[0].get("fkey") else (data_[0].get("reason") or "")}
    return {"sev": "green", "text": "On track", "kind": "ok", "reason": "Everything owed is on track and every source is fresh"}


def initials(name):
    words = [w for w in re.split(r"[\s\-]+", name or "") if w and w[0].isalnum()]
    return ("".join(w[0] for w in words[:2]) or "?").upper()


def logos():
    """{client_id: {has, bg, v}}: which clients have a logo (blobs aren't loaded here)."""
    return {r["client_id"]: {"has": bool(r["has"]), "bg": r["bg"], "v": int(r["fetched_at"] or 0), "how": r["how"], "mime": r["mime"],
                             "note": r["note"], "source_url": r["source_url"]}
            for r in db.q("SELECT client_id, bg, fetched_at, how, note, source_url, mime, content IS NOT NULL AS has FROM cc_logos")}


SEV_RANK = {"red": 0, "amber": 1, "green": 2, "paused": 3, None: 1}


@app.route("/delivery")
@cc_required
def cc_portfolio():
    P = data.people()
    rows = [_row(ctx, P) for ctx in data.contexts(light=True).values()]
    f_am, f_owner, f_pkg, f_flag, f_status = (request.args.get(k, "") for k in ("am", "owner", "package", "flag", "status"))
    f_status = f_status or "active"
    allrows = rows
    if f_status != "all":
        rows = [r for r in rows if r["c"]["status"] == f_status]
    if f_am:
        rows = [r for r in rows if str(r["c"]["am_id"] or "none") == f_am]
    if f_owner:
        rows = [r for r in rows if str(r["c"]["seo_id"] or "none") == f_owner]
    if f_pkg:
        rows = [r for r in rows if (r["c"]["package"] or "none") == f_pkg]
    if f_flag:
        rows = [r for r in rows if any(fl["code"] == f_flag for fl in r["flags"])]
    f_sort = "worst" if request.args.get("sort") == "worst" else "az"  # change 88: A to Z unless asked for worst first
    if f_sort == "worst":
        rows.sort(key=lambda r: (SEV_RANK.get(r["health"], 1), -r["reds"], -r["ambers"], (r["c"]["name"] or "").lower()))
    else:
        rows.sort(key=lambda r: re.sub(r"[^a-z0-9]+", " ", (r["c"]["name"] or "").lower()).strip())
    active = [r for r in allrows if r["c"]["status"] == "active"]
    codes = lambda r: {fl["code"] for fl in r["flags"]}  # noqa: E731
    strip = {
        "red": sum(1 for r in active if r["health"] == "red"),
        "overdue": sum(sum(1 for fl in r["flags"] if fl["code"] in ("short", "report_late")) for r in active),
        "approvals": sum(sum(1 for fl in r["flags"] if fl["code"] in ("approval", "client_input")) for r in active),
        "scope": sum(1 for r in active if "scope_missing" in codes(r)),
        "sources": sum(1 for r in active if r["src_off"]),
        "total": len(active),
    }
    flag_codes = sorted({fl["code"] for r in active for fl in r["flags"]})
    last_sync = db.q("SELECT MAX(finished_at) AS t FROM cc_sync WHERE status='done'", one=True)["t"]
    running = db.q("SELECT COUNT(*) AS n FROM cc_sync WHERE status IN ('queued','running')", one=True)["n"]
    return render_template("cc_portfolio.html", rows=rows, strip=strip, P=P, flag_codes=flag_codes, month=month_of(today()), L=logos(),
                           f={"am": f_am, "owner": f_owner, "package": f_pkg, "flag": f_flag, "status": f_status, "sort": f_sort},
                           last_sync=last_sync, running=running, clickup_on=clickup.connected(), n_clients=len(allrows))


@app.route("/delivery/clients/<int:cid>")
@cc_required
def cc_client(cid):
    ctx = data.context(cid)
    if not ctx:
        abort(404)
    P = data.people()
    c, t = ctx["client"], ctx["today"]
    gbp_ok = gbp_live(ctx["sources"])
    start = ctx.get("track_from")
    months, grid = ledger.year_grid(c["go_live"], ctx["scope"], ctx["deliverables"], t, 12, gbp_ok, start)
    cur = month_of(t)
    scope_now = {i: ledger.scope_qty(ctx["scope"], i, cur) for i, _ in ITEMS}
    flags = []
    tasks_by_id = {x["task_id"]: x for x in ctx["tasks"]}
    for fl in health.sort_flags(ctx["flags"]):
        lvl, wd = health.escalation(fl["first_seen"], t)
        flags.append({**fl, "level": lvl, "days": wd, "owner_name": _flag_owner(fl, c, P), "v": flag_view(fl, tasks_by_id)})
    recent_from = max(add_months(cur, -2), ctx.get("track_from") or "")
    recent = [x for x in ctx["deliverables"] if x["month"] >= recent_from and not x["merged_into"]]
    recent.sort(key=lambda x: (x["month"], x["item"], x["published_at"] or 0), reverse=True)
    tasks = sorted(ctx["tasks"], key=lambda x: (x["status_type"] == "closed", x["due"] or 9e12))
    for x in tasks:
        x["people"] = ", ".join(a["name"] for a in json.loads(x["assignees"] or "[]")) or None
    rules = db.q("SELECT * FROM cc_rules WHERE client_id=? ORDER BY active DESC, set_at DESC", (cid,))
    notes = db.q("SELECT * FROM cc_notes WHERE client_id=? ORDER BY created_at DESC LIMIT 50", (cid,))
    hist = db.q("SELECT * FROM cc_history WHERE client_id=? ORDER BY at DESC LIMIT 80", (cid,))
    jobs_ = db.q("SELECT * FROM cc_sync WHERE client_id=? ORDER BY queued_at DESC LIMIT 8", (cid,))
    scope_hist = db.q("SELECT * FROM cc_scope WHERE client_id=? ORDER BY item, start_month DESC", (cid,))
    aeo = data.aeo_latest(c["domain"])
    # change 65: the client page is laid out by channel (content and SEO, Search Console, Business Profile, Analytics)
    gbp_months = months[-6:] if not start else months
    gbp_grid = {i: [ledger.cell(c["go_live"], ctx["scope"], ctx["deliverables"], i, mo, t, gbp_ok, start) for mo in gbp_months]
                for i in GBP_ITEMS}
    sections = {
        "content": [x for x in recent if x["item"] == "blog"],
        "gbp": [x for x in recent if x["item"] in GBP_ITEMS],
        "report": [x for x in recent if x["item"] == "report"],
    }
    gbp_tasks = [x for x in tasks if x["item"] in GBP_ITEMS]
    # every post known on the site (feed or sitemap, before tracking too), newest first: the blog cards
    site_posts = [data.d(p) for p in db.q("""SELECT id, title, url, published_at, month, verified_at, check_note, image_url, source
        FROM cc_deliverables WHERE client_id=? AND item='blog' AND source IN ('wp','sitemap') AND merged_into IS NULL AND not_ours=0
        AND url IS NOT NULL ORDER BY COALESCE(published_at, 0) DESC, CASE WHEN month='baseline' THEN 0 ELSE 1 END DESC, month DESC, id DESC
        LIMIT 300""", (cid,))]
    return render_template("cc_client.html", ctx=ctx, c=c, P=P, months=months, grid=grid, scope_now=scope_now, flags=flags,
                           recent=recent, tasks=tasks, rules=rules, notes=notes, hist=hist, jobs=jobs_, scope_hist=scope_hist,
                           aeo=aeo, cur=cur, row=_row(ctx, P), now_ts=now(), logo=logos().get(cid),
                           gsc=gsc_view(ctx), gbp_profile=gbp_from_aeo(aeo), gbp_months=gbp_months, gbp_grid=gbp_grid,
                           gbp=gbp_view(ctx), gbp_dir=_gbp_directory(), gbp_ok=gbp_ok,
                           ga=ga4_view(ctx), ga4_dir=_ga4_directory(), bg=bing_view(ctx),
                           gbp_cfg=_gbp_mod().configured(), gbp_conn=_gbp_mod().stored(), ga4_conn=_ga4().stored(),
                           GBP_ITEMS=GBP_ITEMS, sections=sections, gbp_tasks=gbp_tasks, site_posts=site_posts,
                           src={k: ctx["sources"].get(k) or {} for k, _, _ in SOURCES}, cc_client_id=cid,
                           recent_from=recent_from, track_from=start, reply_days=reply_days(c["package"]),
                           flags_by=_flags_by_section(flags))


GBP_ITEMS = ("gbp_post", "gbp_photo", "review_reply")
SECTION_OF_FLAG = {"gsc": "gsc", "gbp": "gbp", "ga4": "ga4", "gbp_unverified": "gbp", "gbp_closed": "gbp", "gbp_website": "gbp",
                   "gbp_reviews": "gbp", "gbp_no_post": "gbp", "ga4_no_data": "ga4", "ga4_no_key_events": "ga4",
                   "bing": "bing", "wordpress": "content", "blog": "content", "site_down": "content", "closed_no_url": "content",
                   "gbp_post": "gbp", "gbp_photo": "gbp", "review_reply": "gbp"}


def _flags_by_section(flags):
    """Open flags per dashboard section, from the flag key (source:gsc, short:blog, site_down ...)."""
    out = {}
    for f in flags:
        tail = f["fkey"].split(":")[-1]
        sec = SECTION_OF_FLAG.get(tail) or SECTION_OF_FLAG.get(f["fkey"])
        if sec:
            out.setdefault(sec, []).append(f)
    return out


def gsc_view(ctx):
    """The stored Search Console summary, ready for the page (None when there's no data)."""
    from .sources import gsc
    m = ctx["metrics"].get(("gsc", "overview", "latest"))
    try:
        s = json.loads(m["text"]) if m and m["text"] else None
    except ValueError:
        s = None
    if not s:
        return None
    s["fetched_at"] = m["fetched_at"]
    s["console"] = gsc.console_url(s.get("site_url") or "")
    s["max_clicks"] = max((d["clicks"] or 0 for d in s.get("daily") or []), default=0)
    s["max_impressions"] = max((d["impressions"] or 0 for d in s.get("daily") or []), default=0)
    s["tagged_pages"] = sum(1 for p in s.get("top_pages") or [] if p.get("tagged"))
    return s


def _json_metric(ctx, source, metric):
    row = ctx["metrics"].get((source, metric, "latest"))
    try:
        return (json.loads(row["text"]), row["fetched_at"]) if row and row["text"] else (None, None)
    except ValueError:
        return None, None


def bing_view(ctx):
    s, at = _json_metric(ctx, "bing", "summary")
    if not s:
        return None
    s["fetched_at"] = at
    s["max_clicks"] = max((d["clicks"] for d in s.get("daily") or []), default=0)
    s["max_impressions"] = max((d["impressions"] for d in s.get("daily") or []), default=0)
    return s


def _ga4_directory():
    from . import ga4_sync
    return ga4_sync.directory()


def ga4_view(ctx):
    """The stored Analytics summary, ready for the page (None until a property is matched and read)."""
    row = ctx["metrics"].get(("ga4", "summary", "latest"))
    try:
        sm = json.loads(row["text"]) if row and row["text"] else None
    except ValueError:
        sm = None
    if not sm:
        return None
    sm["fetched_at"] = row["fetched_at"]
    sm["daily30"] = (sm.get("daily") or [])[-30:]
    sm["max_organic"] = max((d["organic"] for d in sm["daily30"]), default=0)
    sm["max_all"] = max((d["all"] for d in sm["daily30"]), default=0)
    pid = (sm.get("property") or "").split("/")[-1]
    sm["ga_url"] = f"https://analytics.google.com/analytics/web/#/p{pid}/reports/intelligenthome" if pid else None
    return sm


def _gbp_directory():
    from . import gbp_sync
    return gbp_sync.directory()


def gbp_view(ctx):
    """Live Business Profile numbers stored by the sync (None until a profile is matched and read)."""
    def js(m):
        row = ctx["metrics"].get(("gbp", m, "latest"))
        try:
            return json.loads(row["text"]) if row and row["text"] else None
        except ValueError:
            return None
    prof = js("profile")
    if not prof:
        return None
    daily, rv = js("daily") or [], js("reviews") or {}
    last30, prev30 = daily[-30:], daily[-60:-30]

    def total(rows, k):
        return sum(r.get(k) or 0 for r in rows) if rows else None
    keys = ("calls", "website", "directions", "views", "conversations")
    out = {"profile": prof, "reviews": rv, "daily": last30, "days": len(last30),
           "d30": {k: total(last30, k) for k in keys},
           "prev": {k: (total(prev30, k) if len(prev30) == 30 else None) for k in keys},
           "max": {k: max((r.get(k) or 0 for r in last30), default=0) for k in keys},
           "through": last30[-1]["date"] if last30 else None,
           "fetched_at": (ctx["metrics"].get(("gbp", "profile", "latest")) or {}).get("fetched_at")}
    if rv.get("oldest_waiting"):
        out["oldest_days"] = int((now() - rv["oldest_waiting"]) / 86400)
    return out


def gbp_from_aeo(aeo):
    """Profile basics (rating, reviews, category, photos) from the latest AEO Snapshot audit, dated. No new lookups."""
    if not aeo:
        return None
    try:
        from webapp import store
        a = store.load_json(aeo["id"]) or {}
    except Exception:  # noqa: BLE001  (missing or unreadable audit file: just no snapshot)
        return None
    gp = (a.get("profile") or {}).get("gbp") or ((a.get("datawise") or {}).get("gbp"))
    if not gp or not gp.get("title"):
        return None
    return {**gp, "audit_id": aeo["id"], "at": aeo["finished_at"]}


ROLE_SHORT = {"am": "AM", "seo": "SEO", "dev": "Developer", "writer": "Writer"}
ROLE_LONG = {"am": "account manager", "seo": "SEO owner", "dev": "developer", "writer": "writer"}


def _flag_owner(fl, c, P):
    if fl["owner"] == "you":
        return "You"
    pid = data._owner_id(c, fl["owner"])
    return f"{_person(P, pid)} · {ROLE_SHORT.get(fl['owner'], fl['owner'])}" if pid else f"No {ROLE_LONG.get(fl['owner'], fl['owner'])} named"


# where each kind of problem is fixed: (tab or page, button label)
ITEM_TAB = {"blog": "t-content", "gbp_post": "t-gbp", "gbp_photo": "t-gbp", "review_reply": "t-gbp", "report": "t-tasks"}
SOURCE_TAB = {"gsc": "t-gsc", "gbp": "t-gbp", "ga4": "t-ga4", "bing": "t-bing", "wordpress": "t-content", "clickup": "t-tasks"}


def flag_view(fl, tasks_by_id=None):
    """A flag as the screens show it: a short title, a plain detail line, and the place it gets fixed."""
    code, reason, key = fl["code"], (fl["reason"] or "").strip(), fl["fkey"]
    tail = key.split(":", 1)[1] if ":" in key else ""
    title, detail, fix, label = reason, "", None, None

    def split(text, sep=": "):
        a, _, b = text.partition(sep)
        return (a, b) if b else (text, "")
    if code == "scope_missing":
        title, detail = "Scope isn't written down", reason.split(": ", 1)[-1].replace(" (nothing is written down)", "").capitalize()
        fix, label = "#t-setup", "Fill in scope"
    elif code == "owner_missing":
        title, detail, fix, label = "No SEO owner named", "Nobody is answerable for this client's delivery", "#t-setup", "Name an owner"
    elif code == "am_missing":
        title, detail, fix, label = "No account manager named", "", "#t-setup", "Name an AM"
    elif code == "golive_missing":
        title, detail, fix, label = "Go-live date missing", "Owed months can't be counted from launch", "#t-setup", "Add the date"
    elif code == "domain_missing":
        title, detail, fix, label = "No website recorded", "Nothing can be checked live without it", "#t-setup", "Add the website"
    elif code == "no_task":
        lbl = ITEM_LABEL.get(tail, tail)
        title = f"No ClickUp task for {lbl if lbl.startswith('GBP') else lbl.lower()} this month"
        detail, fix, label = "Without a task nothing is due, so nothing can show as overdue", "#t-tasks", "See tasks"
    elif code == "short":
        title, _, rest = reason.partition(" (")
        detail = ("Also " + rest.rstrip(")").replace("also ", "")) if rest else "Last month fell short of what's owed"
        fix, label = "#" + ITEM_TAB.get(tail, "t-overview"), "See delivery"
    elif code == "unverified":
        title, detail = f"{ITEM_LABEL.get(tail, tail)} marked done but not verified", reason.split(": ", 1)[-1]
        fix, label = "#" + ITEM_TAB.get(tail, "t-overview"), "Check them"
    elif code == "report_late":
        title, detail, fix, label = reason.replace(" not confirmed sent by the 7th", " not marked sent"), "Reports are due by the 7th", "#t-tasks", "See the report"
    elif code in ("overdue_tasks", "unassigned"):
        detail = "Open ClickUp tasks past their due date" if code == "overdue_tasks" else "Deliverable tasks nobody is assigned to"
        fix, label = "#t-tasks", "See tasks"
    elif code in ("approval", "client_input"):
        title, detail = split(reason)
        t = (tasks_by_id or {}).get(tail)
        fix, label = (t["url"], "Open in ClickUp") if t and t.get("url") else ("#t-tasks", "See tasks")
    elif code == "closed_no_url":
        detail, fix, label = "Closed in ClickUp, but no matching post is live on the site", "#t-content", "See blog posts"
    elif code == "site_down":
        title, detail, fix, label = "Homepage isn't responding", reason, "#t-content", "See website"
    elif code in ("source_off", "source_error", "source_partial", "source_blocked", "stale"):
        title, detail = split(reason)
        title = title.replace(" not connected", " isn't connected").replace(" sync failing", " sync is failing")
        if "quota" in detail:
            title, detail = title, "Waiting for Google to approve API access"
        fix, label = "#" + SOURCE_TAB.get(tail, "t-notes"), "See why"
    elif code.startswith("gbp_"):
        title, detail = split(reason)
        fix, label = "#t-gbp", "Open Business Profile"
    elif code.startswith("ga4_"):
        title, detail = split(reason)
        fix, label = "#t-ga4", "Open Analytics"
    brand = ("clickup" if code in ("no_task", "overdue_tasks", "unassigned", "approval", "client_input", "closed_no_url", "report_late")
             else tail if code in ("source_off", "source_error", "source_partial", "source_blocked", "stale") and tail in ("clickup", "gsc", "gbp", "ga4", "bing", "wordpress")
             else "gbp" if code.startswith("gbp_") else "ga4" if code.startswith("ga4_") else None)
    return {"title": title.strip().rstrip("."), "detail": detail.strip(), "fix": fix, "fix_label": label,
            "external": bool(fix and fix.startswith("http")), "brand": brand}


# ---------------------------------------------------------------- client edits (all logged)
CLIENT_FIELDS = ["name", "domain", "package", "status", "am_id", "seo_id", "writer_id", "dev_id", "scope_doc", "go_live", "clickup_folder"]


def _clean_field(k, v):
    v = (v or "").strip()
    if k.endswith("_id"):
        return int(v) if v.isdigit() else None
    if k == "domain":
        return root_domain(v) or None
    if k == "go_live":
        d = parse_date(v)
        return d.isoformat() if d else None
    if k == "status":
        return v if v in CLIENT_STATUSES else "active"
    if k == "scope_doc" and v and not v.startswith(("http://", "https://")):
        return None
    return v or None


def _save_client(cid, form, fields=CLIENT_FIELDS):
    c = db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)
    changes = {}
    for k in fields:
        if k not in form:
            continue
        v = _clean_field(k, form.get(k))
        if k == "name" and not v:
            continue
        if (c[k] or None) != v:
            changes[k] = v
            data.log_change(cid, "client", cid, k, c[k], v, _me())
    if changes:
        sets = ", ".join(f"{k}=?" for k in changes)
        db.x(f"UPDATE cc_clients SET {sets}, updated_at=? WHERE id=?", (*changes.values(), now(), cid))
    return changes


@app.route("/delivery/clients/<int:cid>/edit", methods=["POST"])
@cc_required
def cc_client_edit(cid):
    if not db.q("SELECT 1 FROM cc_clients WHERE id=?", (cid,), one=True):
        abort(404)
    ch = _save_client(cid, request.form)
    if ch:
        data.recompute(cid)
        flash(f"Saved {len(ch)} change{'s' if len(ch) != 1 else ''}.", "good")
    return redirect(url_for("cc_client", cid=cid) + "#details")


def set_scope(cid, item, qty, by):
    """Change one item's monthly quantity. History is kept: the old row ends last month, the new one starts this month.
    The very first scope for an item starts at go-live (or this month), so past months are judged by it too."""
    cur = month_of(today())
    rows = db.q("SELECT * FROM cc_scope WHERE client_id=? AND item=?", (cid, item))
    old = ledger.scope_qty(rows, item, cur)
    if old == qty:
        return False
    if qty is None:  # cleared: scope is missing again from this month
        for r in rows:
            if not r["end_month"] or r["end_month"] >= cur:
                if r["start_month"] >= cur:
                    db.x("DELETE FROM cc_scope WHERE id=?", (r["id"],))
                else:
                    db.x("UPDATE cc_scope SET end_month=? WHERE id=?", (add_months(cur, -1), r["id"]))
    elif not rows:
        c = db.q("SELECT go_live FROM cc_clients WHERE id=?", (cid,), one=True)
        start = (c["go_live"] or "")[:7] or cur
        db.x("INSERT INTO cc_scope (client_id, item, qty, start_month, set_by, set_at) VALUES (?,?,?,?,?,?)", (cid, item, qty, min(start, cur), by, now()))
    else:
        for r in rows:
            if r["start_month"] >= cur:
                db.x("DELETE FROM cc_scope WHERE id=?", (r["id"],))
            elif not r["end_month"] or r["end_month"] >= cur:
                db.x("UPDATE cc_scope SET end_month=? WHERE id=?", (add_months(cur, -1), r["id"]))
        db.x("INSERT INTO cc_scope (client_id, item, qty, start_month, set_by, set_at) VALUES (?,?,?,?,?,?)", (cid, item, qty, cur, by, now()))
    data.log_change(cid, "scope", item, ITEM_LABEL[item], old, qty, by)
    return True


def _qty(v):
    v = (v or "").strip()
    if v == "":
        return None
    if v == "every":
        return EVERY
    try:
        return max(0, min(99, int(float(v))))
    except ValueError:
        return "bad"


@app.route("/delivery/clients/<int:cid>/scope", methods=["POST"])
@cc_required
def cc_client_scope(cid):
    if not db.q("SELECT 1 FROM cc_clients WHERE id=?", (cid,), one=True):
        abort(404)
    n = 0
    for i, _ in ITEMS:
        q = _qty(request.form.get(i))
        if q != "bad":
            n += set_scope(cid, i, q, _me())
    if n:
        data.recompute(cid)
        flash("Scope saved.", "good")
    return redirect(url_for("cc_client", cid=cid) + "#scope")


@app.route("/delivery/scope", methods=["GET", "POST"])
@cc_required
def cc_scope():
    """Bulk editor: one row per client. Blank means 'not written down' (red), 0 means 'not in the package'."""
    if request.method == "POST":
        changed = set()
        for c in db.q("SELECT id FROM cc_clients"):
            cid = c["id"]
            if f"row-{cid}" not in request.form:
                continue
            for i, _ in ITEMS:
                q = _qty(request.form.get(f"{i}-{cid}"))
                if q != "bad" and set_scope(cid, i, q, _me()):
                    changed.add(cid)
            form = {k: request.form.get(f"{k}-{cid}") for k in ("package", "go_live", "am_id", "seo_id", "domain") if f"{k}-{cid}" in request.form}
            if _save_client(cid, form, list(form)):
                changed.add(cid)
        for cid in changed:
            data.recompute(cid)
        flash(f"Saved changes for {len(changed)} client{'s' if len(changed) != 1 else ''}." if changed else "Nothing changed.", "good")
        return redirect(url_for("cc_scope", status=request.args.get("status", "active")))
    status = request.args.get("status", "active")
    clients = db.q("SELECT * FROM cc_clients" + ("" if status == "all" else " WHERE status=?") + " ORDER BY name",
                   () if status == "all" else (status,))
    cur = month_of(today())
    scope = {}
    for r in db.q("SELECT * FROM cc_scope"):
        scope.setdefault(r["client_id"], []).append(r)
    grid = {c["id"]: {i: ledger.scope_qty(scope.get(c["id"], []), i, cur) for i, _ in ITEMS} for c in clients}
    return render_template("cc_scope.html", clients=clients, grid=grid, P=data.people(), status=status)


@app.route("/delivery/clients/<int:cid>/rules", methods=["POST"])
@cc_required
def cc_client_rules(cid):
    act = request.form.get("action")
    if act == "add":
        kind = request.form.get("kind", "custom")
        note = (request.form.get("note") or "").strip()[:300]
        pattern = (request.form.get("pattern") or "").strip()[:300] or None
        if note or pattern:
            rid = db.x("INSERT INTO cc_rules (client_id, kind, pattern, note, active, set_by, set_at) VALUES (?,?,?,?,1,?,?)",
                       (cid, kind, pattern, note, _me(), now()))
            data.log_change(cid, "rule", rid, "added", None, note or pattern, _me())
    elif act in ("off", "on"):
        rid = int(request.form.get("rule_id", "0") or 0)
        r = db.q("SELECT * FROM cc_rules WHERE id=? AND client_id=?", (rid, cid), one=True)
        if r:
            db.x("UPDATE cc_rules SET active=? WHERE id=?", (1 if act == "on" else 0, rid))
            data.log_change(cid, "rule", rid, "active", r["active"], 1 if act == "on" else 0, _me())
    return redirect(url_for("cc_client", cid=cid) + "#rules")


@app.route("/delivery/clients/<int:cid>/notes", methods=["POST"])
@cc_required
def cc_client_notes(cid):
    body = (request.form.get("body") or "").strip()[:4000]
    kind = "handover" if request.form.get("kind") == "handover" else "note"
    if body:
        db.x("INSERT INTO cc_notes (client_id, kind, body, author, created_at) VALUES (?,?,?,?,?)", (cid, kind, body, _me(), now()))
    return redirect(url_for("cc_client", cid=cid) + "#notes")


@app.route("/delivery/clients/<int:cid>/deliverables/<int:did>", methods=["POST"])
@cc_required
def cc_deliverable(cid, did):
    r = db.q("SELECT * FROM cc_deliverables WHERE id=? AND client_id=?", (did, cid), one=True)
    if not r:
        abort(404)
    act = request.form.get("action")
    if act == "verify":  # a hand check: who looked, when, and how
        how = (request.form.get("how") or "").strip()[:200] or "checked by hand"
        db.x("UPDATE cc_deliverables SET verified_at=?, verified_how=? WHERE id=?", (now(), f"Hand check by {_me()}: {how}", did))
        data.log_change(cid, "deliverable", did, "verified", None, how, _me())
    elif act == "unverify":
        db.x("UPDATE cc_deliverables SET verified_at=NULL, verified_how=NULL WHERE id=?", (did,))
        data.log_change(cid, "deliverable", did, "verified", r["verified_how"], None, _me())
    elif act in ("not_ours", "ours"):
        db.x("UPDATE cc_deliverables SET not_ours=? WHERE id=?", (1 if act == "not_ours" else 0, did))
        data.log_change(cid, "deliverable", did, "not ours", r["not_ours"], 1 if act == "not_ours" else 0, _me())
    elif act == "blocker":
        text = (request.form.get("blocker") or "").strip()[:300] or None
        owner = request.form.get("blocker_owner_id", "")
        db.x("UPDATE cc_deliverables SET blocker=?, blocker_owner_id=?, blocked_since=? WHERE id=?",
             (text, int(owner) if owner.isdigit() else None, (r["blocked_since"] or now()) if text else None, did))
        data.log_change(cid, "deliverable", did, "blocker", r["blocker"], text, _me())
    data.recompute(cid)
    return redirect(url_for("cc_client", cid=cid) + "#deliverables")


@app.route("/delivery/clients/<int:cid>/sync", methods=["POST"])
@cc_required
def cc_client_sync(cid):
    n = sum(sync.enqueue(cid, s, "manual") for s in SYNC_SOURCES)
    _kick()
    flash("Sync queued. It runs in the background; refresh in a minute." if n else "A sync for this client is already queued.", "good")
    return redirect(url_for("cc_client", cid=cid))


@app.route("/delivery/clients/new", methods=["POST"])
@cc_required
def cc_client_new():
    name = (request.form.get("name") or "").strip()[:200]
    if not name:
        flash("Give the client a name.", "bad")
        return redirect(url_for("cc_import"))
    cid = db.x("""INSERT INTO cc_clients (name, domain, package, status, created_at, updated_at) VALUES (?,?,?, 'active', ?, ?)""",
               (name, root_domain(request.form.get("domain")) or None, request.form.get("package") or None, now(), now()))
    data.log_change(cid, "client", cid, "created", None, name, _me())
    data.recompute(cid)
    return redirect(url_for("cc_client", cid=cid))


# ---------------------------------------------------------------- logos
LOGO_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml", "image/x-icon"}


@app.route("/delivery/clients/<int:cid>/logo")
@cc_required
def cc_logo(cid):
    r = db.q("SELECT content, mime FROM cc_logos WHERE client_id=?", (cid,), one=True)
    if not r or r["content"] is None or r["mime"] not in LOGO_TYPES:
        abort(404)
    resp = app.response_class(bytes(r["content"]), mimetype=r["mime"])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    # an SVG opened directly must not run anything
    resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; sandbox"
    return resp


# ---------------------------------------------------------------- blog post images (change 74)
POST_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}  # no SVG from other sites
POST_IMAGE_MAX = 4_000_000
_post_img_cache = {}  # did -> (url, bytes, mime): a small in-process cache, so a page reload doesn't refetch


@app.route("/delivery/clients/<int:cid>/posts/<int:did>/image")
@cc_required
def cc_post_image(cid, did):
    """A blog post's own featured/share image, fetched through the SSRF guard and served from here (the page's
    security policy only shows images from this app). Only the image URL saved for that post can be fetched."""
    import urllib.request
    from aeo import netsafe
    from .sources import logo as logo_src
    from .sources.wordpress import UA
    r = db.q("SELECT image_url FROM cc_deliverables WHERE id=? AND client_id=? AND item='blog'", (did, cid), one=True)
    if not r or not r["image_url"]:
        abort(404)
    hit = _post_img_cache.get(did)
    if hit and hit[0] == r["image_url"]:
        blob, mime = hit[1], hit[2]
    else:
        try:
            req = urllib.request.Request(r["image_url"], headers={"User-Agent": UA, "Accept": "image/avif,image/webp,image/*;q=0.8"})
            with netsafe.urlopen(req, timeout=15) as resp:
                blob = resp.read(POST_IMAGE_MAX + 1) if resp.status == 200 else b""
        except Exception:  # noqa: BLE001  (blocked address, timeout, error: the card shows its drawn cover instead)
            blob = b""
        mime = logo_src.sniff(blob)
        if not blob or len(blob) > POST_IMAGE_MAX or mime not in POST_IMAGE_TYPES:
            abort(404)
        if len(_post_img_cache) > 150:
            _post_img_cache.clear()
        _post_img_cache[did] = (r["image_url"], blob, mime)
    resp = app.response_class(blob, mimetype=mime)
    resp.headers["Cache-Control"] = "private, max-age=604800"
    resp.headers["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return resp


@app.route("/delivery/clients/<int:cid>/logo", methods=["POST"])
@cc_required
def cc_logo_edit(cid):
    from .sources import logo as logo_src
    c = db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True)
    if not c:
        abort(404)
    act = request.form.get("action")
    row = db.q("SELECT bg FROM cc_logos WHERE client_id=?", (cid,), one=True)
    bg = row["bg"] if row else "auto"
    if act == "upload":
        f = request.files.get("file")
        blob = f.read(logo_src.MAX_BYTES + 1) if f else b""
        mime = logo_src.sniff(blob)
        if len(blob) > logo_src.MAX_BYTES or mime not in LOGO_TYPES - {"image/x-icon"}:
            flash("Use a PNG, JPG, WebP, GIF or SVG under 1.5 MB.", "bad")
        else:
            db.upsert("cc_logos", {"client_id": cid, "content": blob, "mime": mime, "source_url": None, "how": "upload", "bg": bg,
                                   "note": f"Uploaded by {_me()}", "fetched_at": now()}, ["client_id"])
            data.log_change(cid, "logo", cid, "uploaded", None, (f.filename or "file")[:120], _me())
            flash("Logo saved.", "good")
    elif act == "refetch":
        found = sync.maybe_logo(dict(c), [0], force=True)
        data.log_change(cid, "logo", cid, "looked up on site", None, "found" if found else "not found", _me())
        flash("Found a logo on their site." if found else "No usable logo found on their homepage. Upload one instead.", "good" if found else "bad")
    elif act == "remove":
        db.upsert("cc_logos", {"client_id": cid, "content": None, "mime": None, "source_url": None, "how": "upload", "bg": bg,
                               "note": f"Removed by {_me()} (initials shown)", "fetched_at": now()}, ["client_id"])
        data.log_change(cid, "logo", cid, "removed", None, None, _me())
    elif act in ("bg-auto", "bg-light", "bg-dark"):
        if row:
            db.x("UPDATE cc_logos SET bg=? WHERE client_id=?", (act[3:], cid))
    return redirect(url_for("cc_client", cid=cid) + "#setup")


# ---------------------------------------------------------------- import from ClickUp
@app.route("/delivery/import", methods=["GET", "POST"])
@cc_required
def cc_import():
    if request.method == "POST" and request.form.get("action") == "load":
        try:
            calls = [0]
            folders = clickup.folders_for_import(calls)
            kv.update("cc_clickup_folders", lambda _: {"at": now(), "folders": folders, "calls": calls[0]}, {})
            flash(f"Loaded {len(folders)} folders from ClickUp ({calls[0]} read-only requests).", "good")
        except clickup.NotConnected:
            flash("ClickUp isn't connected: add CLICKUP_API_TOKEN to the server's settings first.", "bad")
        except clickup.ClickUpError as e:
            flash(str(e), "bad")
        return redirect(url_for("cc_import"))
    if request.method == "POST" and request.form.get("action") == "import":
        cache = kv.get("cc_clickup_folders", {}) or {}
        by_id = {f["folder_id"]: f for f in cache.get("folders", [])}
        made = 0
        for fid in request.form.getlist("folder"):
            f = by_id.get(fid)
            if not f or db.q("SELECT 1 FROM cc_clients WHERE clickup_folder=?", (fid,), one=True):
                continue
            pkg = request.form.get(f"package-{fid}") or f["package"] or None
            cid = db.x("""INSERT INTO cc_clients (name, package, status, clickup_folder, clickup_space, created_at, updated_at)
                          VALUES (?,?, 'active', ?, ?, ?, ?)""", (f["name"], pkg, fid, f["space"], now(), now()))
            data.log_change(cid, "client", cid, "imported from ClickUp", None, f"{f['space']} / {f['name']}", _me())
            sync.enqueue(cid, "clickup", "import")
            data.recompute(cid)
            made += 1
        _kick()
        flash(f"Imported {made} client{'s' if made != 1 else ''}. Their ClickUp tasks are syncing now; fill in domains and scope next."
              if made else "Nothing new to import.", "good")
        return redirect(url_for("cc_scope") if made else url_for("cc_import"))
    cache = kv.get("cc_clickup_folders", {}) or {}
    folders = cache.get("folders", [])
    have = {r["clickup_folder"]: r["id"] for r in db.q("SELECT id, clickup_folder FROM cc_clients WHERE clickup_folder IS NOT NULL")}
    names = {}
    for f in folders:
        names.setdefault(f["name"].lower(), []).append(f["folder_id"])
    spaces = {}
    for f in folders:
        spaces.setdefault(f["space"], []).append({**f, "imported": have.get(f["folder_id"]), "dupe": len(names[f["name"].lower()]) > 1})
    return render_template("cc_import.html", spaces=spaces, loaded_at=cache.get("at"), clickup_on=clickup.connected(),
                           n_clients=db.q("SELECT COUNT(*) AS n FROM cc_clients", one=True)["n"])


# ---------------------------------------------------------------- people and access
@app.route("/delivery/people", methods=["GET", "POST"])
@cc_required
def cc_people():
    if request.method == "POST":
        act = request.form.get("action")
        if act == "add":
            name = (request.form.get("name") or "").strip()[:120]
            if name:
                db.x("INSERT INTO cc_people (name, email, roles, active, created_at) VALUES (?,?,?,1,?)",
                     (name, (request.form.get("email") or "").strip()[:200] or None, ",".join(request.form.getlist("roles")), now()))
        elif act == "save":
            for p in db.q("SELECT * FROM cc_people"):
                if f"p-{p['id']}" not in request.form:
                    continue
                roles = ",".join(r for r, _ in ROLES if request.form.get(f"role-{r}-{p['id']}"))
                active = 1 if request.form.get(f"active-{p['id']}") else 0
                name = (request.form.get(f"name-{p['id']}") or p["name"]).strip()[:120] or p["name"]
                if (roles, active, name) != (p["roles"] or "", p["active"], p["name"]):
                    db.x("UPDATE cc_people SET roles=?, active=?, name=? WHERE id=?", (roles, active, name, p["id"]))
                    data.log_change(None, "person", p["id"], "roles/active/name", f"{p['name']}|{p['roles']}|{p['active']}",
                                    f"{name}|{roles}|{active}", _me())
        elif act in ("grant", "revoke"):
            uid = int(request.form.get("user_id", "0") or 0)
            u = db.q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True)
            if u and (u["is_admin"] or u["role"] == "admin"):
                db.x("UPDATE users SET cc_access=? WHERE id=?", (1 if act == "grant" else 0, uid))
                data.log_change(None, "access", uid, u["email"], None, act, _me())
        flash("Saved.", "good")
        return redirect(url_for("cc_people"))
    P = db.q("SELECT * FROM cc_people ORDER BY active DESC, name")
    owns = {}
    for c in db.q("SELECT id, name, am_id, seo_id, writer_id, dev_id FROM cc_clients WHERE status='active'"):
        for role in ("am", "seo", "writer", "dev"):
            if c[f"{role}_id"]:
                owns.setdefault(c[f"{role}_id"], []).append((role, c["name"], c["id"]))
    admins = db.q("SELECT id, email, name, cc_access FROM users WHERE active=1 AND (is_admin=1 OR role='admin') ORDER BY name")
    return render_template("cc_people.html", people=P, owns=owns, admins=admins, env_emails=access_emails())


# ---------------------------------------------------------------- tracking start (change 71)
@app.route("/delivery/settings/tracking", methods=["POST"])
@cc_required
def cc_track_from():
    from aeo import kv as _kv
    v = (request.form.get("track_from") or "").strip()[:7]
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", v):
        flash("Pick a month to start tracking from.", "bad")
        return redirect(url_for("cc_scope"))
    old = track_from()
    if v != old:
        _kv.update("cc_track_from", lambda _o: v, "")
        data.log_change(None, "setting", "track_from", "tracking starts", old, v, _me())
        data.recompute_all()
        flash(f"Delivery is now judged from {month_label(v, True)}; earlier months are left out.", "good")
    return redirect(url_for("cc_scope"))


# ---------------------------------------------------------------- copy the setup between copies of the app (change 77)
PENDING_MAX_AGE = 30 * 60


def _pending_key():
    return f"cc_import_pending_{g.user['id']}"


@app.route("/delivery/setup", methods=["GET", "POST"])
@cc_required
def cc_transfer():
    from . import transfer
    if request.method == "POST":
        act = request.form.get("action")
        if act == "preview":
            f = request.files.get("file")
            try:
                payload = transfer.parse(f.read(transfer.MAX_BYTES + 1) if f else b"")
            except transfer.BadFile as e:
                flash(str(e), "bad")
                return redirect(url_for("cc_transfer"))
            kv.update(_pending_key(), lambda _o: {"at": time.time(), "payload": payload}, {})
            return redirect(url_for("cc_transfer"))
        if act == "cancel":
            kv.update(_pending_key(), lambda _o: {}, {})
            flash("Import cancelled. Nothing was changed.", "good")
            return redirect(url_for("cc_transfer"))
        if act == "apply":
            pend = kv.get(_pending_key()) or {}
            if not pend.get("payload") or time.time() - pend.get("at", 0) > PENDING_MAX_AGE:
                flash("That preview has expired. Upload the file again.", "bad")
                return redirect(url_for("cc_transfer"))
            done = transfer.apply(pend["payload"], _me())
            kv.update(_pending_key(), lambda _o: {}, {})
            sync.enqueue_all("setup imported")
            _kick()
            flash(f"Setup imported: {done['clients_added']} clients added, {done['clients_updated']} updated, {done['scope_rows']} scope rows, "
                  f"{done['rules']} rules, {done['notes']} notes, {done['logos']} logos. A full sync is running in the background.", "good")
            return redirect(url_for("cc_portfolio"))
        abort(400)
    pend = kv.get(_pending_key()) or {}
    prev = None
    if pend.get("payload") and time.time() - pend.get("at", 0) <= PENDING_MAX_AGE:
        prev = transfer.preview(pend["payload"])
    counts = {"clients": db.q("SELECT COUNT(*) AS n FROM cc_clients", one=True)["n"],
              "people": db.q("SELECT COUNT(*) AS n FROM cc_people", one=True)["n"],
              "scope": db.q("SELECT COUNT(*) AS n FROM cc_scope", one=True)["n"]}
    return render_template("cc_transfer.html", prev=prev, counts=counts)


@app.route("/delivery/setup/export")
@cc_required
def cc_export():
    from . import transfer
    body = json.dumps(transfer.export(_me()), separators=(",", ":"))
    data.log_change(None, "setup", "export", "exported", None, "setup file downloaded", _me())
    resp = app.response_class(body, mimetype="application/json")
    resp.headers["Content-Disposition"] = f"attachment; filename=command-center-setup-{today().isoformat()}.json"
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- first-time guide (change 69)
@app.route("/delivery/guide")
@cc_required
def cc_guide():
    return render_template("cc_guide.html", sources=SOURCES)


# ---------------------------------------------------------------- sync log
@app.route("/delivery/sync", methods=["GET", "POST"])
@cc_required
def cc_sync():
    if request.method == "POST":
        n = sync.enqueue_all("manual")
        _kick()
        flash(f"Queued {n} sync job{'s' if n != 1 else ''}." if n else "Everything is already queued.", "good")
        return redirect(url_for("cc_sync"))
    log_rows = db.q("""SELECT s.*, c.name AS client FROM cc_sync s LEFT JOIN cc_clients c ON c.id=s.client_id
                       ORDER BY s.queued_at DESC LIMIT 150""")
    states = {}
    for r in db.q("""SELECT s.source, s.state, COUNT(*) AS n FROM cc_sources s JOIN cc_clients c ON c.id=s.client_id
                     WHERE c.status='active' GROUP BY s.source, s.state"""):  # active clients only, so it can't read 31 of 28
        states.setdefault(r["source"], {})[r["state"]] = r["n"]
    n_active = db.q("SELECT COUNT(*) AS n FROM cc_clients WHERE status='active'", one=True)["n"]
    from . import gbp_sync
    from .sources import gbp
    return render_template("cc_sync.html", log=log_rows, states=states, n_active=n_active, clickup_on=clickup.connected(),
                           daily=kv.get("cc_daily", ""), gbp_cfg=gbp.configured(), gbp_conn=gbp.stored(), gbp_extra=gbp.ACC.extras(),
                           ga4_extra=_ga4().ACC.extras(),
                           gbp_dir=len(gbp_sync.directory()), gbp_dir_at=kv.get("cc_gbp_dir_at", 0), gbp_redirect=_gbp_redirect(),
                           ga4_conn=_ga4().stored(), ga4_dir=len(_ga4_directory()), ga4_dir_at=kv.get("cc_ga4_dir_at", 0),
                           ga4_redirect=_ga4_redirect(), bing_on=_bing().connected(),
                           bing_sites=len((kv.get("cc_bing_sites") or {}).get("sites") or []))


def _bing():
    from .sources import bing
    return bing


def _ga4():
    from .sources import ga4
    return ga4


def _gbp_mod():
    from .sources import gbp
    return gbp


# ---------------------------------------------------------------- Google Business Profile sign-in (change 66)
def _gbp_redirect():
    """Where Google sends the person back. PUBLIC_URL when set (production), otherwise the address in use (local)."""
    from webapp.mcp import base_url
    return (PUBLIC_URL or base_url()) + "/delivery/gbp/callback"


@app.route("/delivery/gbp/connect")
@cc_required
def cc_gbp_connect():
    """A link, not a form: the site's form-action 'self' policy (rightly) stops a form from redirecting to Google.
    Only a click from our own pages starts it (Sec-Fetch-Site), and it changes nothing until Google sends the person back."""
    from .sources import gbp
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        abort(403)
    if not gbp.configured():
        flash("Add the Google OAuth client ID and secret to the server's settings first.", "bad")
        return redirect(url_for("cc_sync"))
    verifier, challenge = gbp.pkce()
    state = secrets.token_urlsafe(24)
    session["cc_gbp_oauth"] = {"state": state, "verifier": verifier, "at": time.time(), **_extra_target()}
    return redirect(gbp.auth_url(_gbp_redirect(), state, challenge))


def _extra_target():
    """change 87: ?extra=1 adds another Google account beside the main one; ?client=<id> comes back to that client."""
    out = {"extra": request.args.get("extra") == "1"}
    cid = request.args.get("client", "")
    if cid.isdigit() and db.q("SELECT 1 FROM cc_clients WHERE id=?", (int(cid),), one=True):
        out["client"] = int(cid)
    return out


def _after_google(pending, source):
    """Where to land after a Google sign-in: the client it was started from, else the Sources page."""
    if pending.get("client"):
        sync.enqueue(pending["client"], source, "another Google account connected")
        return redirect(url_for("cc_client", cid=pending["client"]) + "#t-setup")
    return redirect(url_for("cc_sync"))


@app.route("/delivery/gbp/callback")
@cc_required
def cc_gbp_callback():
    from . import gbp_sync
    from .sources import gbp
    pending = session.pop("cc_gbp_oauth", None) or {}
    if request.args.get("error"):
        flash(f"Google sign-in was cancelled ({request.args.get('error')[:60]}). Nothing changed.", "bad")
        return redirect(url_for("cc_sync"))
    if not pending or not secrets.compare_digest(request.args.get("state", ""), pending.get("state", "")) \
            or time.time() - pending.get("at", 0) > 900:
        flash("That sign-in link was stale or didn't come from this page. Start again from Connect Business Profile.", "bad")
        return redirect(url_for("cc_sync"))
    try:
        refresh, email = gbp.exchange(request.args.get("code", ""), _gbp_redirect(), pending["verifier"])
    except gbp.GBPError as e:
        flash(str(e), "bad")
        return redirect(url_for("cc_sync"))
    extra = bool(pending.get("extra")) and gbp.stored() is not None
    gbp.save(refresh, email, _me(), extra=extra)
    data.log_change(None, "source", "gbp", "connected" + (" (extra account)" if extra else ""), None, email or "Google account", _me())
    calls = [0]
    try:
        n = gbp_sync.refresh_directory(calls, force=True)
        msg = (f"{email or 'That account'} added beside the main Business Profile account. " if extra else
               f"Business Profile connected{(' as ' + email) if email else ''}. ") + \
            f"{n} profile{'s' if n != 1 else ''} visible across the connected accounts; syncing now."
    except gbp.NotApproved as e:
        msg = f"Signed in{(' as ' + email) if email else ''}, but Google isn't serving data yet: {e}"
    except gbp.GBPError as e:
        msg = f"Signed in, but listing the profiles failed: {e}"
    sync.enqueue_all("gbp connected", sources=("gbp",))
    _kick()
    flash(msg, "good")
    return _after_google(pending, "gbp")


@app.route("/delivery/gbp/disconnect", methods=["POST"])
@cc_required
def cc_gbp_disconnect():
    from . import gbp_sync
    from .sources import gbp
    email = (request.form.get("email") or "").strip() or None  # change 87: one extra account, or the main one
    with gbp.use(email):
        revoked = gbp.revoke()
    gbp.forget(email)
    data.log_change(None, "source", "gbp", "connected", email or "main account", "disconnected", _me())
    if gbp.connected():
        try:
            gbp_sync.refresh_directory([0], force=True)
        except (gbp.GBPError, gbp.NotApproved, gbp.Reauth, gbp.NotConnected):
            pass
    flash(f"{email or 'Business Profile'} disconnected" + (" and Google's access revoked." if revoked else ". Also remove the app under that Google account's third-party access."), "good")
    return redirect(url_for("cc_sync"))


# ---------------------------------------------------------------- Google Analytics sign-in (change 67)
def _ga4_redirect():
    from webapp.mcp import base_url
    return (PUBLIC_URL or base_url()) + "/delivery/ga4/callback"


@app.route("/delivery/ga4/connect")
@cc_required
def cc_ga4_connect():
    from .sources import ga4, gbp
    if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        abort(403)
    if not ga4.configured():
        flash("Add the Google OAuth client ID and secret to the server's settings first.", "bad")
        return redirect(url_for("cc_sync"))
    verifier, challenge = gbp.pkce()
    state = secrets.token_urlsafe(24)
    session["cc_ga4_oauth"] = {"state": state, "verifier": verifier, "at": time.time(), **_extra_target()}
    return redirect(ga4.auth_url(_ga4_redirect(), state, challenge))


@app.route("/delivery/ga4/callback")
@cc_required
def cc_ga4_callback():
    from . import ga4_sync
    from .sources import ga4
    pending = session.pop("cc_ga4_oauth", None) or {}
    if request.args.get("error"):
        flash(f"Google sign-in was cancelled ({request.args.get('error')[:60]}). Nothing changed.", "bad")
        return redirect(url_for("cc_sync"))
    if not pending or not secrets.compare_digest(request.args.get("state", ""), pending.get("state", "")) \
            or time.time() - pending.get("at", 0) > 900:
        flash("That sign-in link was stale or didn't come from this page. Start again from Connect Analytics.", "bad")
        return redirect(url_for("cc_sync"))
    try:
        refresh, email = ga4.exchange(request.args.get("code", ""), _ga4_redirect(), pending["verifier"])
    except ga4.GA4Error as e:
        flash(str(e), "bad")
        return redirect(url_for("cc_sync"))
    extra = bool(pending.get("extra")) and ga4.stored() is not None
    ga4.save(refresh, email, _me(), extra=extra)
    data.log_change(None, "source", "ga4", "connected" + (" (extra account)" if extra else ""), None, email or "Google account", _me())
    try:
        n = ga4_sync.refresh_directory([0], force=True)
        msg = (f"{email or 'That account'} added beside the main Analytics account. " if extra else
               f"Analytics connected{(' as ' + email) if email else ''}. ") + \
            f"{n} propert{'ies' if n != 1 else 'y'} visible across the connected accounts; syncing now."
    except (ga4.GA4Error, ga4.QuotaRefused) as e:
        msg = f"Signed in{(' as ' + email) if email else ''}, but listing the properties failed: {e}"
    sync.enqueue_all("ga4 connected", sources=("ga4",))
    _kick()
    flash(msg, "good")
    return _after_google(pending, "ga4")


@app.route("/delivery/ga4/disconnect", methods=["POST"])
@cc_required
def cc_ga4_disconnect():
    from . import ga4_sync
    from .sources import ga4
    email = (request.form.get("email") or "").strip() or None  # change 87
    with ga4.use(email):
        revoked = ga4.revoke()
    ga4.forget(email)
    data.log_change(None, "source", "ga4", "connected", email or "main account", "disconnected", _me())
    if ga4.connected():
        try:
            ga4_sync.refresh_directory([0], force=True)
        except (ga4.GA4Error, ga4.QuotaRefused, ga4.Reauth, ga4.NotConnected):
            pass
    flash(f"{email or 'Analytics'} disconnected" + (" and Google's access revoked." if revoked else ". Also remove the app under that Google account's third-party access."), "good")
    return redirect(url_for("cc_sync"))


@app.route("/delivery/clients/<int:cid>/ga4", methods=["POST"])
@cc_required
def cc_client_ga4(cid):
    c = db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True) or abort(404)
    choice = (request.form.get("ga4") or "").strip()
    if choice in ("auto", "none"):
        how, prop = choice, (None if choice == "none" else c["ga4_property"])
    elif db.q("SELECT 1 FROM cc_ga4_properties WHERE name=?", (choice,), one=True):
        how, prop = "manual", choice
    else:
        abort(400)
    data.log_change(cid, "client", cid, "ga4_property", f"{c['ga4_how']}:{c['ga4_property'] or ''}", f"{how}:{prop or ''}", _me())
    db.x("UPDATE cc_clients SET ga4_how=?, ga4_property=?, updated_at=? WHERE id=?", (how, prop, now(), cid))
    sync.enqueue(cid, "ga4", "property choice")
    _kick()
    flash("Analytics choice saved; it syncs in the background.", "good")
    return redirect(url_for("cc_client", cid=cid) + "#t-setup")


@app.route("/delivery/clients/<int:cid>/gbp", methods=["POST"])
@cc_required
def cc_client_gbp(cid):
    c = db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True) or abort(404)
    choice = (request.form.get("gbp") or "").strip()
    if choice == "auto":
        how, loc = "auto", None
    elif choice == "none":
        how, loc = "none", None
    elif db.q("SELECT 1 FROM cc_gbp_locations WHERE name=?", (choice,), one=True):
        how, loc = "manual", choice
    else:
        abort(400)
    if (c["gbp_how"], c["gbp_location"]) != (how, loc if how == "manual" else c["gbp_location"]):
        data.log_change(cid, "client", cid, "gbp_location", f"{c['gbp_how']}:{c['gbp_location'] or ''}", f"{how}:{loc or ''}", _me())
    db.x("UPDATE cc_clients SET gbp_how=?, gbp_location=?, updated_at=? WHERE id=?",
         (how, loc if how == "manual" else (None if how == "none" else c["gbp_location"]), now(), cid))
    sync.enqueue(cid, "gbp", "profile choice")
    _kick()
    flash("Business Profile choice saved; it syncs in the background.", "good")
    return redirect(url_for("cc_client", cid=cid) + "#t-setup")
