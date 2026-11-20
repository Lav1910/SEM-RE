"""Command Center (change 64): shared definitions, access rule and date helpers.

Access: the feature flag HBM_COMMAND_CENTER=1 must be on, and the person must be an active admin who is either listed in
COMMAND_CENTER_EMAILS or was granted access (users.cc_access). Everyone else gets "not found", on every page and action.
Dates: one business clock, UK time (Europe/London); working days are Monday to Friday (no holidays in v1).
"""
import datetime
import os
import re
import time

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/London")
except Exception:  # no time-zone data on the host: fall back to UTC (at most an hour off)
    TZ = datetime.timezone.utc

# What a package can promise each month. Quantities are per month; the report is 0 or 1.
ITEMS = [("blog", "Blogs"), ("gbp_post", "GBP posts"), ("gbp_photo", "GBP photos & videos"), ("review_reply", "Review replies"),
         ("report", "Monthly report")]
ITEM_LABEL = dict(ITEMS)
ITEM_SHORT = {"blog": "Blogs", "gbp_post": "GBP", "gbp_photo": "Photos", "review_reply": "Reviews", "report": "Report"}
GRID_ITEMS = ("blog", "gbp_post", "report")  # the portfolio bars and the 12-month heatmap

STATUSES = [("planned", "Planned"), ("drafted", "Drafted"), ("in_review", "In internal review"),
            ("awaiting_approval", "Awaiting approval"), ("approved", "Approved"), ("published", "Published / sent"),
            ("verified", "Verified live")]
STATUS_LABEL = dict(STATUSES)

PACKAGES = ["Essentials", "Growth", "Scale", "Organic Lite", "Organic Max", "Blog only", "Consulting", "Internal", "Other"]
EVERY = -1  # change 72: a scope of "every new review" (review replies follow a rule, not a monthly count)
# reply to every new review within this many business days (scope ledger 2026-11-19): 1 on Scale and Organic Max, else 2
REPLY_DAYS = {"Scale": 1, "Organic Max": 1, "Internal": 1}  # HBM itself gets the top package's rule
REPLY_DAYS_DEFAULT = 2


def reply_days(package):
    return REPLY_DAYS.get(package or "", REPLY_DAYS_DEFAULT)
CLIENT_STATUSES = ["active", "paused", "churned"]
ROLES = [("am", "Account manager"), ("seo", "SEO owner"), ("writer", "Writer"), ("dev", "Developer")]

# Data sources. Phase 2/3 sources show as "not connected" (amber) until they're built and set up.
SOURCES = [("clickup", "ClickUp", 1), ("wordpress", "WordPress / sitemap", 1), ("gsc", "Search Console", 1),
           ("gbp", "Google Business Profile", 1), ("ga4", "Google Analytics", 1),
           ("bing", "Bing Webmaster Tools", 1)]
SOURCE_LABEL = {k: v for k, v, _ in SOURCES}
SYNC_SOURCES = ("clickup", "wordpress", "gsc", "gbp", "ga4", "bing")  # the ones that have a connector (Search Console reads DataWise's copy)
STALE_HOURS = 48
GBP_LIVE = ("ok", "partial")  # the profile is connected for this client, so Google's view decides GBP deliverables


def gbp_live(sources):
    s = (sources or {}).get("gbp") or {}
    return s.get("state") in GBP_LIVE and "No Business Profile in this client's scope" not in (s.get("detail") or "")


TRACK_FROM_DEFAULT = "2026-11"  # change 71: the Command Center judges delivery from November 2026 on


def track_from():
    """First month the Command Center counts as owed (YYYY-MM). Settings store, then CC_TRACK_FROM, then the default."""
    v = ""
    try:
        from aeo import kv
        v = kv.get("cc_track_from", "") or ""
    except Exception:  # noqa: BLE001
        pass
    v = v or os.environ.get("CC_TRACK_FROM", "") or TRACK_FROM_DEFAULT
    return v if re.fullmatch(r"\d{4}-\d{2}", v) else TRACK_FROM_DEFAULT


def enabled():
    return os.environ.get("HBM_COMMAND_CENTER", "").strip() == "1"


def access_emails():
    return {e.strip().lower() for e in os.environ.get("COMMAND_CENTER_EMAILS", "").split(",") if e.strip()}


def can_edit(user):
    """Owners (change 78): the emails in COMMAND_CENTER_EMAILS, a server setting only the account owner can change.
    Everyone else who can open the panel (granted on the People page) can see it but not change it."""
    return can_open(user) and user["email"].lower() in access_emails()


def can_open(user):
    if not enabled() or not user:
        return False
    from webapp import auth
    if not auth.can(user, "admin"):
        return False
    keys = user.keys() if hasattr(user, "keys") else user
    granted = bool(user["cc_access"]) if "cc_access" in keys else False
    return user["email"].lower() in access_emails() or granted


# ---------------------------------------------------------------- dates
def now():
    return time.time()


def today():
    return datetime.datetime.now(TZ).date()


def ts_date(ts):
    return datetime.datetime.fromtimestamp(ts, TZ).date() if ts else None


def month_of(d):
    return f"{d.year:04d}-{d.month:02d}"


def add_months(month, n):
    y, m = int(month[:4]), int(month[5:7])
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return f"{y:04d}-{m:02d}"


def month_start(month):
    return datetime.date(int(month[:4]), int(month[5:7]), 1)


def month_end(month):
    return month_start(add_months(month, 1)) - datetime.timedelta(days=1)


def month_label(month, year=False):
    d = month_start(month)
    return d.strftime("%b %Y") if year else d.strftime("%b")


def working_days_between(a, b):
    """Monday-to-Friday days after `a`, up to and including `b` (0 when b <= a)."""
    if not a or not b or b <= a:
        return 0
    days, d = 0, a
    while d < b:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def parse_date(s):
    try:
        return datetime.date.fromisoformat((s or "").strip()[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------- names
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")  # zero-width, direction marks, BOM, soft hyphen


def clean_name(s):
    """ClickUp folder names carry hidden direction marks (e.g. 'JEP‎ Contracting'); drop them and tidy spaces."""
    return re.sub(r"\s+", " ", _INVISIBLE.sub("", s or "")).strip()


def root_domain(s):
    s = (s or "").strip().lower()
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split("?")[0].split("#")[0]
    s = s.split("@")[-1].split(":")[0]
    return s[4:] if s.startswith("www.") else s


def same_url(a, b):
    def norm(u):
        u = (u or "").strip().lower().split("#")[0]
        u = re.sub(r"^https?://(www\.)?", "", u)
        return u.rstrip("/")
    return bool(a) and norm(a) == norm(b)
