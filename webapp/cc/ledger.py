"""Owed vs delivered (change 64). Pure functions: no database, no network, so they're easy to test.

Rules
- Owed comes from the client's scope for that month (versioned rows: start_month..end_month). No row = scope missing,
  which is never treated as zero.
- Nothing is owed before the first full month after go-live (the launch month itself isn't counted), nor before the
  tracking start month (change 71: the Command Center counts from November 2026; earlier months aren't judged).
- Delivered means verified, not "task closed":
  blog          counts only when its live URL answered 200, isn't noindex and its canonical points to itself;
  report        counts when the AM marked it sent (ClickUp task closed/sent) or it was verified by hand;
  GBP items     count only when verified. With the profile connected (change 66) only what Google shows on it counts
                (a live post, a photo the business added, a reply); without it, a hand check with "checked by".
- "Reported" = the task says published/sent but nothing verified it yet. It shows, but doesn't count.
"""
from .core import GRID_ITEMS, ITEMS, add_months, month_of

COUNTS_WHEN_REPORTED = {"report"}  # the AM's confirmation is the evidence for a report
CHECKABLE_NOW = {"blog", "report"}  # without GBP, GBP work can only be verified by hand


def scope_qty(rows, item, month):
    """Monthly quantity for `item` in `month`, or None when no scope covers that month."""
    best = None
    for r in rows:
        if r["item"] != item or r["start_month"] > month or (r["end_month"] and r["end_month"] < month):
            continue
        if best is None or (r["start_month"], r["set_at"] or 0) > (best["start_month"], best["set_at"] or 0):
            best = r
    return None if best is None else int(best["qty"])


def first_owed_month(go_live, start=None):
    """First month anything is owed: the first full month after go-live, and never before the tracking start.
    None when neither is known (then every month with scope is owed)."""
    first = add_months(str(go_live)[:7], 1) if go_live else None
    if start and (first is None or start > first):
        return start
    return first


def months_shown(start, cur, n=6):
    """Columns for the owed-vs-delivered grids: from the tracking start (at least n months, so the coming ones show),
    at most the last 12. Without a start: the last 12 months."""
    if not start:
        return [add_months(cur, -k) for k in range(11, -1, -1)]
    first = max(start, add_months(cur, -11))
    last = max(cur, add_months(first, n - 1))
    out, m = [], first
    while m <= last:
        out.append(m)
        m = add_months(m, 1)
    return out


GBP_ITEMS = ("gbp_post", "gbp_photo", "review_reply")


def counts(rows, item, month, google_only=False):
    ds = [d for d in rows if d["item"] == item and d["month"] == month and not d["not_ours"] and not d["merged_into"]]
    if google_only:  # the profile is connected: ClickUp tasks and hand checks become "claimed", Google's view is the count
        g = [d for d in ds if d.get("source") == "gbp"]
        verified = sum(1 for d in g if d["verified_at"])
        reported = sum(1 for d in ds if d.get("source") != "gbp" and (d["verified_at"] or d["status"] == "published"))
        return verified, reported, len(ds) - len(g) - reported
    verified = sum(1 for d in ds if d["verified_at"])
    reported = sum(1 for d in ds if not d["verified_at"] and d["status"] == "published")
    working = len(ds) - verified - reported
    return verified, reported, working


def cell(go_live, scope_rows, deliverables, item, month, today, gbp_connected=False, start=None):
    owed = scope_qty(scope_rows, item, month)
    verified, reported, working = counts(deliverables, item, month, gbp_connected and item in GBP_ITEMS)
    counted = verified + (reported if item in COUNTS_WHEN_REPORTED else 0)
    claimed = verified + reported
    cur = month_of(today)
    first = first_owed_month(go_live, start)
    checkable = CHECKABLE_NOW | (set(GBP_ITEMS) if gbp_connected else set())
    if month > cur:
        state = "future"
    elif start and month < start:
        state = "not_tracked"
    elif first and month < first:
        state = "pre_launch"
    elif owed is None:
        state = "scope_missing"
    elif owed == 0:
        state = "not_owed"
    elif owed < 0:
        state = "rule"  # "every new review": judged by reply time on the profile, not a monthly count
    elif counted >= owed:
        state = "done"
    elif month < cur:
        state = "unverified" if (claimed >= owed and item not in checkable) else "short"
    elif item == "report" and today.day > 7:
        state = "late"  # the month's report is due by the 7th
    else:
        state = "open"
    return {"item": item, "month": month, "owed": owed, "verified": verified, "reported": reported, "working": working,
            "counted": counted, "claimed": claimed, "state": state}


def month_row(go_live, scope_rows, deliverables, month, today, items=None, gbp_connected=False, start=None):
    return {i: cell(go_live, scope_rows, deliverables, i, month, today, gbp_connected, start) for i in (items or [k for k, _ in ITEMS])}


def year_grid(go_live, scope_rows, deliverables, today, n=12, gbp_connected=False, start=None, items=GRID_ITEMS):
    """Oldest -> newest month, for the heatmap (from the tracking start when there is one)."""
    cur = month_of(today)
    months = months_shown(start, cur) if start else [add_months(cur, -k) for k in range(n - 1, -1, -1)]
    return months, {i: [cell(go_live, scope_rows, deliverables, i, m, today, gbp_connected, start) for m in months] for i in items}


def short_run(go_live, scope_rows, deliverables, item, today, gbp_connected=False, look_back=6, start=None):
    """Completed months in a row (most recent first) where the item fell short: [(month, counted, owed), ...]."""
    out, m = [], add_months(month_of(today), -1)
    for _ in range(look_back):
        c = cell(go_live, scope_rows, deliverables, item, m, today, gbp_connected, start)
        if c["state"] != "short":
            break
        out.append((m, c["counted"], c["owed"]))
        m = add_months(m, -1)
    return out
