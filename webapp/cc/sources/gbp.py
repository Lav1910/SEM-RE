"""Google Business Profile connector (change 66): READ ONLY towards the profiles.

How it connects
- An admin clicks "Connect Business Profile" once and signs in with the Google account that manages the client profiles
  (OAuth 2.0 with PKCE). Google gives back a refresh token, stored encrypted (secretbox) in the settings store.
- The OAuth client ID and secret are server settings (GBP_OAUTH_CLIENT_ID / GBP_OAUTH_CLIENT_SECRET), entered by a person.
- Google only offers the full "business.manage" permission for these APIs (there's no read-only scope), so read-only is
  enforced here: every Business Profile call goes through get(), which only ever sends GET (a test checks this). The only
  POSTs are to Google's sign-in endpoints (exchange the code, refresh the access token, revoke on disconnect).
- Until Google approves API access for the Cloud project its quota is 0, and calls answer 403/429: that's reported as
  "not approved yet", never as zero activity.

APIs read (all GET)
  mybusinessaccountmanagement v1   accounts
  mybusinessbusinessinformation v1 locations (title, website, address, Maps link, verification signal)
  businessprofileperformance v1    daily call clicks, website clicks, direction requests, views, conversations
  mybusiness v4                    reviews (with replies), local posts, the business's own photos
"""
import base64
import datetime
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

from aeo import config, kv

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/business.manage openid email"
KV_KEY = "cc_gbp_token"

ACCOUNTS = "https://mybusinessaccountmanagement.googleapis.com/v1/accounts"
INFO = "https://mybusinessbusinessinformation.googleapis.com/v1"
PERF = "https://businessprofileperformance.googleapis.com/v1"
V4 = "https://mybusiness.googleapis.com/v4"
READ_MASK = "name,title,websiteUri,storefrontAddress,metadata,openInfo"

METRICS = {  # Google's DailyMetric -> our key (views are the four impression metrics added together)
    "CALL_CLICKS": "calls", "WEBSITE_CLICKS": "website", "BUSINESS_DIRECTION_REQUESTS": "directions",
    "BUSINESS_CONVERSATIONS": "conversations",
    "BUSINESS_IMPRESSIONS_DESKTOP_MAPS": "views", "BUSINESS_IMPRESSIONS_DESKTOP_SEARCH": "views",
    "BUSINESS_IMPRESSIONS_MOBILE_MAPS": "views", "BUSINESS_IMPRESSIONS_MOBILE_SEARCH": "views",
}
STARS = {"ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}


class NotConnected(Exception):
    pass


class NotApproved(Exception):
    """Google hasn't approved Business Profile API access for the Cloud project (quota 0) or an API is switched off."""


class Reauth(Exception):
    """The stored sign-in no longer works (revoked, password change, SECRET_KEY changed): connect again."""


class GBPError(Exception):
    pass


# change 87: a main sign-in plus extra Google accounts (see gaccounts.py); use(email) picks the account for a block of calls
from .gaccounts import Accounts  # noqa: E402
ACC = Accounts(KV_KEY, "Business Profile", NotConnected, Reauth)
use, accounts_connected = ACC.use, ACC.all


# ---------------------------------------------------------------- settings and stored sign-in
def client_id():
    return config.key("GBP_OAUTH_CLIENT_ID")


def client_secret():
    return config.key("GBP_OAUTH_CLIENT_SECRET")


def configured():
    return bool(client_id() and client_secret())


def stored():
    """{email, by, connected_at, state, main} of the main sign-in, without the token, or None."""
    return ACC.stored()


def connected():
    """True when the server key is set and at least one connected Google account can be used."""
    return bool(configured() and ACC.connected())


def _refresh_token():
    return ACC.refresh_token()


def save(refresh_token, email, by, extra=False):
    ACC.save(refresh_token, email, by, extra)


def mark(state, detail=None):
    ACC.mark(state, detail)


def forget(email=None):
    ACC.forget(email)


# ---------------------------------------------------------------- OAuth (the only POSTs, all to Google's sign-in)
def pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(redirect_uri, state, challenge, login_hint=None):
    q = {"client_id": client_id(), "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true", "state": state,
         "code_challenge": challenge, "code_challenge_method": "S256"}
    if login_hint:
        q["login_hint"] = login_hint
    return AUTH_URL + "?" + urllib.parse.urlencode(q)


def _post_form(url, fields, timeout=20):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "hbm-command-center/1"})
    try:
        with _open(req, timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode() or "{}")
        except ValueError:
            body = {}
        return {"error": body.get("error") or f"http_{e.code}", "error_description": body.get("error_description")}


