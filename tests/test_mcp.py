"""Change 50: AEO Snapshot as an MCP server for AI agents (Claude, ChatGPT, Grok, Cursor): OAuth + access tokens, tools."""
import os as _os, tempfile as _tf
_os.environ.setdefault("HBM_KV_DIR", _tf.mkdtemp(prefix="hbm-kv-test-"))  # keep tests away from the real spend/health files
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("HBM_DATA_DIR", tempfile.mkdtemp(prefix="hbm-test-"))
SAMPLE = os.environ.get("HBM_SAMPLE_AUDIT") or os.path.join(ROOT, "data", "sample_audit.json")  # not in the repo (client data)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from webapp import app as appmod, db
        cls.app, cls.db = appmod.app, db
        appmod.app.config["TESTING"] = True
        db.init()
        for email, adm in (("mcboss@homebuildermarketers.com", 1), ("mcrep@homebuildermarketers.com", 0)):
            if not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, email.split("@")[0], "!", adm, "admin" if adm else "sales", time.time()))
        cls.ids = {r["email"].split("@")[0]: r["id"] for r in db.q("SELECT id, email FROM users WHERE email LIKE 'mc%'")}

    def setUp(self):
        from aeo import kv
        kv.update("mcp", lambda d: {"enabled": True, "daily": 20}, {})
        self.db.x("DELETE FROM agent_log")
        self.db.x("UPDATE api_tokens SET revoked=1")

    def web(self, who):
        c = self.app.test_client()
        with c.session_transaction() as s:
            s["uid"], s["csrf"] = self.ids[who], "t"
        return c

    def pat(self, who="mcrep", name="Test agent"):
        r = self.web(who).post("/account/ai/token", data={"csrf": "t", "name": name})
        self.assertEqual(r.status_code, 200)
        return re.search(r'id="new-token" type="text" value="(aeo_pat_[^"]+)"', r.get_data(as_text=True)).group(1)

    def rpc(self, token, method, params=None, mid=1):
        body = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            body["params"] = params
        return self.app.test_client().post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"} if token else {})

    def call(self, token, tool, **args):
        r = self.rpc(token, "tools/call", {"name": tool, "arguments": args})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        res = r.get_json()["result"]
        return res, res.get("structuredContent")


