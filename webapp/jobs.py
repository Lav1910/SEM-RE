"""Runs queued work: audits (in resumable steps) and Content Generator pieces.

The same step functions run in two setups:
- A normal server (your computer, Render, a VPS): background threads loop over the queue (start()).
- Vercel (serverless, no background threads): each POST /api/jobs/tick works for JOB_TICK_SECONDS, saves its place,
  and hands over to the next tick. Ticks come from the app itself when work is queued or a progress screen is open,
  and from a once-a-minute Vercel Cron as a safety net.

An audit moves through phases: prepare (read site, questions, search data; about 30-90 s) -> ask (AI platforms, in
waves that stop at the deadline) -> finish (analysis and report). Between ticks its state lives in the audit's files
(state.json), and a lease on the row stops two ticks working on the same audit.
"""
import json
import logging
import os
import sys
import threading
import time
import traceback
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import audit as engine  # noqa: E402
from aeo import budget  # noqa: E402
from . import db, store  # noqa: E402

log = logging.getLogger("hbm.jobs")
SERVERLESS = bool(os.environ.get("VERCEL")) or os.environ.get("HBM_SERVERLESS") == "1"
WORKERS = int(os.environ.get("AUDIT_WORKERS", "3"))
TICK = float(os.environ.get("JOB_TICK_SECONDS", "240" if SERVERLESS else "900"))
BULK_PARALLEL = int(os.environ.get("BULK_PARALLEL", "3"))  # bulk audits running at once (AI platform rate limits)
MAX_ATTEMPTS = 4  # a phase that keeps getting cut off (crash, timeout) fails the audit instead of looping forever
_started = False


# ---------------------------------------------------------------- audits
def _progress_writer(aid, row):
    feed = json.loads(row["activity"] or "[]") if row["activity"] else []
    eng = json.loads(row["engines"] or "{}") if row["engines"] else {}
    last = [0.0]
    top = [int(row["progress"] or 0)]
    lock = threading.Lock()

    def progress(stage, pct, event=None, engines=None):
        with lock:  # change 29: answers arrive from several threads at once; the bar only ever moves forward
            top[0] = pct = max(top[0], int(pct))
            _write(stage, pct, event, engines)

    def _write(stage, pct, event, engines):
        if event:
            feed.append({"t": time.time(), "text": event})
            del feed[:-40]
        if engines:
            eng.clear(); eng.update(engines)
        now = time.time()
        if pct >= 100 or event or now - last[0] > 0.7:  # throttle DB writes
            last[0] = now
            db.x("UPDATE audits SET stage=?, progress=?, activity=?, engines=? WHERE id=?",
                 (stage, int(pct), json.dumps(feed[-40:]), json.dumps(eng), aid))
    return progress


def _fail(aid, msg):
    db.x("UPDATE audits SET status='failed', error=?, finished_at=?, lease_until=NULL, phase=NULL WHERE id=?", (msg, time.time(), aid))
    store.delete(aid, "state.json")


def step_audit(row, deadline):
    """Advance one audit as far as the deadline allows. Returns True when it is finished (done or failed)."""
    aid = row["id"]
    progress = _progress_writer(aid, row)
    phase = row["phase"] or "prepare"
    if row["attempts"] > MAX_ATTEMPTS:
        _fail(aid, "This audit kept stopping part-way. Try again; if it keeps happening, tell an admin.")
        return True
    try:
        while True:
            if phase == "prepare":
                if row["status"] == "queued":
                    db.x("UPDATE audits SET status='running', started_at=?, stage='Starting', progress=1 WHERE id=?", (time.time(), aid))
                S = engine.prepare(row["url"], row["city"] or None, row["state"] or None, 6 if row["test_mode"] else 14,
                                   progress=progress, scope_kind=row["scope"] or "root",
                                   locations_chosen=json.loads(row["locations"]) if row["locations"] else None)
                store.save_json(aid, "state.json", S)
                phase = "ask"
                db.x("UPDATE audits SET phase='ask', attempts=0 WHERE id=?", (aid,))
            elif phase == "ask":
                S = store.load_json(aid, "state.json")
                if S is None:  # state lost: start again
                    phase = "prepare"; continue
                done = engine.ask_batch(S, progress, deadline=deadline - 20)
                store.save_json(aid, "state.json", S)
                if not done:
                    db.x("UPDATE audits SET attempts=0 WHERE id=?", (aid,))
                    return False
                phase = "finish"
                db.x("UPDATE audits SET phase='finish', attempts=0 WHERE id=?", (aid,))
            elif phase == "finish":
                S = store.load_json(aid, "state.json")
                if S is None:
                    phase = "prepare"; continue
                A, report_html, loom = engine.finish(S, progress)
                store.save_json(aid, "audit.json", A)
                store.put(aid, "report.html", report_html)
                store.put(aid, "loom.txt", loom)
                store.delete(aid, "state.json")
                k, p, m = A["analysis"]["kpis"], A["profile"], A["market"]
                out_dir = None if store.USE_DB else os.path.join(db.DATA_DIR, "audits", aid)
                try:
                    summ = json.dumps(_summary(A))
                except Exception:
                    summ = None
                db.x("""UPDATE audits SET status='done', stage='Done', progress=100, finished_at=?, out_dir=?, domain=?,
                        builder_name=?, market=?, score=?, mentioned=?, answers=?, pack=?, pack_of=?, checks_passed=?, checks_total=?,
                        engines_measured=?, summary=?, cost_usd=?, phase=NULL, lease_until=NULL WHERE id=?""",
                     (time.time(), out_dir, p["domain"], p["name"], f"{m['city']}, {m['state_code']}", k["score"], k["mentioned"],
                      k["answers"], k["pack"], k["pack_of"], k["checks_passed"], k["checks_total"], k["engines_measured"], summ,
                      budget.audit_cost(A), aid))
                progress("Done", 100, event="Report ready")
                return True
            if time.time() > deadline - 20:
                db.x("UPDATE audits SET lease_until=NULL WHERE id=?", (aid,))
                return False
    except engine.AuditError as e:
        _fail(aid, str(e))
        return True
    except Exception as e:
        log.error("audit %s failed: %s", aid, traceback.format_exc())
        _fail(aid, f"Something went wrong while running this audit ({type(e).__name__}). Try again; if it keeps failing, tell an admin.")
        return True


