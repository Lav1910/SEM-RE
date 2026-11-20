"""Google Analytics sync (change 67): which GA4 property is whose, and what it recorded. Read-only (sources/ga4.py).

Per client, each daily run (7 small report queries):
  this 30 days / previous 30 days / same 30 days last year, by channel  -> organic sessions, engaged sessions, key events
  90 days by day and channel                                           -> the daily chart and the "no data" check
  top organic landing pages, AI-assistant referrals, key events by name (this 30 days)
Periods end yesterday (today is never complete). Key events are GA4's own flags: they're not confirmed leads.
"""
import datetime
import json
import re

from aeo import kv
from webapp import db
from . import data
from .core import now, root_domain, today
from .sources import ga4

DIR_MAX_AGE = 20 * 3600
ORGANIC = "Organic Search"
# AI assistants seen as GA4 session sources. A documented list, not a guess: copied links with no referrer land in Direct.
AI_SOURCES = ["chatgpt.com", "chat.openai.com", "openai.com", "perplexity.ai", "gemini.google.com", "bard.google.com",
              "copilot.microsoft.com", "copilot.cloud.microsoft", "claude.ai", "you.com", "poe.com", "meta.ai", "deepseek.com",
              "chat.deepseek.com", "grok.com", "chat.mistral.ai"]
AI_REGEX = "(.*\\.)?(" + "|".join(re.escape(s) for s in AI_SOURCES) + ")"
_SUFFIX = re.compile(r"\b(llc|inc|ltd|co|corp|company|the|ga4|website|site)\b")


def _norm(name):
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")))).strip()


# ---------------------------------------------------------------- directory
def refresh_directory(calls, force=False):
    if not force and now() - float(kv.get("cc_ga4_dir_at", 0) or 0) < DIR_MAX_AGE:
        return None
    from concurrent.futures import ThreadPoolExecutor
    t, seen = now(), set()
    props = [(acc, ps) for acc in ga4.account_summaries(calls) for ps in acc.get("propertySummaries") or [] if ps.get("property")]

    def streams_of(prop):  # an agency account can see 100+ properties: read their websites 8 at a time, not one by one
        try:
            return ga4.web_streams(prop, calls)
        except (ga4.GA4Error, ga4.QuotaRefused):
            return []
    with ThreadPoolExecutor(max_workers=8) as pool:
        all_streams = list(pool.map(streams_of, [ps["property"] for _, ps in props]))
    for (acc, ps), streams in zip(props, all_streams):
        prop = ps["property"]
        seen.add(prop)
        web = (streams[0].get("webStreamData") or {}) if streams else {}
        db.upsert("cc_ga4_properties", {"name": prop, "display_name": ps.get("displayName"), "account": acc.get("account"),
                                        "account_name": acc.get("displayName"), "website": web.get("defaultUri"),
                                        "measurement_id": web.get("measurementId"), "seen_at": t}, ["name"])
    for r in db.q("SELECT name FROM cc_ga4_properties"):
        if r["name"] not in seen:
            db.x("DELETE FROM cc_ga4_properties WHERE name=?", (r["name"],))
    kv.update("cc_ga4_dir_at", lambda _o: t, 0)
    return len(seen)


def directory():
    return [data.d(r) for r in db.q("SELECT * FROM cc_ga4_properties ORDER BY display_name")]


def match(c, rows):
    """-> (property row or None, how, other candidates). Website of the web stream first, then the property's name."""
    if c.get("ga4_how") == "none":
        return None, "none", []
    if c.get("ga4_how") == "manual" and c.get("ga4_property"):
        return next((r for r in rows if r["name"] == c["ga4_property"]), None), "manual", []
    dom = root_domain(c.get("domain"))
    cands = [r for r in rows if dom and root_domain(r.get("website")) == dom]
    if not cands and dom:
        cands = [r for r in rows if dom in (r.get("display_name") or "").lower()]
    if not cands:
        cands = [r for r in rows if _norm(r.get("display_name")) and _norm(r.get("display_name")) == _norm(c.get("name"))]
    if not cands:
        return None, "auto", []
    cands.sort(key=lambda r: (r["name"] != c.get("ga4_property"), r.get("display_name") or ""))
    return cands[0], "auto", cands[1:]


# ---------------------------------------------------------------- per-client sync
def periods(t):
    end = t - datetime.timedelta(days=1)
    cur = (end - datetime.timedelta(days=29), end)
    prev = (cur[0] - datetime.timedelta(days=30), cur[0] - datetime.timedelta(days=1))
    try:
        ly = (cur[0].replace(year=cur[0].year - 1), end.replace(year=end.year - 1))
    except ValueError:  # 29 February
        ly = (cur[0] - datetime.timedelta(days=365), end - datetime.timedelta(days=365))
    return {"cur": cur, "prev": prev, "ly": ly}


def _range(p):
    return [{"startDate": p[0].isoformat(), "endDate": p[1].isoformat()}]


def _organic():
    return {"filter": {"fieldName": "sessionDefaultChannelGroup", "stringFilter": {"matchType": "EXACT", "value": ORGANIC}}}


def _channels(prop, p, calls):
    rows = ga4.report(prop, {"dateRanges": _range(p), "dimensions": [{"name": "sessionDefaultChannelGroup"}],
                             "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "keyEvents"}, {"name": "totalUsers"}],
                             "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": 25}, calls)
    org = next((r for r in rows if r.get("sessionDefaultChannelGroup") == ORGANIC), None)
    return rows, {"sessions": (org or {}).get("sessions", 0), "engaged": (org or {}).get("engagedSessions", 0),
                  "key_events": (org or {}).get("keyEvents", 0), "users": (org or {}).get("totalUsers", 0),
                  "all_sessions": sum(r.get("sessions") or 0 for r in rows), "all_key_events": sum(r.get("keyEvents") or 0 for r in rows)}


