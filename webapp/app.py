"""HBM AI Visibility Audit: web app for the sales team.

Salespeople sign in, paste a builder's website, watch the audit run, then open the report, copy a share link for
the prospect, and grab Loom talking points. Admins manage users, see platform status and set the spending cap.
"""
import collections
import datetime
import functools
import io
import json
import logging
import os
import re
import secrets
import sys
import time
import urllib.parse
import uuid

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from aeo import budget, config, engines, semrush, site  # noqa: E402
from aeo import dates  # noqa: E402
from aeo import scope as scopes  # noqa: E402
from . import auth, db, jobs, mailer, markdown, pdf, status, store, views  # noqa: E402

DAILY_LIMIT = int(os.environ.get("DAILY_AUDITS_PER_USER", "100"))


def _secret_key():
    k = os.environ.get("SECRET_KEY")
    if k:
        return k
    if jobs.SERVERLESS:
        raise SystemExit("Set SECRET_KEY in the host's environment variables (a long random string).")
    path = os.path.join(db.DATA_DIR, "secret_key")
    os.makedirs(db.DATA_DIR, exist_ok=True)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return open(path).read().strip()


PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # e.g. https://audit.homebuildermarketers.com
SECURE = os.environ.get("COOKIE_SECURE", "1" if PUBLIC_URL.startswith("https://") else "0") == "1"

app = Flask(__name__)
app.config.update(SECRET_KEY=_secret_key(), SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=SECURE, SESSION_COOKIE_NAME="__Host-hbm" if SECURE else "hbm_session",
                  PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=int(os.environ.get("SESSION_DAYS", "7"))),
                  MAX_CONTENT_LENGTH=2 * 1024 * 1024)
WINDOW = 900  # rate-limit window (15 minutes); counts come from the security log, so they hold across servers


def client_ip():
    # remote_addr is the real client when Waitress trusts the proxy in front (TRUSTED_PROXY); never trust raw headers
    return request.remote_addr or ""


def security_event(event, email=None, detail=None, user_id=None):
    try:
        db.x("INSERT INTO security_log (t, user_id, email, ip, event, detail) VALUES (?,?,?,?,?,?)",
             (time.time(), user_id or (g.user["id"] if getattr(g, "user", None) else None), email, client_ip(), event, (detail or "")[:300]))
    except Exception:
        pass


# ---------------- helpers ----------------
@app.context_processor
def latest_audit():
    if not getattr(g, "user", None):
        return {}
    r = db.q("SELECT id FROM audits WHERE status='done' ORDER BY created_at DESC LIMIT 1", one=True)
    return {"latest_audit_id": r["id"] if r else None}


@app.before_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = db.q("SELECT * FROM users WHERE id=? AND active=1 AND pending=0", (uid,), one=True)
        if not g.user:
            session.pop("uid", None)
        elif not auth.allowed_email(g.user["email"]):
            g.user = None; session.pop("uid", None)  # only HBM addresses, even for accounts created before the rule
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    if request.method == "POST" and request.path != "/api/jobs/tick":  # the tick endpoint is checked by its own secret
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "")):
            abort(400, "Your form expired. Go back and try again.")


CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com data:; img-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'; frame-ancestors 'none'")


@app.after_request
def headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    resp.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    if SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/r/"):
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    else:
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Content-Security-Policy"] = CSP
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    if g.get("user") and resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


def login_required(f):
    @functools.wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w


def admin_required(f):
    @functools.wraps(f)
    @login_required
    def w(*a, **k):
        if not auth.can(g.user, "admin"):
            abort(403)
        return f(*a, **k)
    return w


def permission_required(perm):
    def deco(f):
        @functools.wraps(f)
        @login_required
        def w(*a, **k):
            if not auth.can(g.user, perm):
                abort(403)
            return f(*a, **k)
        return w
    return deco


app.jinja_env.globals["can"] = lambda perm: auth.can(getattr(g, "user", None), perm)
app.jinja_env.globals["role_label"] = lambda u: auth.ROLES.get(auth.role_of(u), "")


def ago(ts):
    if not ts:
        return "—"
    s = time.time() - ts
    if s < 60: return "just now"
    if s < 3600: return f"{int(s // 60)} min ago"
    if s < 86400: return f"{int(s // 3600)} h ago"
    return dates.short_date(ts)


app.jinja_env.filters["ago"] = ago
app.jinja_env.filters["md"] = markdown.render


