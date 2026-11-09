"""Background workers that run queued audits (Semrush-style job flow: submit -> id -> poll -> result)."""
import json
import logging
import os
import sys
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import audit as engine  # noqa: E402
from . import db  # noqa: E402

log = logging.getLogger("hbm.jobs")
WORKERS = int(os.environ.get("AUDIT_WORKERS", "3"))
_started = False


def _run_one(row):
    aid = row["id"]
    last = [0.0]

    feed, eng = [], {}

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

    out_dir = os.path.join(db.DATA_DIR, "audits", aid)
    try:
        engine.run(row["url"], row["city"] or None, row["state"] or None, 6 if row["test_mode"] else 14,
                   progress=progress, out_dir=out_dir, scope_kind=(row["scope"] if "scope" in row.keys() else None) or "root",
                   locations_chosen=json.loads(row["locations"]) if "locations" in row.keys() and row["locations"] else None)
        A = json.load(open(os.path.join(out_dir, "audit.json")))
        k, p, m = A["analysis"]["kpis"], A["profile"], A["market"]
        db.x("""UPDATE audits SET status='done', stage='Done', progress=100, finished_at=?, out_dir=?, domain=?,
                builder_name=?, market=?, score=?, mentioned=?, answers=?, pack=?, pack_of=?, checks_passed=?, checks_total=?,
                engines_measured=? WHERE id=?""",
             (time.time(), out_dir, p["domain"], p["name"], f"{m['city']}, {m['state_code']}", k["score"], k["mentioned"],
              k["answers"], k["pack"], k["pack_of"], k["checks_passed"], k["checks_total"], k["engines_measured"], aid))
    except engine.AuditError as e:
        db.x("UPDATE audits SET status='failed', error=?, finished_at=? WHERE id=?", (str(e), time.time(), aid))
    except Exception as e:
        log.error("audit %s failed: %s", aid, traceback.format_exc())
        db.x("UPDATE audits SET status='failed', error=?, finished_at=? WHERE id=?",
             (f"Something went wrong while running this audit ({type(e).__name__}). Try again; if it keeps failing, tell an admin.",
              time.time(), aid))


def _worker():
    while True:
        try:
            row = db.claim_next()
        except Exception:
            log.exception("queue error"); time.sleep(5); continue
        if not row:
            time.sleep(1.5)
            continue
        _run_one(row)


def start():
    global _started
    if _started:
        return
    _started = True
    db.requeue_interrupted()
    for i in range(WORKERS):
        threading.Thread(target=_worker, name=f"audit-worker-{i}", daemon=True).start()