# ---------------------------------------------------------------- content generator pieces
def run_piece(row):
    from aeo import budget, strategy
    aid, kind, key, provider = row["audit_id"], row["kind"], row["key"], row["provider"] or "gemini"
    where = "WHERE audit_id=? AND kind=? AND key=?"
    db.x(f"UPDATE strategy_content SET status='running', updated_at=? {where}", (time.time(), aid, kind, key))
    A = store.load_json(aid)
    if A is None:
        db.x(f"UPDATE strategy_content SET status='failed', error=?, lease_until=NULL {where}", ("Audit data not found.", aid, kind, key))
        return
    reserved = budget.reserve("chatgpt" if provider == "chatgpt" else "gemini")
    if reserved is None:
        db.x(f"UPDATE strategy_content SET status='failed', error=?, updated_at=?, lease_until=NULL {where}",
             ("Spending cap reached. An admin can raise it.", time.time(), aid, kind, key))
        return
    try:
        md, cost = strategy.generate(A, kind, key, provider)
        budget.settle(reserved, cost)
        db.x(f"UPDATE strategy_content SET status='done', content=?, error=NULL, cost=?, updated_at=?, lease_until=NULL {where}",
             (md, cost, time.time(), aid, kind, key))
    except Exception as e:
        budget.settle(reserved, 0.0)
        db.x(f"UPDATE strategy_content SET status='failed', error=?, updated_at=?, lease_until=NULL {where}",
             (str(e)[:300], time.time(), aid, kind, key))


# ---------------------------------------------------------------- the loop
def _claim_audit(seconds):
    """Single audits first; bulk audits only while fewer than BULK_PARALLEL are running and their batch isn't paused."""
    row = db.claim("audits", "status IN ('queued','running') AND batch_id IS NULL", (), "created_at",
                   lease_seconds=seconds + 90, extra_set=", attempts=attempts+1")
    if row:
        return row
    busy = db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id IS NOT NULL AND status='running' AND lease_until>?",
                (time.time(),), one=True)["n"]
    if busy >= BULK_PARALLEL:
        return None
    return db.claim("audits", "status IN ('queued','running') AND batch_id IN (SELECT id FROM batches WHERE status='running')",
                    (), "created_at", lease_seconds=seconds + 90, extra_set=", attempts=attempts+1")


def _summary(A):
    """Short facts kept on the audit row for bulk result tables (so a 100-site table doesn't load 100 audit files)."""
    X, qs = A["analysis"], A["questions"]
    top = next((b["name"] for b in X["leaderboard"] if not b.get("prospect") and b.get("mentions")), None)
    demand = sum(q.get("volume") or 0 for q in qs if q.get("kind") != "brand")
    k = X["kpis"]
    score = k.get("score")
    opp = None
    if score is not None:  # 0-100: low visibility, real local demand and a competitor winning = a better prospect
        opp = round(min(100, (100 - score) * 0.6 + min(demand / 40, 25) + (10 if top else 0)
                        + (5 if k.get("pack") == 0 and k.get("pack_of") else 0)))
    return {"top_competitor": top, "demand": demand, "opportunity": opp}


def after_audit(aid):
    """When a bulk audit ends, close its batch if everything in it has finished, and email the person who started it."""
    r = db.q("SELECT batch_id FROM audits WHERE id=?", (aid,), one=True)
    bid = r["batch_id"] if r else None
    if not bid:
        return
    left = db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id=? AND status IN ('queued','running')", (bid,), one=True)["n"]
    if left:
        return
    b = db.q("SELECT * FROM batches WHERE id=?", (bid,), one=True)
    if not b or b["status"] == "done":
        return
    db.x("UPDATE batches SET status='done', finished_at=? WHERE id=? AND status!='done'", (time.time(), bid))
    try:
        notify_batch(bid)
    except Exception:
        log.exception("batch email failed")