def duration(secs):
    """Run time to the second: 47s, 2m 47s, 1h 3m 5s (change 25)."""
    if secs is None:
        return "—"
    s_ = max(0, int(round(secs)))
    h, rem = divmod(s_, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s" if h else (f"{m}m {sec}s" if m else f"{sec}s")


app.jinja_env.filters["dur"] = duration
app.jinja_env.filters["datefmt"] = lambda ts: dates.date_time(ts) if ts else "—"
app.jinja_env.globals["engine_labels"] = engines.ENGINE_LABELS


def platform_logo(engine, size=18):
    from markupsafe import Markup, escape
    return Markup(f'<img class="eng-logo" src="/static/logos/{escape(engine)}.svg" width="{size}" height="{size}" alt="" aria-hidden="true">')


app.jinja_env.globals["platform_logo"] = platform_logo


def normalize_url(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    p = urllib.parse.urlparse(raw)
    if not p.netloc or "." not in p.netloc or " " in p.netloc:
        return None
    return f"https://{p.netloc.lower()}/"


def get_audit(aid):
    """Any signed-in teammate can open any audit, so the team can share prospects."""
    a = db.q("SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id WHERE a.id=?", (aid,), one=True)
    if a and g.get("user") and a["user_id"] != g.user["id"] and not auth.can(g.user, "team_audits"):
        a = None
    if not a:
        abort(404)
    return a


@app.route("/healthz")
def healthz():
    db.q("SELECT 1", one=True)
    return "ok", 200, {"Content-Type": "text/plain"}


# ---------------- auth ----------------
def _start_session(u, method):
    session.clear(); session.permanent = True; session["uid"] = u["id"]; session["csrf"] = secrets.token_hex(16)
    db.x("UPDATE users SET last_login=? WHERE id=?", (time.time(), u["id"]))
    security_event("login", u["email"], method, u["id"])


def _safe_next(nxt):
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt else "/"


def _redirect_uri():
    return (PUBLIC_URL or request.host_url.rstrip("/")) + url_for("google_callback")


@app.route("/login", methods=["GET", "POST"])
def login():
    ctx = {"google": auth.google_enabled(), "password": auth.password_login_enabled(), "domain": auth.ALLOWED_DOMAIN}
    if request.method == "POST":
        if not ctx["password"]:
            abort(404)
        ip, email = client_ip(), request.form.get("email", "").strip().lower()[:200]
        if not request.form.get("password"):
            flash("Enter your password, or choose “Email me a sign-in code”.", "bad")
            return render_template("login.html", **ctx)
        now = time.time()
        if (db.recent_count("login_failed", now - WINDOW, ip=ip) >= 8 or
                db.recent_count("login_failed", now - WINDOW, email=email) >= 5):
            security_event("login_blocked", email, "too many attempts")
            flash("Too many attempts. Wait 15 minutes and try again.", "bad")
            return render_template("login.html", **ctx), 429
        u = db.q("SELECT * FROM users WHERE email=? AND active=1 AND pending=0", (email,), one=True)
        ok = bool(u) and u["password_hash"] not in ("", "!") and check_password_hash(u["password_hash"], request.form.get("password", ""))
        if ok and not auth.allowed_email(email):
            ok = False
        if ok:
            _start_session(u, "password")
            return redirect(_safe_next(request.args.get("next")))
        if not u:
            check_password_hash("pbkdf2:sha256:600000$x$" + "0" * 64, "x")  # same work either way: don't reveal which emails exist
        security_event("login_failed", email, "password")
        flash("That email and password don't match.", "bad")
    return render_template("login.html", **ctx)


def _base_url():
    return PUBLIC_URL or request.host_url.rstrip("/")


def _name_from_email(email):
    return " ".join(p.capitalize() for p in email.split("@")[0].replace("_", ".").replace("-", ".").split(".") if p) or email


def _self_signup_user(email):
    """Change 23: anyone with an HBM address can sign in without an admin adding them first.
    Returns the account to send a code to, creating a not-yet-verified one (pending=2, hidden from the Admin list)
    for a new address. Deactivated accounts get nothing."""
    u = db.q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if u:
        return u if u["active"] else None
    db.x("""INSERT INTO users (email, name, password_hash, is_admin, role, pending, created_at) VALUES (?,?, '!', 0, 'sales', 2, ?)""",
         (email, _name_from_email(email), time.time()))
    return db.q("SELECT * FROM users WHERE email=?", (email,), one=True)


def _activate_self_signup(u, how):
    """After a correct code or link: a new or waiting account becomes a normal Sales account."""
    if u["pending"]:
        db.x("UPDATE users SET pending=0 WHERE id=?", (u["id"],))
        security_event("account_created", u["email"], f"self sign-up ({how})", u["id"])
        u = db.q("SELECT * FROM users WHERE id=?", (u["id"],), one=True)
    return u


@app.route("/login/code", methods=["POST"])
def login_code():
    """Email a one-time sign-in code + link. Same answer whether or not the address has an account."""
    email = request.form.get("email", "").strip().lower()[:200]
    nxt = _safe_next(request.args.get("next"))
    if not auth.allowed_email(email):
        flash(f"Use your @{auth.ALLOWED_DOMAIN} email address.", "bad")
        return redirect(url_for("login", next=nxt))
    since = time.time() - WINDOW
    if (db.recent_count("code_sent,code_unknown_email", since, ip=client_ip()) >= 10 or
            db.recent_count("code_sent,code_unknown_email", since, email=email) >= 3):
        security_event("code_blocked", email, "too many code requests")
        flash("Too many codes requested. Wait 15 minutes and try again.", "bad")
        return redirect(url_for("login", next=nxt))
    u = _self_signup_user(email)
    if u:
        code, token = auth.new_code(db, u, app.secret_key, client_ip())
        try:
            mailer.sign_in_email(email, u["name"], code, _base_url() + url_for("login_link", token=token), auth.CODE_MINUTES)
            security_event("code_sent", email)
        except Exception as e:
            security_event("code_failed", email, str(e)[:200])
            flash("The sign-in email couldn't be sent. Ask an admin to check the email settings.", "bad")
            return redirect(url_for("login", next=nxt))
    else:
        security_event("code_unknown_email", email, "deactivated account")
    session["code_email"], session["next"] = email, nxt
    flash(f"A 6-digit code is on its way to {email}. It expires in {auth.CODE_MINUTES} minutes.", "good")
    return redirect(url_for("login_verify"))


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    email = session.get("code_email")
    if not email:
        return redirect(url_for("login"))
    if request.method == "POST":
        if db.recent_count("login_failed", time.time() - WINDOW, ip=client_ip()) >= 8:
            flash("Too many attempts. Wait 15 minutes and try again.", "bad")
            return render_template("login_code.html", email=email), 429
        u = db.q("SELECT * FROM users WHERE email=? AND active=1", (email,), one=True)
        if u and auth.check_code(db, u, request.form.get("code", "").replace(" ", ""), app.secret_key):
            u = _activate_self_signup(u, "email code")
            session.pop("code_email", None)
            nxt = session.get("next", "/")
            _start_session(u, "email code")
            session["code_login_t"] = time.time()
            return redirect(url_for("account", setup=1) if u["password_hash"] in ("", "!") else _safe_next(nxt))
        security_event("login_failed", email, "email code")
        flash("That code isn't right or has expired. Check the latest email, or request a new code.", "bad")
    return render_template("login_code.html", email=email)


@app.route("/login/link/<token>")
def login_link(token):
    uid = auth.check_link(db, token, app.secret_key)
    u = db.q("SELECT * FROM users WHERE id=? AND active=1", (uid,), one=True) if uid else None
    if u and auth.allowed_email(u["email"]):
        u = _activate_self_signup(u, "email link")
    if not u or not auth.allowed_email(u["email"]):
        security_event("login_failed", None, "expired or used sign-in link")
        flash("That sign-in link has expired or was already used. Request a new code.", "bad")
        return redirect(url_for("login"))
    _start_session(u, "email link")
    session["code_login_t"] = time.time()
    return redirect(url_for("account", setup=1) if u["password_hash"] in ("", "!") else "/")


@app.route("/auth/google")
def google_login():
    if not auth.google_enabled():
        abort(404)
    session["next"] = _safe_next(request.args.get("next"))
    return redirect(auth.google_start_url(_redirect_uri(), session))


@app.route("/auth/google/callback")
def google_callback():
    if not auth.google_enabled():
        abort(404)
    nxt = session.get("next", "/")
    try:
        info = auth.google_finish(request.args, _redirect_uri(), session)
    except ValueError as e:
        security_event("login_failed", None, f"google: {e}")
        flash(str(e), "bad")
        return redirect(url_for("login"))
    u = db.q("SELECT * FROM users WHERE email=?", (info["email"],), one=True)
    if not u:
        is_admin = info["email"] in auth.ADMIN_EMAILS
        db.x("""INSERT INTO users (email, name, password_hash, is_admin, role, google_sub, pending, created_at)
                VALUES (?,?, '!', ?, ?, ?, ?, ?)""",
             (info["email"], info["name"], 1 if is_admin else 0, "admin" if is_admin else "sales", info["sub"], 0, time.time()))
        u = db.q("SELECT * FROM users WHERE email=?", (info["email"],), one=True)
        security_event("account_created", info["email"], "admin (ADMIN_EMAILS)" if is_admin else "self sign-up (Google)", u["id"])
    elif u["google_sub"] and info["sub"] and u["google_sub"] != info["sub"]:
        security_event("login_failed", info["email"], "google account id changed")
        flash("This Google account doesn't match the one on file. Ask an admin.", "bad")
        return redirect(url_for("login"))
    else:
        if not u["google_sub"]:
            db.x("UPDATE users SET google_sub=? WHERE id=?", (info["sub"], u["id"]))
        if info["email"] in auth.ADMIN_EMAILS and (u["pending"] or not u["is_admin"]):
            db.x("UPDATE users SET pending=0, is_admin=1, role='admin', active=1 WHERE id=?", (u["id"],))
        elif u["pending"] and u["active"]:  # was waiting for approval, or started an email sign-up: Google confirms it's theirs
            db.x("UPDATE users SET pending=0, name=CASE WHEN pending=2 THEN ? ELSE name END WHERE id=?", (info["name"], u["id"]))
            security_event("account_created", info["email"], "self sign-up (Google)", u["id"])
        u = db.q("SELECT * FROM users WHERE id=?", (u["id"],), one=True)
    if u["pending"]:
        return render_template("pending.html", email=info["email"]), 403
    if not u["active"]:
        security_event("login_failed", info["email"], "deactivated")
        flash("This account has been deactivated. Ask an admin.", "bad")
        return redirect(url_for("login"))
    _start_session(u, "google")
    return redirect(_safe_next(nxt))


@app.route("/logout", methods=["POST"])
def logout():
    if getattr(g, "user", None):
        security_event("logout", g.user["email"])
    session.clear()
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    has_pw = g.user["password_hash"] not in ("", "!")
    via_code = time.time() - session.get("code_login_t", 0) < auth.CODE_MINUTES * 60  # just signed in with an emailed code
    if request.method == "POST":
        new = request.form.get("new", "")
        if has_pw and not via_code and not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
            flash("Your current password is wrong. Forgot it? Sign out and sign in with an emailed code.", "bad")
            return redirect(url_for("account", tab="security"))
        elif len(new) < 12:
            flash("Use at least 12 characters for the password.", "bad")
        elif new != request.form.get("confirm", new):
            flash("The two passwords don't match.", "bad")
        else:
            db.x("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(request.form["new"], method="pbkdf2:sha256:600000"), g.user["id"]))
            security_event("password_set" if not has_pw else "password_changed", g.user["email"], "after email code" if via_code else None)
            session.pop("code_login_t", None)
            flash("Password saved. Next time, sign in with your email and this password, or with an emailed code.", "good")
            return redirect(url_for("account", tab="security"))
    tab = "security" if request.method == "POST" or request.args.get("tab") == "security" or request.args.get("setup") else "profile"
    return render_template("account.html", has_pw=has_pw, via_code=via_code, setup=request.args.get("setup"), tab=tab,
                           google=auth.google_enabled())


IMAGE_TYPES = {b"\x89PNG": "image/png", b"\xff\xd8\xff": "image/jpeg", b"RIFF": "image/webp"}


def _image_mime(data):
    for sig, mime in IMAGE_TYPES.items():
        if data.startswith(sig) and (mime != "image/webp" or data[8:12] == b"WEBP"):
            return mime
    return None


@app.route("/account/profile", methods=["POST"])
@login_required
def account_profile():
    if request.form.get("remove_photo"):
        db.x("DELETE FROM user_avatars WHERE user_id=?", (g.user["id"],))
        db.x("UPDATE users SET avatar_at=NULL WHERE id=?", (g.user["id"],))
        flash("Photo removed.", "good")
        return redirect(url_for("account"))
    f = request.files.get("photo")
    if f and f.filename:
        data = f.read(2 * 1024 * 1024 + 1)
        mime = _image_mime(data)
        if len(data) > 2 * 1024 * 1024:
            flash("That photo is over 2 MB. Choose a smaller one.", "bad")
        elif not mime:
            flash("Use a JPG, PNG or WebP photo.", "bad")
        else:
            now = time.time()
            db.upsert("user_avatars", {"user_id": g.user["id"], "content": data, "mime": mime, "updated_at": now}, ("user_id",))
            db.x("UPDATE users SET avatar_at=? WHERE id=?", (now, g.user["id"]))
            flash("Photo updated.", "good")
        return redirect(url_for("account"))
    name = " ".join((request.form.get("name") or "").split())
    if not 2 <= len(name) <= 100:
        flash("Use between 2 and 100 characters for your name.", "bad")
    else:
        db.x("UPDATE users SET name=? WHERE id=?", (name, g.user["id"]))
        flash("Profile saved.", "good")
    return redirect(url_for("account"))


@app.route("/avatar/<int:uid>")
@login_required
def avatar(uid):
    r = db.q("SELECT content, mime FROM user_avatars WHERE user_id=?", (uid,), one=True)
    if not r:
        abort(404)
    resp = app.response_class(bytes(r["content"]), mimetype=r["mime"])
    resp.headers["Cache-Control"] = "private, max-age=86400"
    resp.headers["Content-Security-Policy"] = "default-src 'none'"
    return resp


# ---------------- salesperson screens ----------------
def _backfill_costs(limit=300):
    """Older audits finished before costs were saved: work theirs out once from the stored answers."""
    from aeo import budget
    for r in db.q("SELECT id FROM audits WHERE status='done' AND cost_usd IS NULL ORDER BY created_at DESC LIMIT ?", (limit,)):
        A = store.load_json(r["id"])
        db.x("UPDATE audits SET cost_usd=? WHERE id=?", (budget.audit_cost(A) if A else 0.0, r["id"]))


PER_PAGE = 25


@app.route("/")
@login_required
def dashboard():
    scope = request.args.get("scope", "mine") if auth.can(g.user, "team_audits") else "mine"
    q = request.args.get("q", "").strip()[:100]
    page = max(1, int(request.args.get("page", "1")) if request.args.get("page", "1").isdigit() else 1)
    conds, args = ([], []) if scope == "team" else (["a.user_id=?"], [g.user["id"]])
    if q:  # change 25: search by builder name, website or who ran it
        like = "%" + q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conds.append("(LOWER(COALESCE(a.builder_name,'')) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(a.domain,'')) LIKE ? ESCAPE '\\' "
                     "OR LOWER(a.url) LIKE ? ESCAPE '\\' OR LOWER(COALESCE(u.name,'')) LIKE ? ESCAPE '\\' OR LOWER(u.email) LIKE ? ESCAPE '\\')")
        args += [like] * 5
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    see_costs = auth.can(g.user, "see_costs")
    if see_costs:
        _backfill_costs()
    content_cost = "(SELECT COALESCE(SUM(sc.cost), 0) FROM strategy_content sc WHERE sc.audit_id=a.id)"
    summary = db.q(f"""SELECT COUNT(*) AS n,
                       AVG(CASE WHEN a.status='done' AND a.started_at IS NOT NULL AND a.finished_at IS NOT NULL
                                THEN a.finished_at - a.started_at END) AS avg_secs,
                       SUM(CASE WHEN a.status='done' THEN COALESCE(a.cost_usd, 0) + {content_cost} ELSE 0 END) AS cost
                       FROM audits a JOIN users u ON u.id=a.user_id {where}""", tuple(args), one=True)
    pages = max(1, -(-summary["n"] // PER_PAGE))
    page = min(page, pages)
    rows = db.q(f"""SELECT a.*, u.name AS owner,
                    CASE WHEN a.started_at IS NOT NULL AND a.finished_at IS NOT NULL THEN a.finished_at - a.started_at END AS run_secs,
                    CASE WHEN a.status='done' THEN COALESCE(a.cost_usd, 0) + {content_cost} END AS total_cost
                    FROM audits a JOIN users u ON u.id=a.user_id {where} ORDER BY a.created_at DESC LIMIT ? OFFSET ?""",
                tuple(args) + (PER_PAGE, (page - 1) * PER_PAGE))
    hist = {"q": q, "page": page, "pages": pages, "n": summary["n"], "avg_secs": summary["avg_secs"],
            "cost": summary["cost"] if see_costs else None, "first": (page - 1) * PER_PAGE + 1 if summary["n"] else 0,
            "last": min(page * PER_PAGE, summary["n"])}
    live = [en for en in engines.ENGINES if engines.is_configured(en)]
    b = budget.status()
    last = db.q("SELECT id FROM audits WHERE status='done' ORDER BY created_at DESC LIMIT 1", one=True)
    LA = store.load_json(last["id"]) if last else None
    from aeo.engines import STATE_CODES
    return render_template("dashboard.html", rows=rows, scope=scope, hist=hist, live=live, budget=b if auth.can(g.user, "see_costs") else {}, pstat=status.current(LA),
                           states=sorted(STATE_CODES.values()))


@app.route("/audits", methods=["POST"])
@login_required
def create_audit():
    url = normalize_url(request.form.get("url"))
    if not url:
        flash("Enter a website address, like justindoylehomes.com.", "bad")
        return redirect(url_for("dashboard"))
    today = db.q("SELECT COUNT(*) AS n FROM audits WHERE user_id=? AND created_at>? AND batch_id IS NULL",
                 (g.user["id"], time.time() - 86400), one=True)["n"]  # bulk audits have their own limits
    if today >= DAILY_LIMIT:
        flash(f"You've run {DAILY_LIMIT} audits in the last 24 hours, the daily limit. Ask an admin to raise it.", "bad")
        return redirect(url_for("dashboard"))
    kind = request.form.get("scope", "root")
    kind = kind if kind in scopes.SCOPES else "root"
    sc = scopes.make(url, kind)
    if kind in ("subfolder", "url") and not sc["path"]:
        kind = "root" if kind == "subfolder" else "url"
    domain = sc["domain"] if kind == "root" else sc["host"]
    prev = db.q("""SELECT a.*, u.name AS owner FROM audits a JOIN users u ON u.id=a.user_id
                   WHERE a.domain=? AND a.status='done' AND a.created_at>? ORDER BY a.created_at DESC LIMIT 1""",
                (domain, time.time() - 30 * 86400), one=True)
    if prev and prev["created_at"] > time.time() - REOPEN_DAYS * 86400 and not request.form.get("confirm_duplicate") \
            and (prev["scope"] or "root") == kind:  # change 29: audited this week, so open it instantly
        return redirect(url_for("audit_view", aid=prev["id"], view="overview", reopened=1))
    if prev and prev["user_id"] != g.user["id"] and not request.form.get("confirm_duplicate"):
        return render_template("duplicate.html", prev=prev, form=request.form)
    aid = uuid.uuid4().hex[:12]
    locs = [l.strip()[:60] for l in request.form.getlist("loc") if l.strip()][:20]
    if request.form.get("loc_all"):
        locs = []  # "All locations": let the audit pick by search demand, up to the cap
    db.x("""INSERT INTO audits (id, user_id, url, domain, scope, locations, city, state, test_mode, status, stage, progress, share_token, created_at)
            VALUES (?,?,?,?,?,?,?,?,?, 'queued', 'Waiting to start', 0, ?, ?)""",
         (aid, g.user["id"], url, domain, kind, json.dumps(locs) if locs else None, request.form.get("city", "").strip() or None, request.form.get("state", "").strip() or None,
          1 if request.form.get("test_mode") else 0, secrets.token_urlsafe(18), time.time()))
    jobs.kick(PUBLIC_URL or request.host_url)
    return redirect(url_for("audit_page", aid=aid))


REOPEN_DAYS = int(os.environ.get("HBM_REOPEN_DAYS", "7"))


@app.route("/audits/<aid>")
@login_required
def audit_page(aid):
    a = get_audit(aid)
    if a["status"] == "done":
        return redirect(url_for("audit_view", aid=aid, view="overview"))
    ahead = db.q("SELECT COUNT(*) AS n FROM audits WHERE status='queued' AND created_at<?", (a["created_at"],), one=True)["n"]
    return render_template("audit.html", a=a, loom="", ahead=ahead, share=None)


@app.route("/api/suggest")
@login_required
def suggest():
    """Builder sites the team has already audited, for the Analyze box (Semrush-style suggestions while typing)."""
    q = re.sub(r"^(https?://)?(www\.)?", "", (request.args.get("q") or "").strip().lower())[:80]
    if len(q) < 2:
        return jsonify([])
    rows = db.q("""SELECT domain, MAX(builder_name) AS name, MAX(created_at) AS last, COUNT(*) AS n FROM audits
                   WHERE domain LIKE ? ESCAPE '\\' AND status='done' GROUP BY domain ORDER BY last DESC LIMIT 6""",
                ("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",))
    return jsonify([{"domain": r["domain"], "name": r["name"], "n": r["n"]} for r in rows])


_loc_cache = {}  # domain -> (time, result); a quick scan takes 10-25 s, so repeat lookups are instant


@app.route("/api/locations")
@login_required
def api_locations():
    """Service locations for the dashboard's Locations dropdown: from the last audit of this site, else a quick scan."""
    from aeo import budget, locations as locmod
    url = normalize_url(request.args.get("url"))
    if not url:
        return jsonify({"error": "Enter a website address."}), 400
    sc = scopes.make(url, request.args.get("scope", "root"))
    domain = sc["domain"]
    live = [e for e in engines.ENGINES if engines.is_configured(e) and status.current().get(e, {}).get("state") != "bad"]
    meta = {"cap": config.SETTINGS["MAX_LOCATIONS"], "platforms": len(live),
            "cost_per_city": round(budget.per_question(live) * 3, 2) if auth.can(g.user, "see_costs") else None}
    last = db.q("SELECT id FROM audits WHERE domain=? AND status='done' ORDER BY created_at DESC LIMIT 1", (domain,), one=True)
    A = store.load_json(last["id"]) if last else None
    if A:
        locs = A.get("locations")
        if locs:
            return jsonify(dict(meta, source="last audit", market=A["market"],
                                locations=[{"label": l["label"], "main": bool(l.get("main")), "sources": l.get("sources", [])} for l in locs]))
    hit = _loc_cache.get(domain)
    if hit and time.time() - hit[0] < 6 * 3600:
        return jsonify(dict(meta, **hit[1]))
    try:
        prof = site.read_site(url, use_browser=False)
        if not prof["homepage_status"] or prof["homepage_status"] >= 400:
            return jsonify(dict(meta, error="Couldn't load that website.", locations=[]))
        mk = site.market_from(prof)
        if not mk.get("city") or not mk.get("state"):
            return jsonify(dict(meta, error="Couldn't tell where this builder works. Add the city below.", locations=[]))
        found = locmod.find(prof, mk)
    except Exception as e:
        return jsonify(dict(meta, error=f"Couldn't scan the site ({type(e).__name__}). Add the city below.", locations=[]))
    res = {"source": "website scan", "market": mk,
           "locations": [{"label": l["label"], "main": bool(l.get("main")), "sources": l.get("sources", [])} for l in found]}
    _loc_cache[domain] = (time.time(), res)
    return jsonify(dict(meta, **res))


@app.route("/feedback", methods=["POST"])
@login_required
def feedback():
    msg = (request.form.get("message") or "").strip()[:4000]
    if msg:
        kind = request.form.get("kind") if request.form.get("kind") in ("idea", "data", "bug") else "idea"
        db.x("INSERT INTO feedback (user_id, audit_id, page, kind, message, created_at) VALUES (?,?,?,?,?,?)",
             (g.user["id"], (request.form.get("audit_id") or "")[:20] or None, (request.form.get("page") or "")[:300], kind, msg, time.time()))
        flash("Thanks, your feedback was sent to the HBM team.", "good")
    page = request.form.get("page") or ""
    return redirect(page if page.startswith("/") and not page.startswith("//") else url_for("dashboard"))


VIEWS = {"overview": "Visibility Overview", "competitors": "Competitor Research", "prompts": "Prompt Research",
         "site": "Site Audit", "sharing": "Share & Loom",
         "strategy": "Strategy Plan", "content": "Content Generator", "fixes": "Technical Fixes"}


def _strategy_ctx(a, A):
    from aeo import strategy as strat
    items = strat.plan(A)
    rows = db.q("""SELECT s.item_id, s.status, s.updated_at, u.name AS who FROM strategy_status s
                   LEFT JOIN users u ON u.id=s.user_id WHERE s.domain=?""", (a["domain"],))
    st = {r["item_id"]: r["status"] for r in rows}
    stamp = {r["item_id"]: {"who": (r["who"] or "").split(" ")[0], "t": r["updated_at"]} for r in rows}
    for it in items:
        it["status"] = st.get(it["id"], "todo")
        it["stamp"] = stamp.get(it["id"])
    contents = collections.defaultdict(dict)
    for r in db.q("SELECT * FROM strategy_content WHERE audit_id=?", (a["id"],)):
        contents[r["kind"]][r["key"]] = dict(r)
    gens = [{"kind": k, "label": lab, "targets": strat.targets(A, k), "items": contents.get(k, {})} for k, lab in strat.GENERATORS.items()]
    fx = strat.fixes(A)
    for f in fx:
        f["status"] = st.get("fix_" + f["key"], "todo")
        f["stamp"] = stamp.get("fix_" + f["key"])
    counts = lambda xs: {k: sum(1 for x in xs if x["status"] == k) for k in ("todo", "doing", "done")}  # noqa: E731
    chat_ok = status.current().get("chatgpt", {}).get("state") == "good"
    return {"items": items, "lanes": strat.LANES, "gens": gens, "fixes": fx, "chat_ok": chat_ok,
            "fix_counts": counts(fx), "item_counts": counts(items),
            "done_n": sum(1 for i in items if i["status"] == "done"), "schema": strat.schema_block(A)}


@app.route("/audits/<aid>/strategy/generate", methods=["POST"])
@login_required
def strategy_generate(aid):
    from aeo import strategy as strat
    from . import strategy_jobs
    a = get_audit(aid)
    if a["status"] != "done":
        abort(404)
    provider = "chatgpt" if request.form.get("provider") == "chatgpt" else "gemini"
    kind = request.form.get("kind", "")
    kinds = list(strat.GENERATORS) if kind == "all" else [kind] if kind in strat.GENERATORS else []
    keys = request.form.getlist("key") or None
    n = sum(strategy_jobs.queue(a, k, provider, g.user["id"], keys) for k in kinds)
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"queued": n})
    flash(f"Writing {n} piece{'s' if n != 1 else ''} in the background. They appear here as each one finishes.", "good")
    return redirect(url_for("audit_view", aid=aid, view="content"))


@app.route("/audits/<aid>/strategy/content.json")
@login_required
def strategy_content_json(aid):
    get_audit(aid)
    rows = db.q("SELECT kind, key, status, error FROM strategy_content WHERE audit_id=?", (aid,))
    return jsonify([dict(r) for r in rows])


@app.route("/audits/<aid>/strategy/piece/<kind>/<key>")
@login_required
def strategy_piece(aid, kind, key):
    get_audit(aid)
    r = db.q("SELECT * FROM strategy_content WHERE audit_id=? AND kind=? AND key=?", (aid, kind, key), one=True)
    if not r:
        abort(404)
    return jsonify({"status": r["status"], "error": r["error"], "label": r["label"], "provider": r["provider"],
                    "html": str(markdown.render(r["content"] or "", levels=True)), "text": r["content"] or ""})


@app.route("/audits/<aid>/strategy/item", methods=["POST"])
@login_required
def strategy_item(aid):
    a = get_audit(aid)
    item, st = request.form.get("item", "")[:60], request.form.get("status")
    if st not in ("todo", "doing", "done"):
        abort(400)
    db.upsert("strategy_status", {"domain": a["domain"], "item_id": item, "status": st, "user_id": g.user["id"],
                                  "updated_at": time.time()}, ("domain", "item_id"))
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"ok": True, "status": st, "who": (g.user["name"] or "").split(" ")[0]})
    return redirect(request.referrer or url_for("audit_view", aid=aid, view="strategy"))


@app.route("/audits/<aid>/strategy/export.<fmt>")
@login_required
def strategy_export(aid, fmt):
    from aeo import strategy as strat
    a = get_audit(aid)
    if a["status"] != "done" or fmt not in ("md", "doc", "pdf"):
        abort(404)
    A = store.load_json(aid)
    name = re.sub(r"[^A-Za-z0-9]+", "-", a["builder_name"] or a["domain"]).strip("-")
    rows = db.q("SELECT * FROM strategy_content WHERE audit_id=? AND status='done' ORDER BY kind, key", (aid,))
    only = request.args.get("kind")
    rows = [r for r in rows if not only or r["kind"] == only]
    if fmt == "pdf":  # the HBM-branded proposal: plan + deliverables
        ctx = _strategy_ctx(a, A)
        from aeo import render as rnd
        html = render_template("strategy_print.html", a=a, A=A, S=ctx, today=dates.long_date(),
                               css=open(os.path.join(ROOT, "tools", "report.css")).read(), logo=rnd._logo())
        data = pdf.make_pdf(html)
        if data is None:  # no PDF engine on this host: open the print-ready page (Save as PDF in the print dialog)
            return _print_page(html)
        return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=f"{name}-AEO-Strategy.pdf")
    if not rows:
        flash("Generate some content first; exports include every finished piece.", "bad")
        return redirect(url_for("audit_view", aid=aid, view="content"))
    md = f"# AEO content for {A['profile']['name']}\n\nPrepared by Home Builder Marketers · {dates.long_date()}\n\n"
    for r in rows:
        md += f"\n\n---\n\n# {strat.GENERATORS.get(r['kind'], r['kind'])}: {r['label']}\n\n{r['content']}\n"
    if fmt == "md":
        return app.response_class(md, mimetype="text/markdown", headers={"Content-Disposition": f'attachment; filename="{name}-AEO-content.md"'})
    body = ('<html><head><meta charset="utf-8"><style>body{font-family:Poppins,Arial,sans-serif;color:#16302F;line-height:1.55}'
            'h2,h3{color:#0A686A}pre{background:#F2F7F7;padding:10px;white-space:pre-wrap;font-size:10pt}mark{background:#FFF1C9}</style></head><body>'
            + str(markdown.render(md, levels=True)) + "</body></html>")
    return app.response_class(body, mimetype="application/msword", headers={"Content-Disposition": f'attachment; filename="{name}-AEO-content.doc"'})