class Protocol(Base):
    def test_needs_sign_in_and_points_to_oauth(self):
        r = self.rpc(None, "initialize", {"protocolVersion": "2025-06-18"})
        self.assertEqual(r.status_code, 401)
        self.assertIn('resource_metadata="http://localhost/.well-known/oauth-protected-resource/mcp"', r.headers["WWW-Authenticate"])
        self.assertEqual(self.rpc("aeo_pat_wrong", "ping").status_code, 401)
        self.assertNotIn("Set-Cookie", r.headers)  # agent endpoints never start a browser session
        meta = self.app.test_client().get("/.well-known/oauth-protected-resource/mcp").get_json()
        self.assertEqual(meta["resource"], "http://localhost/mcp")
        asm = self.app.test_client().get(meta["authorization_servers"][0] + "/.well-known/oauth-authorization-server".replace("http://localhost", "")).get_json()
        self.assertEqual(asm["code_challenge_methods_supported"], ["S256"])
        self.assertTrue(asm["registration_endpoint"].endswith("/oauth/register"))

    def test_initialize_list_and_notifications(self):
        tok = self.pat()
        r = self.rpc(tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}})
        res = r.get_json()["result"]
        self.assertEqual(res["protocolVersion"], "2025-06-18")
        self.assertIn("tools", res["capabilities"])
        self.assertEqual(self.rpc(tok, "initialize", {"protocolVersion": "1999-01-01"}).get_json()["result"]["protocolVersion"], "2025-11-25")
        n = self.app.test_client().post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": f"Bearer {tok}"})
        self.assertEqual(n.status_code, 202)
        tools = {t["name"]: t for t in self.rpc(tok, "tools/list").get_json()["result"]["tools"]}
        for name in ("list_audits", "get_audit", "get_audit_section", "start_audit", "get_audit_status", "create_client_link",
                     "get_link_tracking", "get_deliverables", "add_to_todo", "get_my_account"):
            self.assertIn(name, tools)
            self.assertEqual(tools[name]["inputSchema"]["type"], "object")
        self.assertTrue(tools["list_audits"]["annotations"]["readOnlyHint"])
        self.assertFalse(tools["start_audit"]["annotations"]["readOnlyHint"])
        self.assertEqual(self.rpc(tok, "nope/nope").get_json()["error"]["code"], -32601)
        self.assertEqual(self.app.test_client().get("/mcp", headers={"Authorization": f"Bearer {tok}"}).status_code, 405)
        batch = self.app.test_client().post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}],
                                            headers={"Authorization": f"Bearer {tok}"}).get_json()
        self.assertEqual([m["id"] for m in batch], [1, 2])

    def test_cors_for_browser_clients(self):
        r = self.app.test_client().options("/mcp", headers={"Origin": "http://localhost:6274", "Access-Control-Request-Method": "POST"})
        self.assertEqual(r.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("Authorization", r.headers["Access-Control-Allow-Headers"])


class Tokens(Base):
    def test_token_shown_once_then_revoked(self):
        tok = self.pat(name="My Claude Code")
        self.assertEqual(self.call(tok, "get_my_account")[1]["email"], "mcrep@homebuildermarketers.com")
        row = self.db.q("SELECT * FROM api_tokens WHERE kind='pat' AND revoked=0 AND user_id=?", (self.ids["mcrep"],), one=True)
        self.assertNotIn(tok, json.dumps(dict(row), default=str))  # only the hash is stored
        page = self.web("mcrep").get("/account?tab=ai").get_data(as_text=True)
        self.assertIn("My Claude Code", page)
        self.assertNotIn(tok, page)
        self.web("mcrep").post(f"/account/ai/revoke/{row['id']}", data={"csrf": "t"})
        self.assertEqual(self.rpc(tok, "ping").status_code, 401)

    def test_someone_else_cant_revoke_your_token(self):
        self.pat("mcrep")
        row = self.db.q("SELECT id FROM api_tokens WHERE kind='pat' AND revoked=0 AND user_id=?", (self.ids["mcrep"],), one=True)
        self.web("mcboss").post(f"/account/ai/revoke/{row['id']}", data={"csrf": "t"})
        self.assertEqual(self.db.q("SELECT revoked FROM api_tokens WHERE id=?", (row["id"],), one=True)["revoked"], 0)

    def test_deactivated_person_loses_access(self):
        self.db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                  (f"gone{uuid.uuid4().hex[:5]}@homebuildermarketers.com", "gone", "!", 0, "sales", time.time()))
        uid = self.db.q("SELECT id FROM users WHERE name='gone' ORDER BY id DESC LIMIT 1", one=True)["id"]
        self.ids["gone"] = uid
        tok = self.pat("gone")
        self.assertEqual(self.rpc(tok, "ping").status_code, 200)
        self.db.x("UPDATE users SET active=0 WHERE id=?", (uid,))
        self.assertEqual(self.rpc(tok, "ping").status_code, 401)

    def test_admin_switch_off_stops_everything(self):
        from aeo import kv
        tok = self.pat()
        r = self.web("mcboss").post("/admin/ai", data={"csrf": "t", "daily": "5"})  # checkbox unticked
        self.assertEqual(r.status_code, 302)
        self.assertEqual(kv.get("mcp"), {"enabled": False, "daily": 5})
        self.assertEqual(self.rpc(tok, "ping").status_code, 403)
        self.assertIn("turned off", self.web("mcrep").get("/account?tab=ai").get_data(as_text=True))
        self.assertEqual(self.web("mcrep").post("/admin/ai", data={"csrf": "t", "enabled": "1"}).status_code, 403)  # Sales can't


