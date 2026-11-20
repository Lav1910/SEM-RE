"""Command Center sync jobs (change 64), run by the app's existing job runner (jobs.work).

- Daily: the first tick after 05:00 UK time queues one job per active client per source (Vercel's cron hits the runner
  at 06:00 UTC; on a normal server the worker threads tick all the time).
- Each job is leased (two runners never do the same one), retried with backoff (1, 2, 4 minutes) up to 3 tries, and
  logged in cc_sync with the number of API calls. Errors are stored without tokens.
- After every job the client's flags and colour are recomputed, so pages only ever read the database.
"""
import datetime
import hashlib
import json
import logging
import re
import time

from aeo import kv
from webapp import db
from . import data
from .core import SYNC_SOURCES, TZ, add_months, enabled, month_of, month_start, now, root_domain, same_url, today, ts_date
from .sources import clickup, gsc, logo, wordpress

log = logging.getLogger("hbm.cc")
MAX_TRIES = 3
_last_daily_check = [0.0]


# ---------------------------------------------------------------- queue
def enqueue(client_id, source, why="manual"):
    if db.q("SELECT 1 FROM cc_sync WHERE client_id=? AND source=? AND status IN ('queued','running') LIMIT 1", (client_id, source), one=True):
        return False
    db.x("INSERT INTO cc_sync (client_id, source, status, why, queued_at) VALUES (?,?, 'queued', ?, ?)", (client_id, source, why, now()))
    return True


def enqueue_all(why="manual", sources=SYNC_SOURCES):
    n = 0
    for r in db.q("SELECT id FROM cc_clients WHERE status='active' ORDER BY id"):
        for s in sources:
            n += enqueue(r["id"], s, why)
    return n


def daily_check():
    """Queue the daily sync once per UK day, from 05:00. Cheap: looks at the settings store at most once a minute."""
    if not enabled() or time.time() - _last_daily_check[0] < 60:
        return False
    _last_daily_check[0] = time.time()
    local = datetime.datetime.now(TZ)
    if local.hour < 5:
        return False
    day = local.date().isoformat()
    if kv.get("cc_daily", "") == day:
        return False
    won = []
    kv.update("cc_daily", lambda old: (won.append(1), day)[1] if old != day else old, "")
    if won:
        n = enqueue_all("daily")
        log.info("Command Center: queued %s daily sync jobs", n)
        _prune()
    return bool(won)


def _prune():
    old = now() - 60 * 86400
    db.x("DELETE FROM cc_sync WHERE finished_at IS NOT NULL AND finished_at<?", (old,))


def pending():
    if not enabled():
        return False
    return bool(db.q("SELECT 1 FROM cc_sync WHERE status='queued' AND (not_before IS NULL OR not_before<=?) LIMIT 1", (now(),), one=True))


def claim():
    # a 'running' job whose lease ran out (the runner died) is picked up again
    return db.claim("cc_sync", "((status='queued' AND (not_before IS NULL OR not_before<=?)) OR status='running')", (now(),), "queued_at",
                    lease_seconds=280, extra_set=", attempts=attempts+1, status='running'")


def run(row):
    jid, cid, source = row["id"], row["client_id"], row["source"]
    c = data.d(db.q("SELECT * FROM cc_clients WHERE id=?", (cid,), one=True))
    if not c:
        db.x("UPDATE cc_sync SET status='failed', error='client removed', finished_at=?, lease_until=NULL WHERE id=?", (now(), jid))
        return
    db.x("UPDATE cc_sync SET started_at=? WHERE id=?", (now(), jid))
    calls = [0]
    try:
        note = ADAPTERS[source](c, calls)
        db.x("UPDATE cc_sync SET status='done', finished_at=?, calls=?, note=?, error=NULL, lease_until=NULL WHERE id=?",
             (now(), calls[0], (note or "")[:300], jid))
    except clickup.NotConnected:
        data.set_source(cid, source, "not_connected", "ClickUp token not set on the server")
        db.x("UPDATE cc_sync SET status='skipped', finished_at=?, calls=?, note='source not connected', lease_until=NULL WHERE id=?",
             (now(), calls[0], jid))
    except Exception as e:  # noqa: BLE001
        msg = (str(e) or e.__class__.__name__)[:300]
        log.warning("Command Center sync %s/%s failed: %s", source, cid, msg)
        if row["attempts"] < MAX_TRIES:
            db.x("UPDATE cc_sync SET status='queued', not_before=?, error=?, calls=?, lease_until=NULL WHERE id=?",
                 (now() + 60 * 2 ** (row["attempts"] - 1), msg, calls[0], jid))
        else:
            db.x("UPDATE cc_sync SET status='failed', finished_at=?, error=?, calls=?, lease_until=NULL WHERE id=?", (now(), msg, calls[0], jid))
            data.set_source(cid, source, "error", msg)
    finally:
        try:
            data.recompute(cid)
        except Exception:  # noqa: BLE001
            log.exception("Command Center recompute failed for client %s", cid)