VIEWS_STRATEGY = ("strategy", "content", "fixes")


def _history(a, A, platform="all"):
    """Earlier audits of the same builder, one point per day: the trend line (Semrush: Main Metrics over time)."""
    hist = []
    for h in db.q("SELECT * FROM audits WHERE domain=? AND status='done' AND created_at<=? ORDER BY created_at",
                  (a["domain"], a["created_at"])):
        HA = A if h["id"] == a["id"] else store.load_json(h["id"])
        if HA:
            hv = views.build(HA, platform)
            if not hv["measured_any"]:  # an audit run before any AI platform was connected isn't a real zero
                continue
            day = dates.month_day(h["created_at"])
            if hist and hist[-1]["date"] == day:  # several runs on one day: keep the latest
                hist.pop()
            hist.append({"date": day, "month": datetime.datetime.fromtimestamp(h["created_at"]).strftime("%Y-%m"), "mentions": hv["mentions"],
                         "citations": hv["citations"], "pages": hv["cited_pages"], "score": hv["score"] or 0,
                         "audience": hv["audience"], "id": h["id"]})
    return hist


def _report_html(a, A):
    """The full HBM report, built fresh so page 1 matches the Visibility Overview (with this builder's history)."""
    from aeo import render
    hist = _history(a, A)
    return render.render(A, {"V": views.build(A, "all", hist, clean=True), "chart": views.chart(A, hist)})  # prospect-facing: always clean