def exchange(code, redirect_uri, verifier):
    """-> (refresh_token, email). Raises GBPError with a safe message."""
    r = _post_form(TOKEN_URL, {"code": code, "client_id": client_id(), "client_secret": client_secret(),
                               "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    if r.get("error"):
        raise GBPError(f"Google refused the sign-in ({r['error']})")
    if not r.get("refresh_token"):
        raise GBPError("Google didn't return a lasting sign-in. Remove the app's access in your Google account and connect again.")
    if SCOPE.split()[0] not in (r.get("scope") or ""):
        raise GBPError("The Business Profile permission wasn't granted. Connect again and tick the Business Profile box.")
    return r["refresh_token"], _email_from_id_token(r.get("id_token"))


def _email_from_id_token(tok):
    """The signed-in email from the ID token Google just returned to us over TLS (only used as a label)."""
    try:
        part = tok.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("email")
    except Exception:  # noqa: BLE001
        return None


def access_token():
    cached = ACC.cache_get()
    if cached:
        return cached
    r = _post_form(TOKEN_URL, {"client_id": client_id(), "client_secret": client_secret(),
                               "refresh_token": _refresh_token(), "grant_type": "refresh_token"})
    if r.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
        mark("reauth", "Google no longer accepts the stored sign-in (" + r["error"] + "). Connect again.")
        raise Reauth(r["error"])
    if r.get("error") or not r.get("access_token"):
        raise GBPError(f"Couldn't refresh the Google sign-in ({r.get('error') or 'no token'})")
    ACC.cache_set(r["access_token"], r.get("expires_in"))
    return r["access_token"]


def revoke():
    """Tell Google to drop the app's access (best effort; the local copy is deleted either way)."""
    try:
        tok = _refresh_token()
    except (NotConnected, Reauth):
        return False
    return not _post_form(REVOKE_URL, {"token": tok}).get("error")


# ---------------------------------------------------------------- reading (GET only)
def _open(req, timeout):  # separate so tests can check every request
    return urllib.request.urlopen(req, timeout=timeout)


def get(url, params=None, calls=None):
    if not configured():
        raise NotConnected("Google sign-in key not set on the server")
    full = url + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    refreshed = False
    for attempt in range(4):
        req = urllib.request.Request(full, method="GET", headers={"Authorization": "Bearer " + access_token(),
                                                                   "Accept": "application/json", "User-Agent": "hbm-command-center/1"})
        try:
            with _open(req, 30) as r:
                if calls is not None:
                    calls[0] += 1
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            if calls is not None:
                calls[0] += 1
            try:
                err = (json.loads(e.read().decode() or "{}").get("error") or {})
            except ValueError:
                err = {}
            msg, status = str(err.get("message") or "")[:200], str(err.get("status") or "")
            if e.code == 401 and not refreshed:
                ACC.cache_clear()
                refreshed = True
                continue
            if e.code == 403 and (status == "PERMISSION_DENIED" and ("has not been used" in msg or "disabled" in msg.lower())):
                raise NotApproved("A Business Profile API is switched off in the Google Cloud project: " + msg)
            if e.code == 429 or (e.code == 403 and "quota" in msg.lower()):
                # quota 0 (not approved) and a busy minute give the same answer, so wait twice before calling it
                if attempt < 2:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise NotApproved("Google refused on quota: API access isn't approved for this project yet (quota 0), "
                                  "or the per-minute limit was hit. It tries again on the next sync")
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            where = urllib.parse.urlsplit(url).netloc.split(".")[0]
            raise GBPError(f"Google answered HTTP {e.code} ({where}){': ' + msg if msg else ''}")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise GBPError("Couldn't reach Google")
            time.sleep(2 ** attempt)
    raise GBPError("Google kept failing")


def _pages(url, key, params=None, calls=None, max_pages=5):
    """-> (items, complete): follows nextPageToken up to max_pages."""
    out, token = [], None
    for _ in range(max_pages):
        p = dict(params or {})
        if token:
            p["pageToken"] = token
        r = get(url, p, calls)
        out += r.get(key) or []
        token = r.get("nextPageToken")
        if not token:
            return out, True
    return out, False


def accounts(calls=None):
    return _pages(ACCOUNTS, "accounts", {"pageSize": 20}, calls)[0]


def locations(account, calls=None):
    return _pages(f"{INFO}/{account}/locations", "locations", {"readMask": READ_MASK, "pageSize": 100}, calls, max_pages=10)[0]


def daily_metrics(location, start, end, calls=None):
    """-> {date_iso: {calls, website, directions, conversations, views}} for start..end (dates Google returned)."""
    params = [("dailyMetrics", m) for m in METRICS] + [
        ("dailyRange.startDate.year", start.year), ("dailyRange.startDate.month", start.month), ("dailyRange.startDate.day", start.day),
        ("dailyRange.endDate.year", end.year), ("dailyRange.endDate.month", end.month), ("dailyRange.endDate.day", end.day)]
    r = get(f"{PERF}/{location}:fetchMultiDailyMetricsTimeSeries", params, calls)
    out = {}
    for multi in r.get("multiDailyMetricTimeSeries") or []:
        for s in multi.get("dailyMetricTimeSeries") or []:
            key = METRICS.get(s.get("dailyMetric"))
            if not key:
                continue
            for dv in (s.get("timeSeries") or {}).get("datedValues") or []:
                d = dv.get("date") or {}
                try:
                    iso = datetime.date(d["year"], d["month"], d["day"]).isoformat()
                except (KeyError, TypeError, ValueError):
                    continue
                row = out.setdefault(iso, {"calls": 0, "website": 0, "directions": 0, "conversations": 0, "views": 0})
                row[key] += int(dv.get("value") or 0)  # Google leaves "value" out on days with none
    return out


def _v4(account, location):
    return f"{V4}/{account}/{location}"


def reviews(account, location, calls=None, max_pages=5):
    """-> (reviews, average, total, complete)."""
    out, token, avg, total = [], None, None, None
    for _ in range(max_pages):
        p = {"pageSize": 50, "orderBy": "updateTime desc"}
        if token:
            p["pageToken"] = token
        r = get(_v4(account, location) + "/reviews", p, calls)
        out += r.get("reviews") or []
        avg = r.get("averageRating", avg)
        total = r.get("totalReviewCount", total)
        token = r.get("nextPageToken")
        if not token:
            return out, avg, total, True
    return out, avg, total, False


def local_posts(account, location, calls=None):
    return _pages(_v4(account, location) + "/localPosts", "localPosts", {"pageSize": 100}, calls, max_pages=3)


def media(account, location, calls=None):
    return _pages(_v4(account, location) + "/media", "mediaItems", {"pageSize": 100}, calls, max_pages=3)


# ---------------------------------------------------------------- small helpers
def ts(rfc3339):
    """Google's RFC 3339 time -> unix seconds (None if missing)."""
    if not rfc3339:
        return None
    s = str(rfc3339).replace("Z", "+00:00")
    if "." in s:  # Python 3.9 parses at most 6 fractional digits (Google sends up to 9)
        head, frac = s.split(".", 1)
        n = len(frac) - len(frac.lstrip("0123456789"))
        s = head + "." + frac[:n][:6].ljust(6, "0") + frac[n:]
    try:
        return datetime.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def stars(review):
    return STARS.get(review.get("starRating"))


def address_line(loc):
    a = loc.get("storefrontAddress") or {}
    parts = (a.get("addressLines") or []) + [a.get("locality"), a.get("administrativeArea")]
    return ", ".join(p for p in parts if p) or None
