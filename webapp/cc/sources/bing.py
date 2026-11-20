"""Bing Webmaster Tools connector (change 68): READ ONLY.

Auth: one API key for the Bing Webmaster account that holds the client sites (BING_WEBMASTER_API_KEY, entered by a
person with setup_keys.py or as a server setting). Bing's key can also submit URLs and change settings, so read-only is
enforced here: only the GET methods named in READS are ever called (a test checks this), and the key, which Bing takes
as a URL parameter, never appears in an error message or log.
"""
import datetime
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from aeo import config

API = "https://ssl.bing.com/webmaster/api.svc/json/"
READS = {"GetUserSites", "GetRankAndTrafficStats", "GetQueryStats", "GetPageStats", "GetCrawlStats"}


class NotConnected(Exception):
    pass


class BingError(Exception):
    pass


def key():
    return config.key("BING_WEBMASTER_API_KEY")


def connected():
    return bool(key())


def _open(req, timeout):  # separate so tests can check every request
    return urllib.request.urlopen(req, timeout=timeout)


def get(method, params=None, calls=None):
    if method not in READS:
        raise BingError(f"Refused to call {method}: only reads are allowed")
    k = key()
    if not k:
        raise NotConnected("Bing Webmaster API key not set on the server")
    url = API + method + "?" + urllib.parse.urlencode({**(params or {}), "apikey": k})
    for attempt in range(4):
        req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json", "User-Agent": "hbm-command-center/1"})
        try:
            with _open(req, 45) as r:
                if calls is not None:
                    calls[0] += 1
                body = json.loads(r.read().decode("utf-8", "replace") or "{}")
                return body.get("d", body) if isinstance(body, dict) else body
        except urllib.error.HTTPError as e:
            if calls is not None:
                calls[0] += 1
            try:
                msg = str(json.loads(e.read().decode() or "{}").get("Message") or "")[:200]
            except ValueError:
                msg = ""
            msg = msg.replace(k, "•••")
            if e.code in (401, 403) or "InvalidApiKey" in msg or "key" in msg.lower() and "invalid" in msg.lower():
                raise BingError("Bing refused the API key (check it in Bing Webmaster Tools → Settings → API access)")
            if e.code == 429 or "ThrottleUser" in msg:
                if attempt < 2:
                    time.sleep(5 * 2 ** attempt)
                    continue
                raise BingError("Bing's API limit was reached for now; it tries again on the next sync")
            if e.code >= 500 and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise BingError(f"Bing answered HTTP {e.code} for {method}{': ' + msg if msg else ''}")
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise BingError("Couldn't reach Bing Webmaster Tools")
            time.sleep(2 ** attempt)
    raise BingError("Bing kept failing")


_DATE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")


def date_of(v):
    """Bing's "/Date(1700000000000-0700)/" -> a date (the day it reports for, taken as UTC)."""
    m = _DATE.search(str(v or ""))
    if not m:
        return None
    return datetime.datetime.utcfromtimestamp(int(m.group(1)) / 1000).date()