def _pdf_response(aid, html, cache_name, download_name):
    import hashlib
    key = hashlib.sha256(html.encode("utf-8")).hexdigest()
    data = store.get_bytes(aid, cache_name) if store.get_text(aid, cache_name + ".key") == key else None
    if data is None:
        data = pdf.make_pdf(html)
        if data is None:  # no PDF engine on this host: open the print-ready page (Save as PDF in the print dialog)
            return _print_page(html)
        store.put(aid, cache_name, data)
        store.put(aid, cache_name + ".key", key)
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True, download_name=download_name)


@app.route("/audits/<aid>/export-overview.pdf")
@login_required
def export_overview(aid):
    """One page: just the Visibility Overview, matching the screen's platform and area filters."""
    from aeo import render
    a = get_audit(aid)
    if a["status"] != "done":
        abort(404)
    A = store.load_json(aid)
    if A is None:
        abort(404)
    platform = request.args.get("platform", "all")
    platform = platform if platform in engines.ENGINES else "all"
    area = request.args.get("area") or None
    hist = _history(a, A, platform)
    ov = {"V": views.build(A, platform, hist, None, area, clean=True), "chart": views.chart(A, hist)}  # prospect-facing: always clean
    label = " · ".join(x for x in (area, engines.ENGINE_LABELS.get(platform) + " only" if platform != "all" else None) if x) or None
    html = render.overview_html(A, ov, label)
    name = re.sub(r"[^A-Za-z0-9]+", "-", a["builder_name"] or a["domain"]).strip("-")
    resp = _pdf_response(aid, html, "overview.pdf", f"{name}-AI-Visibility-Overview-{datetime.date.fromtimestamp(a['created_at']).isoformat()}.pdf")
    from . import todo
    todo.mark_exported(g.user["id"], a, "overview")  # ticks this builder on the exporter's to-do list
    return resp


