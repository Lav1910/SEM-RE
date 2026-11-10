"""Security checks: who can sign in, what each role can do, and the website-fetch guard."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import base64
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

from aeo import netsafe  # noqa: E402
from webapp import auth  # noqa: E402


def _row(**kw):
    class R(dict):
        def keys(self):
            return super().keys()
    return R(kw)


class AuthRules(unittest.TestCase):
    def test_only_hbm_addresses(self):
        self.assertTrue(auth.allowed_email("lav@homebuildermarketers.com"))
        self.assertTrue(auth.allowed_email("  Sales@HomeBuilderMarketers.com "))
        for bad in ("x@gmail.com", "x@homebuildermarketers.com.evil.com", "x@evilhomebuildermarketers.com",
                    "a@b@homebuildermarketers.com", "", None):
            self.assertFalse(auth.allowed_email(bad), bad)

    def test_roles(self):
        admin, sales = _row(is_admin=1, role="admin"), _row(is_admin=0, role="sales")
        for p in ("admin", "see_costs", "raw_data"):
            self.assertTrue(auth.can(admin, p)); self.assertFalse(auth.can(sales, p))
        for p in ("run_audits", "strategy", "share", "team_audits"):
            self.assertTrue(auth.can(sales, p))
        self.assertFalse(auth.can(None, "run_audits"))


class GoogleToken(unittest.TestCase):
    def setUp(self):
        auth.GOOGLE_ID, auth.GOOGLE_SECRET = "cid.apps.googleusercontent.com", "secret"

    def _finish(self, **claims):
        base = {"aud": auth.GOOGLE_ID, "iss": "https://accounts.google.com", "exp": time.time() + 300, "nonce": "n1",
                "email": "lav@homebuildermarketers.com", "email_verified": True, "hd": "homebuildermarketers.com", "sub": "123"}
        base.update(claims)
        tok = "x." + base64.urlsafe_b64encode(json.dumps(base).encode()).decode().rstrip("=") + ".sig"
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps({"id_token": tok}).encode()
        sess = {"g_state": "s1", "g_nonce": "n1", "g_t": time.time()}
        with mock.patch("urllib.request.urlopen", return_value=resp):
            return auth.google_finish({"state": "s1", "code": "c"}, "https://x/cb", sess)

    def test_accepts_hbm_workspace(self):
        self.assertEqual(self._finish()["email"], "lav@homebuildermarketers.com")

    def test_rejects(self):
        for bad in ({"hd": "gmail.com"}, {"email": "x@gmail.com", "hd": ""}, {"email_verified": False}, {"aud": "other"},
                    {"iss": "https://evil"}, {"nonce": "zz"}, {"exp": time.time() - 3600}):
            with self.assertRaises(ValueError, msg=str(bad)):
                self._finish(**bad)

    def test_rejects_bad_state(self):
        with self.assertRaises(ValueError):
            auth.google_finish({"state": "nope", "code": "c"}, "https://x/cb", {"g_state": "s1", "g_nonce": "n", "g_t": time.time()})


class FetchGuard(unittest.TestCase):
    def test_blocks_internal(self):
        for u in ("http://127.0.0.1/", "http://localhost:8080/", "http://169.254.169.254/latest/", "http://10.1.2.3/",
                  "http://[::1]/", "file:///etc/passwd", "ftp://example.com/", "http://example.com:22/", "http://printer.local/"):
            with self.assertRaises(netsafe.BlockedURL, msg=u):
                netsafe.check(u)


class WebApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.db, cls.app = db, appmod.app
        appmod.app.config["TESTING"] = True
        db.init()
        now = time.time()
        for email, adm in (("boss@homebuildermarketers.com", 1), ("rep@homebuildermarketers.com", 0), ("old@gmail.com", 1)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email, "!", adm, "admin" if adm else "sales", now))

    def client(self, email):
        c = self.app.test_client()
        u = self.db.q("SELECT id FROM users WHERE email=?", (email,), one=True)
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = u["id"], "t"
        return c

    def test_sales_cannot_open_admin(self):
        self.assertEqual(self.client("rep@homebuildermarketers.com").get("/admin").status_code, 403)
        self.assertEqual(self.client("boss@homebuildermarketers.com").get("/admin").status_code, 200)

    def test_other_domain_signed_out(self):
        r = self.client("old@gmail.com").get("/")
        self.assertEqual(r.status_code, 302); self.assertIn("/login", r.headers["Location"])

    def test_csrf_required(self):
        c = self.client("boss@homebuildermarketers.com")
        self.assertEqual(c.post("/feedback", data={"message": "x"}).status_code, 400)

    def test_security_headers(self):
        r = self.app.test_client().get("/login")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")

    def test_email_code_flow(self):
        self.db.x("UPDATE users SET password_hash='!' WHERE email='rep@homebuildermarketers.com'")  # no password yet
        self.db.x("DELETE FROM security_log WHERE email='rep@homebuildermarketers.com'")
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"
        sent = {}
        with mock.patch("webapp.mailer.sign_in_email", side_effect=lambda to, name, code, link, m: sent.update(code=code, link=link)):
            r = c.post("/login/code", data={"csrf": "t", "email": "rep@homebuildermarketers.com"})
        self.assertEqual(r.status_code, 302); self.assertIn("/login/verify", r.headers["Location"])
        self.assertRegex(sent["code"], r"^\d{6}$")
        # wrong code doesn't sign in; right code does and asks to set a password (account has none)
        self.assertEqual(c.post("/login/verify", data={"csrf": "t", "code": "000000" if sent["code"] != "000000" else "111111"}).status_code, 200)
        r = c.post("/login/verify", data={"csrf": "t", "code": sent["code"]})
        self.assertIn("/account", r.headers["Location"])
        # code can't be reused
        c2 = self.app.test_client()
        with c2.session_transaction() as s:
            s["csrf"], s["code_email"] = "t", "rep@homebuildermarketers.com"
        self.assertEqual(c2.post("/login/verify", data={"csrf": "t", "code": sent["code"]}).status_code, 200)
        # set a password without an old one (just signed in by code), then sign in with it
        with c.session_transaction() as s:
            s["csrf"] = "t"
        r = c.post("/account", data={"csrf": "t", "new": "correct-horse-battery", "confirm": "correct-horse-battery"})
        self.assertEqual(r.status_code, 302)
        c3 = self.app.test_client()
        with c3.session_transaction() as s:
            s["csrf"] = "t"
        r = c3.post("/login", data={"csrf": "t", "email": "rep@homebuildermarketers.com", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 302); self.assertNotIn("/login", r.headers["Location"])

    def test_code_not_sent_outside_or_to_deactivated(self):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"
        if not self.db.q("SELECT 1 FROM users WHERE email='gone@homebuildermarketers.com'", one=True):
            self.db.x("INSERT INTO users (email, name, password_hash, is_admin, role, active, created_at) VALUES (?,?,?,?,?,?,?)",
                      ("gone@homebuildermarketers.com", "Gone", "!", 0, "sales", 0, time.time()))
        with mock.patch("webapp.mailer.sign_in_email") as m:
            c.post("/login/code", data={"csrf": "t", "email": "x@gmail.com"})
            c.post("/login/code", data={"csrf": "t", "email": "gone@homebuildermarketers.com"})
            self.assertFalse(m.called)
        self.assertIsNone(self.db.q("SELECT 1 FROM users WHERE email='x@gmail.com'", one=True))

    def test_new_hbm_address_signs_up_by_code(self):
        """Change 23: an HBM address nobody added can sign in straight away, as Sales, once it proves the inbox."""
        email = f"new{int(time.time() * 1000)}@homebuildermarketers.com"
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"
        sent = {}
        with mock.patch("webapp.mailer.sign_in_email", side_effect=lambda to, name, code, link, m: sent.update(code=code)):
            c.post("/login/code", data={"csrf": "t", "email": email})
        self.assertRegex(sent["code"], r"^\d{6}$")
        u = self.db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
        self.assertEqual((u["pending"], u["role"], u["is_admin"]), (2, "sales", 0))  # not real until the code is used
        team = lambda: self.client("boss@homebuildermarketers.com").get("/admin").get_data(as_text=True).split("Security log")[0]  # noqa: E731
        self.assertNotIn(email, team())
        r = c.post("/login/verify", data={"csrf": "t", "code": sent["code"]})
        self.assertIn("/account", r.headers["Location"])  # signed in, asked to set a password
        u = self.db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
        self.assertEqual((u["pending"], u["active"], u["role"]), (0, 1, "sales"))
        self.assertEqual(c.get("/admin").status_code, 403)  # Sales, not admin
        self.assertIn(email, team())

    def test_new_hbm_google_account_straight_in(self):
        from webapp import auth as authmod
        email = f"g{int(time.time() * 1000)}@homebuildermarketers.com"
        c = self.app.test_client()
        with mock.patch.object(authmod, "google_enabled", return_value=True), \
                mock.patch.object(authmod, "google_finish", return_value={"email": email, "name": "Conor Test", "sub": "sub-" + email}):
            r = c.get("/auth/google/callback?code=x&state=y")
        self.assertEqual(r.status_code, 302); self.assertNotIn("/login", r.headers["Location"])
        u = self.db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
        self.assertEqual((u["pending"], u["active"], u["role"], u["name"]), (0, 1, "sales", "Conor Test"))
        # a deactivated person stays out
        self.db.x("UPDATE users SET active=0 WHERE id=?", (u["id"],))
        with mock.patch.object(authmod, "google_enabled", return_value=True), \
                mock.patch.object(authmod, "google_finish", return_value={"email": email, "name": "Conor Test", "sub": "sub-" + email}):
            r = self.app.test_client().get("/auth/google/callback?code=x&state=y")
        self.assertIn("/login", r.headers["Location"])

    def test_sign_in_link_single_use(self):
        self.db.x("DELETE FROM security_log WHERE email='boss@homebuildermarketers.com'")  # earlier runs count toward the limit
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["csrf"] = "t"
        sent = {}
        with mock.patch("webapp.mailer.sign_in_email", side_effect=lambda to, name, code, link, m: sent.update(link=link)):
            c.post("/login/code", data={"csrf": "t", "email": "boss@homebuildermarketers.com"})
        path = sent["link"].split("localhost", 1)[-1]
        r = self.app.test_client().get(path)
        self.assertNotIn("/login", r.headers["Location"])
        r = self.app.test_client().get(path)
        self.assertIn("/login", r.headers["Location"])

    def test_password_login_rejects_non_hbm(self):
        r = self.app.test_client().post("/login", data={"email": "old@gmail.com", "password": "x", "csrf": ""})
        self.assertIn(r.status_code, (400, 200))


if __name__ == "__main__":
    unittest.main()
