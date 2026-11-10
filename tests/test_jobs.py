"""The job runner: an audit advances in steps across several ticks (as on Vercel) and finishes with its files saved."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))
_os.environ.setdefault("HBM_PREWARM_PDFS", "0")  # no PDF engine runs in tests  # keep tests away from the real spend/health files
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))


class JobSteps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import db
        cls.db = db
        db.init()
        if not db.q("SELECT 1 FROM users WHERE email='jobs@homebuildermarketers.com'", one=True):
            db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                 ("jobs@homebuildermarketers.com", "Jobs", "!", 0, "sales", time.time()))
        cls.uid = db.q("SELECT id FROM users WHERE email='jobs@homebuildermarketers.com'", one=True)["id"]

    def test_audit_in_steps(self):
        import audit as engine
        from webapp import jobs, store
        db = self.db
        aid = "t" + os.urandom(5).hex()
        db.x("""INSERT INTO audits (id, user_id, url, domain, test_mode, status, stage, progress, created_at)
                VALUES (?,?,?,?,1,'queued','Waiting',0,?)""", (aid, self.uid, "https://example.com/", "example.com", time.time()))
        calls = {"ask": 0}

        def fake_prepare(*a, **k):
            k["progress"]("Reading the website", 10, event="Opened the site")
            return {"url": "https://example.com/", "t0": time.time(), "answers": {}, "questions": [{"id": "q1"}], "chosen": ["gemini"]}

        def fake_ask(S, progress=None, deadline=None):
            calls["ask"] += 1
            if calls["ask"] == 1:
                time.sleep(1.2)  # this tick runs out of time; the next tick continues
                return False
            S["answers"]["gemini|q1"] = {"status": "ok"}
            return True

        A = {"profile": {"domain": "example.com", "name": "Example Homes"}, "market": {"city": "Cincinnati", "state_code": "OH"},
             "analysis": {"kpis": {"score": 40, "mentioned": 1, "answers": 2, "pack": None, "pack_of": None, "checks_passed": 3,
                                   "checks_total": 9, "engines_measured": 1}}}
        with mock.patch.object(engine, "prepare", fake_prepare), mock.patch.object(engine, "ask_batch", fake_ask), \
                mock.patch.object(engine, "finish", lambda S, p=None: (A, "<html>report</html>", "loom notes")):
            jobs.work(seconds=26)            # prepare + first wave, then the tick's time is up
            row = db.q("SELECT * FROM audits WHERE id=?", (aid,), one=True)
            self.assertEqual(row["status"], "running"); self.assertEqual(row["phase"], "ask")
            self.assertTrue(store.exists(aid, "state.json"))
            jobs.work(seconds=60)            # next tick: finishes
        row = db.q("SELECT * FROM audits WHERE id=?", (aid,), one=True)
        self.assertEqual(row["status"], "done"); self.assertEqual(row["score"], 40)
        self.assertEqual(store.get_text(aid, "report.html"), "<html>report</html>")
        self.assertEqual(store.load_json(aid)["profile"]["name"], "Example Homes")
        self.assertFalse(store.exists(aid, "state.json"))
        self.assertIn("Opened the site", row["activity"])

    def test_leased_audit_not_taken_twice(self):
        db = self.db
        aid = "l" + os.urandom(5).hex()
        db.x("""INSERT INTO audits (id, user_id, url, test_mode, status, progress, created_at, lease_until)
                VALUES (?,?,?,1,'running',5,?,?)""", (aid, self.uid, "https://example.com/", time.time() - 9999, time.time() + 300))
        got = db.claim("audits", "status IN ('queued','running') AND id=?", (aid,), "created_at", 60)
        self.assertIsNone(got)
        db.x("UPDATE audits SET status='failed' WHERE id=?", (aid,))

    def test_content_piece(self):
        from webapp import jobs, store
        db = self.db
        aid = "c" + os.urandom(5).hex()
        db.x("""INSERT INTO audits (id, user_id, url, test_mode, status, progress, created_at) VALUES (?,?,?,1,'done',100,?)""",
             (aid, self.uid, "https://example.com/", time.time()))
        store.save_json(aid, "audit.json", {"profile": {"name": "X"}})
        db.upsert("strategy_content", {"audit_id": aid, "kind": "faq", "key": "all", "label": "FAQ", "status": "queued",
                                       "provider": "gemini", "updated_at": time.time()}, ("audit_id", "kind", "key"))
        with mock.patch("aeo.strategy.generate", return_value=("### Q\n\nA", 0.001)), \
                mock.patch("aeo.budget.reserve", return_value=0.05), mock.patch("aeo.budget.settle"):
            jobs.work(seconds=60)
        r = db.q("SELECT status, content FROM strategy_content WHERE audit_id=?", (aid,), one=True)
        self.assertEqual(r["status"], "done"); self.assertIn("### Q", r["content"])


class TickEndpoint(unittest.TestCase):
    def test_needs_secret(self):
        from webapp import app as appmod, jobs
        c = appmod.app.test_client()
        self.assertEqual(c.post("/api/jobs/tick").status_code, 404)
        self.assertEqual(c.get("/api/jobs/tick", headers={"Authorization": "Bearer wrong"}).status_code, 404)
        with mock.patch.object(jobs, "work", return_value=False), mock.patch.object(jobs, "pending", return_value=False), \
                mock.patch.object(jobs, "tick_secret", return_value="s3cret"):
            self.assertEqual(c.post("/api/jobs/tick", headers={"Authorization": "Bearer s3cret"}).status_code, 200)
            self.assertEqual(c.get("/api/jobs/tick", headers={"Authorization": "Bearer s3cret"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