def work_one():
    """Called from jobs.work(): run one queued sync job. Returns True if it did something."""
    if not enabled():
        return False
    daily_check()
    row = claim()
    if not row:
        return False
    run(row)
    return True


# ---------------------------------------------------------------- ClickUp
def sync_clickup(c, calls):
    cid = c["id"]
    if not clickup.connected():
        raise clickup.NotConnected()
    if not c["clickup_folder"]:
        data.set_source(cid, "clickup", "not_connected", "No ClickUp folder linked to this client")
        return "no folder linked"
    tasks = clickup.folder_tasks(c["clickup_folder"], calls)
    t0, dom = now(), root_domain(c["domain"])
    seen, n_items = [], 0
    for t in tasks:
        tid = str(t["id"])
        seen.append(tid)
        st = t.get("status") or {}
        item = clickup.classify(t.get("name"), t.get("_list"))
        people = [{"id": data.person_for_clickup(a), "name": a.get("username") or a.get("email") or ""} for a in t.get("assignees") or []]
        due, closed = clickup.ms(t.get("due_date")), clickup.ms(t.get("date_closed") or t.get("date_done"))
        created, updated = clickup.ms(t.get("date_created")), clickup.ms(t.get("date_updated"))
        live = clickup.live_url_of(t, dom)
        prev = db.q("SELECT last_comment_at FROM cc_tasks WHERE task_id=?", (tid,), one=True)
        db.upsert("cc_tasks", {"task_id": tid, "client_id": cid, "list_name": t.get("_list"), "name": (t.get("name") or "")[:300],
                               "item": item, "parent": t.get("parent"), "status": st.get("status"), "status_type": st.get("type"), "assignees": json.dumps(people),
                               "due": due, "recurring": clickup.recurring_of(t), "url": t.get("url"), "live_url": live,
                               "date_created": created, "date_updated": updated, "date_closed": closed,
                               "last_comment_at": prev["last_comment_at"] if prev else None, "fetched_at": t0}, ["task_id"])
        if not item:
            continue
        n_items += 1
        when = due or closed or created or t0
        status = clickup.map_status(st.get("status"), st.get("type"))
        owner = people[0]["id"] if people else None
        ex = db.q("SELECT id FROM cc_deliverables WHERE client_id=? AND source='clickup' AND ext_id=?", (cid, tid), one=True)
        due_s = ts_date(due).isoformat() if due else None
        if ex:
            db.x("""UPDATE cc_deliverables SET item=?, month=?, title=?, owner_id=?, status=?, due=?, task_id=?,
                    url=COALESCE(?, url), updated_at=? WHERE id=?""",
                 (item, month_of(ts_date(when)), t.get("name"), owner, status, due_s, tid, live, t0, ex["id"]))
        else:
            db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, owner_id, status, due, task_id, url, source, ext_id, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?, 'clickup', ?, ?)""",
                 (cid, item, month_of(ts_date(when)), t.get("name"), owner, status, due_s, tid, live, tid, t0))
    # a subtask of the same kind as its parent (e.g. "Blog draft" under "Blog #2") is part of that deliverable, not another one
    kinds = {str(t["id"]): clickup.classify(t.get("name"), t.get("_list")) for t in tasks}
    for t in tasks:
        par = str(t.get("parent") or "")
        if par and kinds.get(str(t["id"])) and kinds.get(par) == kinds.get(str(t["id"])):
            db.x("DELETE FROM cc_deliverables WHERE client_id=? AND source='clickup' AND ext_id=?", (cid, str(t["id"])))
            n_items -= 1
    # tasks deleted or moved out of the folder
    db.x("DELETE FROM cc_tasks WHERE client_id=? AND fetched_at<?", (cid, t0))
    db.x("DELETE FROM cc_deliverables WHERE client_id=? AND source='clickup' AND updated_at<? AND verified_at IS NULL", (cid, t0))
    # last comment, only for work waiting on someone (one call each, capped)
    from .health import awaits_approval, awaits_client
    waiting = [r for r in db.q("SELECT task_id, status FROM cc_tasks WHERE client_id=? AND status_type!='closed'", (cid,))
               if awaits_approval(r["status"]) or awaits_client(r["status"])][:10]
    for r in waiting:
        try:
            db.x("UPDATE cc_tasks SET last_comment_at=? WHERE task_id=?", (clickup.last_comment_at(r["task_id"], calls), r["task_id"]))
        except clickup.ClickUpError:
            pass
    merge_blogs(cid)
    data.set_source(cid, "clickup", "ok", f"{len(tasks)} tasks, {n_items} deliverable tasks", ok=True)
    return f"{len(tasks)} tasks read, {n_items} look like deliverables"


def _norm_title(s):
    return re.sub(r"[^a-z0-9 ]+", "", (s or "").lower()).replace("blog", "").strip()


def merge_blogs(cid):
    """Pair ClickUp blog tasks with the live posts they produced (by URL, else by title), so one blog counts once and
    the post picks up the task's owner."""
    posts = [data.d(r) for r in db.q("SELECT * FROM cc_deliverables WHERE client_id=? AND source IN ('wp','sitemap') AND month!='baseline'", (cid,))]
    db.x("UPDATE cc_deliverables SET merged_into=NULL WHERE client_id=? AND source='clickup'", (cid,))
    used = set()
    for t in db.q("SELECT * FROM cc_deliverables WHERE client_id=? AND source='clickup' AND item='blog'", (cid,)):
        match = next((p for p in posts if p["id"] not in used and t["url"] and same_url(t["url"], p["url"])), None)
        if not match:
            nt = _norm_title(t["title"])
            match = next((p for p in posts if p["id"] not in used and len(nt) >= 12 and p["title"]
                          and (nt in _norm_title(p["title"]) or _norm_title(p["title"]) in nt) and len(_norm_title(p["title"])) >= 12), None)
        if match:
            used.add(match["id"])
            db.x("UPDATE cc_deliverables SET merged_into=? WHERE id=?", (match["id"], t["id"]))
            db.x("UPDATE cc_deliverables SET task_id=?, owner_id=COALESCE(owner_id, ?) WHERE id=?", (t["task_id"], t["owner_id"], match["id"]))


