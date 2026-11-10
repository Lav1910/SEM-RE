"""Account settings (name, photo, sign out location) and report exports (overview first, overview-only PDF)."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import io
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


class AccountAndReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        if not db.q("SELECT 1 FROM users WHERE email='acct@homebuildermarketers.com'", one=True):
            db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                 ("acct@homebuildermarketers.com", "Acct Tester", "!", 1, "admin", time.time()))
        cls.uid = db.q("SELECT id FROM users WHERE email='acct@homebuildermarketers.com'", one=True)["id"]

    def client(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.uid, "t"
        return c

    def test_profile_name_and_photo(self):
        c = self.client()
        page = c.get("/account").data
        self.assertIn(b"Ask an admin to change it", page); self.assertIn(b"Sign out", page)
        c.post("/account/profile", data={"csrf": "t", "name": "  Lav   Tester "})
        self.assertEqual(self.db.q("SELECT name FROM users WHERE id=?", (self.uid,), one=True)["name"], "Lav Tester")
        c.post("/account/profile", data={"csrf": "t", "photo": (io.BytesIO(b"<svg>not a photo</svg>"), "x.svg")}, content_type="multipart/form-data")
        self.assertIsNone(self.db.q("SELECT avatar_at FROM users WHERE id=?", (self.uid,), one=True)["avatar_at"])
        c.post("/account/profile", data={"csrf": "t", "photo": (io.BytesIO(PNG), "me.png")}, content_type="multipart/form-data")
        r = c.get(f"/avatar/{self.uid}")
        self.assertEqual(r.status_code, 200); self.assertEqual(r.mimetype, "image/png")
        self.assertEqual(self.app.test_client().get(f"/avatar/{self.uid}").status_code, 302)  # signed out: no photos
        c.post("/account/profile", data={"csrf": "t", "remove_photo": "1"})
        self.assertEqual(c.get(f"/avatar/{self.uid}").status_code, 404)

    def test_rail_has_no_sign_out_button(self):
        html = self.client().get("/").data.decode()
        rail = html[html.index('<aside class="rail"'):html.index("</aside>")]
        self.assertNotIn("logout", rail); self.assertIn("/admin", rail)

    @unittest.skipUnless(os.path.exists(SAMPLE), "no sample audit")
    def test_report_starts_with_overview_and_overview_export(self):
        from webapp import store
        A = json.load(open(SAMPLE))
        aid = "r" + os.urandom(5).hex()
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, progress, created_at, builder_name) VALUES (?,?,?,?, 'done', 100, ?, ?)""",
                  (aid, self.uid, A.get("input_url") or "https://x.com/", A["profile"]["domain"], time.time(), A["profile"]["name"]))
        store.save_json(aid, "audit.json", A)
        c = self.client()
        html = c.get(f"/audits/{aid}/report").data.decode()
        self.assertLess(html.index("AI visibility overview"), html.index("Summary"))
        self.assertIn("What this means", html)
        with mock.patch("webapp.pdf.make_pdf", return_value=b"%PDF-1.7 test") as mk:
            r = c.get(f"/audits/{aid}/export-overview.pdf")
            self.assertEqual(r.mimetype, "application/pdf")
            one_page = mk.call_args[0][0]
            self.assertEqual(one_page.count('<section class="page'), 1)
            r = c.get(f"/audits/{aid}/export.pdf")
            self.assertEqual(r.mimetype, "application/pdf")
        with mock.patch("webapp.pdf.make_pdf", return_value=None):  # no PDF engine: print-ready page instead
            r = c.get(f"/audits/{aid}/export-overview.pdf?area=Nowhere")
            self.assertIn(b"window.print", r.data)


if __name__ == "__main__":
    unittest.main()
