"""Change 31: an interactive client link (Visibility Overview and Competitor Research only, no sign-in)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
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


@unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
class ClientLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db, store
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("clboss@homebuildermarketers.com", 1), ("clrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'cl%'")}
        cls.A = json.load(open(SAMPLE))

    def setUp(self):
        from webapp import store
        self.aid = "c" + uuid.uuid4().hex[:11]
        dom = self.A["profile"]["domain"]
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, builder_name, created_at, started_at, finished_at, share_token)
                     VALUES (?,?,?,?, 'done', ?, ?, ?, ?, ?)""",
                  (self.aid, self.ids["clrep"], f"https://{dom}/", dom, self.A["profile"]["name"], time.time(), time.time(), time.time(), uuid.uuid4().hex))
        store.save_json(self.aid, "audit.json", self.A)

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def make_link(self, who="clrep", act="create"):
        r = self.client(who).post(f"/audits/{self.aid}/client-link", data={"csrf": "t", "action": act}, headers={"Accept": "application/json"})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["link"]

    def test_client_sees_only_client_tabs(self):
        link = self.make_link()
        path = link["url"].split("localhost", 1)[-1]
        anon = self.app.test_client()
        r = anon.get(path)
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Visibility Overview", html)
        self.assertIn("Competitor Research", html)
        self.assertIn("Strategy Plan", html)  # change 42: the Strategy tabs are on the client link too
        for hidden in ("Prompt Research", "Run again", "Export to PDF", "Generate all", "Send feedback", "Site Audit", "rail-item"):
            self.assertNotIn(hidden, html)
        self.assertEqual(r.headers["X-Robots-Tag"], "noindex, nofollow")
        self.assertEqual(anon.get(path.replace("/overview", "/competitors")).status_code, 200)
        self.assertEqual(anon.get(path.replace("/overview", "/prompts")).status_code, 404)
        self.assertEqual(anon.get(path + "?platform=perplexity").status_code, 200)  # toggles work

    def test_views_are_counted_once_per_visit(self):
        link = self.make_link()
        path = link["url"].split("localhost", 1)[-1]
        anon = self.app.test_client()
        anon.get(path); anon.get(path + "?platform=gemini"); anon.get(path.replace("/overview", "/competitors"))
        self.app.test_client().get(path)  # a second person
        row = self.db.q("SELECT client_views FROM audits WHERE id=?", (self.aid,), one=True)
        self.assertEqual(row["client_views"], 2)
        self.assertEqual(self.make_link()["views"], 2)  # the same link is reused and shows the count

    def test_turn_off_new_and_expiry(self):
        first = self.make_link()
        p1 = first["url"].split("localhost", 1)[-1]
        second = self.make_link(act="new")
        self.assertNotEqual(first["url"], second["url"])
        self.assertEqual(self.app.test_client().get(p1).status_code, 404)  # the old link stops working
        self.assertIsNone(self.make_link(act="off"))
        r = self.app.test_client().get(second["url"].split("localhost", 1)[-1])
        self.assertEqual(r.status_code, 404)
        self.assertIn("This link has expired", r.get_data(as_text=True))
        third = self.make_link()
        self.db.x("UPDATE audits SET client_expires=? WHERE id=?", (time.time() - 1, self.aid))
        self.assertEqual(self.app.test_client().get(third["url"].split("localhost", 1)[-1]).status_code, 404)
        self.assertEqual(self.app.test_client().get("/c/not-a-real-token-at-all-xx/overview").status_code, 404)

    def test_expiry_clock(self):
        """Change 33: the client sees when the link stops working; the last day counts down in amber."""
        link = self.make_link()
        path = link["url"].split("localhost", 1)[-1]
        html = self.app.test_client().get(path).get_data(as_text=True)
        self.assertIn("Link expires in 30 days", html)
        self.assertNotIn("client-expiry soon", html)
        self.db.x("UPDATE audits SET client_expires=? WHERE id=?", (time.time() + 2 * 3600 + 60, self.aid))
        html = self.app.test_client().get(path).get_data(as_text=True)
        self.assertIn("client-expiry soon", html)
        self.assertIn("Expires in 2h 0m", html)

    def test_link_ticks_the_todo_list(self):
        self.db.x("DELETE FROM todos WHERE user_id=?", (self.ids["clrep"],))
        self.db.x("INSERT INTO todos (user_id, domain, url, status, created_at) VALUES (?,?,?, 'todo', ?)",
                  (self.ids["clrep"], self.A["profile"]["domain"], "https://" + self.A["profile"]["domain"] + "/", time.time() - 10))
        self.make_link()
        row = self.db.q("SELECT status, done_via FROM todos WHERE user_id=?", (self.ids["clrep"],), one=True)
        self.assertEqual((row["status"], row["done_via"]), ("done", "client"))


if __name__ == "__main__":
    unittest.main()
