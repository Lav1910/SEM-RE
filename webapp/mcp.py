"""change 50: AEO Snapshot as an MCP server, so AI agents (Claude, ChatGPT, Grok, Cursor, ...) can use the app as the
person who connected them.

- One endpoint, /mcp: MCP over streamable HTTP with plain JSON responses and no sessions (works on serverless).
- Sign-in, two ways:
  * OAuth 2.1 with PKCE (S256) and dynamic client registration: what Claude's and ChatGPT's custom connectors use.
    Adding the connector opens /oauth/authorize, the person signs in as usual and clicks Allow.
  * Personal access tokens from Account > AI connections, sent as "Authorization: Bearer aeo_pat_...".
- Agents get exactly the person's permissions (role, team access, clean data for Sales). Agent audits count toward
  the spending cap like any other, and each person has a daily limit on agent-started audits (set on the Admin page).
- Tokens are stored only as SHA-256 hashes. Every tool call is logged (agent_log) and shown on the person's
  AI connections page. Admins can turn the whole feature off.
"""
import base64
import datetime
import hashlib
import json
import logging
import random
import re
import secrets
import time
import urllib.parse

from flask import abort, flash, g, jsonify, redirect, render_template, request, url_for
from itsdangerous import BadData, URLSafeTimedSerializer

from aeo import budget, business, kv
from aeo import scope as scopes
from . import auth, db, jobs, store, views
from .app import (DAILY_LIMIT, DEPTH_INFO, PUBLIC_URL, REOPEN_DAYS, admin_required, app, login_required, normalize_url,
                  security_event)

log = logging.getLogger("hbm.mcp")
PROTOCOLS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
ACCESS_TTL = 24 * 3600          # OAuth access tokens; agents refresh them on their own
REFRESH_TTL = 90 * 24 * 3600    # a connection unused for 90 days has to sign in again
CODE_TTL = 600
SCOPE = "audits"
DEFAULTS = {"enabled": True, "daily": 20}
CALLS_PER_MINUTE = 90


# ---------------------------------------------------------------- settings and helpers
def settings():
    s = dict(DEFAULTS)
    s.update(kv.get("mcp", None) or {})
    return s


def base_url():
    """The address the agent used (so a custom domain and the Vercel address both work)."""
    u = request.host_url.rstrip("/")
    if u.startswith("http://") and (request.headers.get("X-Forwarded-Proto") == "https" or PUBLIC_URL.startswith("https://")) \
            and request.host.split(":")[0] not in ("localhost", "127.0.0.1"):
        u = "https://" + u[7:]
    return u