class OAuth(Base):
    def register(self, name="Claude", uris=("https://claude.ai/api/mcp/auth_callback",)):
        r = self.app.test_client().post("/oauth/register", json={"client_name": name, "redirect_uris": list(uris),
                                                                 "token_endpoint_auth_method": "none", "grant_types": ["authorization_code", "refresh_token"]})
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()

    def handed_back(self, r):
        import html as _html
        body = r.get_data(as_text=True)
        refresh = _html.unescape(re.search(r'http-equiv="refresh" content="0;url=([^"]+)"', body).group(1))
        link = _html.unescape(re.search(r'<a class="btn btn-primary" href="([^"]+)"', body).group(1))
        self.assertEqual(refresh, link)
        return link

    def authorize(self, client, who="mcrep", uri=None):
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": uri or client["redirect_uris"][0],
             "code_challenge": challenge, "code_challenge_method": "S256", "state": "xyz", "scope": "audits",
             "resource": "http://localhost/mcp"}
        web = self.web(who)
        page = web.get("/oauth/authorize?" + urllib.parse.urlencode(q))
        self.assertEqual(page.status_code, 200, page.get_data(as_text=True)[:300])
        self.assertIn("form-action 'self';", page.headers["Content-Security-Policy"])  # the form only posts here
        r = web.post("/oauth/authorize", data=dict(q, csrf="t", decision="allow"))
        # not a redirect: browsers apply form-action to every hop after a form post, and AI apps hop again
        # (www.cursor.com -> cursor.com froze the Allow button). A page sends the browser on instead.
        self.assertEqual(r.status_code, 200)
        target = self.handed_back(r)
        self.assertTrue(target.startswith(q["redirect_uri"] + "?"))
        loc = urllib.parse.urlsplit(target)
        params = dict(urllib.parse.parse_qsl(loc.query))
        self.assertEqual(params["state"], "xyz")
        return params["code"], verifier, q

    def test_full_connector_flow(self):
        client = self.register()
        code, verifier, q = self.authorize(client)
        t = self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                                                              "redirect_uri": q["redirect_uri"], "code_verifier": verifier})
        self.assertEqual(t.status_code, 200, t.get_data(as_text=True))
        tok = t.get_json()
        self.assertEqual(tok["token_type"], "Bearer")
        self.assertEqual(self.call(tok["access_token"], "get_my_account")[1]["name"], "mcrep")
        # the code works once
        again = self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                                                                  "redirect_uri": q["redirect_uri"], "code_verifier": verifier})
        self.assertEqual(again.get_json()["error"], "invalid_grant")
        # refresh rotates both tokens
        r = self.app.test_client().post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client["client_id"]})
        new = r.get_json()
        self.assertNotEqual(new["access_token"], tok["access_token"])
        self.assertEqual(self.rpc(tok["access_token"], "ping").status_code, 401)
        self.assertEqual(self.rpc(new["access_token"], "ping").status_code, 200)
        old = self.app.test_client().post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client["client_id"]})
        self.assertEqual(old.status_code, 400)
        # shows under Connected apps and can be disconnected
        page = self.web("mcrep").get("/account?tab=ai").get_data(as_text=True)
        self.assertIn("Connected apps", page)
        self.assertIn("Claude", page)
        row = self.db.q("SELECT id FROM api_tokens WHERE kind='oauth' AND revoked=0 AND user_id=?", (self.ids["mcrep"],), one=True)
        self.web("mcrep").post(f"/account/ai/revoke/{row['id']}", data={"csrf": "t"})
        self.assertEqual(self.rpc(new["access_token"], "ping").status_code, 401)

    def test_pkce_and_redirect_checks(self):
        client = self.register()
        code, verifier, q = self.authorize(client)
        bad = self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                                                                "redirect_uri": q["redirect_uri"], "code_verifier": "x" * 50})
        self.assertEqual(bad.get_json()["error"], "invalid_grant")
        r = self.web("mcrep").get("/oauth/authorize?" + urllib.parse.urlencode(dict(q, redirect_uri="https://evil.example/cb")))
        self.assertEqual(r.status_code, 400)  # never redirects to an address the app didn't register
        r = self.web("mcrep").get("/oauth/authorize?" + urllib.parse.urlencode({k: v for k, v in q.items() if k != "code_challenge"}))
        self.assertIn("error=invalid_request", r.headers["Location"])
        r = self.app.test_client().post("/oauth/register", json={"client_name": "x", "redirect_uris": ["javascript:alert(1)"]})
        self.assertEqual(r.status_code, 400)
        r = self.app.test_client().post("/oauth/register", json={"client_name": "x", "redirect_uris": ["http://evil.example/cb"]})
        self.assertEqual(r.status_code, 400)

    def test_signed_out_goes_to_login_then_back(self):
        client = self.register()
        q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": client["redirect_uris"][0],
             "code_challenge": "a" * 43, "code_challenge_method": "S256", "state": "s"}
        r = self.app.test_client().get("/oauth/authorize?" + urllib.parse.urlencode(q))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login?next=", r.headers["Location"])
        nxt = urllib.parse.unquote(urllib.parse.parse_qs(urllib.parse.urlsplit(r.headers["Location"]).query)["next"][0])
        self.assertTrue(nxt.startswith("/oauth/authorize?") and client["client_id"] in nxt)

    def test_deny_and_loopback_ports(self):
        client = self.register("Claude Code", ("http://localhost:33418/callback",))
        q = {"response_type": "code", "client_id": client["client_id"], "redirect_uri": "http://localhost:50111/callback",
             "code_challenge": "a" * 43, "code_challenge_method": "S256", "state": "s"}
        r = self.web("mcrep").post("/oauth/authorize", data=dict(q, csrf="t", decision="deny"))
        self.assertTrue(self.handed_back(r).startswith("http://localhost:50111/callback?error=access_denied"))

    def test_cursor_web_callback_hops_are_not_blocked(self):
        """Ash's freeze: Cursor's callback www.cursor.com 308-redirects to cursor.com."""
        client = self.register("Cursor", ("https://www.cursor.com/agents/mcp/oauth/callback",))
        code, verifier, q = self.authorize(client)
        self.assertTrue(code)

    def test_app_name_cant_inject_script(self):
        client = self.register(name="Evil');alert(1);('")
        code, verifier, q = self.authorize(client)
        self.app.test_client().post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "client_id": client["client_id"],
                                                          "redirect_uri": q["redirect_uri"], "code_verifier": verifier})
        page = self.web("mcrep").get("/account?tab=ai").get_data(as_text=True)
        self.assertNotIn("onsubmit", page)
        self.assertNotIn("Evil');alert", page)


