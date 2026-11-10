"""Change 41 (client link tracking), 42 (Strategy tabs on the client link, Preview / Full) and 43 (To-do selection)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


@unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
class Tracking(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("trboss@homebuildermarketers.com", 1), ("trrep@homebuildermarketers.com", 0), ("trrep2@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'tr%'")}
        cls.A = json.load(open(SAMPLE))

    def setUp(self):
        from webapp import store
        self.aid = "t" + uuid.uuid4().hex[:11]
        dom = self.A["profile"]["domain"]
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, builder_name, created_at, share_token) VALUES (?,?,?,?, 'done', ?, ?, ?)""",
                  (self.aid, self.ids["trrep"], f"https://{dom}/", dom, self.A["profile"]["name"], time.time(), uuid.uuid4().hex))
        store.save_json(self.aid, "audit.json", self.A)

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def link(self, who="trrep", act="create"):
        r = self.client(who).post(f"/audits/{self.aid}/client-link", data={"csrf": "t", "action": act}, headers={"Accept": "application/json"})
        return r.get_json()["link"]

    def path(self, link, view="overview"):
        return link["url"].split("localhost", 1)[-1].replace("/overview", "/" + view)

    def test_visits_pings_and_team_views(self):
        from webapp import mailer
        l = self.link()
        with mock.patch.object(mailer, "configured", return_value=True), mock.patch.object(mailer, "send") as sent:
            anon = self.app.test_client()
            anon.get(self.path(l), headers={"User-Agent": "Mozilla/5.0 (iPhone)", "X-Vercel-IP-City": "Cincinnati", "X-Vercel-IP-Country-Region": "OH"})
            anon.get(self.path(l, "competitors"))
            with anon.session_transaction() as s:
                csrf = s["csrf"]
            anon.post(self.path(l, "ping"), data={"csrf": csrf, "d": json.dumps({"s": {"Search demand": 40, "Distribution by AI platform": 12},
                                                                                     "a": [{"k": "platform", "d": "ChatGPT"}]})})
            anon.post(self.path(l, "ping"), data={"csrf": csrf, "d": json.dumps({"s": {"Search demand": 20}})})
            self.client("trboss").get(self.path(l))  # a signed-in teammate: not counted
            self.assertEqual(sent.call_count, 1)  # one alert to the sender
            self.assertIn("just opened", sent.call_args[0][1])
        v = self.db.q("SELECT * FROM client_visits WHERE audit_id=?", (self.aid,))
        self.assertEqual(len(v), 1)
        v = v[0]
        self.assertEqual((v["device"], v["city"], v["region"], v["pages"], v["seconds"]), ("Phone", "Cincinnati", "OH", 2, 72))
        self.assertEqual(json.loads(v["sections"])["Search demand"], 60)
        self.assertEqual(self.db.q("SELECT client_views FROM audits WHERE id=?", (self.aid,), one=True)["client_views"], 1)
        detail = self.client("trrep").get(f"/audits/{self.aid}/tracking").get_data(as_text=True)
        for want in ("Search demand", "Switched to", "ChatGPT", "Cincinnati, OH", "Phone"):
            self.assertIn(want, detail)

    def test_every_main_page_loads_with_visits(self):
        """The Client links badge and markers run on every page; they must work on Postgres too."""
        l = self.link()
        self.app.test_client().get(self.path(l))  # one client visit
        for who in ("trboss", "trrep"):
            c = self.client(who)
            for url in ("/", "/?scope=team", f"/audits/{self.aid}/tracking", "/links", "/inventory", "/inventory?mode=all", "/todo",
                        f"/audits/{self.aid}/overview", f"/audits/{self.aid}/content", "/bulk", "/account"):
                self.assertEqual(c.get(url).status_code, 200, (who, url))
        self.assertEqual(self.client("trboss").get("/admin").status_code, 200)

    def test_strategy_tabs_open_and_content_written(self):
        """change 46: no locks on the client link, and every content piece is queued when the link is created."""
        from aeo import strategy as strat
        from markupsafe import escape
        with mock.patch("webapp.jobs.kick"):
            l = self.link()
            queued = self.db.q("SELECT COUNT(*) AS n FROM strategy_content WHERE audit_id=? AND status='queued'", (self.aid,), one=True)["n"]
            want = sum(len(strat.targets(self.A, k)) for k in strat.GENERATORS)
            self.assertEqual(queued, want)
            self.link()  # opening the panel again doesn't queue (or pay for) anything twice
            self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM strategy_content WHERE audit_id=?", (self.aid,), one=True)["n"], want)
        anon = self.app.test_client()
        hows = [it["how"] for it in strat.plan(self.A) if it.get("how")]
        for view in ("strategy", "content", "fixes"):
            html = anon.get(self.path(l, view)).get_data(as_text=True)
            for hidden in ("<form", "data-gen-form", "Export plan", "status-ctl", 'class="locked"'):
                self.assertNotIn(hidden, html, (view, hidden))
            self.assertIn("Want Home Builder Marketers to put this live for you?", html)
            self.assertIn("Download all (Word)", html)
        html = anon.get(self.path(l, "strategy")).get_data(as_text=True)
        self.assertIn(str(escape(hows[0])), html)
        self.assertIn("Being written", anon.get(self.path(l, "content")).get_data(as_text=True))

    def test_tracking_lives_in_the_audit(self):
        """change 44: Report tracking in the audit sidebar; the Audits page has no Client links tab."""
        l = self.link()
        self.app.test_client().get(self.path(l))
        c = self.client("trrep")
        page = c.get(f"/audits/{self.aid}/tracking").get_data(as_text=True)
        self.assertIn("Report tracking", page)
        self.assertIn(l["url"], page)
        self.assertIn("Report tracking", c.get("/").get_data(as_text=True))  # change 48: in the Audits sidebar
        self.assertIn(f"/audits/{self.aid}/tracking", c.get(f"/links/{self.aid}").headers["Location"])

    def test_inventory_word_and_zip(self):
        """change 47/48: Inventory lists every audit's files; the Word file and zip download work."""
        c = self.client("trrep")
        html = c.get("/inventory?mode=all").get_data(as_text=True)
        for want in ("Full report", "Overview (1 page)", "All deliverables", "Strategy plan", "Loom talking points", "storage used"):
            self.assertIn(want, html)
        self.assertNotIn("Audit data", html)  # Sales don't get raw data
        self.assertIn(self.A["profile"]["name"], c.get("/inventory").get_data(as_text=True))  # folders view
        r = c.get(f"/audits/{self.aid}/deliverables.docx")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data[:2], b"PK")  # a real .docx (zip) file
        import zipfile, io as _io
        r = c.post("/inventory/zip", data={"csrf": "t", "f": [f"{self.aid}:word", f"{self.aid}:loom"]})
        self.assertEqual(r.status_code, 200)
        names = zipfile.ZipFile(_io.BytesIO(r.data)).namelist()
        self.assertTrue(any(n.endswith("All-Deliverables.docx") for n in names), names)
        l = self.link()
        r = self.app.test_client().get(self.path(l, "deliverables.docx"))  # the client can download it too
        self.assertEqual(r.data[:2], b"PK")
        self.assertIn("Talk to us", self.app.test_client().get(self.path(l, "content")).get_data(as_text=True))
        self.assertIn("info%40homebuildermarketers.com", self.app.test_client().get(self.path(l, "content")).get_data(as_text=True).replace("@", "%40"))

    def test_todo_bulk_actions(self):
        uid = self.ids["trrep2"]
        self.db.x("DELETE FROM todos WHERE user_id=?", (uid,))
        for i in range(4):
            self.db.x("INSERT INTO todos (user_id, domain, url, status, created_at) VALUES (?,?,?, 'todo', ?)", (uid, f"b{i}.com", f"https://b{i}.com/", time.time() + i))
        ids = [r["id"] for r in self.db.q("SELECT id FROM todos WHERE user_id=? ORDER BY id", (uid,))]
        c = self.client("trrep2")
        c.post("/todo/bulk", data={"csrf": "t", "action": "done", "ids": ids[:2]})
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM todos WHERE user_id=? AND status='done'", (uid,), one=True)["n"], 2)
        c.post("/todo/bulk", data={"csrf": "t", "action": "remove", "ids": ids[2:]})
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM todos WHERE user_id=?", (uid,), one=True)["n"], 2)
        with mock.patch("webapp.jobs.kick"):
            c.post("/todo/bulk", data={"csrf": "t", "action": "quick", "ids": ids[:2]})
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM audits WHERE user_id=? AND depth='quick' AND status='queued'", (uid,), one=True)["n"], 2)
        other = self.client("trrep").post("/todo/bulk", data={"csrf": "t", "action": "remove", "ids": ids[:2]})  # not their rows
        self.assertEqual(self.db.q("SELECT COUNT(*) AS n FROM todos WHERE user_id=?", (uid,), one=True)["n"], 2)


if __name__ == "__main__":
    unittest.main()
