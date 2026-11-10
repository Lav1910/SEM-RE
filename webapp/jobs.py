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
from . import db, store  # noqa: E402

log = logging.getLogger("hbm.jobs")
SERVERLESS = bool(os.environ.get("VERCEL")) or os.environ.get("HBM_SERVERLESS") == "1"
WORKERS = int(os.environ.get("AUDIT_WORKERS", "3"))
TICK = float(os.environ.get("JOB_TICK_SECONDS", "240" if SERVERLESS else "900"))
MAX_ATTEMPTS = 4  # a phase that keeps getting cut off (crash, timeout) fails the audit instead of looping forever
_started = False


# ---------------------------------------------------------------- audits
def _progress_writer(aid, row):
    feed = json.loads(row["activity"] or "[]") if row["activity"] else []
    eng = json.loads(row["engines"] or "{}") if row["engines"] else {}
    last = [0.0]

    def progress(stage, pct, event=None, engines=None):
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
                db.x("""UPDATE audits SET status='done', stage='Done', progress=100, finished_at=?, out_dir=?, domain=?,
                        builder_name=?, market=?, score=?, mentioned=?, answers=?, pack=?, pack_of=?, checks_passed=?, checks_total=?,
                        engines_measured=?, phase=NULL, lease_until=NULL WHERE id=?""",
                     (time.time(), out_dir, p["domain"], p["name"], f"{m['city']}, {m['state_code']}", k["score"], k["mentioned"],
                      k["answers"], k["pack"], k["pack_of"], k["checks_passed"], k["checks_total"], k["engines_measured"], aid))
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
def work(seconds=TICK):
    """Do queued work until `seconds` run out. Returns True if anything was worked on."""
    deadline = time.time() + seconds
    did = False
    while time.time() < deadline - 25:
        row = db.claim("audits", "status IN ('queued','running')", (), "created_at", lease_seconds=seconds + 90,
                       extra_set=", attempts=attempts+1")
        if row:
            did = True
            if not step_audit(row, deadline):
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
    return bool(db.q("SELECT 1 FROM audits WHERE status IN ('queued','running') LIMIT 1", one=True) or
                db.q("SELECT 1 FROM strategy_content WHERE status IN ('queued','running') LIMIT 1", one=True))


def kick(base_url=None):
    """Serverless only: start a tick in another request without waiting for it."""
    if not SERVERLESS:
        return
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
