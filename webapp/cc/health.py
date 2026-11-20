"""Health flags and colour per client (change 64). Pure functions over data already in the database.

A blank is a red flag, not neutral: missing scope, owners, sources or verification never come out green.
Every flag carries a reason, an owner (role) and, once stored, a first-seen date that drives escalation:
2 working days -> the owner, 5 working days -> you.
"""
from . import ledger
from .core import ITEM_LABEL, ITEMS, SOURCES, STALE_HOURS, add_months, gbp_live, reply_days, month_label, month_of, month_start, ts_date, \
    working_days_between

RANK = {"red": 0, "amber": 1, "info": 2}
APPROVAL_DAYS = 5  # working days
CLIENT_INPUT_DAYS = 7  # calendar days


def _f(fkey, code, severity, reason, owner, evidence=None):
    return {"fkey": fkey, "code": code, "severity": severity, "reason": reason, "owner": owner, "evidence": evidence}


def awaits_approval(status):
    s = (status or "").lower()
    return "approv" in s and "approved" not in s


def awaits_client(status):
    s = (status or "").lower()
    return any(k in s for k in ("client input", "waiting on client", "awaiting client", "need info", "needs info",
                                "waiting for client", "client to send", "pending client"))


def evaluate(ctx):
    c, today, now = ctx["client"], ctx["today"], ctx["now"]
    scope, dl, tasks = ctx["scope"], ctx["deliverables"], ctx["tasks"]
    sources, metrics = ctx["sources"], ctx["metrics"]
    if c["status"] != "active":
        return []
    out = []
    cur, prev = month_of(today), add_months(month_of(today), -1)
    start = ctx.get("track_from")  # months before it aren't judged (change 71)
    src_ok = {k: (sources.get(k) or {}).get("state") in ("ok", "partial") for k, _, _ in SOURCES}
    gbp_ok = gbp_live(sources)

    # ---- setup: the things whose absence let Marcello slip
    if not c["domain"]:
        out.append(_f("domain_missing", "domain_missing", "red", "No website domain, so nothing can be verified live", "you"))
    if not c["seo_id"]:
        out.append(_f("owner_missing", "owner_missing", "red", "No SEO owner named for delivery", "you"))
    if not c["am_id"]:
        out.append(_f("am_missing", "am_missing", "amber", "No account manager named", "you"))
    # the go-live date is optional (change 76): with none recorded, owed months simply start at the tracking start
    missing = [ITEM_LABEL[i] for i, _ in ITEMS if ledger.scope_qty(scope, i, cur) is None]
    if missing:
        out.append(_f("scope_missing", "scope_missing", "red", "Scope missing: " + ", ".join(missing).lower()
                      + (" (nothing is written down)" if len(missing) == len(ITEMS) else ""), "you"))

    # ---- recurring work: without a task nothing is ever due, so nothing can be overdue
    if src_ok["clickup"]:
        mstart = month_start(cur)
        for i, _ in ITEMS:
            if (ledger.scope_qty(scope, i, cur) or 0) <= 0 or (ledger.first_owed_month(c["go_live"], start) or cur) > cur:
                continue
            covered = any(t["item"] == i and (t["recurring"] == 1 or (t["status_type"] != "closed" and t["due"] and ts_date(t["due"]) >= mstart))
                          for t in tasks)
            if not covered:
                out.append(_f(f"no_task:{i}", "no_task", "red",
                              f"No open or recurring ClickUp task for {ITEM_LABEL[i].lower()} this month, so nothing can go overdue",
                              "am" if i == "report" else "seo"))

    # ---- last month's promises
    for i, _ in ITEMS:
        x = ledger.cell(c["go_live"], scope, dl, i, prev, today, gbp_ok, start)
        if x["state"] == "short":
            run = ledger.short_run(c["go_live"], scope, dl, i, today, gbp_ok, start=start)
            also = [month_label(m) for m, _, _ in run[1:]]
            out.append(_f(f"short:{i}", "short", "red",
                          f"{ITEM_LABEL[i]}: {x['counted']} of {x['owed']} delivered in {month_label(prev)}"
                          + (f" (also short in {', '.join(also)})" if also else ""), "am" if i == "report" else "seo",
                          {"months": [m for m, _, _ in run]}))
        elif x["state"] == "unverified":
            out.append(_f(f"unverified:{i}", "unverified", "amber",
                          f"{ITEM_LABEL[i]}: {x['claimed']} of {x['owed']} marked done in {month_label(prev)} but not verified on the profile",
                          "you"))
    rep = ledger.cell(c["go_live"], scope, dl, "report", cur, today, gbp_ok, start)
    if rep["state"] == "late":
        out.append(_f("report_late", "report_late", "red", f"{month_label(cur)} report not confirmed sent by the 7th", "am"))

    # ---- ClickUp hand-offs
    if src_ok["clickup"]:
        open_t = [t for t in tasks if t["status_type"] != "closed"]
        since_start = month_start(start) if start else None
        overdue = [t for t in open_t if t["due"] and ts_date(t["due"]) < today and (not since_start or ts_date(t["due"]) >= since_start)]
        if overdue:
            out.append(_f("overdue_tasks", "overdue_tasks", "amber", f"{len(overdue)} overdue ClickUp task{'s' if len(overdue) != 1 else ''}",
                          "seo", {"tasks": [t["task_id"] for t in overdue[:50]]}))
        unassigned = [t for t in open_t if t["item"] and not (t["assignees"] or "").strip("[] ")]
        if unassigned:
            out.append(_f("unassigned", "unassigned", "amber", f"{len(unassigned)} deliverable task{'s' if len(unassigned) != 1 else ''} with no assignee",
                          "seo", {"tasks": [t["task_id"] for t in unassigned[:50]]}))
        for t in open_t:
            since = ts_date(t["date_updated"] or t["date_created"])
            if awaits_approval(t["status"]):
                wd = working_days_between(since, today)
                if wd > APPROVAL_DAYS:
                    out.append(_f(f"approval:{t['task_id']}", "approval", "amber",
                                  f"Waiting on approval, no change for {wd} working days: {t['name']}", "am", {"task": t["task_id"]}))
            elif awaits_client(t["status"]) and since and (today - since).days > CLIENT_INPUT_DAYS:
                out.append(_f(f"client_input:{t['task_id']}", "client_input", "amber",
                              f"Waiting on client input for {(today - since).days} days, no fallback recorded: {t['name']}", "am",
                              {"task": t["task_id"]}))
        closed_no_url = [d for d in dl if d["source"] == "clickup" and d["item"] == "blog" and d["status"] == "published"
                         and not d["merged_into"] and not d["verified_at"] and not d["not_ours"] and d["month"] >= max(add_months(cur, -2), start or "")]
        if closed_no_url:
            out.append(_f("closed_no_url", "closed_no_url", "amber",
                          f"{len(closed_no_url)} blog task{'s' if len(closed_no_url) != 1 else ''} closed in ClickUp with no matching live post",
                          "seo", {"deliverables": [d["id"] for d in closed_no_url]}))

    # ---- live site
    fails = metrics.get(("wordpress", "home_fails", "latest"))
    if fails and (fails["value"] or 0) >= 2:
        st = metrics.get(("wordpress", "home_status", "latest"))
        code = int(st["value"]) if st and st["value"] else 0
        out.append(_f("site_down", "site_down", "red", f"Homepage failed {int(fails['value'])} checks in a row (last: "
                      + (f"HTTP {code}" if code else "no response") + ")", "dev"))

    # ---- Business Profile (change 66): what Google shows on the profile
    if gbp_ok:
        out += gbp_flags(c, scope, metrics, today, now)

    # ---- Google Analytics (change 67): tracking that has gone quiet
    if src_ok.get("ga4"):
        out += ga4_flags(metrics)

    # ---- data sources: missing or stale data is a warning, never neutral
    for k, label, phase in SOURCES:
        s = sources.get(k) or {}
        st = s.get("state")
        if st in (None, "not_connected"):
            why = f": {s['detail']}" if s.get("detail") else (" (comes in Phase 2)" if phase > 1 else "")
            out.append(_f(f"source:{k}", "source_off", "amber", f"{label} not connected{why}", "you"))
        elif st == "error":
            out.append(_f(f"source:{k}", "source_error", "amber", f"{label} sync failing: {s.get('detail') or 'unknown error'}", "you"))
        elif st == "blocked":
            out.append(_f(f"source:{k}", "source_blocked", "amber", f"{label}: {s.get('detail') or 'the site blocked our reader'}", "dev"))
        elif st == "partial":
            out.append(_f(f"source:{k}", "source_partial", "amber", f"{label}: {s.get('detail') or 'partial data'}", "you"))
        if st in ("ok", "partial") and s.get("last_ok") and now - s["last_ok"] > STALE_HOURS * 3600:
            out.append(_f(f"stale:{k}", "stale", "amber", f"{label} data is {int((now - s['last_ok']) / 3600)} hours old", "you"))
    return out


