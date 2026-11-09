"""Background writing for the Content Generator: each piece runs in a small thread pool and is saved as it finishes."""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from aeo import budget, strategy
from . import db

_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("HBM_WRITER_WORKERS", "4")), thread_name_prefix="writer")
_lock = threading.Lock()


def queue(audit_row, kind, provider, user_id, keys=None):
    A = json.load(open(os.path.join(audit_row["out_dir"], "audit.json")))
    todo = [(k, lab) for k, lab in strategy.targets(A, kind) if not keys or k in keys]
    for key, label in todo:
        with _lock:
            cur = db.q("SELECT status FROM strategy_content WHERE audit_id=? AND kind=? AND key=?", (audit_row["id"], kind, key), one=True)
            if cur and cur["status"] in ("queued", "running"):
                continue
            db.x("""INSERT OR REPLACE INTO strategy_content (audit_id, kind, key, label, status, provider, user_id, updated_at)
                    VALUES (?,?,?,?, 'queued', ?, ?, ?)""", (audit_row["id"], kind, key, label, provider, user_id, time.time()))
        _pool.submit(_run, audit_row["id"], A, kind, key, provider)
    return len(todo)


def _run(aid, A, kind, key, provider):
    db.x("UPDATE strategy_content SET status='running', updated_at=? WHERE audit_id=? AND kind=? AND key=?", (time.time(), aid, kind, key))
    engine = "chatgpt" if provider == "chatgpt" else "gemini"
    reserved = budget.reserve(engine)
    if reserved is None:
        db.x("UPDATE strategy_content SET status='failed', error=?, updated_at=? WHERE audit_id=? AND kind=? AND key=?",
             ("Spending cap reached. An admin can raise it.", time.time(), aid, kind, key))
        return
    try:
        md, cost = strategy.generate(A, kind, key, provider)
        budget.settle(reserved, cost)
        db.x("UPDATE strategy_content SET status='done', content=?, error=NULL, cost=?, updated_at=? WHERE audit_id=? AND kind=? AND key=?",
             (md, cost, time.time(), aid, kind, key))
    except Exception as e:
        budget.settle(reserved, 0.0)
        db.x("UPDATE strategy_content SET status='failed', error=?, updated_at=? WHERE audit_id=? AND kind=? AND key=?",
             (str(e)[:300], time.time(), aid, kind, key))
