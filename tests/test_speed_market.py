"""Change 29 (faster audits: parallel lanes, early asking, instant re-open, live counts) and change 30 (automatic
market lookup)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))


def _fake_ask(delay):
    def ask(en, q, market):
        time.sleep(delay)
        return {"engine": en, "question_id": q["id"], "status": "ok", "text": "Try Acme Homes or Zed Builders.",
                "citations": [{"domain": "acmehomes.com", "url": "https://acmehomes.com/", "title": ""}]}
    return ask


class ParallelLanes(unittest.TestCase):
    def test_platforms_run_side_by_side(self):
        """30 answers at 0.3 s each: in waves of 8 this took over a second; in lanes it takes about one answer's time."""
        import audit
        from aeo import engines
        qs = [{"id": f"q{i}", "prompt": "p", "keyword": "k"} for i in range(6)]
        S = {"questions": qs, "chosen": list(engines.ENGINES), "answers": {}, "market": {"city": "X", "state": "OH"},
             "est": {en: {"done": 0, "total": 6, "state": "running"} for en in engines.ENGINES},
             "profile": {"name": "Acme Homes", "domain": "acmehomes.com"}}
        with mock.patch.object(engines, "ask", side_effect=_fake_ask(0.3)):
            t = time.time()
            self.assertTrue(audit.ask_batch(S))
            took = time.time() - t
        self.assertEqual(len(S["answers"]), 30)
        self.assertLess(took, 1.0)
        self.assertEqual(S["est"]["chatgpt"]["named"], 6)  # live "named so far" count

    def test_deadline_stops_new_work(self):
        import audit
        from aeo import engines
        qs = [{"id": f"q{i}", "prompt": "p", "keyword": "k"} for i in range(30)]
        S = {"questions": qs, "chosen": ["chatgpt"], "answers": {}, "market": {"city": "X", "state": "OH"},
             "est": {"chatgpt": {"done": 0, "total": 30, "state": "running"}}}
        with mock.patch.object(engines, "ask", side_effect=_fake_ask(0.2)), mock.patch.dict(audit.config.SETTINGS, {"PER_PLATFORM": 5}):
            done = audit.ask_batch(S, deadline=time.time() + 0.3)
        self.assertFalse(done)
        self.assertLess(len(S["answers"]), 30)  # the rest is picked up by the next run


class MarketLookup(unittest.TestCase):
    def setUp(self):
        from aeo import datawise, locations, site
        from aeo import engines
        self.patches = [mock.patch.object(engines, "ask", side_effect=_fake_ask(0)),  # never call a real AI platform in tests
                        mock.patch.object(site, "rea_available", return_value=False),
            mock.patch.object(site, "infer_market_llm", return_value=None),  # no real AI calls in tests
                        mock.patch.object(site, "sitemap_urls", return_value=[]),
                        mock.patch.object(site, "fetch", side_effect=lambda url, timeout=25: (403, "text/html", "", url)),
                        mock.patch.object(datawise, "available", return_value=False),
                        mock.patch("aeo.dfs_volume.available", return_value=False),  # no real DataForSEO calls in tests
                        mock.patch.object(locations, "find", side_effect=lambda p, m: [{"label": f"{m['city']}, {m['state_code']}", "main": True}])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_found_market_runs_without_asking(self):
        import audit
        from aeo import site
        with mock.patch.object(site, "lookup_market", return_value=("Solana Beach", "CA", "a Google search")):
            S = audit.prepare("https://tourmalinebuilders.com/", max_q=6, only=["perplexity"], use_semrush=False)
        self.assertEqual((S["market"]["city"], S["market"]["state_code"]), ("Solana Beach", "CA"))

    def test_nothing_found_asks_for_market(self):
        import audit
        from aeo import site
        with mock.patch.object(site, "lookup_market", return_value=(None, None, None)):
            with self.assertRaises(audit.AuditError) as cm:
                audit.prepare("https://tourmalinebuilders.com/", max_q=6, only=["perplexity"], use_semrush=False)
        self.assertIn("city and state", str(cm.exception))

    def test_lookup_rejects_unclear_answers(self):
        from aeo import config, engines, site
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "x"}), mock.patch.object(config, "key", side_effect=lambda n: "x" if n == "GEMINI_API_KEY" else None), \
                mock.patch.object(engines, "_post", return_value={}), \
                mock.patch.object(engines, "parse_gemini", return_value=('{"city": "Paris", "state": "France"}', [])):
            self.assertEqual(site.lookup_market("Acme", "acme.com"), (None, None, None))
        with mock.patch.object(config, "key", side_effect=lambda n: "x" if n == "GEMINI_API_KEY" else None), \
                mock.patch.object(engines, "_post", return_value={}), \
                mock.patch.object(engines, "parse_gemini", return_value=('Sure: {"city": "Solana Beach", "state": "ca"}', [])):
            self.assertEqual(site.lookup_market("Acme", "acme.com"), ("Solana Beach", "CA", "a Google search"))


class ReopenAndWording(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("spdboss@homebuildermarketers.com", 1), ("spdrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'spd%'")}

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def test_recent_audit_opens_instantly(self):
        dom = f"recent{uuid.uuid4().hex[:6]}.com"
        aid = "r" + uuid.uuid4().hex[:11]
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, scope, status, created_at, share_token)
                     VALUES (?,?,?,?, 'root', 'done', ?, ?)""", (aid, self.ids["spdboss"], f"https://{dom}/", dom, time.time() - 86400, uuid.uuid4().hex))
        with mock.patch("webapp.jobs.kick"):
            r = self.client("spdrep").post("/audits", data={"csrf": "t", "url": dom})
            self.assertIn(f"/audits/{aid}/overview", r.headers["Location"])
            self.assertIn("reopened=1", r.headers["Location"])
            r = self.client("spdrep").post("/audits", data={"csrf": "t", "url": dom, "confirm_duplicate": "1"})  # "Run fresh"
            self.assertNotIn(aid, r.headers["Location"])

    def test_sales_get_a_question_not_an_error(self):
        aid = "f" + uuid.uuid4().hex[:11]
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, error, created_at, share_token) VALUES (?,?,?,?, 'failed', ?, ?, ?)""",
                  (aid, self.ids["spdrep"], "https://x.com/", "x.com",
                   "This website turns away automated visitors, so we couldn't read the builder's city and state. Enter them and run again.",
                   time.time(), uuid.uuid4().hex))
        rep = self.client("spdrep").get(f"/audits/{aid}").get_data(as_text=True)
        self.assertIn("We couldn't confirm where this builder is based", rep)
        self.assertNotIn("turns away automated visitors", rep)
        self.assertIn("Run with this market", rep)
        boss = self.client("spdboss").get(f"/audits/{aid}").get_data(as_text=True)
        self.assertIn("turns away automated visitors", boss)

    def test_progress_bar_never_moves_back(self):
        from webapp import jobs
        aid = "g" + uuid.uuid4().hex[:11]
        self.db.x("INSERT INTO audits (id, user_id, url, domain, status, created_at, share_token, progress) VALUES (?,?, 'https://y.com/', 'y.com', 'running', ?, ?, 0)",
                  (aid, self.ids["spdrep"], time.time(), uuid.uuid4().hex))
        row = self.db.q("SELECT * FROM audits WHERE id=?", (aid,), one=True)
        p = jobs._progress_writer(aid, row)
        p("Asking", 45, event="a")
        p("Finding locations", 20, event="b")
        self.assertEqual(self.db.q("SELECT progress FROM audits WHERE id=?", (aid,), one=True)["progress"], 45)


if __name__ == "__main__":
    unittest.main()