def notify_batch(bid):
    from . import mailer
    b = db.q("SELECT b.*, u.email, u.name AS who FROM batches b JOIN users u ON u.id=b.user_id WHERE b.id=?", (bid,), one=True)
    if not b or b["notified"]:
        return
    n = db.q("""SELECT SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS ok, SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS bad
                FROM audits WHERE batch_id=?""", (bid,), one=True)
    base = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
    link = f"{base}/bulk/{bid}" if base else f"/bulk/{bid}"
    name = b["name"] or "Your bulk analysis"
    text = (f"Hi {(b['who'] or '').split(' ')[0] or 'there'},\n\n{name} has finished: {n['ok'] or 0} audits ready"
            + (f", {n['bad']} could not be completed" if n["bad"] else "") + f".\n\nOpen the results: {link}\n\nHome Builder Marketers")
    import html as H
    body = (f'<div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;color:#16302F"><div style="background:#0C3335;padding:18px 22px;'
            f'border-radius:12px 12px 0 0;color:#fff;font-weight:bold;letter-spacing:.04em">HOME BUILDER MARKETERS</div><div style="border:1px solid #E3EBEC;'
            f'border-top:0;border-radius:0 0 12px 12px;padding:22px"><p><b>{H.escape(name)}</b> has finished.</p><p style="font-size:22px;color:#0A686A;'
            f'font-weight:bold;margin:6px 0">{n["ok"] or 0} audits ready</p>' + (f'<p>{n["bad"]} could not be completed (you can retry them).</p>' if n["bad"] else "")
            + f'<p><a href="{H.escape(link)}" style="display:inline-block;background:#0A686A;color:#fff;text-decoration:none;padding:11px 18px;'
            f'border-radius:8px;font-weight:bold">Open the results</a></p></div></div>')
    mailer.send(b["email"], f"Bulk analysis finished: {name}", text, body)
    db.x("UPDATE batches SET notified=1 WHERE id=?", (bid,))


def work(seconds=TICK):
    """Do queued work until `seconds` run out. Returns True if anything was worked on."""
    deadline = time.time() + seconds
    did = False
    while time.time() < deadline - 25:
        row = _claim_audit(seconds)
        if row:
            did = True
            if step_audit(row, deadline):
                after_audit(row["id"])
            else:
                db.x("UPDATE audits SET lease_until=NULL WHERE id=?", (row["id"],))
            continue
        piece = db.claim("strategy_content", "status IN ('queued','running')", (), "updated_at", lease_seconds=240,
                         key="audit_id,kind,key")
        if piece:
            did = True
            run_piece(piece)
            continue
        break
    return did


def pending():
    return bool(db.q("""SELECT 1 FROM audits WHERE status IN ('queued','running')
                        AND (batch_id IS NULL OR batch_id IN (SELECT id FROM batches WHERE status='running')) LIMIT 1""", one=True) or
                db.q("SELECT 1 FROM strategy_content WHERE status IN ('queued','running') LIMIT 1", one=True))


def kick(base_url=None, times=1):
    """Serverless only: start tick(s) in other requests without waiting for them (bulk runs start several in parallel)."""
    if not SERVERLESS:
        return
    for _ in range(max(1, times) - 1):
        threading.Thread(target=kick, args=(base_url, 1), daemon=True).start()
    base = (base_url or os.environ.get("PUBLIC_URL") or
            ("https://" + os.environ["VERCEL_PROJECT_PRODUCTION_URL"] if os.environ.get("VERCEL_PROJECT_PRODUCTION_URL") else "") or
            ("https://" + os.environ["VERCEL_URL"] if os.environ.get("VERCEL_URL") else "")).rstrip("/")
    if not base:
        return
    req = urllib.request.Request(base + "/api/jobs/tick", data=b"", method="POST",
                                 headers={"Authorization": "Bearer " + tick_secret(), "User-Agent": "hbm-aeo-tick"})
    try:
        urllib.request.urlopen(req, timeout=2)
    except Exception:
        pass  # the tick keeps running on its own; we only needed to start it


def tick_secret():
    return os.environ.get("CRON_SECRET") or os.environ.get("SECRET_KEY") or ""


def _worker():
    while True:
        try:
            if not work(TICK):
                time.sleep(1.5)
        except Exception:
            log.exception("queue error")
            time.sleep(5)


def start():
    """Normal servers: background worker threads. Does nothing on serverless hosts."""
    global _started
    if _started or SERVERLESS:
        return
    _started = True
    db.x("UPDATE audits SET lease_until=NULL WHERE status IN ('queued','running')")  # restarted: pick up where we left off
    db.x("UPDATE strategy_content SET lease_until=NULL WHERE status IN ('queued','running')")
    for i in range(WORKERS):
        threading.Thread(target=_worker, name=f"audit-worker-{i}", daemon=True).start()