@app.route("/audits/<aid>/<view>")
@login_required
def audit_view(aid, view):
    if view not in VIEWS:
        abort(404)
    a = get_audit(aid)
    if a["status"] != "done":
        return redirect(url_for("audit_page", aid=aid))
    A = store.load_json(aid)
    if A is None:
        abort(404)
    platform = request.args.get("platform", "all")
    if platform != "all" and platform not in engines.ENGINES:
        platform = "all"
    hist = _history(a, A, platform)
    vs = [v for v in request.args.getlist("vs") if v]
    area = request.args.get("area") or None
    clean = not auth.can(g.user, "admin")  # change 24: only admins see platform errors and connection labels
    if clean and platform != "all" and platform not in views.measured_engines(A):
        platform = "all"
    V = views.build(A, platform, hist, vs, area, clean=clean)
    V["chart"] = views.chart(A, hist)
    if view in VIEWS_STRATEGY:
        V["S"] = _strategy_ctx(a, A)
    V["score_all"] = V["score"] if not area else views.build(A, platform)["score"]
    V["area_scores"] = {ar: views.build(A, platform, None, None, ar)["score"] for ar in (V["areas"] if len(V["areas"]) > 1 else [])}
    others = db.q("SELECT id, created_at FROM audits WHERE domain=? AND status='done' ORDER BY created_at DESC LIMIT 12", (a["domain"],))
    loom = store.get_text(aid, "loom.txt") or ""
    share = url_for("shared_report", token=a["share_token"], _external=True) if a["share_token"] else None
    return render_template(f"view_{view}.html", a=a, A=A, V=V, view=view, views_nav=VIEWS, platform=platform,
                           others=others, loom=loom, share=share, client=_client_info(a))