# ---------------------------------------------------------------- WordPress / sitemap
def sync_wordpress(c, calls):
    cid, dom = c["id"], root_domain(c["domain"])
    if not dom:
        data.set_source(cid, "wordpress", "not_connected", "No website domain recorded")
        return "no domain"
    t0 = now()
    home = wordpress.home_status(dom)
    calls[0] += 1
    prev = data.metric(cid, "wordpress", "home_fails")
    fails = 0 if 200 <= home < 400 else int((prev["value"] if prev else 0) or 0) + 1
    data.set_metric(cid, "wordpress", "home_status", "latest", home)
    data.set_metric(cid, "wordpress", "home_fails", "latest", fails)
    if 200 <= home < 400:
        try:
            maybe_logo(c, calls)
        except Exception:  # noqa: BLE001  a logo is nice to have; never fail the sync over it
            log.warning("Command Center: logo lookup failed for %s", dom, exc_info=True)
    cur = month_of(today())
    first_month = add_months(cur, -12)
    reason, note = None, ""
    try:
        ps = wordpress.posts(dom, month_start(first_month).isoformat() + "T00:00:00", calls)
        ids = []
        for p in ps:
            ids.append(p["id"])
            when = None
            try:
                when = datetime.datetime.fromisoformat(p["date"]).replace(tzinfo=TZ).timestamp()
            except ValueError:
                pass
            ex = db.q("SELECT id FROM cc_deliverables WHERE client_id=? AND source='wp' AND ext_id=?", (cid, p["id"]), one=True)
            if ex:
                db.x("""UPDATE cc_deliverables SET month=?, title=?, url=?, published_at=?, status='published', updated_at=?,
                        image_url=COALESCE(?, image_url) WHERE id=?""", (p["date"][:7], p["title"][:300], p["url"], when, t0, p.get("image"), ex["id"]))
            else:
                db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, status, url, published_at, source, ext_id, updated_at, image_url)
                        VALUES (?, 'blog', ?, ?, 'published', ?, ?, 'wp', ?, ?, ?)""",
                     (cid, p["date"][:7], p["title"][:300], p["url"], when, p["id"], t0, p.get("image")))
        db.x("DELETE FROM cc_deliverables WHERE client_id=? AND source='wp' AND month>=? AND updated_at<?", (cid, first_month, t0))
        data.set_source(cid, "wordpress", "ok", f"WordPress posts feed: {len(ps)} posts in the last 12 months", ok=True)
        note = f"{len(ps)} posts via WordPress"
    except (wordpress.Blocked, wordpress.NotWordPress) as e:
        reason = str(e)
        blocked = isinstance(e, wordpress.Blocked)
        urls = wordpress.sitemap_post_urls(dom, calls)
        if not urls:
            data.set_source(cid, "wordpress", "blocked" if blocked else "partial",
                            f"{reason}; no posts sitemap either, so new blogs can't be counted")
            note = "no posts feed or posts sitemap"
        else:
            first_run = not db.q("SELECT 1 FROM cc_deliverables WHERE client_id=? AND source='sitemap' LIMIT 1", (cid,), one=True)
            new = 0
            for u in urls:
                ext = hashlib.sha1(u.encode(), usedforsecurity=False).hexdigest()[:16]  # an ID for the URL, not security
                if db.q("SELECT 1 FROM cc_deliverables WHERE client_id=? AND source='sitemap' AND ext_id=?", (cid, ext), one=True):
                    continue
                slug = u.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").strip().capitalize()
                db.x("""INSERT INTO cc_deliverables (client_id, item, month, title, status, url, source, ext_id, updated_at)
                        VALUES (?, 'blog', ?, ?, 'published', ?, 'sitemap', ?, ?)""", (cid, "baseline" if first_run else cur, slug[:300], u, ext, t0))
                new += not first_run
            base = db.q("SELECT MIN(updated_at) AS t FROM cc_deliverables WHERE client_id=? AND source='sitemap'", (cid,), one=True)["t"]
            bd = ts_date(base)
            since = f"{bd.day} {bd:%b %Y}" if bd else "today"
            data.set_source(cid, "wordpress", "partial", f"{reason}; counting posts that appear in the sitemap after {since} "
                            "(publish dates aren't exposed)", ok=True)
            note = f"sitemap: {len(urls)} post URLs" + (" (baseline saved)" if first_run else f", {new} new")
    # live checks, never-checked and newest first: unverified posts daily, verified ones weekly (they can break or go noindex later)
    rows = db.q("""SELECT * FROM cc_deliverables WHERE client_id=? AND source IN ('wp','sitemap') AND month!='baseline' AND month>=?
                   AND not_ours=0 AND (checked_at IS NULL OR (verified_at IS NULL AND checked_at<?) OR checked_at<?)
                   ORDER BY (checked_at IS NOT NULL), month DESC, COALESCE(checked_at, 0) LIMIT 25""", (cid, first_month, t0 - 20 * 3600, t0 - 7 * 86400))
    for r in rows:
        res = wordpress.check_live(r["url"])
        calls[0] += 1
        if res["ok"]:
            db.x("UPDATE cc_deliverables SET verified_at=COALESCE(verified_at, ?), verified_how=?, check_note=?, checked_at=? WHERE id=?",
                 (t0, "Live check: " + res["note"], res["note"], t0, r["id"]))
        else:
            db.x("UPDATE cc_deliverables SET verified_at=NULL, verified_how=NULL, check_note=?, checked_at=? WHERE id=?", (res["note"], t0, r["id"]))
        _keep_page_details(r, res, t0)
    calls[0] += post_images(cid, t0)
    merge_blogs(cid)
    return f"{note}; homepage HTTP {home or 'no response'}; {len(rows)} live checks"


def _keep_page_details(r, res, t0):
    """From a page we already opened: its share image, and a real title for posts known only from the sitemap."""
    sets, args = ["image_checked_at=?"], [t0]
    if res.get("image") and not r["image_url"]:
        sets.append("image_url=?"); args.append(res["image"][:600])
    if res.get("title") and r["source"] == "sitemap":
        sets.append("title=?"); args.append(re.sub(r"\s+[|\-–—]\s+[^|\-–—]{2,60}$", "", res["title"])[:300])  # drop " | Site name"
    db.x(f"UPDATE cc_deliverables SET {', '.join(sets)} WHERE id=?", (*args, r["id"]))


def post_images(cid, t0, limit=12):
    """Top up share images for posts that have none yet (older and sitemap posts included), a few per sync.
    Read-only page loads; a post that has no image is retried after a week. Returns the number of requests."""
    rows = db.q("""SELECT * FROM cc_deliverables WHERE client_id=? AND item='blog' AND source IN ('wp','sitemap') AND url IS NOT NULL
                   AND image_url IS NULL AND merged_into IS NULL AND (image_checked_at IS NULL OR image_checked_at<?)
                   ORDER BY COALESCE(published_at, 0) DESC, id DESC LIMIT ?""", (cid, t0 - 7 * 86400, limit))
    for r in rows:
        code, _, body, final = wordpress.fetch(r["url"])
        _keep_page_details(r, {"image": wordpress.page_image(body, final) if code == 200 else None,
                               "title": wordpress.page_title(body) if code == 200 else None}, t0)
    return len(rows)


def maybe_logo(c, calls, force=False):
    """Find the client's logo on their site: once a month when found, weekly retry when not. Uploaded logos stay."""
    cid, dom = c["id"], root_domain(c["domain"])
    row = db.q("SELECT how, bg, content IS NOT NULL AS has, fetched_at FROM cc_logos WHERE client_id=?", (cid,), one=True)
    if row and row["how"] == "upload" and not force:
        return False
    if row and not force and now() - row["fetched_at"] < (30 if row["has"] else 7) * 86400:
        return False
    res = logo.find(dom, calls, name=c.get("name")) if dom else None
    bg = row["bg"] if row else "auto"
    if res:
        content, mime, url, why = res
        db.upsert("cc_logos", {"client_id": cid, "content": content, "mime": mime, "source_url": url[:500], "how": "auto",
                               "bg": bg, "note": why, "fetched_at": now()}, ["client_id"])
        return True
    db.upsert("cc_logos", {"client_id": cid, "content": None, "mime": None, "source_url": None, "how": "auto", "bg": bg,
                           "note": "No logo found on the homepage", "fetched_at": now()}, ["client_id"])
    return False


