"""Change 24 (Sales and reports never show platform errors) and change 25 (audit history: run time, cost, search, pages)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import copy
import json
import os
import sys
import tempfile
import time
import unittest
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


class Helpers(unittest.TestCase):
    def test_duration(self):
        from webapp.app import duration
        self.assertEqual([duration(None), duration(7), duration(167), duration(3785)], ["—", "7s", "2m 47s", "1h 3m 5s"])

    def test_audit_cost(self):
        from aeo import budget
        A = {"answers": [
            {"engine": "perplexity", "status": "ok", "cost_usd": 0.004},       # exact, reported by the provider
            {"engine": "chatgpt", "status": "ok", "cost_usd": None},           # typical price
            {"engine": "gemini", "status": "error", "cost_usd": None},         # failed calls cost nothing
            {"engine": "google_aio", "status": "ok", "source": "semrush"},     # Semrush units, not dollars
            {"engine": "google_ai_mode", "status": "no_answer", "cost_usd": 0.003}]}
        self.assertAlmostEqual(budget.audit_cost(A), 0.004 + budget.TYPICAL["chatgpt"] + 0.003, places=4)


@unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
class CleanViews(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        A = json.load(open(SAMPLE))
        for a in A["answers"]:  # make ChatGPT fail everywhere, like a rejected key
            if a["engine"] == "chatgpt":
                a.update(status="error", error="HTTP 401: invalid api key", text="", citations=[])
        cls.A = A

    def test_sales_view_shows_failed_platforms_as_zero(self):
        from webapp import views
        full, clean = views.build(copy.deepcopy(self.A)), views.build(copy.deepcopy(self.A), clean=True)
        self.assertEqual(full["dist"][[d["engine"] for d in full["dist"]].index("chatgpt")]["status"]["label"], "Key rejected")
        row = next(d for d in clean["dist"] if d["engine"] == "chatgpt")  # change 28: listed as 0%, no label
        self.assertEqual((row["mentions"], row["visibility"], row["status"]["label"]), (0, 0, ""))
        self.assertEqual(len(clean["dist"]), 5)
        self.assertNotIn("chatgpt", clean["platforms"])  # but not offered in the platform filter
        self.assertTrue(all(r["status"] in ("ok", "no_answer") for p in clean["prompts"] for r in p["rows"]))
        self.assertEqual(full["score"], clean["score"])  # the score never counted failed answers

    def test_report_has_no_error_wording(self):
        from aeo import render
        from webapp import views
        html = render.render(self.A, {"V": views.build(self.A, "all", clean=True), "chart": views.chart(self.A, [])})
        self.assertNotIn("Not measured in this audit:", html)
        self.assertNotIn("<th>ChatGPT</th>", html)
        for word in ("Key rejected", "Needs verification", "Needs credits"):
            self.assertNotIn(word, html)


class HistoryAndStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("histboss@homebuildermarketers.com", 1), ("histrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'hist%'")}
        cls.tag = uuid.uuid4().hex[:6]
        now = time.time()
        for i in range(30):  # 30 finished audits by the rep, 167 s each, $0.50 each
            db.x("""INSERT INTO audits (id, user_id, url, domain, status, builder_name, created_at, started_at, finished_at, cost_usd, share_token)
                    VALUES (?,?,?,?, 'done', ?, ?, ?, ?, 0.5, ?)""",
                 (f"h{cls.tag}{i:02d}", cls.ids["histrep"], f"https://b{i}{cls.tag}.com", f"b{i}{cls.tag}.com",
                  ("Zebra Homes " if i == 7 else "Builder ") + cls.tag, now - 1000 + i, now - 500 + i, now - 333 + i, uuid.uuid4().hex))

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def test_pages_of_25(self):
        page1 = self.client("histrep").get(f"/?q={self.tag}").get_data(as_text=True)
        self.assertIn("1–25 of 30", page1)
        self.assertEqual(page1.count('class="btn btn-ghost btn-small" href="/audits/h'), 25)
        page2 = self.client("histrep").get(f"/?q={self.tag}&page=2").get_data(as_text=True)
        self.assertIn("26–30 of 30", page2)
        self.assertIn("2m 47s", page1)  # run time to the second

    def test_search(self):
        html = self.client("histrep").get("/?q=zebra").get_data(as_text=True)
        self.assertIn("Zebra Homes", html)
        self.assertNotIn(f"Builder {self.tag}", html)

    def test_cost_only_for_admins(self):
        boss = self.client("histboss").get(f"/?scope=team&q={self.tag}").get_data(as_text=True)
        self.assertIn("Total cost", boss)
        self.assertIn("$15.00", boss)  # 30 × $0.50, across all pages
        rep = self.client("histrep").get(f"/?q={self.tag}").get_data(as_text=True)
        self.assertNotIn("Total cost", rep)
        self.assertNotIn("$0.50", rep)

    def test_live_progress_hides_problems_from_sales(self):
        aid = "p" + self.tag
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, created_at, progress, engines, activity, share_token)
                     VALUES (?,?, 'https://x.com', 'x.com', 'done', ?, 60, ?, ?, ?)""",
                  (aid, self.ids["histrep"], time.time(),
                   json.dumps({"chatgpt": {"state": "needs_credits"}, "gemini": {"state": "running"}, "google_ai_mode": {"state": "off"}}),
                   json.dumps([{"t": 1, "text": "Skipped ChatGPT: the account needs API credits"}, {"t": 2, "text": "Asked Gemini: who?"}]),
                   uuid.uuid4().hex))
        rep = self.client("histrep").get(f"/api/audits/{aid}").get_json()
        self.assertEqual(list(rep["engines"]), ["gemini"])
        self.assertEqual([e["text"] for e in rep["activity"]], ["Asked Gemini: who?"])
        boss = self.client("histboss").get(f"/api/audits/{aid}").get_json()
        self.assertEqual(len(boss["engines"]), 3)


if __name__ == "__main__":
    unittest.main()
