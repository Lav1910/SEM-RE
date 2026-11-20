"""Search Console for the Command Center (change 65), read through DataWise's stored copy.

DataWise already holds a Google connection to the Search Console properties (datawise_search_console reads its own
stored data, so there's no DataForSEO cost). Read-only: we list the properties, pick the one for the client's domain
and read its overview. Turning a property's syncing on is done in DataWise itself, never from here.

Which property, best first: switched on in DataWise and synced, then a Domain property (sc-domain:, covers every
subdomain and protocol) over a URL-prefix one, then the most recently synced.
"""
import datetime
import json
import re
import urllib.parse

from ..core import root_domain

TOOL = "datawise_search_console"
DATAWISE_STALE_DAYS = 4  # Google's own data runs 2-3 days behind; more than this means DataWise stopped syncing


def host_of(site_url):
    u = site_url or ""
    if u.startswith("sc-domain:"):
        return root_domain(u[len("sc-domain:"):])
    return root_domain(u)


def match(props, domain):
    """-> (best switched-on property or None, every property for this domain)."""
    dom = root_domain(domain)
    cands = [p for p in props or [] if dom and host_of(p.get("site_url")) == dom]
    on = [p for p in cands if p.get("is_enabled") and p.get("last_synced_at")]
    on.sort(key=lambda p: (not (p.get("site_url") or "").startswith("sc-domain:"), _neg_time(p.get("last_synced_at"))))
    return (on[0] if on else None), cands


def _neg_time(s):
    try:
        return -datetime.datetime.fromisoformat(str(s)).timestamp()
    except (TypeError, ValueError):
        return 0


def console_url(site_url):
    return "https://search.google.com/search-console/performance/search-analytics?resource_id=" + urllib.parse.quote(site_url, safe="")


def page_path(url):
    """Readable page label: the path without tracking parameters (GBP links carry utm_* tags)."""
    p = urllib.parse.urlsplit(url or "")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith("utm_")]
    path = (p.path or "/") + ("?" + urllib.parse.urlencode(q) if q else "")
    tagged = len(q) != len(urllib.parse.parse_qsl(p.query))
    return path, tagged


def _num(x):
    return x if isinstance(x, (int, float)) else None


def summarize(resp, site_url):
    """Keep what the screens need from DataWise's overview (detailed). Missing values stay None, never 0."""
    ov = (resp or {}).get("overview") or {}
    rng = ov.get("range") or {}
    summ = ov.get("summary") or {}
    qs = ov.get("query_summary") or {}
    daily = [{"date": d.get("date"), "clicks": _num(d.get("clicks")), "impressions": _num(d.get("impressions"))}
             for d in (rng.get("daily") or ov.get("daily_trend") or []) if d.get("date")]

    def tot(k):
        s = summ.get(k) or {}
        c, i = _num(s.get("total_clicks")), _num(s.get("total_impressions"))
        return {"clicks": c, "impressions": i, "position": _num(s.get("avg_position")),
                "ctr": (c / i) if c is not None and i else None}

    pages = []
    for p in (ov.get("top_pages") or [])[:12]:
        path, tagged = page_path(p.get("page"))
        pages.append({"url": p.get("page"), "path": path, "tagged": tagged, "clicks": _num(p.get("clicks")),
                      "impressions": _num(p.get("impressions")), "position": _num(p.get("avg_position"))})
    queries = [{"query": q.get("query"), "clicks": _num(q.get("clicks")), "impressions": _num(q.get("impressions")),
                "position": _num(q.get("avg_position")), "ctr": _num(q.get("avg_ctr"))}
               for q in (ov.get("top_queries") or [])[:15] if q.get("query")]
    return {
        "property": ov.get("property") or site_url, "site_url": site_url, "last_synced": ov.get("last_synced"),
        "days": _num(rng.get("days")) or 30,
        "d7": tot("last_7_days"), "d30": tot("last_30_days"), "d90": tot("last_90_days"),
        "prev": {"clicks": _num(rng.get("prev_clicks")), "impressions": _num(rng.get("prev_impressions")),
                 "position": _num(rng.get("prev_avg_position"))},
        "queries_total": _num(qs.get("total_queries")), "top3": _num(qs.get("top_3")), "top10": _num(qs.get("top_10")),
        "striking": _num(qs.get("striking_distance")),
        "daily": daily, "top_queries": queries, "top_pages": pages,
        "data_through": daily[-1]["date"] if daily else None,
    }


def stale(last_synced, today):
    try:
        d = datetime.date.fromisoformat(str(last_synced)[:10])
    except (TypeError, ValueError):
        return True
    return (today - d).days > DATAWISE_STALE_DAYS


def is_error(resp):
    return not isinstance(resp, dict) or bool(resp.get("isError")) or ("overview" not in resp and "properties" not in resp)


def error_text(resp):
    if isinstance(resp, dict):
        return re.sub(r"\s+", " ", str(resp.get("text") or resp.get("error") or json.dumps(resp)[:200]))[:200]
    return str(resp)[:200]