def sync(c, calls):
    cid = c["id"]
    if c.get("ga4_how") == "none":
        data.set_source(cid, "ga4", "ok", "No Analytics property in this client's scope (set on Setup)", ok=True)
        return "no property in scope"
    if not ga4.configured():
        data.set_source(cid, "ga4", "not_connected", "Google sign-in key not set on the server")
        return "not configured"
    if not ga4.connected():
        st = ga4.stored()
        data.set_source(cid, "ga4", "error" if st else "not_connected",
                        (st or {}).get("detail") or "Analytics isn't connected yet (Sync log → Connect Analytics)")
        return "not connected"
    refresh_directory(calls)
    prop, how, others = match(c, directory())
    if not prop:
        why = ("The property picked on Setup is no longer visible to the connected Google account" if how == "manual"
               else f"No Analytics property for {c.get('domain') or c['name']} in the connected Google account")
        data.set_source(cid, "ga4", "not_connected", why)
        data.set_metric(cid, "ga4", "summary", "latest", text=None)
        return "no property"
    if prop["name"] != c.get("ga4_property"):
        data.log_change(cid, "client", cid, "ga4_property", c.get("ga4_property"), prop["name"], "sync (matched by website)")
        db.x("UPDATE cc_clients SET ga4_property=? WHERE id=?", (prop["name"], cid))

    P = periods(today())
    name = prop["name"]
    ch_cur, cur = _channels(name, P["cur"], calls)
    _, prev = _channels(name, P["prev"], calls)
    _, ly = _channels(name, P["ly"], calls)
    start90 = P["cur"][1] - datetime.timedelta(days=89)
    daily_rows = ga4.report(name, {"dateRanges": [{"startDate": start90.isoformat(), "endDate": P["cur"][1].isoformat()}],
                                   "dimensions": [{"name": "date"}, {"name": "sessionDefaultChannelGroup"}],
                                   "metrics": [{"name": "sessions"}], "limit": 5000}, calls)
    by_day = {}
    for r in daily_rows:
        d = r.get("date") or ""
        iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        row = by_day.setdefault(iso, {"date": iso, "organic": 0, "all": 0})
        row["all"] += r.get("sessions") or 0
        if r.get("sessionDefaultChannelGroup") == ORGANIC:
            row["organic"] += r.get("sessions") or 0
    day = start90
    daily = []
    while day <= P["cur"][1]:  # days with no rows were zero sessions (GA4 leaves them out)
        daily.append(by_day.get(day.isoformat(), {"date": day.isoformat(), "organic": 0, "all": 0}))
        day += datetime.timedelta(days=1)
    pages = ga4.report(name, {"dateRanges": _range(P["cur"]), "dimensions": [{"name": "landingPagePlusQueryString"}],
                              "metrics": [{"name": "sessions"}, {"name": "engagedSessions"}, {"name": "keyEvents"}],
                              "dimensionFilter": _organic(), "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": 12}, calls)
    ai = ga4.report(name, {"dateRanges": _range(P["cur"]), "dimensions": [{"name": "sessionSource"}],
                           "metrics": [{"name": "sessions"}, {"name": "keyEvents"}],
                           "dimensionFilter": {"filter": {"fieldName": "sessionSource",
                                                          "stringFilter": {"matchType": "FULL_REGEXP", "value": AI_REGEX, "caseSensitive": False}}},
                           "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}], "limit": 20}, calls)
    kev = ga4.report(name, {"dateRanges": _range(P["cur"]), "dimensions": [{"name": "eventName"}], "metrics": [{"name": "keyEvents"}],
                            "metricFilter": {"filter": {"fieldName": "keyEvents", "numericFilter": {"operation": "GREATER_THAN", "value": {"int64Value": "0"}}}},
                            "orderBys": [{"metric": {"metricName": "keyEvents"}, "desc": True}], "limit": 10}, calls)
    summary = {
        "property": name, "display_name": prop.get("display_name"), "account": prop.get("account_name"), "website": prop.get("website"),
        "measurement_id": prop.get("measurement_id"), "how": how, "others": [o.get("display_name") for o in others],
        "periods": {k: [v[0].isoformat(), v[1].isoformat()] for k, v in P.items()},
        "cur": cur, "prev": prev, "ly": ly,
        "channels": [{"channel": r.get("sessionDefaultChannelGroup"), "sessions": r.get("sessions"), "key_events": r.get("keyEvents")} for r in ch_cur],
        "daily": daily, "last7_all": sum(d["all"] for d in daily[-7:]),
        "pages": [{"page": r.get("landingPagePlusQueryString"), "sessions": r.get("sessions"), "engaged": r.get("engagedSessions"),
                   "key_events": r.get("keyEvents")} for r in pages],
        "ai": [{"source": r.get("sessionSource"), "sessions": r.get("sessions"), "key_events": r.get("keyEvents")} for r in ai],
        "ai_sessions": sum(r.get("sessions") or 0 for r in ai),
        "key_events": [{"event": r.get("eventName"), "count": r.get("keyEvents")} for r in kev],
    }
    data.set_metric(cid, "ga4", "summary", "latest", text=json.dumps(summary))
    data.set_metric(cid, "ga4", "organic_30d", "latest", value=cur["sessions"])
    if others:
        data.set_source(cid, "ga4", "partial", f"{len(others) + 1} properties match this website; using {prop.get('display_name')}. Pick one on Setup.", ok=True)
    else:
        data.set_source(cid, "ga4", "ok", f"{prop.get('display_name')} · {prop.get('account_name') or ''}".strip(" ·"), ok=True)
    return f"{prop.get('display_name')}: {cur['sessions']} organic sessions / 30 days"