@app.route("/audits/<aid>/export.pdf")
@login_required
def export_pdf(aid):
    a = get_audit(aid)
    if a["status"] != "done":
        abort(404)
    A = store.load_json(aid)
    if A is None:
        abort(404)
    html = _report_html(a, A)
    name = re.sub(r"[^A-Za-z0-9]+", "-", a["builder_name"] or a["domain"]).strip("-")
    resp = _pdf_response(aid, html, "report.pdf",
                         f"{name}-AI-Visibility-Audit-{datetime.date.fromtimestamp(a['created_at']).isoformat()}.pdf")
    from . import todo
    todo.mark_exported(g.user["id"], a, "full")  # ticks this builder on the exporter's to-do list
    return resp


@app.route("/api/audits/<aid>")
@login_required
def api_status(aid):
    a = get_audit(aid)
    ahead = db.q("SELECT COUNT(*) AS n FROM audits WHERE status='queued' AND created_at<?", (a["created_at"],), one=True)["n"]
    data = {k: a[k] for k in ("id", "status", "stage", "progress", "error")}
    data["ahead"] = ahead
    data["activity"] = json.loads(a["activity"] or "[]")
    data["engines"] = json.loads(a["engines"] or "{}")
    if not auth.can(g.user, "admin"):  # change 24: Sales see only platforms that are working, and no "skipped" lines
        data["engines"] = {k: v for k, v in data["engines"].items() if v.get("state") not in ("off", "needs_credits")}
        data["activity"] = [ev for ev in data["activity"] if not str(ev.get("text", "")).startswith("Skipped")]
    if a["status"] in ("queued", "running") and (a["lease_until"] or 0) < time.time():
        jobs.kick(PUBLIC_URL or request.host_url)  # serverless: nobody is working on it right now, start a tick
    return jsonify(data)


