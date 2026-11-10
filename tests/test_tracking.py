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
        detail = self.client("trrep").get(f"/links/{self.aid}").get_data(as_text=True)
        for want in ("Search demand", "Switched to", "ChatGPT", "Cincinnati, OH", "Phone"):
            self.assertIn(want, detail)
        lst = self.client("trrep").get("/links").get_data(as_text=True)
        self.assertIn(self.A["profile"]["name"], lst)
        self.assertNotIn(self.A["profile"]["name"], self.client("trrep2").get("/links").get_data(as_text=True))  # Sales see their own
        self.assertIn(self.A["profile"]["name"], self.client("trboss").get("/links").get_data(as_text=True))   # admins see everyone's

    def test_every_main_page_loads_with_visits(self):
        """The Client links badge and markers run on every page; they must work on Postgres too."""
        l = self.link()
        self.app.test_client().get(self.path(l))  # one client visit
        for who in ("trboss", "trrep"):
            c = self.client(who)
            for url in ("/", "/?scope=team", "/links", f"/links/{self.aid}", "/todo", f"/audits/{self.aid}/overview", "/bulk", "/account"):
                self.assertEqual(c.get(url).status_code, 200, (who, url))
        self.assertEqual(self.client("trboss").get("/admin").status_code, 200)

    def test_strategy_tabs_preview_and_full(self):
        l = self.link()
        anon = self.app.test_client()
        from aeo import strategy as strat
        hows = [it["how"] for it in strat.plan(self.A) if it.get("how")]
        for view in ("strategy", "content", "fixes"):
            r = anon.get(self.path(l, view))
            self.assertEqual(r.status_code, 200, view)
            html = r.get_data(as_text=True)
            for hidden in ("<form", "data-gen-form", "Export plan", "status-ctl", "Regenerate"):
                self.assertNotIn(hidden, html, (view, hidden))  # read-only: no controls for the client
        html = anon.get(self.path(l, "strategy")).get_data(as_text=True)
        self.assertIn("Get this done with HBM", html)  # Preview by default
        from markupsafe import escape
        for h in hows:
            self.assertNotIn(str(escape(h)), html)  # the how-to is never sent to a Preview link, not even hidden
        self.link(act="full")
        html = self.app.test_client().get(self.path(l, "strategy")).get_data(as_text=True)
        self.assertNotIn('class="locked"', html)
        self.assertIn(str(escape(hows[0])), html)

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
