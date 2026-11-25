"""Google Analytics 4 connector (change 67): READ ONLY, enforced by Google itself.

- Sign-in: the same Google Cloud OAuth client as Business Profile (GBP_OAUTH_CLIENT_ID / _SECRET), its own consent and
  its own stored token (encrypted, settings store key cc_ga4_token), so Analytics can use a different Google account.
- Permission: analytics.readonly. Google refuses any change with this permission, whatever the code sends.
- Calls: Admin API (GET) to list properties and their web streams; Data API runReport (POST is how Google takes a report
  query; it reads only). get()/report() refuse any other host or method, and a test checks every request.
- No Google approval is needed (unlike Business Profile); normal per-property quotas apply.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from aeo import kv
from . import gbp as _g

SCOPE = "https://www.googleapis.com/auth/analytics.readonly openid email"
KV_KEY = "cc_ga4_token"
ADMIN = "https://analyticsadmin.googleapis.com/v1beta"
DATA = "https://analyticsdata.googleapis.com/v1beta"

NotConnected, Reauth, GBPError = _g.NotConnected, _g.Reauth, _g.GBPError


class GA4Error(Exception):
    pass


class QuotaRefused(Exception):
    pass


configured = _g.configured  # same OAuth client

# ---------------------------------------------------------------- stored sign-ins (own keys, same encryption)
# change 87: a main sign-in plus extra Google accounts (see gaccounts.py)
from .gaccounts import Accounts  # noqa: E402
ACC = Accounts(KV_KEY, "Analytics", NotConnected, Reauth)
use, accounts_connected = ACC.use, ACC.all


def stored():
    return ACC.stored()


def connected():
    return bool(configured() and ACC.connected())


def save(refresh_token, email, by, extra=False):
    ACC.save(refresh_token, email, by, extra)


def mark(state, detail=None):
    ACC.mark(state, detail)


def forget(email=None):
    ACC.forget(email)


def _refresh_token():
    return ACC.refresh_token()


def auth_url(redirect_uri, state, challenge):
    q = {"client_id": _g.client_id(), "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
         "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
    return _g.AUTH_URL + "?" + urllib.parse.urlencode(q)


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
        return {"error": body.get("error") or f"http_{e.code}"}


def exchange(code, redirect_uri, verifier):
    r = _post_form(_g.TOKEN_URL, {"code": code, "client_id": _g.client_id(), "client_secret": _g.client_secret(),
                                  "redirect_uri": redirect_uri, "grant_type": "authorization_code", "code_verifier": verifier})
    if r.get("error"):
        raise GA4Error(f"Google refused the sign-in ({r['error']})")
    if not r.get("refresh_token"):
        raise GA4Error("Google didn't return a lasting sign-in. Remove the app's access in your Google account and connect again.")
    if SCOPE.split()[0] not in (r.get("scope") or ""):
        raise GA4Error("The Analytics permission wasn't granted. Connect again and tick the Analytics box.")
    return r["refresh_token"], _g._email_from_id_token(r.get("id_token"))


def access_token():
    cached = ACC.cache_get()
    if cached:
        return cached
    r = _post_form(_g.TOKEN_URL, {"client_id": _g.client_id(), "client_secret": _g.client_secret(),
                                  "refresh_token": _refresh_token(), "grant_type": "refresh_token"})
    if r.get("error") in ("invalid_grant", "unauthorized_client", "invalid_client"):
        mark("reauth", "Google no longer accepts the stored Analytics sign-in (" + r["error"] + "). Connect again.")
        raise Reauth(r["error"])
    if r.get("error") or not r.get("access_token"):
        raise GA4Error(f"Couldn't refresh the Google sign-in ({r.get('error') or 'no token'})")
    ACC.cache_set(r["access_token"], r.get("expires_in"))
    return r["access_token"]


def revoke():
    try:
        tok = _refresh_token()
    except (NotConnected, Reauth):
        return False
    return not _post_form(_g.REVOKE_URL, {"token": tok}).get("error")


# ---------------------------------------------------------------- reading
def _open(req, timeout):  # separate so tests can check every request
    return urllib.request.urlopen(req, timeout=timeout)


def _call(method, url, body=None, calls=None):
    host = urllib.parse.urlsplit(url).netloc
    allowed = (method == "GET" and url.startswith(ADMIN)) or (method == "POST" and url.startswith(DATA) and url.split("?")[0].endswith(":runReport"))
    if not allowed:
        raise GA4Error(f"Refused to send {method} to {host}: only reads are allowed")
    if not configured():
        raise NotConnected("Google sign-in key not set on the server")
    refreshed = False
    for attempt in range(4):
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                     headers={"Authorization": "Bearer " + access_token(), "Accept": "application/json",
                                              "Content-Type": "application/json", "User-Agent": "hbm-command-center/1"})
        try:
            with _open(req, 45) as r:
                if calls is not None:
                    calls[0] += 1
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            if calls is not None:
                calls[0] += 1
            try:
                err = json.loads(e.read().decode() or "{}").get("error") or {}
            except ValueError:
                err = {}
            msg, status = str(err.get("message") or "")[:220], str(err.get("status") or "")
            if e.code == 401 and not refreshed:
                ACC.cache_clear()
                refreshed = True
                continue
            if e.code == 403 and ("has not been used" in msg or "disabled" in msg.lower()):
                raise GA4Error("An Analytics API is switched off in the Google Cloud project: " + msg)
            if e.code == 403:
                raise GA4Error("The connected Google account can't read this property (needs at least Viewer access)")
            if e.code == 429 or status == "RESOURCE_EXHAUSTED":
                if attempt < 2:
                    time.sleep(3 * 2 ** attempt)
                    continue
                raise QuotaRefused("Google Analytics quota reached for now; it tries again on the next sync")
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise GA4Error(f"Google Analytics answered HTTP {e.code}{': ' + msg if msg else ''}")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise GA4Error("Couldn't reach Google Analytics")
            time.sleep(2 ** attempt)
    raise GA4Error("Google Analytics kept failing")


def get(url, params=None, calls=None):
    return _call("GET", url + ("?" + urllib.parse.urlencode(params) if params else ""), None, calls)


def account_summaries(calls=None):
    out, token = [], None
    for _ in range(10):
        p = {"pageSize": 200}
        if token:
            p["pageToken"] = token
        r = get(f"{ADMIN}/accountSummaries", p, calls)
        out += r.get("accountSummaries") or []
        token = r.get("nextPageToken")
        if not token:
            break
    return out


def web_streams(prop, calls=None):
    r = get(f"{ADMIN}/{prop}/dataStreams", {"pageSize": 50}, calls)
    return [s for s in r.get("dataStreams") or [] if s.get("type") == "WEB_DATA_STREAM"]


def report(prop, body, calls=None):
    """-> list of row dicts {dimension name: value, metric name: number}."""
    r = _call("POST", f"{DATA}/{prop}:runReport", body, calls)
    dims = [h["name"] for h in r.get("dimensionHeaders") or []]
    mets = [h["name"] for h in r.get("metricHeaders") or []]
    rows = []
    for row in r.get("rows") or []:
        d = {n: v.get("value") for n, v in zip(dims, row.get("dimensionValues") or [])}
        for n, v in zip(mets, row.get("metricValues") or []):
            try:
                d[n] = float(v.get("value")) if "." in str(v.get("value")) else int(v.get("value") or 0)
            except (TypeError, ValueError):
                d[n] = None
        rows.append(d)
    return rows
