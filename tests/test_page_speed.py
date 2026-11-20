"""Change 82: page speed: a page request remembers its reads, and the portfolio reads little."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
import os
import sys
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))

from aeo import kv  # noqa: E402
from webapp import db  # noqa: E402


class ReadMemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init()

    def tearDown(self):
        db.memo_stop(); kv.memo_stop()

    def test_reads_remembered_until_a_write(self):
        email = f"memo{time.time()}@homebuildermarketers.com"
        db.x("INSERT INTO users (email, name, password_hash, created_at) VALUES (?,?,?,?)", (email, "A", "!", time.time()))
        db.memo_start()
        sql = "SELECT name FROM users WHERE email=?"
        self.assertEqual(db.q(sql, (email,), one=True)["name"], "A")
        self.assertEqual(db.q(sql, (email,), one=True)["name"], "A")
        self.assertEqual(db._local.stats[0], 1)  # read once
        self.assertEqual(db._local.stats[2], 1)  # answered from memory once
        db.x("UPDATE users SET name='B' WHERE email=?", (email,))  # a write forgets what was remembered
        self.assertEqual(db.q(sql, (email,), one=True)["name"], "B")

    def test_remembered_rows_cannot_be_changed_by_a_caller(self):
        db.memo_start()
        rows = db.q("SELECT id, email FROM users")
        if rows and isinstance(rows[0], dict):
            rows[0]["email"] = "changed"
            self.assertNotEqual(db.q("SELECT id, email FROM users")[0]["email"], "changed")

    def test_other_threads_always_read_fresh(self):
        db.memo_start()
        seen = []
        t = threading.Thread(target=lambda: seen.append(getattr(db._local, "memo", None)))
        t.start(); t.join()
        self.assertEqual(seen, [None])

    def test_settings_memo(self):
        kv.update("speed_test", lambda _o: {"n": 1}, {})
        kv.memo_start()
        v = kv.get("speed_test")
        v["n"] = 99  # a caller changing its copy doesn't change the remembered value
        self.assertEqual(kv.get("speed_test"), {"n": 1})
        self.assertEqual(kv._local.stats[0], 1)
        kv.update("speed_test", lambda _o: {"n": 2}, {})
        self.assertEqual(kv.get("speed_test"), {"n": 2})
        self.assertIsNone(kv.get("speed_test_missing"))
        self.assertEqual(kv.get("speed_test_missing", 5), 5)


class PageHooks(unittest.TestCase):
    def test_signed_in_pages_report_server_timing(self):
        from webapp import app as appmod
        app = appmod.app
        app.config["TESTING"] = True
        db.init()
        email = "speed@homebuildermarketers.com"
        if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                 (email, "S", "!", 0, "sales", time.time()))
        c = app.test_client()
        with c.session_transaction() as s:
            s["uid"] = db.q("SELECT id FROM users WHERE email=?", (email,), one=True)["id"]
            s["csrf"] = "t"
        r = c.get("/todo")
        self.assertEqual(r.status_code, 200)
        self.assertIn("db;desc=", r.headers["Server-Timing"])
        self.assertIn('type="speculationrules"', r.get_data(as_text=True))
        self.assertIsNone(getattr(db._local, "memo", None))  # turned off when the request ends
        self.assertNotIn("Server-Timing", app.test_client().get("/login").headers)  # signed-out pages say nothing


@unittest.skipUnless(db.PG, "Postgres only (set DATABASE_URL)")
class SharedConnections(unittest.TestCase):
    def test_connections_are_reused_across_threads(self):
        from aeo import pgpool
        pgpool.close_all()
        db.q("SELECT 1 AS one")
        first = pgpool._idle[db.URL][-1][0]
        seen = []
        t = threading.Thread(target=lambda: (db.q("SELECT 2 AS two"), seen.append(pgpool._idle[db.URL][-1][0])))
        t.start(); t.join()
        self.assertIs(seen[0], first)  # another thread (a new Vercel request) used the same open connection

    def test_open_transaction_is_never_handed_back(self):
        from aeo import pgpool
        pgpool.close_all()
        with pgpool.conn(db.URL) as c:
            c.execute("BEGIN")
            c.execute("SELECT 1")
        self.assertEqual(pgpool._idle.get(db.URL, []), [])
        self.assertTrue(c.closed)
        # settings still work through the pool, transaction included
        kv.update("pool_test", lambda o: {"n": (o or {}).get("n", 0) + 1}, {})
        self.assertGreaterEqual(kv.get("pool_test")["n"], 1)


if __name__ == "__main__":
    unittest.main()