# ---------------------------------------------------------------- Search Console (through DataWise)
def _datawise():
    """The DataWise MCP client, or None when no token is set (tests swap this out)."""
    from aeo import datawise
    return datawise._client() if datawise.available() else None


def sync_gsc(c, calls):
    cid, dom = c["id"], root_domain(c.get("domain"))
    if not dom:
        data.set_source(cid, "gsc", "not_connected", "No website set for this client")
        return "no domain"
    dw = _datawise()
    if dw is None:
        data.set_source(cid, "gsc", "not_connected", "DataWise token not set on the server")
        return "DataWise not connected"
    listing = dw.call(gsc.TOOL, {"action": "list_properties", "limit": 200})
    calls[0] += 1
    if gsc.is_error(listing):
        raise RuntimeError("DataWise property list: " + gsc.error_text(listing))
    if listing.get("needs_reconnect"):
        data.set_source(cid, "gsc", "error", "DataWise's Google connection needs reconnecting (in DataWise)")
        return "needs reconnect"
    best, cands = gsc.match(listing.get("properties"), dom)
    if not cands:
        data.set_source(cid, "gsc", "not_connected", f"No Search Console property for {dom} in DataWise")
        return "no property"
    if not best:
        waiting = sorted({p.get("site_url") for p in cands if p.get("is_enabled")})
        if waiting:  # switched on, but DataWise hasn't pulled any data yet
            data.set_source(cid, "gsc", "not_connected", f"{', '.join(waiting)} is switched on in DataWise but hasn't synced yet")
            return "waiting for DataWise's first sync"
        names = ", ".join(sorted({p.get("site_url") for p in cands}))
        data.set_source(cid, "gsc", "not_connected", f"{names} is in DataWise, but syncing is switched off there")
        return "property switched off in DataWise"
    resp = dw.call(gsc.TOOL, {"action": "overview", "property_id": best["id"], "range_days": 30, "response_format": "detailed"})
    calls[0] += 1
    if gsc.is_error(resp):
        raise RuntimeError("DataWise overview: " + gsc.error_text(resp))
    s = gsc.summarize(resp, best.get("site_url"))
    data.set_metric(cid, "gsc", "overview", "latest", text=json.dumps(s))
    for k in ("clicks", "impressions", "position"):
        data.set_metric(cid, "gsc", f"{k}_30d", "latest", value=s["d30"][k])
    if gsc.stale(s["last_synced"], today()):
        data.set_source(cid, "gsc", "partial", f"DataWise last synced {s['property']} on {str(s['last_synced'] or 'never')[:10]}", ok=True)
    else:
        data.set_source(cid, "gsc", "ok", f"{s['property']} via DataWise", ok=True)
    return f"{s['property']}: {s['d30']['clicks']} clicks / 30 days"


def sync_gbp(c, calls):
    from . import gbp_sync
    return gbp_sync.sync(c, calls)


def sync_ga4(c, calls):
    from . import ga4_sync
    return ga4_sync.sync(c, calls)


def sync_bing(c, calls):
    from . import bing_sync
    return bing_sync.sync(c, calls)


ADAPTERS = {"clickup": sync_clickup, "wordpress": sync_wordpress, "gsc": sync_gsc, "gbp": sync_gbp, "ga4": sync_ga4,
            "bing": sync_bing}
