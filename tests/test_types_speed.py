"""Change 34 (re-ask stragglers, head start, PDFs ready early, cached files), 35 (Bulk is Quick-only, no brand
questions) and 36 (Quick / Standard / Full audit types)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import json
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
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


class Hedging(unittest.TestCase):
    def test_slow_answer_is_asked_again(self):
        import audit
        from aeo import engines
        calls = []

        def ask(en, q, m):
            calls.append(time.time())
            if len(calls) == 1:
                time.sleep(2.0)  # the straggler
                return {"status": "ok", "text": "slow"}
            return {"status": "ok", "text": "fast"}
        with mock.patch.object(engines, "ask", side_effect=ask), mock.patch.dict(audit.config.SETTINGS, {"HEDGE_AFTER": 0.2}):
            t = time.time()
            r = audit._hedged("chatgpt", {"id": "q"}, {})
        self.assertEqual(r["text"], "fast")
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(calls), 2)

    def test_fast_answer_is_not_asked_twice(self):
        import audit
        from aeo import engines
        with mock.patch.object(engines, "ask", return_value={"status": "ok", "text": "x"}) as m:
            audit._hedged("gemini", {"id": "q"}, {})
        self.assertEqual(m.call_count, 1)


class HeadStartAndBrand(unittest.TestCase):
    def setUp(self):
        from aeo import datawise, engines, locations, site
        self.asked = []
        self.read_done = [None]

        def fake_ask(en, q, m):
            self.asked.append((time.time(), q.get("kind")))
            return {"engine": en, "question_id": q["id"], "status": "ok", "text": "", "citations": []}

        real_read = site.read_site

        def slow_read(*a, **k):
            time.sleep(0.6)
            p = real_read(*a, **k)
            self.read_done[0] = time.time()
            return p
        self.patches = [mock.patch.object(engines, "ask", side_effect=fake_ask),
                        mock.patch.object(site, "rea_available", return_value=False),
                        mock.patch.object(site, "infer_market_llm", return_value=None),
                        mock.patch.object(site, "lookup_market", return_value=(None, None, None)),
                        mock.patch.object(site, "sitemap_urls", return_value=[]),
                        mock.patch.object(site, "fetch", side_effect=lambda url, timeout=25: (403, "text/html", "", url)),
                        mock.patch.object(site, "read_site", side_effect=slow_read),
                        mock.patch.object(datawise, "available", return_value=False),
                        mock.patch.object(locations, "find", side_effect=lambda p, m: [{"label": f"{m['city']}, {m['state_code']}", "main": True}])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_known_builder_starts_asking_before_the_site_is_read(self):
        import audit
        prior = {"market": {"city": "Cincinnati", "state": "Ohio", "state_code": "OH"},
                 "profile": {"name": "Acme Homes", "domain": "acme.com", "services": [], "areas_served": []}}
        S = audit.prepare("https://acme.com/", max_q=6, only=["perplexity"], use_semrush=False, prior=prior)
        self.assertEqual(S["market"]["city"], "Cincinnati")  # a blocked site still gets its market from last time
        self.assertTrue(self.asked)
        self.assertLess(min(t for t, _ in self.asked), self.read_done[0])  # the AI questions went out first

    def test_bulk_skips_brand_questions(self):
        import audit
        S = audit.prepare("https://acme.com/", "Cincinnati", "OH", max_q=6, only=["perplexity"], use_semrush=False, skip_brand=True)
        self.assertFalse([q for q in S["questions"] if q.get("kind") == "brand"])
        self.assertFalse(S["hedge"] is None)


class TypesBulkCache(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("tyboss@homebuildermarketers.com", 1), ("tyrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'ty%'")}

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def test_audit_types_are_saved_and_rerun_can_change_them(self):
        dom = f"t{uuid.uuid4().hex[:6]}.com"
        with mock.patch("webapp.jobs.kick"):
            r = self.client("tyrep").post("/audits", data={"csrf": "t", "url": dom, "depth": "standard"})
            aid = r.headers["Location"].rsplit("/", 1)[-1]
            self.assertEqual(self.db.q("SELECT depth, test_mode FROM audits WHERE id=?", (aid,), one=True)["depth"], "standard")
            r = self.client("tyrep").post(f"/audits/{aid}/rerun", data={"csrf": "t", "depth": "full"})
            nid = r.headers["Location"].rsplit("/", 1)[-1]
        row = self.db.q("SELECT depth, test_mode FROM audits WHERE id=?", (nid,), one=True)
        self.assertEqual((row["depth"], row["test_mode"]), ("full", 0))

    def test_dashboard_has_the_type_toggle(self):
        html = self.client("tyrep").get("/").get_data(as_text=True)
        for label in ("Quick", "Standard", "Full"):
            self.assertIn(f"<span>{label}</span>", html)
        self.assertIn('value="standard" checked', html)
        self.assertNotIn("$", html.split("depth-pick")[1].split("</div>")[0])  # Sales see time, not cost

    def test_jobs_pass_type_and_bulk_rules(self):
        from webapp import jobs
        got = {}

        class Stop(Exception):
            pass

        def fake_prepare(url, city, state, max_q, **kw):
            got.update(max_q=max_q, **kw)
            raise Stop()
        for depth, batch, want in (("standard", None, (9, 2, False, True)), ("quick", "b1", (6, 2, True, False)), ("full", None, (14, None, False, True))):
            row = {"id": "x" + uuid.uuid4().hex[:6], "url": "https://z.com/", "domain": "z.com", "city": None, "state": None, "test_mode": 0,
                   "depth": depth, "scope": "root", "locations": None, "batch_id": batch, "phase": None, "attempts": 0, "status": "running",
                   "activity": None, "engines": None, "progress": 0}
            with mock.patch.object(jobs.engine, "prepare", side_effect=fake_prepare):
                try:
                    jobs.step_audit(row, time.time() + 100)
                except Stop:
                    pass
            self.assertEqual((got["max_q"], got["max_locations"], got["skip_brand"], got["hedge"]), want, depth)

    def test_static_files_are_versioned_and_cached(self):
        with self.app.test_request_context():
            from flask import url_for
            u = url_for("static", filename="app.css")
        self.assertIn("?v=", u)
        r = self.app.test_client().get(u)
        self.assertIn("immutable", r.headers.get("Cache-Control", ""))

    @unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
    def test_pdfs_are_ready_before_export(self):
        from webapp import app as appmod, pdf, store
        A = json.load(open(SAMPLE))
        aid = "p" + uuid.uuid4().hex[:11]
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, builder_name, created_at, share_token) VALUES (?,?,?,?, 'done', ?, ?, ?)""",
                  (aid, self.ids["tyrep"], "https://" + A["profile"]["domain"] + "/", A["profile"]["domain"], A["profile"]["name"], time.time(), uuid.uuid4().hex))
        store.save_json(aid, "audit.json", A)
        with mock.patch.object(pdf, "make_pdf", return_value=b"%PDF-1.4 fake") as m:
            with self.app.test_request_context():
                appmod.prewarm_pdfs(aid)
            self.assertEqual(m.call_count, 2)  # Full and Overview built in the background
            r = self.client("tyrep").get(f"/audits/{aid}/export-overview.pdf")
            self.assertEqual(r.data, b"%PDF-1.4 fake")
            self.assertEqual(m.call_count, 2)  # export used the ready file, no new PDF made


if __name__ == "__main__":
    unittest.main()
