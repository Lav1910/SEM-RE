"""Bing Webmaster Tools sync (change 68): clicks, impressions, top queries and pages, and crawl/index health.

The site list is read at most every 20 hours. Each client's site is matched by domain (verified first, then https).
Per client: 4 reads (traffic by day, queries, pages, crawl stats). Bing reports queries and pages in weekly buckets,
so the 30-day tables add up the buckets that fall in the period; positions are impression-weighted averages.
"""
import datetime
import json

from aeo import kv
from . import data
from .core import now, root_domain, today
from .sources import bing

SITES_MAX_AGE = 20 * 3600


def sites(calls, force=False):
    cache = kv.get("cc_bing_sites") or {}
    if not force and cache.get("at") and now() - cache["at"] < SITES_MAX_AGE:
        return cache.get("sites") or []
    rows = [{"url": s.get("Url"), "verified": bool(s.get("IsVerified"))} for s in (bing.get("GetUserSites", calls=calls) or []) if s.get("Url")]
    kv.update("cc_bing_sites", lambda _o: {"at": now(), "sites": rows}, {})
    return rows


def match(domain, rows):
    dom = root_domain(domain)
    cands = [r for r in rows if dom and root_domain(r["url"]) == dom]
    cands.sort(key=lambda r: (not r["verified"], not r["url"].startswith("https://"), "www." in r["url"]))
    return (cands[0] if cands else None), cands[1:]


def _sum_buckets(rows, start, key_field):
    agg = {}
    for r in rows or []:
        d = bing.date_of(r.get("Date"))
        if not d or d < start:
            continue
        k = r.get(key_field)
        a = agg.setdefault(k, {"key": k, "clicks": 0, "impressions": 0, "_pos": 0.0})
        imp = int(r.get("Impressions") or 0)
        a["clicks"] += int(r.get("Clicks") or 0)
        a["impressions"] += imp
        pos = r.get("AvgImpressionPosition")
        if isinstance(pos, (int, float)) and pos > 0:
            a["_pos"] += pos * imp
    out = []
    for a in agg.values():
        pos_sum = a.pop("_pos")
        a["position"] = round(pos_sum / a["impressions"], 1) if a["impressions"] and pos_sum else None
        out.append(a)
    out.sort(key=lambda a: (-a["clicks"], -a["impressions"]))
    return out


def sync(c, calls):
    cid = c["id"]
    if not bing.connected():
        data.set_source(cid, "bing", "not_connected", "Bing Webmaster API key not set on the server")
        return "not configured"
    if not root_domain(c.get("domain")):
        data.set_source(cid, "bing", "not_connected", "No website set for this client")
        return "no domain"
    site, others = match(c.get("domain"), sites(calls))
    if not site:
        data.set_source(cid, "bing", "not_connected", f"{root_domain(c.get('domain'))} isn't in the connected Bing Webmaster account")
        data.set_metric(cid, "bing", "summary", "latest", text=None)
        return "no site"
    url = site["url"]
    t = today()
    traffic = sorted(({"date": bing.date_of(r.get("Date")), "clicks": int(r.get("Clicks") or 0), "impressions": int(r.get("Impressions") or 0)}
                      for r in bing.get("GetRankAndTrafficStats", {"siteUrl": url}, calls) or []), key=lambda r: r["date"] or datetime.date.min)
    traffic = [r for r in traffic if r["date"] and r["date"] >= t - datetime.timedelta(days=90)]
    start30, start60 = t - datetime.timedelta(days=30), t - datetime.timedelta(days=60)
    cur = [r for r in traffic if r["date"] >= start30]
    prev = [r for r in traffic if start60 <= r["date"] < start30]
    queries = _sum_buckets(bing.get("GetQueryStats", {"siteUrl": url}, calls), start30, "Query")
    pages = _sum_buckets(bing.get("GetPageStats", {"siteUrl": url}, calls), start30, "Query")  # Bing puts the page URL in "Query"
    crawl = sorted(bing.get("GetCrawlStats", {"siteUrl": url}, calls) or [], key=lambda r: bing.date_of(r.get("Date")) or datetime.date.min)
    last = crawl[-1] if crawl else {}
    recent = [r for r in crawl if (bing.date_of(r.get("Date")) or datetime.date.min) >= start30]
    summary = {
        "site": url, "verified": site["verified"], "others": [o["url"] for o in others],
        "d30": {"clicks": sum(r["clicks"] for r in cur), "impressions": sum(r["impressions"] for r in cur)} if cur else {"clicks": None, "impressions": None},
        "prev": {"clicks": sum(r["clicks"] for r in prev), "impressions": sum(r["impressions"] for r in prev)} if len(prev) >= 25 else {"clicks": None, "impressions": None},
        "daily": [{"date": r["date"].isoformat(), "clicks": r["clicks"], "impressions": r["impressions"]} for r in cur],  # same days as the totals
        "queries": queries[:15], "pages": pages[:12],
        "crawl": {"date": (bing.date_of(last.get("Date")) or t).isoformat() if last else None, "in_index": last.get("InIndex"),
                  "crawled": sum(int(r.get("CrawledPages") or 0) for r in recent) if recent else None,
                  "errors": sum(int(r.get("CrawlErrors") or 0) for r in recent) if recent else None,
                  "blocked": last.get("BlockedByRobotsTxt"), "code4xx": sum(int(r.get("Code4xx") or 0) for r in recent) if recent else None,
                  "code5xx": sum(int(r.get("Code5xx") or 0) for r in recent) if recent else None},
    }
    data.set_metric(cid, "bing", "summary", "latest", text=json.dumps(summary))
    if not site["verified"]:
        data.set_source(cid, "bing", "partial", f"{url} is in Bing Webmaster but not verified", ok=True)
    else:
        data.set_source(cid, "bing", "ok", f"{url} via Bing Webmaster Tools", ok=True)
    return f"{url}: {summary['d30']['clicks']} clicks / 30 days"
