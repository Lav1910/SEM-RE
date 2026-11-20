"""A few open Postgres connections shared by the whole process (change 82).

Vercel runs each request on a new thread, so a connection kept per thread was opened again on every page: a new
encrypted connection to the database (several network round trips) before the first read, once for the app's data
and once for its settings. Here a request borrows an idle connection and gives it back when its statement is done.

- Connections are in autocommit mode; a transaction (`with c.transaction():`) must finish inside the borrow.
- Only a connection that is healthy and idle (no transaction left open) goes back; anything else is closed.
- Idle connections older than IDLE_MAX_AGE are closed instead of reused, before the server or its pooler drops them.
"""
import contextlib
import threading
import time

MAX_IDLE = 6
IDLE_MAX_AGE = 240  # seconds
_idle = {}  # url -> [(connection, returned_at)]
_lock = threading.Lock()


def _close(c):
    try:
        c.close()
    except Exception:
        pass


def _new(url):
    import psycopg
    # prepare_threshold=None: works through connection poolers (Neon's pooled URL, PgBouncer)
    return psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=10)


@contextlib.contextmanager
def conn(url):
    import psycopg
    c, stale = None, []
    with _lock:
        idle = _idle.setdefault(url, [])
        while idle:
            cand, at = idle.pop()
            if not cand.closed and time.time() - at < IDLE_MAX_AGE:
                c = cand
                break
            stale.append(cand)
    for s in stale:
        _close(s)
    if c is None:
        c = _new(url)
    try:
        yield c
    finally:
        reusable = (not c.closed and not c.broken
                    and c.info.transaction_status == psycopg.pq.TransactionStatus.IDLE)
        if reusable:
            with _lock:
                idle = _idle.setdefault(url, [])
                if len(idle) < MAX_IDLE:
                    idle.append((c, time.time()))
                    c = None
        if c is not None:
            _close(c)


def close_all():
    """Tests: drop every idle connection."""
    with _lock:
        items = [c for lst in _idle.values() for c, _ in lst]
        _idle.clear()
    for c in items:
        _close(c)