def _print_page(html):
    """Report HTML that opens the browser's print dialog, for hosts without a PDF engine."""
    snippet = '<script>window.addEventListener("load",function(){setTimeout(function(){window.print()},400)})</script>'
    body = html.replace("</body>", snippet + "</body>") if "</body>" in html else html + snippet
    resp = app.response_class(body, mimetype="text/html")
    resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
    return resp


@app.route("/api/jobs/tick", methods=["GET", "POST"])
def jobs_tick():
    """Serverless worker: Vercel Cron (GET, every minute) and the app itself (POST) call this to run queued work."""
    sent = request.headers.get("Authorization", "")
    if not jobs.tick_secret() or not secrets.compare_digest(sent, "Bearer " + jobs.tick_secret()):
        abort(404)
    did = jobs.work()
    if did and jobs.pending():  # hand over only after real work, so idle ticks never chain forever
        jobs.kick(PUBLIC_URL or request.host_url)
    return jsonify({"worked": did})


@app.route("/audits/<aid>/report")
@login_required
def audit_report(aid):
    a = get_audit(aid)
    A = store.load_json(aid) if a["status"] == "done" else None
    if A is None:
        abort(404)
    return app.response_class(_report_html(a, A), mimetype="text/html")


@app.route("/audits/<aid>/data.json")
@permission_required("raw_data")
def audit_data(aid):
    a = get_audit(aid)
    data = store.get_bytes(aid, "audit.json")
    if data is None:
        abort(404)
    return send_file(io.BytesIO(data), mimetype="application/json", as_attachment=True, download_name=f"{a['domain']}-audit.json")


@app.route("/audits/<aid>/rerun", methods=["POST"])
@login_required
def rerun(aid):
    a = get_audit(aid)
    nid = uuid.uuid4().hex[:12]
    db.x("""INSERT INTO audits (id, user_id, url, domain, scope, locations, city, state, test_mode, status, stage, progress, share_token, created_at)
            VALUES (?,?,?,?,?,?,?,?,?, 'queued', 'Waiting to start', 0, ?, ?)""",
         (nid, g.user["id"], a["url"], a["domain"], a["scope"] or "root", a["locations"], a["city"], a["state"], a["test_mode"], secrets.token_urlsafe(18), time.time()))
    jobs.kick(PUBLIC_URL or request.host_url)
    return redirect(url_for("audit_page", aid=nid))


# ---------------- client link (change 31) ----------------
CLIENT_VIEWS = ("overview", "competitors")
CLIENT_DAYS = int(os.environ.get("HBM_CLIENT_LINK_DAYS", "30"))


def vurl(aid, view, **kw):
    """Link to an audit screen; on a client link, only the two client screens exist (others return None)."""
    tok = g.get("client_token")
    if tok:
        return url_for("client_view", token=tok, view=view, **kw) if view in CLIENT_VIEWS else None
    return url_for("audit_view", aid=aid, view=view, **kw)


app.jinja_env.globals["vurl"] = vurl


def _client_info(a):
    if not a["client_token"] or (a["client_expires"] or 0) < time.time():
        return None
    return {"url": url_for("client_view", token=a["client_token"], view="overview", _external=True),
            "views": a["client_views"] or 0, "last": a["client_last_view"], "expires": a["client_expires"]}


@app.route("/audits/<aid>/client-link", methods=["POST"])
@login_required
def client_link(aid):
    """Create (or reuse), renew or turn off the client's interactive link for a finished audit."""
    if not auth.can(g.user, "share"):
        abort(403)
    a = get_audit(aid)
    if a["status"] != "done":
        abort(404)
    act = request.form.get("action", "create")
    if act == "off":
        db.x("UPDATE audits SET client_token=NULL, client_expires=NULL WHERE id=?", (aid,))
        security_event("client_link_off", g.user["email"], aid)
        info = None
    else:
        if act == "new" or not _client_info(a):
            db.x("UPDATE audits SET client_token=?, client_expires=?, client_views=0, client_last_view=NULL WHERE id=?",
                 (secrets.token_urlsafe(24), time.time() + CLIENT_DAYS * 86400, aid))
            security_event("client_link_created", g.user["email"], aid)
        from . import todo
        todo.mark_exported(g.user["id"], a, "client")  # sending the link counts like exporting a PDF
        info = _client_info(get_audit(aid))
    if request.headers.get("Accept", "").startswith("application/json"):
        return jsonify({"link": info and dict(info, expires_label=f'{datetime.date.fromtimestamp(info["expires"]):%b} {datetime.date.fromtimestamp(info["expires"]).day}',
                                              last_label=ago(info["last"]) if info["last"] else None)})
    flash(("Client link ready: " + info["url"]) if info else "Client link turned off.", "good")
    return redirect(url_for("audit_view", aid=aid, view="overview"))


@app.route("/c/<token>")
@app.route("/c/<token>/<view>")
def client_view(token, view="overview"):
    """The client's interactive page: Visibility Overview and Competitor Research only, no sign-in, read-only."""
    if view not in CLIENT_VIEWS:
        abort(404)
    a = db.q("SELECT * FROM audits WHERE client_token=? AND status='done'", (token,), one=True) if len(token) >= 20 else None
    if not a or (a["client_expires"] or 0) < time.time():
        return render_template("client_expired.html"), 404
    A = store.load_json(a["id"])
    if A is None:
        abort(404)
    seen = session.get("cv", {})
    if time.time() - seen.get(token, 0) > 1800:  # count a visit, not every click
        db.x("UPDATE audits SET client_views=COALESCE(client_views, 0)+1, client_last_view=? WHERE id=?", (time.time(), a["id"]))
        seen[token] = time.time()
        session["cv"] = {k: v for k, v in seen.items() if time.time() - v < 86400}
    g.user, g.client_token = None, token  # a teammate opening it sees exactly what the client sees
    platform = request.args.get("platform", "all")
    if platform != "all" and platform not in views.measured_engines(A):
        platform = "all"
    area = request.args.get("area") or None
    vs = [v for v in request.args.getlist("vs") if v][:6]
    hist = _history(a, A, platform)
    V = views.build(A, platform, hist, vs, area, clean=True)
    V["chart"] = views.chart(A, hist)
    V["score_all"] = V["score"] if not area else views.build(A, platform, clean=True)["score"]
    V["area_scores"] = {ar: views.build(A, platform, None, None, ar, clean=True)["score"] for ar in (V["areas"] if len(V["areas"]) > 1 else [])}
    exp = datetime.date.fromtimestamp(a["client_expires"])
    resp = app.make_response(render_template(f"view_{view}.html", base_tpl="client_base.html", a=a, A=A, V=V, view=view,
                                             views_nav=VIEWS, platform=platform, others=[], loom="", share=None,
                                             now_ts=time.time(), exp_date=f"{exp:%b} {exp.day}"))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.route("/audits/<aid>/share", methods=["POST"])