POST_GAP_DAYS = 14     # posts owed but none live for this long


def _json_metric(metrics, m):
    import json
    row = metrics.get(("gbp", m, "latest"))
    try:
        return json.loads(row["text"]) if row and row["text"] else None
    except ValueError:
        return None


def gbp_flags(c, scope, metrics, today, now):
    out = []
    prof, rv = _json_metric(metrics, "profile"), _json_metric(metrics, "reviews")
    if not prof:
        return out
    if prof.get("verified") == 0:
        out.append(_f("gbp_unverified", "gbp_unverified", "red",
                      "Google reports the Business Profile isn't under verified owner control (unverified, pending or suspended)", "am"))
    if prof.get("open_status") and prof["open_status"] not in ("OPEN", "OPEN_FOR_BUSINESS_UNSPECIFIED"):
        out.append(_f("gbp_closed", "gbp_closed", "red", f"Business Profile is marked {prof['open_status'].replace('_', ' ').lower()}", "am"))
    if prof.get("website_match") is False:
        out.append(_f("gbp_website", "gbp_website", "amber",
                      f"The profile's website ({prof.get('website')}) isn't {c.get('domain') or 'the client site'}", "seo"))
    replies_owed = ledger.scope_qty(scope, "review_reply", month_of(today))
    if rv and rv.get("waiting") and rv.get("oldest_waiting") and replies_owed != 0:
        limit = reply_days(c.get("package"))
        waited = working_days_between(ts_date(rv["oldest_waiting"]), today)
        if waited > limit:
            out.append(_f("gbp_reviews", "gbp_reviews", "amber",
                          f"{rv['waiting']} review{'s' if rv['waiting'] != 1 else ''} without a reply: the oldest has waited "
                          f"{waited} business days (the {c.get('package') or 'package'} rule is {limit})", "am"))
    owed_posts = ledger.scope_qty(scope, "gbp_post", month_of(today))
    if owed_posts:
        gap = int((now - prof["last_post"]) / 86400) if prof.get("last_post") else None
        if gap is None or gap > POST_GAP_DAYS:
            out.append(_f("gbp_no_post", "gbp_no_post", "amber",
                          "No live Business Profile post " + (f"for {gap} days" if gap is not None else "found on the profile"), "seo"))
    return out


