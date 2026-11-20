"""ClickUp connector (change 64): READ ONLY. Only GET requests are ever sent (a test checks this).

Auth: a personal API token in CLICKUP_API_TOKEN (Vercel env var, or ~/.config/hbm-aeo/keys.env locally via
setup_keys.py). It's sent only in the Authorization header and never logged or shown.
Rate limits: ClickUp's lower plans allow about 100 requests a minute per token, so calls are spaced ~0.65 s apart, and a
429 waits for the reset time it names. 5xx and network errors retry with backoff (4 tries).
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from aeo import config
from ..core import clean_name

API = "https://api.clickup.com/api/v2"
MIN_GAP = 0.65
_gap_lock = threading.Lock()
_last = [0.0]


class NotConnected(Exception):
    pass


class ClickUpError(Exception):
    pass


def token():
    return config.key("CLICKUP_API_TOKEN")


def connected():
    return bool(token())


def _open(req, timeout):  # separate so tests can check what would be sent
    return urllib.request.urlopen(req, timeout=timeout)


def get(path, params=None, calls=None):
    tok = token()
    if not tok:
        raise NotConnected("ClickUp token not set")
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    for attempt in range(4):
        with _gap_lock:
            wait = _last[0] + MIN_GAP - time.time()
            if wait > 0:
                time.sleep(wait)
            _last[0] = time.time()
        req = urllib.request.Request(url, method="GET", headers={"Authorization": tok, "Accept": "application/json",
                                                                  "User-Agent": "hbm-command-center/1"})
        try:
            with _open(req, 30) as r:
                if calls is not None:
                    calls[0] += 1
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            if calls is not None:
                calls[0] += 1
            if e.code == 429:
                reset = e.headers.get("X-RateLimit-Reset")
                try:
                    time.sleep(min(60, max(1, int(reset) - time.time())) if reset else 5 * 2 ** attempt)
                except ValueError:
                    time.sleep(5 * 2 ** attempt)
                continue
            if e.code >= 500:
                time.sleep(2 ** attempt)
                continue
            if e.code in (401, 403):
                raise ClickUpError(f"ClickUp refused the token (HTTP {e.code})")
            raise ClickUpError(f"ClickUp answered HTTP {e.code} for {path.split('?')[0]}")
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(2 ** attempt)
    raise ClickUpError("ClickUp didn't respond after 4 tries")


# ---------------------------------------------------------------- reading the workspace
def team_id(calls=None):
    teams = get("/team", calls=calls).get("teams") or []
    if not teams:
        raise ClickUpError("The token can't see any ClickUp workspace")
    return teams[0]["id"]


PACKAGE_FROM_SPACE = [("essentials", "Essentials"), ("growth", "Growth"), ("scale", "Scale"), ("organic", "Organic"),
                      ("consult", "Consulting")]
SKIP_SPACES = ("template", "wiki", "tasks", "ads only", "social media")


def package_guess(space_name):
    s = (space_name or "").lower()
    return next((p for k, p in PACKAGE_FROM_SPACE if k in s), "")


def folders_for_import(calls=None):
    """Every client folder in the workspace, with the package guessed from its space. Template/wiki spaces are left out."""
    tid = team_id(calls)
    out = []
    for sp in get(f"/team/{tid}/space", {"archived": "false"}, calls).get("spaces") or []:
        if any(k in (sp.get("name") or "").lower() for k in SKIP_SPACES):
            continue
        for fo in get(f"/space/{sp['id']}/folder", {"archived": "false"}, calls).get("folders") or []:
            raw = fo.get("name") or ""
            out.append({"space_id": sp["id"], "space": clean_name(sp.get("name")), "folder_id": fo["id"], "name": clean_name(raw),
                        "hidden_chars": raw != clean_name(raw) and raw.strip() != clean_name(raw), "package": package_guess(sp.get("name")),
                        "template": "template" in raw.lower()})
    return out


def folder_tasks(folder_id, calls=None, max_pages=10):
    tasks = []
    for li in get(f"/folder/{folder_id}/list", {"archived": "false"}, calls).get("lists") or []:
        for page in range(max_pages):
            data = get(f"/list/{li['id']}/task", {"include_closed": "true", "subtasks": "true", "page": page}, calls)
            batch = data.get("tasks") or []
            for t in batch:
                t["_list"] = li.get("name") or ""
            tasks += batch
            if data.get("last_page", True) or len(batch) < 100:
                break
    return tasks


def last_comment_at(task_id, calls=None):
    cs = get(f"/task/{task_id}/comment", calls=calls).get("comments") or []
    ds = [int(c["date"]) / 1000 for c in cs if str(c.get("date", "")).isdigit()]
    return max(ds) if ds else None


# ---------------------------------------------------------------- understanding tasks
GBP = r"(gbp|gmb|google business( profile)?|business profile)"
ITEM_PATTERNS = [  # tuned on HBM's real task names (Nov 2026): "Weekly posts (GMB)", "Bi-weekly GBP posting", "Manage & respond to reviews"
    ("review_reply", re.compile(r"review\s*(repl|respon)|(respond|reply|replies|responding|replying)\w*\s+(to\s+)?(\w+\s+)?reviews", re.I)),
    ("gbp_photo", re.compile(rf"\b{GBP}\b.*\bphotos?\b|\bphotos?\b.*\b{GBP}\b|\bupload\w*\s+(to|on)\s+{GBP}\b", re.I)),
    ("gbp_post", re.compile(rf"\b{GBP}\b.*\bpost(s|ing)?\b|\bpost(s|ing)?\b.*\b{GBP}\b|\b(bi-?)?weekly\s+{GBP}\b|\bgoogle\s+posts?\b", re.I)),
    ("report", re.compile(r"\breport(s|ing)?\b", re.I)),
    ("blog", re.compile(r"\bblogs?\b|\barticles?\b", re.I)),
]


def classify(name, list_name=""):
    """Which owed deliverable a task is (or None). The task name wins over the list name."""
    for text in (name or "", list_name or ""):
        for item, rx in ITEM_PATTERNS:
            if rx.search(text):
                return item
    return None


def map_status(status, status_type=None):
    s = (status or "").lower().strip()
    if status_type == "closed" or s in ("complete", "completed", "done", "closed"):
        return "published"
    if "approved" in s:
        return "approved"
    if "approv" in s or "client review" in s:
        return "awaiting_approval"
    if any(k in s for k in ("publish", "posted", "sent", "live", "scheduled")):
        return "published"
    if "review" in s or s == "qa":
        return "in_review"
    if any(k in s for k in ("draft", "progress", "writing", "doing", "working")):
        return "drafted"
    return "planned"


def is_hold(status):
    s = (status or "").lower()
    return "hold" in s or "paused" in s


def recurring_of(t):
    """ClickUp's task list doesn't reliably say whether a task repeats. Use it if present; otherwise None (unknown),
    and coverage falls back to "an open task due this month or later"."""
    for k in ("recurring", "is_recurring"):
        if k in t and t[k] is not None:
            return 1 if t[k] else 0
    return None


URL_RX = re.compile(r"https?://[^\s<>\"')\]]+")


def live_url_of(t, domain=""):
    """A live URL from a URL-type custom field, or the first link to the client's own site in the description."""
    for f in t.get("custom_fields") or []:
        v = f.get("value")
        if isinstance(v, str) and v.startswith("http") and any(k in (f.get("name") or "").lower() for k in ("url", "link", "live")):
            return v.strip()
    text = (t.get("text_content") or t.get("description") or "")
    for u in URL_RX.findall(text):
        if domain and domain in u.lower() and "wp-admin" not in u and "preview" not in u:
            return u.rstrip(".,;")
    return None


def ms(v):
    try:
        return int(v) / 1000 if v not in (None, "") else None
    except (TypeError, ValueError):
        return None
