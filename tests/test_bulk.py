"""Bulk Analysis: CSV reading, list checks, starting a batch, priority and limits in the job runner, finishing."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))


class CSVReading(unittest.TestCase):
    def setUp(self):
        from webapp import bulk
        self.b = bulk

    def test_header_csv(self):
        rows = self.b.parse_rows("Website,Builder,City,State\njdh.com,Justin Doyle,Cincinnati,OH\nhttps://www.cgh.com/,,,\n")
        self.assertEqual([r["url"] for r in rows], ["jdh.com", "https://www.cgh.com/"])
        self.assertEqual(rows[0]["name"], "Justin Doyle"); self.assertEqual(rows[0]["state"], "OH")

    def test_plain_list_and_semicolons(self):
        self.assertEqual([r["url"] for r in self.b.parse_rows("a-homes.com\nb-homes.com\n\n")], ["a-homes.com", "b-homes.com"])
        rows = self.b.parse_rows("domain;notes\nc-homes.com;hot lead\n")
        self.assertEqual(rows[0]["url"], "c-homes.com"); self.assertEqual(rows[0]["notes"], "hot lead")

    def test_checks(self):
        rows = self.b.check_rows(self.b.parse_rows("website\nd-homes.com\nwww.d-homes.com\nnot a site\n"))
        self.assertEqual([r["flag"] for r in rows], ["ok", "duplicate", "invalid"])


class BulkFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("bulkboss@homebuildermarketers.com", 1), ("bulkrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))

    def client(self, email):
        c = self.app.test_client()
        u = self.db.q("SELECT id FROM users WHERE email=?", (email,), one=True)
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = u["id"], "t"
        return c

    def _start(self, c, sites, depth="quick"):
        import json
        rows = json.dumps([{"url": s} for s in sites])
        with mock.patch("webapp.jobs.kick"):
            return c.post("/bulk/start", data={"csrf": "t", "rows": rows, "depth": depth, "name": "Test batch"})

    def test_sales_can_start_and_rows_become_audits(self):
        c = self.client("bulkrep@homebuildermarketers.com")
        self.assertEqual(c.get("/bulk").status_code, 200)
        r = self._start(c, ["e1-homes.com", "e2-homes.com", "e3-homes.com"])
        self.assertEqual(r.status_code, 302)
        bid = r.headers["Location"].rsplit("/", 1)[-1]
        aud = self.db.q("SELECT * FROM audits WHERE batch_id=?", (bid,))
        self.assertEqual(len(aud), 3); self.assertTrue(all(a["test_mode"] == 1 and a["status"] == "queued" for a in aud))
        self.assertEqual(c.get(f"/bulk/{bid}").status_code, 200)
        self.assertEqual(c.get(f"/api/bulk/{bid}").json["counts"]["queued"], 3)
        self.assertIn(b"Opportunity", c.get(f"/bulk/{bid}/export.csv").data)
        self.db.x("UPDATE audits SET status='failed' WHERE batch_id=?", (bid,))
        self.db.x("UPDATE batches SET status='done' WHERE id=?", (bid,))

    def test_preview_from_upload(self):
        import io
        c = self.client("bulkboss@homebuildermarketers.com")
        r = c.post("/bulk/preview", data={"csrf": "t", "csv": (io.BytesIO(b"website\nf-homes.com\nnope\n"), "list.csv")},
                   content_type="multipart/form-data")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"Start bulk analysis (1 site)", r.data)

    def test_spending_cap_blocks(self):
        c = self.client("bulkrep@homebuildermarketers.com")
        with mock.patch("aeo.budget.status", return_value={"limit_usd": 0.01, "spent_usd": 0.0, "calls": {}}), \
                mock.patch("webapp.bulk.estimate", return_value={"minutes": 5, "cost": 5.0}):
            r = self._start(c, ["g-homes.com"])
        self.assertIn("/bulk", r.headers["Location"]); self.assertNotIn("/bulk/b", r.headers["Location"])

    def test_priority_pause_and_limit(self):
        from webapp import jobs
        db = self.db
        self.db.x("UPDATE audits SET status='failed' WHERE status IN ('queued','running')")  # clean slate
        c = self.client("bulkboss@homebuildermarketers.com")
        bid = self._start(c, [f"p{i}-homes.com" for i in range(5)]).headers["Location"].rsplit("/", 1)[-1]
        uid = db.q("SELECT id FROM users WHERE email='bulkboss@homebuildermarketers.com'", one=True)["id"]
        sid = "s" + os.urandom(5).hex()
        db.x("""INSERT INTO audits (id, user_id, url, test_mode, status, progress, created_at) VALUES (?, ?, 'https://s.com/', 1,
                'queued', 0, ?)""", (sid, uid, time.time() + 100))
        first = jobs._claim_audit(60)
        self.assertEqual(first["id"], sid)  # a single audit goes ahead of the older bulk ones
        db.x("UPDATE audits SET status='done', lease_until=NULL WHERE id=?", (sid,))
        got = []
        for _ in range(5):
            r = jobs._claim_audit(60)
            if r:
                db.x("UPDATE audits SET status='running' WHERE id=?", (r["id"],)); got.append(r["id"])
        self.assertEqual(len(got), jobs.BULK_PARALLEL)  # never more than 3 bulk audits at once
        db.x("UPDATE audits SET lease_until=NULL, status='queued' WHERE batch_id=?", (bid,))
        db.x("UPDATE batches SET status='paused' WHERE id=?", (bid,))
        self.assertIsNone(jobs._claim_audit(60))   # paused batches wait
        db.x("UPDATE batches SET status='running' WHERE id=?", (bid,))
        self.assertIsNotNone(jobs._claim_audit(60))
        # finishing: the last audit closes the batch and emails once
        db.x("UPDATE audits SET status='done', lease_until=NULL WHERE batch_id=?", (bid,))
        one = db.q("SELECT id FROM audits WHERE batch_id=? LIMIT 1", (bid,), one=True)["id"]
        with mock.patch("webapp.mailer.send") as send:
            jobs.after_audit(one); jobs.after_audit(one)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(db.q("SELECT status FROM batches WHERE id=?", (bid,), one=True)["status"], "done")

    def test_controls(self):
        c = self.client("bulkboss@homebuildermarketers.com")
        bid = self._start(c, ["h1-homes.com", "h2-homes.com"]).headers["Location"].rsplit("/", 1)[-1]
        with mock.patch("webapp.jobs.kick"):
            c.post(f"/bulk/{bid}/action", data={"csrf": "t", "action": "pause"})
            self.assertEqual(self.db.q("SELECT status FROM batches WHERE id=?", (bid,), one=True)["status"], "paused")
            c.post(f"/bulk/{bid}/action", data={"csrf": "t", "action": "cancel"})
            self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id=? AND status='failed'", (bid,), one=True)["n"], 2)
            c.post(f"/bulk/{bid}/action", data={"csrf": "t", "action": "retry"})
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM audits WHERE batch_id=? AND status='queued'", (bid,), one=True)["n"], 2)
        self.db.x("UPDATE audits SET status='failed' WHERE batch_id=?", (bid,))
        self.db.x("UPDATE batches SET status='done' WHERE id=?", (bid,))


if __name__ == "__main__":
    unittest.main()
