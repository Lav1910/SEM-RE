"""Sign-in and permissions.

Who can get in
- Only email addresses at ALLOWED_EMAIL_DOMAIN (default homebuildermarketers.com), for every sign-in method.
- Google sign-in (recommended): set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET. Google is asked to show only accounts
  in the HBM Google Workspace (hd=...), and the returned token is checked again here (audience, issuer, expiry,
  nonce, verified email, workspace domain).
- A first Google sign-in by someone new creates a *pending* account that an admin approves (and gives a role) on the
  Admin page. Emails listed in ADMIN_EMAILS are approved as admins automatically (bootstraps the first admin).
- Email + password, always available. Passwords are hashed with PBKDF2-SHA256 (600,000 rounds).
- One-time sign-in code or link by email, always available: a 6-digit code and a single-use link (15 minutes, 5 tries).
  It also lets someone set a password the first time, or reset a forgotten one. Codes go only to existing, approved
  accounts; an admin adds people on the Admin page (email only, no password needed).

Roles (see PERMISSIONS): admin sees and manages everything; sales runs audits, uses every audit, Strategy and Loom
screen, exports and share links, but not the Admin page, spending figures or raw audit data.
"""
import base64
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

ALLOWED_DOMAIN = os.environ.get("ALLOWED_EMAIL_DOMAIN", "homebuildermarketers.com").lower().strip()
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}
GOOGLE_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

ROLES = {"admin": "Admin", "sales": "Sales"}
PERMISSIONS = {
    "admin": {"admin", "see_costs", "raw_data", "team_audits", "run_audits", "strategy", "share", "manage_any_share"},
    "sales": {"team_audits", "run_audits", "strategy", "share"},
}


def google_enabled():
    return bool(GOOGLE_ID and GOOGLE_SECRET)


def password_login_enabled():
    return True  # email + password and one-time email codes are always available (Google sign-in is optional)


# ---------------- one-time email codes / links ----------------
CODE_MINUTES = 15
CODE_ATTEMPTS = 5


def _digest(secret_key, value):
    import hashlib
    import hmac
    return hmac.new(secret_key.encode(), value.encode(), hashlib.sha256).hexdigest()


def new_code(db, user, secret_key, ip):
    """Create a one-time code and link token for a user. Returns (code, token). Older unused codes stop working."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    token = secrets.token_urlsafe(32)
    now = time.time()
    db.x("UPDATE login_codes SET used=1 WHERE user_id=? AND used=0", (user["id"],))
    db.x("""INSERT INTO login_codes (user_id, code_hash, link_hash, expires, attempts, used, created_at, ip)
            VALUES (?,?,?,?,0,0,?,?)""", (user["id"], _digest(secret_key, f"{user['id']}:{code}"), _digest(secret_key, token),
                                          now + CODE_MINUTES * 60, now, ip))
    return code, token


def check_code(db, user, code, secret_key):
    """True if the code is right; each wrong try counts, and 5 wrong tries burn the code."""
    row = db.q("SELECT * FROM login_codes WHERE user_id=? AND used=0 AND expires>? ORDER BY created_at DESC LIMIT 1",
               (user["id"], time.time()), one=True)
    if not row:
        return False
    if row["attempts"] >= CODE_ATTEMPTS:
        db.x("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
        return False
    good = secrets.compare_digest(row["code_hash"], _digest(secret_key, f"{user['id']}:{(code or '').strip()}"))
    db.x("UPDATE login_codes SET attempts=attempts+1, used=? WHERE id=?", (1 if good else 0, row["id"]))
    return good


def check_link(db, token, secret_key):
    """Returns the user id for a valid, unused, unexpired link token (and burns it), else None."""
    row = db.q("SELECT * FROM login_codes WHERE link_hash=? AND used=0 AND expires>?", (_digest(secret_key, token or ""), time.time()), one=True)
    if not row:
        return None
    db.x("UPDATE login_codes SET used=1 WHERE id=?", (row["id"],))
    return row["user_id"]


def allowed_email(email):
    email = (email or "").strip().lower()
    return bool(ALLOWED_DOMAIN) and email.endswith("@" + ALLOWED_DOMAIN) and email.count("@") == 1


def role_of(user):
    if not user:
        return None
    return "admin" if user["is_admin"] else (user["role"] if "role" in user.keys() and user["role"] in ROLES else "sales")


def can(user, perm):
    r = role_of(user)
    return bool(r) and perm in PERMISSIONS.get(r, set())


# ---------------- Google OpenID Connect (authorization code flow) ----------------
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def google_start_url(redirect_uri, session):
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    session["g_state"], session["g_nonce"], session["g_t"] = state, nonce, time.time()
    q = {"client_id": GOOGLE_ID, "redirect_uri": redirect_uri, "response_type": "code", "scope": "openid email profile",
         "state": state, "nonce": nonce, "prompt": "select_account", "hd": ALLOWED_DOMAIN, "access_type": "online"}
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode())


def google_finish(args, redirect_uri, session):
    """Returns {"email", "name", "sub"} or raises ValueError with a message safe to show."""
    state, t = session.pop("g_state", None), session.pop("g_t", 0)
    nonce = session.pop("g_nonce", None)
    if args.get("error"):
        raise ValueError("Google sign-in was cancelled.")
    if not state or not secrets.compare_digest(state, args.get("state", "")) or time.time() - t > 600:
        raise ValueError("That sign-in link expired. Try again.")
    body = urllib.parse.urlencode({"code": args.get("code", ""), "client_id": GOOGLE_ID, "client_secret": GOOGLE_SECRET,
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.loads(r.read().decode())
    except Exception:
        raise ValueError("Google didn't accept the sign-in. Try again.")
    # The ID token comes straight from Google's token endpoint over TLS, so its claims can be trusted after these
    # checks (OpenID Connect Core 3.1.3.7); the signature check is only required for tokens passed through the browser.
    try:
        claims = _b64json(tok["id_token"].split(".")[1])
    except Exception:
        raise ValueError("Google sent an unreadable sign-in token.")
    now = time.time()
    if claims.get("aud") != GOOGLE_ID or claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com"):
        raise ValueError("That sign-in token wasn't meant for this app.")
    if claims.get("exp", 0) < now - 60 or not nonce or claims.get("nonce") != nonce:
        raise ValueError("That sign-in token expired. Try again.")
    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or claims.get("hd", "").lower() != ALLOWED_DOMAIN or not allowed_email(email):
        raise ValueError(f"Only {ALLOWED_DOMAIN} Google Workspace accounts can sign in.")
    return {"email": email, "name": claims.get("name") or email.split("@")[0], "sub": claims.get("sub")}