@login_required
def share_toggle(aid):
    a = get_audit(aid)
    if a["user_id"] != g.user["id"] and not auth.can(g.user, "manage_any_share"):
        abort(403)
    token = None if request.form.get("action") == "off" else secrets.token_urlsafe(18)
    db.x("UPDATE audits SET share_token=? WHERE id=?", (token, aid))
    flash("Share link turned off." if token is None else "New share link created. The old link no longer works.", "good")
    return redirect(url_for("audit_page", aid=aid))


@app.route("/r/<token>")
def shared_report(token):
    a = db.q("SELECT * FROM audits WHERE share_token=? AND status='done'", (token,), one=True)
    A = store.load_json(a["id"]) if a else None
    if A is None:
        abort(404)
    return app.response_class(_report_html(a, A), mimetype="text/html")


@app.route("/logo.webp")
def logo():
    for p in [os.path.join(ROOT, "data", "hbm-logo.webp"), os.path.join(ROOT, "webapp", "static", "hbm-logo.webp")]:
        if os.path.exists(p):
            return send_file(p, mimetype="image/webp", max_age=86400)
    abort(404)


# ---------------- admin ----------------
@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    if request.method == "POST":
        act = request.form.get("action")
        uid = int(request.form.get("uid", 0) or 0)
        target = db.q("SELECT * FROM users WHERE id=?", (uid,), one=True) if uid else None
        role = request.form.get("role") if request.form.get("role") in auth.ROLES else "sales"
        if act == "add_user":
            email = request.form.get("email", "").strip().lower()
            pw = request.form.get("password", "")
            if not auth.allowed_email(email):
                flash(f"Only {auth.ALLOWED_DOMAIN} email addresses can have an account.", "bad")
            elif pw and len(pw) < 12:
                flash("A temporary password needs at least 12 characters (or leave it blank).", "bad")
            elif db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
                flash("That email already has an account.", "bad")
            else:
                ph = generate_password_hash(pw, method="pbkdf2:sha256:600000") if pw else "!"
                db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?,?,?,?,?)",
                     (email, request.form.get("name", "").strip()[:80] or email, ph, 1 if role == "admin" else 0, role, time.time()))
                security_event("user_added", email, role)
                flash(f"Added {email} as {auth.ROLES[role]}. They can sign in now with an emailed code"
                      + (" or the temporary password you set (share it privately)." if pw else " and set their own password.")
                      + (" Google sign-in works too." if auth.google_enabled() else ""), "good")
        elif act in ("deactivate", "activate", "approve", "reject", "set_role") and target:
            if target["id"] == g.user["id"] and act in ("deactivate", "reject", "set_role"):
                flash("You can't change your own access. Ask another admin.", "bad")
            elif act == "approve":
                db.x("UPDATE users SET pending=0, active=1, role=?, is_admin=? WHERE id=?", (role, 1 if role == "admin" else 0, target["id"]))
                security_event("user_approved", target["email"], role)
                flash(f"Approved {target['email']} as {auth.ROLES[role]}.", "good")
            elif act == "reject":
                db.x("UPDATE users SET pending=0, active=0 WHERE id=?", (target["id"],))
                security_event("user_rejected", target["email"])
            elif act == "set_role":
                db.x("UPDATE users SET role=?, is_admin=? WHERE id=?", (role, 1 if role == "admin" else 0, target["id"]))
                security_event("role_changed", target["email"], role)
                flash(f"{target['email']} is now {auth.ROLES[role]}.", "good")
            else:
                db.x("UPDATE users SET active=? WHERE id=?", (1 if act == "activate" else 0, target["id"]))
                security_event("user_" + act + "d", target["email"])
        elif act == "cap":
            raw = request.form.get("limit", "").strip()
            budget.set_limit(float(raw) if raw else None, reset=bool(request.form.get("reset")))
            flash("Spending cap updated.", "good")
        return redirect(url_for("admin"))
    users = db.q("""SELECT u.*, (SELECT COUNT(*) FROM audits a WHERE a.user_id=u.id AND a.created_at>?) AS month_n,
                    (SELECT COUNT(*) FROM audits a WHERE a.user_id=u.id AND a.created_at>?) AS day_n
                    FROM users u WHERE u.pending != 2 ORDER BY u.pending DESC, u.active DESC, u.name""", (time.time() - 30 * 86400, time.time() - 86400))
    live = status.current()
    platforms = [(engines.ENGINE_LABELS[en], live[en]["state"], live[en]["label"]) for en in engines.ENGINES]
    platforms += [("Semrush", "good" if semrush.available() else "off", "Connected" if semrush.available() else "Not connected"),
                  ("Browser page rendering (REA)", "good" if site.rea_available() else "off", "On" if site.rea_available() else "Off (plain page fetch)")]
    stats = db.q("""SELECT COUNT(*) AS total, SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                    SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN status IN ('queued','running') THEN 1 ELSE 0 END) AS active FROM audits WHERE created_at>?""", (time.time() - 86400,), one=True)
    fb = db.q("""SELECT f.*, u.name AS who FROM feedback f JOIN users u ON u.id=f.user_id ORDER BY f.created_at DESC LIMIT 50""")
    seclog = db.q("SELECT * FROM security_log ORDER BY t DESC LIMIT 40")
    return render_template("admin.html", feedback=fb, seclog=seclog, auth_info={"google": auth.google_enabled(),
                           "password": True, "mail": mailer.configured(), "domain": auth.ALLOWED_DOMAIN}, roles=auth.ROLES, users=users, platforms=platforms, budget=budget.status(), stats=stats,
                           workers=jobs.WORKERS, keys_file=config.KEYS_FILE)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(413)
@app.errorhandler(429)
def err(e):
    return render_template("error.html", code=e.code, message=getattr(e, "description", "")), e.code


@app.errorhandler(500)
def err500(e):
    return render_template("error.html", code=500, message="Something went wrong on our side. Try again; if it keeps happening, tell an admin."), 500


def _bootstrap_admins():
    for email in sorted(auth.ADMIN_EMAILS):
        if auth.allowed_email(email) and not db.q("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            db.x("INSERT INTO users (email, name, password_hash, is_admin, role, created_at) VALUES (?,?, '!', 1, 'admin', ?)",
                 (email, email.split("@")[0].replace(".", " ").title(), time.time()))
            logging.getLogger("hbm").info("Created admin account for %s (sign in with an emailed code)", email)


def create_app():
    if jobs.SERVERLESS and not db.PG:
        raise SystemExit("This host has no lasting disk: add a Postgres database (Vercel → Storage → Neon) so DATABASE_URL is set.")
    db.init()
    _bootstrap_admins()
    if os.environ.get("APP_ENV") == "production":
        if not os.environ.get("SECRET_KEY") and not os.path.exists(os.path.join(db.DATA_DIR, "secret_key")):
            logging.getLogger("hbm").warning("SECRET_KEY not set; a random key was saved in the data folder")
        if not mailer.configured():
            logging.getLogger("hbm").warning("SMTP not set: emailed sign-in codes won't work until SMTP_HOST/MAIL_FROM are set")
    jobs.start()
    return app


from . import bulk, todo  # noqa: E402,F401  (Bulk Analysis and To-do routes)