@unittest.skipUnless(os.path.exists(SAMPLE), "needs a sample audit (set HBM_SAMPLE_AUDIT)")
class Tools(Base):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.A = json.load(open(SAMPLE))

    def add_audit(self, who="mcrep", **kw):
        from webapp import store
        aid = uuid.uuid4().hex[:12]
        dom = kw.get("domain", self.A["profile"]["domain"])
        self.db.x("""INSERT INTO audits (id, user_id, url, domain, status, builder_name, score, created_at, started_at, finished_at, share_token, depth)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (aid, self.ids[who], f"https://{dom}/", dom, kw.get("status", "done"), self.A["profile"]["name"], 31,
                   kw.get("created", time.time()), time.time(), time.time(), uuid.uuid4().hex, "standard"))
        store.save_json(aid, "audit.json", self.A)
        return aid

    def test_read_tools(self):
        aid = self.add_audit()
        tok = self.pat()
        _, d = self.call(tok, "list_audits", query=self.A["profile"]["domain"][:6])
        self.assertIn(aid, [x["audit_id"] for x in d["audits"]])
        _, s = self.call(tok, "get_audit", audit_id=aid)
        self.assertEqual(s["ai_visibility_score"], 31)
        self.assertEqual(len(s["platforms"]), 5)
        self.assertIn("top_competitors", s)
        _, s2 = self.call(tok, "get_audit", audit_id=self.A["profile"]["domain"])  # by website works too
        self.assertEqual(s2["website"], self.A["profile"]["domain"])
        for sec in ("overview", "prompts", "competitors", "site", "strategy", "fixes", "content"):
            res, d = self.call(tok, "get_audit_section", audit_id=aid, section=sec)
            self.assertFalse(res["isError"], (sec, res["content"][0]["text"][:200]))
        _, p = self.call(tok, "get_audit_section", audit_id=aid, section="prompts", include_answers=True)
        self.assertTrue(any("answer" in r for q in p["questions"] for r in q["results"]))
        res, _ = self.call(tok, "get_audit", audit_id="000000000000")
        self.assertTrue(res["isError"])
        text = json.dumps(self.call(tok, "get_audit_section", audit_id=aid, section="prompts", include_answers=True)[1])
        for name in ("Semrush", "DataForSEO", "DataWise"):  # change 49 holds for agents too (Sales)
            self.assertNotIn(name + " ", text)

    def test_start_audit_reuse_and_limits(self):
        from aeo import kv
        tok = self.pat()
        dom = f"agent{uuid.uuid4().hex[:6]}.com"
        with mock.patch("webapp.jobs.kick"):
            _, d = self.call(tok, "start_audit", website=dom, type="full")
            self.assertEqual(d["status"], "queued")
            row = self.db.q("SELECT * FROM audits WHERE id=?", (d["audit_id"],), one=True)
            self.assertEqual((row["depth"], row["via"], row["user_id"]), ("full", "agent:Test agent", self.ids["mcrep"]))
            _, again = self.call(tok, "start_audit", website="https://www." + dom + "/")
            self.assertTrue(again["reused"])  # already running: no second paid audit
            _, st = self.call(tok, "get_audit_status", audit_id=d["audit_id"])
            self.assertEqual(st["status"], "queued")
            kv.update("mcp", lambda x: {"enabled": True, "daily": 1}, {})
            res, _ = self.call(tok, "start_audit", website="other" + dom, type="quick")
            self.assertTrue(res["isError"])
            self.assertIn("1 agent-started audits", res["content"][0]["text"])
        html = self.web("mcrep").get("/").get_data(as_text=True)
        self.assertIn("AI agent</span>", html)
        log = self.web("mcrep").get("/account?tab=ai").get_data(as_text=True)
        self.assertIn("Started Full audit · " + dom, log)
        self.assertRegex(log, r"<b>\d+ of 1</b> agent audits used today")

    def test_recent_audit_is_reused(self):
        aid = self.add_audit(domain=f"recent{uuid.uuid4().hex[:5]}.com")
        dom = self.db.q("SELECT domain FROM audits WHERE id=?", (aid,), one=True)["domain"]
        tok = self.pat()
        with mock.patch("webapp.jobs.kick"):
            _, d = self.call(tok, "start_audit", website=dom)
            self.assertEqual(d["audit_id"], aid)
            _, d2 = self.call(tok, "start_audit", website=dom, force_new=True)
            self.assertNotEqual(d2["audit_id"], aid)

    def test_client_link_tracking_deliverables_todo(self):
        aid = self.add_audit()
        tok = self.pat()
        from webapp import pdf
        with mock.patch("webapp.app._write_all_content", return_value=0):
            _, link = self.call(tok, "create_client_link", audit_id=aid)
        self.assertIn("/c/", link["url"])
        _, tr = self.call(tok, "get_link_tracking", audit_id=aid)
        self.assertEqual(tr["total_visits"], 0)
        _, dl = self.call(tok, "get_deliverables", audit_id=aid)
        url = dl["downloads"]["one_page_overview_pdf"].replace("http://localhost", "")
        with mock.patch.object(pdf, "make_pdf", return_value=b"%PDF-1.4 fake"):
            r = self.app.test_client().get(url)  # no cookies: the signed link is enough
        self.assertEqual(r.data, b"%PDF-1.4 fake")
        self.assertEqual(self.app.test_client().get(url[:-3] + "xyz").status_code, 404)
        _, td = self.call(tok, "add_to_todo", websites=["newbuilder1.com", "newbuilder1.com", "not a site"])
        self.assertEqual(td["added"], ["newbuilder1.com"])

    def test_sales_only_sees_their_own_when_no_team_access(self):
        from webapp import auth
        aid = self.add_audit(who="mcboss")
        tok = self.pat("mcrep")
        with mock.patch.dict(auth.PERMISSIONS, {"sales": auth.PERMISSIONS["sales"] - {"team_audits"}}):
            res, _ = self.call(tok, "get_audit", audit_id=aid)
        self.assertTrue(res["isError"])


if __name__ == "__main__":
    unittest.main()