def ga4_flags(metrics):
    import json
    row = metrics.get(("ga4", "summary", "latest"))
    try:
        sm = json.loads(row["text"]) if row and row["text"] else None
    except ValueError:
        sm = None
    if not sm:
        return []
    out = []
    if not sm.get("last7_all"):
        out.append(_f("ga4_no_data", "ga4_no_data", "red", "Google Analytics recorded no visits at all in the last 7 days: the tag may be missing or broken", "dev"))
    elif (sm.get("cur") or {}).get("all_sessions") and not (sm.get("cur") or {}).get("all_key_events"):
        out.append(_f("ga4_no_key_events", "ga4_no_key_events", "amber",
                      "No key events (form sends, calls) recorded in Google Analytics in 30 days: check the lead tracking", "dev"))
    return out


def colour(flags, status="active"):
    if status != "active":
        return "paused"
    sev = {f["severity"] for f in flags}
    return "red" if "red" in sev else "amber" if "amber" in sev else "green"


ORDER = ["site_down", "ga4_no_data", "gbp_unverified", "gbp_closed", "scope_missing", "short", "no_task", "report_late", "owner_missing",
         "domain_missing", "gbp_reviews", "gbp_no_post", "gbp_website", "ga4_no_key_events", "approval",
         "client_input", "overdue_tasks", "closed_no_url", "unverified", "unassigned", "am_missing", "golive_missing",
         "source_error", "source_blocked", "stale", "source_partial", "source_off"]


def sort_flags(flags):
    return sorted(flags, key=lambda f: (RANK.get(f["severity"], 9), ORDER.index(f["code"]) if f["code"] in ORDER else 99))


def escalation(first_seen, today):
    """(level, working days open): new -> owner (day 2) -> you (day 5)."""
    wd = working_days_between(ts_date(first_seen), today)
    return ("you" if wd >= 5 else "owner" if wd >= 2 else "new"), wd
