"""Prospect to-do list: adding, statuses, ticking on PDF export only, per-person lists."""
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


class TodoList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("todoboss@homebuildermarketers.com", 1), ("todorep@homebuildermarketers.com", 0), ("todorep2@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'todo%'")}

    def setUp(self):
        self.db.x("DELETE FROM todos WHERE user_id IN (?,?,?)", tuple(self.ids.values()))

    def client(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def _audit(self, who, domain):
        import json
        from webapp import store
        aid = "t" + os.urandom(5).hex()
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, progress, created_at, builder_name, score)
                     VALUES (?,?,?,?, 'done', 100, ?, 'Some Homes', 40)""", (aid, self.ids[who], f"https://{domain}/", domain, time.time()))
        sample = os.environ.get("HBM_SAMPLE_AUDIT")
        A = json.load(open(sample)) if sample and os.path.exists(sample) else {"profile": {"name": "Some Homes", "domain": domain}}
        store.save_json(aid, "audit.json", A)
        return aid

    def test_add_dedupe_and_statuses(self):
        c = self.client("todorep")
        c.post("/todo/add", data={"csrf": "t", "paste": "alpha-homes.com\nhttps://www.alpha-homes.com/\nnot a site\nbeta-homes.com"})
        rows = self.db.q("SELECT domain FROM todos WHERE user_id=? ORDER BY id", (self.ids["todorep"],))
        self.assertEqual([r["domain"] for r in rows], ["alpha-homes.com", "beta-homes.com"])
        self._audit("todorep", "alpha-homes.com")
        html = c.get("/todo").data.decode()
        self.assertIn("◐ Audited", html)

    def test_only_export_ticks_and_only_your_list(self):
        c, other = self.client("todorep"), self.client("todorep2")
        c.post("/todo/add", data={"csrf": "t", "paste": "gamma-homes.com"})
        other.post("/todo/add", data={"csrf": "t", "paste": "gamma-homes.com"})
        aid = self._audit("todorep", "gamma-homes.com")
        status = lambda who: self.db.q("SELECT status, done_via FROM todos WHERE user_id=?", (self.ids[who],), one=True)  # noqa: E731
        import contextlib
        stub = contextlib.ExitStack()
        if not os.environ.get("HBM_SAMPLE_AUDIT"):  # no real audit data: stub the screen's numbers
            stub.enter_context(mock.patch("webapp.views.build", return_value={}))
            stub.enter_context(mock.patch("webapp.views.chart", return_value={}))
        with stub, mock.patch("webapp.app._history", return_value=[]), mock.patch("aeo.render.overview_html", return_value="<html>x</html>"), \
                mock.patch("webapp.pdf.make_pdf", return_value=b"%PDF-1.7"):
            if os.environ.get("HBM_SAMPLE_AUDIT"):
                self.assertEqual(c.get(f"/audits/{aid}/overview").status_code, 200)  # viewing doesn't count
            self.assertEqual(status("todorep")["status"], "todo")
            c.get(f"/audits/{aid}/export-overview.pdf")
        self.assertEqual(dict(status("todorep")), {"status": "done", "done_via": "overview"})
        self.assertEqual(status("todorep2")["status"], "todo")  # someone else's export doesn't touch your list

    def test_full_report_export_ticks(self):
        c = self.client("todorep")
        c.post("/todo/add", data={"csrf": "t", "paste": "www.delta-homes.com"})
        aid = self._audit("todorep", "delta-homes.com")
        with mock.patch("webapp.app._report_html", return_value="<html>r</html>"), mock.patch("webapp.pdf.make_pdf", return_value=b"%PDF-1.7"):
            c.get(f"/audits/{aid}/export.pdf")
        self.assertEqual(self.db.q("SELECT done_via FROM todos WHERE user_id=?", (self.ids["todorep"],), one=True)["done_via"], "full")

    def test_manual_toggle_clear_and_visibility(self):
        c = self.client("todorep")
        c.post("/todo/add", data={"csrf": "t", "paste": "eps-homes.com"})
        tid = self.db.q("SELECT id FROM todos WHERE user_id=?", (self.ids["todorep"],), one=True)["id"]
        c.post(f"/todo/{tid}/toggle", data={"csrf": "t"})
        self.assertEqual(self.db.q("SELECT status FROM todos WHERE id=?", (tid,), one=True)["status"], "done")
        # another salesperson can't see or change it; an admin can view it
        rep2 = self.client("todorep2")
        self.assertNotIn(b"eps-homes.com", rep2.get(f"/todo?user={self.ids['todorep']}").data)
        self.assertEqual(rep2.post(f"/todo/{tid}/toggle", data={"csrf": "t"}).status_code, 404)
        self.assertIn(b"eps-homes.com", self.client("todoboss").get(f"/todo?user={self.ids['todorep']}").data)
        c.post("/todo/clear", data={"csrf": "t"})
        self.assertEqual(self.db.q("SELECT cleared FROM todos WHERE id=?", (tid,), one=True)["cleared"], 1)

    def test_run_audit_from_list(self):
        c = self.client("todorep")
        c.post("/todo/add", data={"csrf": "t", "paste": "zeta-homes.com"})
        tid = self.db.q("SELECT id FROM todos WHERE user_id=?", (self.ids["todorep"],), one=True)["id"]
        with mock.patch("webapp.jobs.kick"):
            r = c.post(f"/todo/{tid}/run", data={"csrf": "t", "depth": "quick"})
        self.assertIn("/audits/", r.headers["Location"])
        a = self.db.q("SELECT * FROM todos t JOIN audits a ON a.id=t.audit_id WHERE t.id=?", (tid,), one=True)
        self.assertEqual(a["domain"], "zeta-homes.com"); self.assertEqual(a["test_mode"], 1)
        self.db.x("UPDATE audits SET status='failed' WHERE id=?", (a["audit_id"],))


if __name__ == "__main__":
    unittest.main()
