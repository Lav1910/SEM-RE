"""Queues Content Generator pieces. The job runner (webapp/jobs.py) writes them: background threads on a normal
server, short ticks on Vercel."""
import time

from aeo import strategy
from . import db, jobs, store


def queue(audit_row, kind, provider, user_id, keys=None):
    A = store.load_json(audit_row["id"])
    if A is None:
        return 0
    todo = [(k, lab) for k, lab in strategy.targets(A, kind) if not keys or k in keys]
    n = 0
    for key, label in todo:
        cur = db.q("SELECT status FROM strategy_content WHERE audit_id=? AND kind=? AND key=?", (audit_row["id"], kind, key), one=True)
        if cur and cur["status"] in ("queued", "running"):
            continue
        db.upsert("strategy_content", {"audit_id": audit_row["id"], "kind": kind, "key": key, "label": label, "status": "queued",
                                       "content": None, "error": None, "provider": provider, "cost": None, "user_id": user_id,
                                       "updated_at": time.time(), "lease_until": None}, ("audit_id", "kind", "key"))
        n += 1
    if n:
        jobs.kick()
    return len(todo)