def _h(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _ts(t):
    return datetime.datetime.utcfromtimestamp(t).strftime("%Y-%m-%d %H:%M UTC") if t else None


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def agent_audits_today(uid):
    return db.q("SELECT COUNT(*) AS n FROM audits WHERE user_id=? AND via IS NOT NULL AND created_at>?",
                (uid, time.time() - 86400), one=True)["n"]


@app.after_request
def _cors(resp):
    """Browser-based MCP clients (e.g. the MCP Inspector) need CORS; tokens travel in a header, never a cookie."""
    if request.path.startswith(("/mcp", "/oauth/token", "/oauth/register", "/oauth/revoke", "/.well-known/")) \
            and not request.path.startswith("/mcp/files/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Mcp-Protocol-Version, Mcp-Session-Id, Last-Event-ID"
        resp.headers["Access-Control-Expose-Headers"] = "WWW-Authenticate, Mcp-Session-Id"
        resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


# ---------------------------------------------------------------- tokens
def create_token(user_id, kind, name, client_id=None):
    """Returns (row id, access token, refresh token or None)."""
    pre = "aeo_pat_" if kind == "pat" else "aeo_at_"
    raw = pre + secrets.token_urlsafe(32)
    refresh = "aeo_rt_" + secrets.token_urlsafe(32) if kind == "oauth" else None
    now = time.time()
    tid = db.x("""INSERT INTO api_tokens (user_id, kind, name, prefix, token_hash, refresh_hash, client_id, scope, created_at,
                  expires_at, refresh_expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
               (user_id, kind, name[:60], raw[:12] + "…" + raw[-4:], _h(raw), _h(refresh) if refresh else None, client_id, SCOPE, now,
                now + ACCESS_TTL if kind == "oauth" else None, now + REFRESH_TTL if kind == "oauth" else None))
    return tid, raw, refresh


def token_user(raw):
    if not raw or len(raw) > 200:
        return None, None
    row = db.q("SELECT * FROM api_tokens WHERE token_hash=? AND revoked=0", (_h(raw),), one=True)
    if not row or (row["expires_at"] and row["expires_at"] < time.time()):
        return None, None
    u = db.q("SELECT * FROM users WHERE id=? AND active=1 AND pending=0", (row["user_id"],), one=True)
    if not u or not auth.allowed_email(u["email"]):
        return None, None
    if not row["last_used_at"] or time.time() - row["last_used_at"] > 60:
        db.x("UPDATE api_tokens SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
    return u, row


def _bearer():
    h = request.headers.get("Authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else ""


def _unauthorized(had_token):
    resp = jsonify({"error": "invalid_token" if had_token else "unauthorized",
                    "error_description": "Sign in to AEO Snapshot to use this connector."})
    resp.status_code = 401
    meta = f"{base_url()}/.well-known/oauth-protected-resource/mcp"
    resp.headers["WWW-Authenticate"] = f'Bearer resource_metadata="{meta}"' + (', error="invalid_token"' if had_token else "")
    return _no_store(resp)


# ---------------------------------------------------------------- discovery (RFC 9728, RFC 8414)
@app.route("/.well-known/oauth-protected-resource")
@app.route("/.well-known/oauth-protected-resource/<path:rest>")
def oauth_resource_meta(rest=None):
    b = base_url()
    return jsonify({"resource": b + "/mcp", "authorization_servers": [b], "scopes_supported": [SCOPE],
                    "bearer_methods_supported": ["header"], "resource_name": "AEO Snapshot",
                    "resource_documentation": b + "/account?tab=ai"})


@app.route("/.well-known/oauth-authorization-server")
@app.route("/.well-known/oauth-authorization-server/<path:rest>")
def oauth_server_meta(rest=None):
    b = base_url()
    return jsonify({"issuer": b, "authorization_endpoint": b + "/oauth/authorize", "token_endpoint": b + "/oauth/token",
                    "registration_endpoint": b + "/oauth/register", "revocation_endpoint": b + "/oauth/revoke",
                    "response_types_supported": ["code"], "response_modes_supported": ["query"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                    "revocation_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                    "scopes_supported": [SCOPE], "service_documentation": b + "/account?tab=ai"})


# ---------------------------------------------------------------- OAuth: client registration (RFC 7591)
BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "ftp", "ws", "wss"}


def _ok_redirect(u):
    if not isinstance(u, str) or len(u) > 500 or "#" in u or any(c in u for c in " \t\r\n<>\"'\\"):
        return False
    p = urllib.parse.urlsplit(u)
    if p.scheme == "https":
        return bool(p.hostname)
    if p.scheme == "http":
        return p.hostname in ("localhost", "127.0.0.1", "::1")  # native apps (Claude Code, Cursor) listen on loopback
    return bool(re.fullmatch(r"[a-z][a-z0-9+.\-]{1,40}", p.scheme or "")) and p.scheme not in BAD_SCHEMES


def _loopback(u):
    p = urllib.parse.urlsplit(u)
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


def _redirect_matches(given, registered):
    if given in registered:
        return True
    if given and _loopback(given):  # RFC 8252 7.3: any port on loopback
        gp = urllib.parse.urlsplit(given)
        for r in registered:
            rp = urllib.parse.urlsplit(r)
            if _loopback(r) and rp.hostname == gp.hostname and rp.path == gp.path:
                return True
    return False


def _oauth_error(code, desc, status=400):
    return _no_store(jsonify({"error": code, "error_description": desc})), status


@app.route("/oauth/register", methods=["POST", "OPTIONS"])
def oauth_register():
    if request.method == "OPTIONS":
        return "", 204
    if not settings()["enabled"]:
        return _oauth_error("access_denied", "AI connections are turned off for this team.", 403)
    ip = request.remote_addr or ""
    if db.recent_count("oauth_register", time.time() - 3600, ip=ip) >= 30:
        return _oauth_error("too_many_requests", "Too many registrations. Try again later.", 429)
    body = request.get_json(silent=True) or {}
    uris = body.get("redirect_uris")
    if not isinstance(uris, list) or not 1 <= len(uris) <= 10 or not all(_ok_redirect(u) for u in uris):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be 1-10 https (or loopback/app) addresses.")
    method = body.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_post", "client_secret_basic"):
        return _oauth_error("invalid_client_metadata", "Supported token_endpoint_auth_method: none, client_secret_post, client_secret_basic.")
    name = re.sub(r"[\x00-\x1f<>]", "", str(body.get("client_name") or "AI agent")).strip()[:60] or "AI agent"
    cid = "aeo_c_" + secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32) if method != "none" else None
    now = time.time()
    db.x("INSERT INTO oauth_clients (client_id, name, redirect_uris, secret_hash, auth_method, created_at, ip) VALUES (?,?,?,?,?,?,?)",
         (cid, name, json.dumps(uris), _h(secret) if secret else None, method, now, ip))
    security_event("oauth_register", None, name)
    out = {"client_id": cid, "client_id_issued_at": int(now), "client_name": name, "redirect_uris": uris,
           "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
           "token_endpoint_auth_method": method, "scope": SCOPE}
    if secret:
        out.update(client_secret=secret, client_secret_expires_at=0)
    return _no_store(jsonify(out)), 201


# ---------------------------------------------------------------- OAuth: the sign-in / Allow page
def _back_url(uri, params):
    p = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qsl(p.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, urllib.parse.urlencode(q), ""))


def _back(uri, params):
    return redirect(_back_url(uri, params), code=302)


def _hand_back(client, uri, params, ok):
    """After Allow or Cancel: a short page that sends the browser on to the AI app. Not a redirect from the form post:
    browsers apply the form's CSP (form-action) to every redirect that follows, and AI apps often hop again
    (www.cursor.com -> cursor.com, or on to an app link), which froze the Allow button."""
    resp = render_template("oauth_consent.html", done=True, ok=ok, client=client, target=_back_url(uri, params),
                           app_host=urllib.parse.urlsplit(uri).netloc or uri.split(":")[0])
    return _no_store(app.make_response(resp))


@app.route("/oauth/authorize", methods=["GET", "POST"])
def oauth_authorize():
    P = request.values
    client = db.q("SELECT * FROM oauth_clients WHERE client_id=?", (P.get("client_id", "")[:100],), one=True)
    if not client:
        return render_template("oauth_consent.html", problem="This connection link isn't valid. Remove the connector in your AI app and add it again."), 400
    registered = json.loads(client["redirect_uris"])
    uri = P.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
    if not _redirect_matches(uri, registered):
        return render_template("oauth_consent.html", problem="This connection link doesn't match the AI app that registered it. Add the connector again."), 400
    state = P.get("state")
    if P.get("response_type") != "code":
        return _back(uri, {"error": "unsupported_response_type", "state": state})
    challenge, method = P.get("code_challenge", ""), P.get("code_challenge_method", "")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{43,128}", challenge) or method != "S256":
        return _back(uri, {"error": "invalid_request", "error_description": "PKCE with S256 is required.", "state": state})
    if not settings()["enabled"]:
        return render_template("oauth_consent.html", problem="AI connections are turned off for your team. Ask an admin to turn them on."), 403
    if not g.user:
        return redirect(url_for("login", next=request.full_path))
    if request.method == "POST":  # CSRF is checked for every form post (app.load_user)
        if P.get("decision") != "allow":
            security_event("oauth_denied", g.user["email"], client["name"])
            return _hand_back(client, uri, {"error": "access_denied", "state": state}, False)
        code = secrets.token_urlsafe(32)
        db.x("INSERT INTO oauth_codes (code_hash, client_id, user_id, redirect_uri, challenge, scope, expires, used) VALUES (?,?,?,?,?,?,?,0)",
             (_h(code), client["client_id"], g.user["id"], uri, challenge, SCOPE, time.time() + CODE_TTL))
        security_event("oauth_allowed", g.user["email"], client["name"])
        return _hand_back(client, uri, {"code": code, "state": state, "iss": base_url()}, True)
    fields = {k: P.get(k) for k in ("client_id", "redirect_uri", "response_type", "code_challenge", "code_challenge_method",
                                     "state", "scope", "resource") if P.get(k) is not None}
    if "redirect_uri" not in fields:
        fields["redirect_uri"] = uri
    return render_template("oauth_consent.html", client=client, fields=fields, app_host=urllib.parse.urlsplit(uri).netloc or uri.split(":")[0],
                           daily=settings()["daily"])


# ---------------------------------------------------------------- OAuth: tokens
def _client_auth():
    """Returns (client row, error response or None)."""
    f = request.form
    cid, secret = f.get("client_id"), f.get("client_secret")
    h = request.headers.get("Authorization", "")
    if h[:6].lower() == "basic ":
        try:
            cid, secret = [urllib.parse.unquote(x) for x in base64.b64decode(h[6:]).decode().split(":", 1)]
        except Exception:
            return None, _oauth_error("invalid_client", "Unreadable client credentials.", 401)
    client = db.q("SELECT * FROM oauth_clients WHERE client_id=?", ((cid or "")[:100],), one=True)
    if not client:
        return None, _oauth_error("invalid_client", "Unknown client. Add the connector again.", 401)
    if client["secret_hash"] and not (secret and secrets.compare_digest(client["secret_hash"], _h(secret))):
        return None, _oauth_error("invalid_client", "Wrong client secret.", 401)
    return client, None


def _token_json(tid, access, refresh):
    return _no_store(jsonify({"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL,
                              "refresh_token": refresh, "scope": SCOPE}))


@app.route("/oauth/token", methods=["POST", "OPTIONS"])
def oauth_token():
    if request.method == "OPTIONS":
        return "", 204
    client, err = _client_auth()
    if err:
        return err
    f, now = request.form, time.time()
    gt = f.get("grant_type")
    if gt == "authorization_code":
        row = db.q("SELECT * FROM oauth_codes WHERE code_hash=?", (_h(f.get("code", "")),), one=True)
        if not row or row["used"] or row["expires"] < now or row["client_id"] != client["client_id"]:
            return _oauth_error("invalid_grant", "That sign-in code is invalid or expired. Connect again.")
        db.x("UPDATE oauth_codes SET used=1 WHERE code_hash=?", (row["code_hash"],))
        if f.get("redirect_uri") and f.get("redirect_uri") != row["redirect_uri"]:
            return _oauth_error("invalid_grant", "redirect_uri doesn't match.")
        verifier = f.get("code_verifier", "")
        calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        if not re.fullmatch(r"[A-Za-z0-9._~\-]{43,128}", verifier) or not secrets.compare_digest(calc, row["challenge"]):
            return _oauth_error("invalid_grant", "PKCE check failed.")
        u = db.q("SELECT * FROM users WHERE id=? AND active=1 AND pending=0", (row["user_id"],), one=True)
        if not u or not settings()["enabled"]:
            return _oauth_error("invalid_grant", "This account can't connect AI agents right now.")
        # one live connection per AI app per person: reconnecting replaces the old one
        db.x("UPDATE api_tokens SET revoked=1 WHERE user_id=? AND kind='oauth' AND name=? AND revoked=0", (u["id"], client["name"]))
        tid, access, refresh = create_token(u["id"], "oauth", client["name"], client["client_id"])
        db.x("DELETE FROM oauth_codes WHERE expires<?", (now - 3600,))
        return _token_json(tid, access, refresh)
    if gt == "refresh_token":
        row = db.q("SELECT * FROM api_tokens WHERE refresh_hash=? AND revoked=0 AND kind='oauth'", (_h(f.get("refresh_token", "")),), one=True)
        if not row or (row["refresh_expires_at"] or 0) < now or row["client_id"] != client["client_id"]:
            return _oauth_error("invalid_grant", "This connection has expired or was disconnected. Connect again.")
        u = db.q("SELECT 1 FROM users WHERE id=? AND active=1 AND pending=0", (row["user_id"],), one=True)
        if not u or not settings()["enabled"]:
            return _oauth_error("invalid_grant", "This account can't use AI agents right now.")
        access, refresh = "aeo_at_" + secrets.token_urlsafe(32), "aeo_rt_" + secrets.token_urlsafe(32)
        db.x("UPDATE api_tokens SET token_hash=?, refresh_hash=?, prefix=?, expires_at=?, refresh_expires_at=?, last_used_at=? WHERE id=?",
             (_h(access), _h(refresh), access[:12] + "…" + access[-4:], now + ACCESS_TTL, now + REFRESH_TTL, now, row["id"]))
        return _token_json(row["id"], access, refresh)
    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")


@app.route("/oauth/revoke", methods=["POST", "OPTIONS"])
def oauth_revoke():
    if request.method == "OPTIONS":
        return "", 204
    t = request.form.get("token", "")
    if t:
        hh = _h(t)
        db.x("UPDATE api_tokens SET revoked=1 WHERE token_hash=? OR refresh_hash=?", (hh, hh))
    return _no_store(jsonify({})), 200


# ---------------------------------------------------------------- tools
class ToolError(Exception):
    pass


def _str(a, k, n=200):
    v = a.get(k)
    return str(v).strip()[:n] if v is not None else ""


def _int(a, k, lo, hi, default):
    try:
        return max(lo, min(hi, int(a.get(k, default))))
    except (TypeError, ValueError):
        return default


def _note(detail, aid=None):
    g.agent_note = (detail, aid)


def _report_url(aid, view="overview"):
    return base_url() + url_for("audit_view", aid=aid, view=view)


def _visible(sql_where="", args=()):
    """WHERE clause limiting audits to what this person may see."""
    if auth.can(g.user, "team_audits"):
        return sql_where, list(args)
    return (sql_where + (" AND " if sql_where else "") + "a.user_id=?"), list(args) + [g.user["id"]]


def _audit(ref):
    """An audit by ID, or the latest audit of a website the person can see."""
    ref = str(ref or "").strip()[:200]
    if not ref:
        raise ToolError("Give an audit_id (from list_audits or start_audit) or a website.")
    if re.fullmatch(r"[A-Za-z0-9]{6,32}", ref) and "." not in ref:
        w, args = _visible("a.id=?", [ref])
        a = db.q(f"SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id WHERE {w}", tuple(args), one=True)
    else:
        url = normalize_url(ref)
        if not url:
            raise ToolError("That isn't an audit ID or a website address.")
        dom = scopes.make(url, "root")["domain"]
        w, args = _visible("(a.domain=? OR a.domain=?)", [dom, "www." + dom])
        a = db.q(f"""SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id WHERE {w}
                     ORDER BY CASE WHEN a.status='done' THEN 0 ELSE 1 END, a.created_at DESC LIMIT 1""", tuple(args), one=True)
    if not a:
        raise ToolError("No audit found for that. Use list_audits to see audits, or start_audit to run one.")
    return a


def _done(a):
    if a["status"] != "done":
        raise ToolError(f"This audit isn't finished (status: {a['status']}). Use get_audit_status to follow it.")
    A = store.load_json(a["id"])
    if A is None:
        raise ToolError("This audit's data isn't available.")
    return A


def _clean():
    return not auth.can(g.user, "admin")


def _depth(a):
    return DEPTH_INFO[jobs.depth_of(a)]["label"]


def _brief(r):
    return {"audit_id": r["id"], "builder": r["builder_name"], "website": r["domain"], "status": r["status"],
            "ai_visibility_score": r["score"] if r["status"] == "done" else None, "type": _depth(r),
            "run_by": r["owner"], "started_by_agent": bool(r["via"]), "created": _ts(r["created_at"]),
            "report_url": _report_url(r["id"]) if r["status"] == "done" else None}


def t_list_audits(a):
    q = _str(a, "query", 100)
    scope = "team" if a.get("scope") == "team" and auth.can(g.user, "team_audits") else "mine"
    conds, args = ([], []) if scope == "team" else (["a.user_id=?"], [g.user["id"]])
    if q:
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(LOWER(COALESCE(a.builder_name,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(a.domain,'')) LIKE ? ESCAPE '\\')")
        args += [like, like]
    st = a.get("status")
    if st in ("done", "running", "failed"):
        conds.append("a.status IN ('queued','running')" if st == "running" else "a.status=?")
        if st != "running":
            args.append(st)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    limit = _int(a, "limit", 1, 50, 20)
    rows = db.q(f"SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id {where} ORDER BY a.created_at DESC LIMIT ?",
                tuple(args) + (limit,))
    _note("Listed audits" + (f" matching “{q}”" if q else "") + (" (whole team)" if scope == "team" else ""))
    return {"scope": scope, "count": len(rows), "audits": [_brief(r) for r in rows]}


def _summary(a, A):
    V = views.build(A, clean=_clean())
    M, D = A["market"], V["demand"]
    out = {"audit_id": a["id"], "builder": A["profile"].get("name") or a["builder_name"], "website": a["domain"],
           "market": f"{M.get('city')}, {M.get('state_code')}", "date": _ts(a["finished_at"] or a["created_at"]), "type": _depth(a),
           "ai_visibility_score": V["score"], "score_band": V["band"], "summary": V["caption"],
           "questions_asked": V["questions_n"], "ai_answers_checked": V["answers"], "times_named": V["mentions"],
           "platforms": [{"platform": d["label"], "visibility_pct": d["visibility"], "named_in_answers": d["mentions"], "answers": d["answers"]}
                         for d in V["dist"]],
           "by_area": [{"area": x["area"], "named_pct": x["pct"]} for x in V["by_area"]],
           "top_competitors": [{"name": c["name"], "times_named": c["mentions"], "share_of_mentions_pct": c["share"], "is_this_builder": c["prospect"]}
                               for c in V["competitors"][:8]],
           "website_checks": {"passed": V["kpis"]["checks_passed"], "total": V["kpis"]["checks_total"]},
           "top_recommendations": [{"title": r["title"], "why": r.get("why"), "how": r.get("how")} for r in V["recommendations"][:5]],
           "report_url": _report_url(a["id"])}
    if D.get("has"):
        out["search_demand"] = {"monthly_searches": D["total"], "named_for_pct": D["pct"], "monthly_searches_going_to_competitors": D["missed"],
                                "biggest_missed_questions": [{"question": t["prompt"], "monthly_searches": t["volume"], "named": t["named"],
                                                              "named_instead": t.get("instead", [])} for t in D["top"]]}
    if V.get("gbp"):
        out["google_business_profile"] = {"rating": V["gbp"].get("rating"), "reviews": V["gbp"].get("reviews"), "category": V["gbp"].get("category")}
    from .app import _client_info
    info = _client_info(a)
    if info:
        out["client_link"] = {"url": info["url"], "expires": _ts(info["expires"]), "views": info["views"]}
    return out


def t_get_audit(a):
    au = _audit(a.get("audit_id"))
    if au["status"] != "done":
        _note("Read audit · " + au["domain"], au["id"])
        return dict(_brief(au), note="Not finished yet. Use get_audit_status to follow it.")
    A = _done(au)
    _note("Read audit · " + au["domain"], au["id"])
    return _summary(au, A)


SECTIONS = ("overview", "prompts", "competitors", "site", "strategy", "fixes", "content")


def t_get_audit_section(a):
    sec = a.get("section")
    if sec not in SECTIONS:
        raise ToolError("section must be one of: " + ", ".join(SECTIONS))
    au = _audit(a.get("audit_id"))
    A = _done(au)
    V = views.build(A, clean=_clean())
    W = business.words(A)
    out = {"audit_id": au["id"], "builder": A["profile"].get("name"), "website": au["domain"], "section": sec,
           "business_type": W["label"], "services_measured": W["services"], "report_url": _report_url(au["id"], sec)}
    if sec == "overview":
        out.update({k: v for k, v in _summary(au, A).items() if k not in out})
        out["topics"] = [{"question": t["topic"], "keyword": t["keyword"], "monthly_searches": t["volume"], "visibility_pct": t["visibility"],
                          "intent": t["intent"], "top_competitors": t["top_competitors"][:5]} for t in V["topics"]]
    elif sec == "prompts":
        full = bool(a.get("include_answers"))
        out["questions"] = [{"question": p["q"]["prompt"], "keyword": p["q"].get("keyword"), "area": p["q"].get("area"),
                             "monthly_searches": p["q"].get("volume"),
                             "results": [dict({"platform": r["label"], "named": r["mentioned"], "position": r["rank"],
                                               "competitors_named": r["competitors"]},
                                              **({"answer": r["text"][:1500], "sources": r["citations"][:5]} if full else {}))
                                         for r in p["rows"] if r["status"] in ("ok", "no_answer") or not _clean()]}
                            for p in V["prompts"]]
        if not full:
            out["tip"] = "Pass include_answers=true to get each AI answer's text and sources."
    elif sec == "competitors":
        out["competitors"] = [{"name": c["name"], "times_named": c["mentions"], "share_of_mentions_pct": c["share"], "platforms": c["platforms"],
                               "is_this_builder": c["prospect"]} for c in V["competitors"]]
        out["google_maps_3_pack"] = [{"name": p["name"], "position": p.get("pack"), "website": p.get("domain")} for p in V["pack"]]
        out["sites_ai_cites"] = [{"site": s["label"] or s["domain"], "domain": s["domain"], "type": s["type"], "times_cited": s["count"],
                                  "cites_this_builder": bool(s["you"])} for s in V["cited_sources"][:15]]
        out["citation_opportunities"] = [{"site": s["label"] or s["domain"], "domain": s["domain"], "competitors_named": s["competitors_mentioned"]}
                                         for s in V["source_opportunities"][:10]]
    elif sec == "site":
        out["checks"] = [{"result": {"good": "pass", "bad": "fail", "warn": "warning"}.get(c[0], c[0]), "check": c[1], "detail": c[2]} for c in V["checks"]]
        out["pages_ai_cites"] = [{"url": p["url"], "times_cited": p["count"]} for p in V["pages"]]
        if V.get("gbp"):
            gb = V["gbp"]
            out["google_business_profile"] = {k: gb.get(k) for k in ("title", "rating", "reviews", "category", "address", "phone", "claimed", "photos")}
    else:
        from .app import _strategy_ctx
        S = _strategy_ctx(au, A)
        if sec == "strategy":
            out["plan"] = [{"title": i["title"], "lane": i.get("lane_label"), "impact": i.get("impact"), "effort": i.get("effort"),
                            "why": i.get("why"), "what_to_do": i.get("how"), "status": i["status"]} for i in S["items"]]
        elif sec == "fixes":
            out["fixes"] = [{"title": f["title"], "impact": f.get("impact"), "effort": f.get("effort"), "why": f.get("why"),
                             "fix": f.get("how"), "code": f.get("snippet"), "status": f["status"]} for f in S["fixes"]]
        else:
            out["content"] = [{"type": gp["label"], "title": c["label"], "status": c["status"], "text": c.get("text") if c["status"] == "done" else None}
                              for gp in S["cards"] for c in gp["cards"]]
            if not any(c["status"] == "done" for gp in S["cards"] for c in gp["cards"]):
                out["tip"] = "No content written yet. Call get_deliverables with write_missing=true (or create_client_link) to write it (1-2 minutes)."
    _note(f"Read {sec} · {au['domain']}", au["id"])
    return out


def t_start_audit(a):
    if not auth.can(g.user, "run_audits"):
        raise ToolError("Your account can't run audits.")
    url = normalize_url(_str(a, "website", 300))
    if not url:
        raise ToolError("Give a website address, like justindoylehomes.com.")
    depth = a.get("type") or "standard"
    if depth not in jobs.DEPTHS:
        raise ToolError("type must be quick, standard or full.")
    sc = scopes.make(url, "root")
    domain = sc["domain"]
    now = time.time()
    svcs = a.get("services")  # change 53: optional; otherwise read from the website
    if svcs is not None:
        if not isinstance(svcs, list):
            raise ToolError("services must be a list of service names, like [\"Bathroom remodeling\"].")
        svcs = list(dict.fromkeys(str(x).strip()[:60] for x in svcs if str(x).strip()))[:8] or None
    if not a.get("force_new") and svcs is None:
        prev = db.q("""SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id WHERE a.domain=?
                       AND COALESCE(a.scope,'root')='root' AND (a.status IN ('queued','running') AND a.created_at>?
                       OR a.status='done' AND a.created_at>?) ORDER BY a.created_at DESC LIMIT 1""",
                    (domain, now - 3600, now - REOPEN_DAYS * 86400), one=True)
        if prev and (prev["user_id"] == g.user["id"] or auth.can(g.user, "team_audits")):
            days = int((now - prev["created_at"]) // 86400)
            _note("Opened recent audit · " + domain, prev["id"])
            msg = ("This website is being audited right now, so here's that audit." if prev["status"] != "done" else
                   f"This website was audited {'today' if not days else f'{days} day' + ('s' if days != 1 else '') + ' ago'}, so here's that audit.")
            return dict(_brief(prev), reused=True, note=msg + " Pass force_new=true to run a fresh one.",
                        next_step="Call get_audit with this audit_id." if prev["status"] == "done" else "Call get_audit_status every 20-30 seconds.")
    s = settings()
    used = agent_audits_today(g.user["id"])
    if s["daily"] and used >= s["daily"]:
        raise ToolError(f"You've used today's {s['daily']} agent-started audits (the count resets 24 hours after each one). "
                        "An admin can raise the limit on the Admin page.")
    if db.q("SELECT COUNT(*) AS n FROM audits WHERE user_id=? AND created_at>? AND batch_id IS NULL", (g.user["id"], now - 86400), one=True)["n"] >= DAILY_LIMIT:
        raise ToolError(f"You've run {DAILY_LIMIT} audits in the last 24 hours, the daily limit.")
    b = budget.status()
    if b.get("limit_usd") and (b.get("spent_usd") or 0) >= b["limit_usd"]:
        raise ToolError("The team's spending cap has been reached, so no new audits can run. Ask an admin.")
    city, state = _str(a, "city", 60) or None, _str(a, "state", 30) or None
    aid = secrets.token_hex(6)
    via = "agent:" + (g.agent_token["name"] or "AI agent")[:50]
    db.x("""INSERT INTO audits (id, user_id, url, domain, scope, locations, services, city, state, test_mode, depth, status, stage, progress,
                                share_token, created_at, via)
            VALUES (?,?,?,?, 'root', NULL, ?, ?,?,?,?, 'queued', 'Waiting to start', 0, ?, ?, ?)""",
         (aid, g.user["id"], url, domain, json.dumps(svcs) if svcs else None, city, state, 1 if depth == "quick" else 0, depth,
          secrets.token_urlsafe(18), now, via))
    jobs.kick(PUBLIC_URL or request.host_url)
    _note(f"Started {DEPTH_INFO[depth]['label']} audit · {domain}", aid)
    return {"audit_id": aid, "status": "queued", "type": DEPTH_INFO[depth]["label"], "website": domain,
            "estimated_seconds": DEPTH_INFO[depth]["secs"] + 15, "agent_audits_used_today": used + 1,
            "agent_audit_daily_limit": s["daily"] or None,
            "next_step": "Call get_audit_status with this audit_id every 20-30 seconds until status is done, then call get_audit.",
            "report_url": _report_url(aid)}


def t_get_audit_status(a):
    au = _audit(a.get("audit_id"))
    out = {"audit_id": au["id"], "website": au["domain"], "status": au["status"], "type": _depth(au),
           "progress_pct": au["progress"], "stage": au["stage"]}
    acts = json.loads(au["activity"] or "[]")
    if _clean():
        acts = [ev for ev in acts if not str(ev.get("text", "")).startswith("Skipped")]
    out["recent_activity"] = [ev.get("text") for ev in acts[-5:] if ev.get("text")]
    if au["status"] in ("queued", "running"):
        if (au["lease_until"] or 0) < time.time():
            jobs.kick(PUBLIC_URL or request.host_url)  # serverless: nobody is working on it right now
        out["next_step"] = "Still running. Check again in 20-30 seconds."
    elif au["status"] == "done":
        out.update(ai_visibility_score=au["score"], builder=au["builder_name"], report_url=_report_url(au["id"]),
                   next_step="Finished. Call get_audit for the results.")
    else:
        out["error"] = au["error"] if not _clean() else "The audit couldn't finish."
        out["next_step"] = "Try start_audit again with force_new=true."
    _note("Checked progress · " + au["domain"], au["id"])
    return out


def t_create_client_link(a):
    if not auth.can(g.user, "share"):
        raise ToolError("Your account can't create client links.")
    au = _audit(a.get("audit_id"))
    _done(au)
    from .app import _ensure_client_link
    info = _ensure_client_link(au, new=bool(a.get("new_link")))
    _note("Created client link · " + au["domain"], au["id"])
    return {"audit_id": au["id"], "builder": au["builder_name"], "url": info["url"], "expires": _ts(info["expires"]), "views": info["views"],
            "note": "The client sees the Overview, Competitors, Strategy, Content and Fixes tabs without signing in. Every deliverable is "
                    "being written now (about 1-2 minutes). You get an email when they first open it; use get_link_tracking to see visits."}


def t_get_link_tracking(a):
    au = _audit(a.get("audit_id"))
    from .app import _client_info, _link_summary
    info = _client_info(au)
    if not info and not au["client_token"]:
        raise ToolError("This audit has no client link yet. Use create_client_link.")
    L = _link_summary(au["id"])
    _note("Read link tracking · " + au["domain"], au["id"])
    return {"audit_id": au["id"], "builder": au["builder_name"], "link": info["url"] if info else None,
            "active": bool(info), "expires": _ts(info["expires"]) if info else None,
            "total_visits": len(L["visits"]), "total_minutes": round(L["seconds"] / 60, 1),
            "most_viewed_section": L["top"],
            "visits": [{"when": _ts(v["started_at"]), "minutes": round((v["seconds"] or 0) / 60, 1), "device": v["device"],
                        "location": ", ".join(x for x in (v["city"], v["region"], v["country"]) if x) or None,
                        "sections": v["sections"], "actions": v["actions"]} for v in L["visits"][:25]]}


_signer = lambda: URLSafeTimedSerializer(app.secret_key, salt="mcp-file")  # noqa: E731


def t_get_deliverables(a):
    au = _audit(a.get("audit_id"))
    A = _done(au)
    from .app import _strategy_ctx, _write_all_content
    queued = _write_all_content(au, g.user["id"]) if a.get("write_missing") else 0
    S = _strategy_ctx(au, A)
    pieces = [{"type": gp["label"], "title": c["label"], "status": c["status"]} for gp in S["cards"] for c in gp["cards"]]
    sign = _signer()
    links = {k: base_url() + url_for("mcp_file", token=sign.dumps({"a": au["id"], "k": k, "u": g.user["id"]}))
             for k in ("report", "overview", "word")}
    _note("Got deliverables · " + au["domain"], au["id"])
    out = {"audit_id": au["id"], "builder": au["builder_name"],
           "downloads": {"full_report_pdf": links["report"], "one_page_overview_pdf": links["overview"], "all_deliverables_word": links["word"]},
           "downloads_expire_in": "1 hour", "content": pieces,
           "tip": "Use get_audit_section with section=content for the full text of each piece."}
    if queued:
        out["writing_now"] = f"{queued} piece(s) are being written (about 1-2 minutes). Call again to see them."
    elif not any(p["status"] == "done" for p in pieces):
        out["tip"] = "No content written yet. Call again with write_missing=true to write every piece (about 1-2 minutes)."
    return out


def t_add_to_todo(a):
    sites = a.get("websites")
    if isinstance(sites, str):
        sites = re.split(r"[\s,]+", sites)
    if not isinstance(sites, list) or not sites:
        raise ToolError("Give websites as a list, like [\"builder1.com\", \"builder2.com\"].")
    from .todo import MAX_ROWS, _root
    have = {_root(r["domain"]) for r in db.q("SELECT domain FROM todos WHERE user_id=? AND cleared=0", (g.user["id"],))}
    added, skipped, bad = [], [], []
    now = time.time()
    for i, raw in enumerate(sites[:MAX_ROWS]):
        u = normalize_url(str(raw)[:300])
        if not u:
            bad.append(str(raw)[:80]); continue
        dom = u.split("/")[2].replace("www.", "", 1)
        if _root(dom) in have:
            skipped.append(dom); continue
        have.add(_root(dom))
        db.x("INSERT INTO todos (user_id, domain, url, name, notes, status, created_at) VALUES (?,?,?,?,?, 'todo', ?)",
             (g.user["id"], dom, u, None, _str(a, "notes", 300) or None, now + i * 0.001))
        added.append(dom)
    _note(f"Added {len(added)} website{'s' if len(added) != 1 else ''} to to-do")
    return {"added": added, "already_on_list": skipped, "not_websites": bad, "todo_url": base_url() + url_for("todo_home")}


def t_get_my_account(a):
    s = settings()
    _note("Checked account")
    return {"name": g.user["name"], "email": g.user["email"], "role": auth.ROLES.get(auth.role_of(g.user)),
            "can_see_team_audits": auth.can(g.user, "team_audits"), "can_run_audits": auth.can(g.user, "run_audits"),
            "agent_audits_used_today": agent_audits_today(g.user["id"]), "agent_audit_daily_limit": s["daily"] or None,
            "audit_types": {k: f"{v['label']}: {v['what']}, about {v['secs'] + 15} seconds" for k, v in DEPTH_INFO.items()}}


AUDIT_REF = {"type": "string", "description": "The audit ID (from list_audits or start_audit), or a website to use its latest audit."}
RO = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}
TOOLS = [
    ("list_audits", "List audits", t_list_audits, RO,
     "List AI visibility audits, newest first: builder, website, status, AI visibility score (0-100) and report link.",
     {"query": {"type": "string", "description": "Filter by builder name or website."},
      "scope": {"type": "string", "enum": ["mine", "team"], "description": "Your audits (default) or the whole team's."},
      "status": {"type": "string", "enum": ["done", "running", "failed"]},
      "limit": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Default 20."}}, []),
    ("get_audit", "Get audit results", t_get_audit, RO,
     "Key results of an audit: AI visibility score, visibility on each AI platform (ChatGPT, Gemini, Perplexity, Google AI "
     "Overviews, Google AI Mode), by area, top competitors, monthly search demand and how much goes to competitors, Google "
     "Business Profile, website checks, top recommendations and the report link.",
     {"audit_id": AUDIT_REF}, ["audit_id"]),
    ("get_audit_section", "Get a report section", t_get_audit_section, RO,
     "One report section in full. overview: score, platforms, topics and search demand. prompts: every question asked and whether "
     "each AI platform named the builder. competitors: who AI names instead, Google Maps 3-pack, sites AI cites. site: website "
     "checks. strategy: the step-by-step plan. fixes: technical fixes with code. content: the written content pieces.",
     {"audit_id": AUDIT_REF, "section": {"type": "string", "enum": list(SECTIONS)},
      "include_answers": {"type": "boolean", "description": "prompts only: include each AI answer's text and sources (longer)."}},
     ["audit_id", "section"]),
    ("start_audit", "Start an audit", t_start_audit, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
     "Run an AI visibility audit on a local business's website (home builders, remodelers and other trades). It reads what "
     "the business sells from its website and asks the questions its local customers ask. Returns an audit_id straight away; "
     "the audit takes about 1-2 minutes, so call get_audit_status until it's done. If the site was audited in the last week, "
     "that audit is returned instead (pass force_new=true for a fresh one). Counts toward your daily agent audit limit and the "
     "team's spending cap.",
     {"website": {"type": "string", "description": "The business's website, e.g. justindoylehomes.com."},
      "services": {"type": "array", "items": {"type": "string"},
                   "description": "Optional: the services to measure, e.g. [\"Bathroom remodeling\", \"Basement finishing\"]. "
                                  "Leave out to use the services found on the website."},
      "type": {"type": "string", "enum": ["quick", "standard", "full"],
               "description": "quick: ~6 questions, main city. standard (default): ~12 questions, main + busiest extra city. full: everything, up to 5 cities."},
      "city": {"type": "string", "description": "Optional: the business's main city, if the website doesn't make it clear."},
      "state": {"type": "string", "description": "Optional: two-letter US state or Canadian province, e.g. OH or BC."},
      "force_new": {"type": "boolean", "description": "Run a fresh audit even if one exists from this week."}}, ["website"]),
    ("get_audit_status", "Check audit progress", t_get_audit_status, RO,
     "Progress of a running audit (status, percent, current step). When status is done, call get_audit.",
     {"audit_id": AUDIT_REF}, ["audit_id"]),
    ("create_client_link", "Create client link", t_create_client_link, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "Create (or reuse) the interactive client link for a finished audit: a 30-day page the prospect can open without signing "
     "in, with every deliverable written for them. Visits are tracked.",
     {"audit_id": AUDIT_REF, "new_link": {"type": "boolean", "description": "Replace the current link with a new one (the old one stops working)."}},
     ["audit_id"]),
    ("get_link_tracking", "Get client link tracking", t_get_link_tracking, RO,
     "Who opened an audit's client link: visits, time spent, device, rough location, sections viewed and actions.",
     {"audit_id": AUDIT_REF}, ["audit_id"]),
    ("get_deliverables", "Get deliverables", t_get_deliverables, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "Download links (valid 1 hour) for the full PDF report, the one-page overview PDF and a Word file with every deliverable, "
     "plus the status of each written content piece (FAQ, blog posts, city pages, cost guide, llms.txt, Google posts, review requests).",
     {"audit_id": AUDIT_REF, "write_missing": {"type": "boolean", "description": "Write any content pieces that don't exist yet (about 1-2 minutes)."}},
     ["audit_id"]),
    ("add_to_todo", "Add to to-do list", t_add_to_todo, {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
     "Add builder websites to your prospect to-do list. Websites already on the list are skipped.",
     {"websites": {"type": "array", "items": {"type": "string"}, "maxItems": 300}, "notes": {"type": "string"}}, ["websites"]),
    ("get_my_account", "Get my account", t_get_my_account, RO,
     "Who you're signed in as, your role, what you can do, the audit types and how many agent audits you have left today.",
     {}, []),
]
TOOL_MAP = {t[0]: t for t in TOOLS}


def _spec(t):
    name, title, _, ann, desc, props, req = t
    return {"name": name, "title": title, "description": desc,
            "inputSchema": {"type": "object", "properties": props, "required": req, "additionalProperties": False},
            "annotations": dict(ann, title=title)}


INSTRUCTIONS = ("AEO Snapshot measures how often AI assistants (ChatGPT, Gemini, Perplexity, Google AI Overviews and AI Mode) "
                "recommend a home builder, compared with competitors, and turns it into a plan. You act as the signed-in Home "
                "Builder Marketers team member. Typical flow: list_audits or start_audit, then get_audit_status until done, then "
                "get_audit, get_audit_section for detail, create_client_link to share with the prospect and get_link_tracking to "
                "see if they looked. The AI visibility score is 0-100 (higher is better). Audits cost money, so reuse recent ones.")


# ---------------------------------------------------------------- the MCP endpoint
def _rpc(mid, result=None, error=None):
    m = {"jsonrpc": "2.0", "id": mid}
    m["error" if error else "result"] = error or result
    return m


def _log(name, ok, detail=None, aid=None):
    try:
        db.x("INSERT INTO agent_log (t, user_id, token_id, client, tool, detail, audit_id, ok) VALUES (?,?,?,?,?,?,?,?)",
             (time.time(), g.user["id"], g.agent_token["id"], g.agent_token["name"], name, (detail or "")[:300], aid, 1 if ok else 0))
        if random.random() < 0.01:
            db.x("DELETE FROM agent_log WHERE t<?", (time.time() - 90 * 86400,))
    except Exception:
        log.exception("agent_log")


def _call(params):
    name, args = params.get("name"), params.get("arguments") or {}
    t = TOOL_MAP.get(name)
    if not t:
        return None, {"code": -32602, "message": f"Unknown tool: {name}"}
    if not isinstance(args, dict):
        return None, {"code": -32602, "message": "arguments must be an object"}
    if db.q("SELECT COUNT(*) AS n FROM agent_log WHERE user_id=? AND t>?", (g.user["id"], time.time() - 60), one=True)["n"] >= CALLS_PER_MINUTE:
        return _tool_text("Too many requests in the last minute. Wait a moment and try again.", True), None
    g.agent_note = None
    try:
        data = t[2](args)
        detail, aid = g.agent_note or (t[1], None)
        _log(name, True, detail, aid)
        return {"content": [{"type": "text", "text": json.dumps(data, indent=1, ensure_ascii=False, default=str)}],
                "structuredContent": data, "isError": False}, None
    except ToolError as e:
        _log(name, False, f"{t[1]}: {e}")
        return _tool_text(str(e), True), None
    except Exception:
        log.exception("MCP tool %s failed", name)
        _log(name, False, f"{t[1]}: error")
        return _tool_text("Something went wrong in AEO Snapshot. Try again in a minute.", True), None


def _tool_text(text, err=False):
    return {"content": [{"type": "text", "text": text}], "isError": err}


def _handle(m):
    if not isinstance(m, dict) or m.get("jsonrpc") != "2.0" or not isinstance(m.get("method"), str):
        return _rpc(m.get("id") if isinstance(m, dict) else None, error={"code": -32600, "message": "Invalid request"})
    if "id" not in m:
        return None  # a notification (e.g. notifications/initialized): nothing to answer
    mid, method, params = m["id"], m["method"], m.get("params") or {}
    if method == "initialize":
        want = params.get("protocolVersion")
        return _rpc(mid, {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": "aeo-snapshot", "title": "AEO Snapshot", "version": "1.0.0"},
                          "instructions": INSTRUCTIONS})
    if method == "ping":
        return _rpc(mid, {})
    if method == "tools/list":
        return _rpc(mid, {"tools": [_spec(t) for t in TOOLS]})
    if method == "tools/call":
        res, err = _call(params)
        return _rpc(mid, res, err)
    if method in ("resources/list", "resources/templates/list", "prompts/list"):
        return _rpc(mid, {{"resources/list": "resources", "resources/templates/list": "resourceTemplates", "prompts/list": "prompts"}[method]: []})
    return _rpc(mid, error={"code": -32601, "message": f"Method not found: {method}"})


@app.route("/mcp", methods=["GET", "POST", "DELETE", "OPTIONS"])
def mcp_endpoint():
    if request.method == "OPTIONS":
        return "", 204
    raw = _bearer()
    u, tok = token_user(raw)
    if not u:
        return _unauthorized(bool(raw))
    if not settings()["enabled"]:
        return _no_store(jsonify(_rpc(None, error={"code": -32001, "message": "AI connections are turned off for this team. Ask an admin."}))), 403
    if request.method != "POST":
        return "", 405, {"Allow": "POST"}  # no server-to-agent stream: every answer comes back on the POST
    try:
        msg = json.loads(request.get_data(as_text=True) or "null")
    except ValueError:
        return _no_store(jsonify(_rpc(None, error={"code": -32700, "message": "Parse error"}))), 400
    g.user, g.agent_token = u, tok
    batch = isinstance(msg, list)
    out = [r for r in (_handle(x) for x in (msg if batch else [msg])) if r is not None]
    if not out:
        return "", 202
    return _no_store(jsonify(out if batch else out[0]))


@app.route("/mcp/files/<token>")
def mcp_file(token):
    """Download links handed to agents by get_deliverables: signed, tied to the person, valid for 1 hour."""
    try:
        d = _signer().loads(token, max_age=3600)
    except BadData:
        abort(404)
    u = db.q("SELECT * FROM users WHERE id=? AND active=1 AND pending=0", (d.get("u"),), one=True)
    if not u or not settings()["enabled"] or d.get("k") not in ("report", "overview", "word"):
        abort(404)
    g.user = u
    from .app import audit_docx, export_overview, export_pdf
    return {"report": export_pdf, "overview": export_overview, "word": audit_docx}[d["k"]](str(d.get("a", "")))


# ---------------------------------------------------------------- Account > AI connections
def account_ctx(user):
    now = time.time()
    rows = db.q("SELECT * FROM api_tokens WHERE user_id=? AND revoked=0 ORDER BY created_at DESC", (user["id"],))
    tokens = [r for r in rows if r["kind"] == "pat"]
    apps = [r for r in rows if r["kind"] == "oauth" and (r["refresh_expires_at"] or 0) > now]
    acts, last = [], None
    for r in db.q("SELECT * FROM agent_log WHERE user_id=? ORDER BY t DESC LIMIT 150", (user["id"],)):
        key = (r["client"], r["detail"], r["ok"])
        if last and last["key"] == key:  # an agent checking progress every 20 seconds shows as one line
            last["n"] += 1
            continue
        last = {"key": key, "client": r["client"], "detail": r["detail"], "ok": r["ok"], "t": r["t"], "n": 1}
        acts.append(last)
    s = settings()
    return {"url": base_url() + "/mcp", "tokens": tokens, "apps": apps, "activity": acts[:20], "enabled": s["enabled"],
            "used": agent_audits_today(user["id"]), "limit": s["daily"]}


@app.route("/account/ai/token", methods=["POST"])
@login_required
def ai_token_create():
    if not settings()["enabled"]:
        flash("AI connections are turned off for your team.", "bad")
        return redirect(url_for("account", tab="ai"))
    if db.q("SELECT COUNT(*) AS n FROM api_tokens WHERE user_id=? AND kind='pat' AND revoked=0", (g.user["id"],), one=True)["n"] >= 10:
        flash("You have 10 access tokens. Revoke one you don't use first.", "bad")
        return redirect(url_for("account", tab="ai"))
    name = re.sub(r"[\x00-\x1f<>]", "", request.form.get("name", "")).strip()[:60] or "Access token"
    _, raw, _ = create_token(g.user["id"], "pat", name)
    security_event("api_token_created", g.user["email"], name)
    # shown once, straight in this response (never stored or put in a cookie)
    return render_template("account.html", tab="ai", has_pw=g.user["password_hash"] not in ("", "!"), via_code=False, setup=None,
                           google=auth.google_enabled(), ai=account_ctx(g.user), new_token={"name": name, "token": raw})


@app.route("/account/ai/revoke/<int:tid>", methods=["POST"])
@login_required
def ai_token_revoke(tid):
    row = db.q("SELECT * FROM api_tokens WHERE id=? AND user_id=?", (tid, g.user["id"]), one=True)
    if row and not row["revoked"]:
        db.x("UPDATE api_tokens SET revoked=1 WHERE id=?", (tid,))
        security_event("api_token_revoked", g.user["email"], row["name"])
        flash(("Disconnected " if row["kind"] == "oauth" else "Revoked ") + row["name"] + ". It can't use your account any more.", "good")
    return redirect(url_for("account", tab="ai"))


# ---------------------------------------------------------------- Admin
def admin_ctx():
    now = time.time()
    return dict(settings(), connections=db.q("""SELECT COUNT(*) AS n FROM api_tokens WHERE revoked=0
                    AND (kind='pat' OR refresh_expires_at>?)""", (now,), one=True)["n"],
                agent_audits=db.q("SELECT COUNT(*) AS n FROM audits WHERE via IS NOT NULL AND created_at>?", (now - 86400,), one=True)["n"],
                calls=db.q("SELECT COUNT(*) AS n FROM agent_log WHERE t>?", (now - 86400,), one=True)["n"])


@app.route("/admin/ai", methods=["POST"])
@admin_required
def admin_ai():
    raw = (request.form.get("daily") or "").strip()
    daily = max(0, min(1000, int(raw))) if raw.isdigit() else DEFAULTS["daily"]
    enabled = bool(request.form.get("enabled"))
    kv.update("mcp", lambda d: dict(d or {}, enabled=enabled, daily=daily), {})
    security_event("ai_settings", g.user["email"], f"enabled={enabled} daily={daily}")
    flash("AI connection settings saved.", "good")
    return redirect(url_for("admin") + "#ai-connections")
