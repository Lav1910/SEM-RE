"""Business Profile sync (change 66): which profile is whose, and what Google shows on it. Read-only (sources/gbp.py).

Per client, each daily run:
1. The location directory (every profile the connected Google account manages) is refreshed at most every 20 hours.
2. The client's profile: picked by hand on the Setup tab, else matched by website domain, else by exact name.
3. From that profile: 90 days of daily performance, reviews (with replies), posts and the business's own photos.
4. Delivered = seen on the profile:
     a live post               -> one GBP post delivered in the month it was created
     a photo the business added -> one GBP photo delivered in that month
     a reply to a review        -> one review reply delivered in the month it was written
   Posts Google rejected are listed but don't count.
"""
import datetime
import json
import re

from aeo import kv
from webapp import db
from . import data
from .core import TZ, add_months, month_of, now, root_domain, today
from .sources import gbp

DIR_MAX_AGE = 20 * 3600
KEEP_MONTHS = 13
_SUFFIX = re.compile(r"\b(llc|inc|ltd|co|corp|company|the)\b")


def _norm(name):
    return re.sub(r"\s+", " ", _SUFFIX.sub(" ", re.sub(r"[^a-z0-9 ]", " ", (name or "").lower().replace("&", " and ")))).strip()


# ---------------------------------------------------------------- directory
def refresh_directory(calls, force=False):
    """Re-list every location the connected accounts can see (change 87: all of them, main first; a profile two accounts
    can see is read through the first). Returns how many."""
    if not force and now() - float(kv.get("cc_gbp_dir_at", 0) or 0) < DIR_MAX_AGE:
        return None
    t, seen, listed, last_err = now(), set(), set(), None
    emails = gbp.ACC.emails()
    for email in emails:
        with gbp.use(email):
            try:
                for acc in gbp.accounts(calls):
                    for loc in gbp.locations(acc["name"], calls):
                        if loc["name"] in seen:
                            continue
                        meta = loc.get("metadata") or {}
                        seen.add(loc["name"])
                        db.upsert("cc_gbp_locations", {
                            "name": loc["name"], "account": acc["name"], "account_name": acc.get("accountName"),
                            "title": loc.get("title"), "website": loc.get("websiteUri"), "place_id": meta.get("placeId"),
                            "maps_uri": meta.get("mapsUri"), "address": gbp.address_line(loc),
                            "verified": 1 if meta.get("hasVoiceOfMerchant") else (0 if "hasVoiceOfMerchant" in meta else None),
                            "open_status": (loc.get("openInfo") or {}).get("status"), "seen_at": t, "via": email}, ["name"])
                listed.add(email)
            except gbp.NotApproved:
                raise  # the Cloud project itself: the same for every account
            except (gbp.Reauth, gbp.NotConnected, gbp.GBPError) as e:
                last_err = e  # this account is flagged (or failed this time); the others still count
    if emails and not listed and last_err:
        raise last_err
    for r in db.q("SELECT name, via FROM cc_gbp_locations"):
        # drop a profile only when the account that saw it was read in full this time, or that account is gone
        if r["name"] not in seen and (r["via"] in listed or r["via"] not in emails):
            db.x("DELETE FROM cc_gbp_locations WHERE name=?", (r["name"],))
    kv.update("cc_gbp_dir_at", lambda _o: t, 0)
    return len(seen)


def directory():
    return [data.d(r) for r in db.q("SELECT * FROM cc_gbp_locations ORDER BY title")]


def match(c, rows):
    """-> (location row or None, how, other candidates)."""
    if c.get("gbp_how") == "none":
        return None, "none", []
    by_name = {r["name"]: r for r in rows}
    if c.get("gbp_how") == "manual" and c.get("gbp_location"):
        return by_name.get(c["gbp_location"]), "manual", []
    dom = root_domain(c.get("domain"))
    cands = [r for r in rows if dom and root_domain(r.get("website")) == dom]
    if not cands:
        cands = [r for r in rows if _norm(r.get("title")) and _norm(r.get("title")) == _norm(c.get("name"))]
    if not cands:
        return None, "auto", []
    # several profiles on one website (e.g. two offices): keep the one already chosen, else the verified one, else the first
    cands.sort(key=lambda r: (r["name"] != c.get("gbp_location"), r.get("verified") != 1, r.get("title") or ""))
    return cands[0], "auto", cands[1:]


# ---------------------------------------------------------------- per-client sync
def sync(c, calls):
    cid = c["id"]
    if c.get("gbp_how") == "none":
        data.set_source(cid, "gbp", "ok", "No Business Profile in this client's scope (set on Setup)", ok=True)
        return "no profile in scope"
    if not gbp.configured():
        data.set_source(cid, "gbp", "not_connected", "Google sign-in key not set on the server")
        return "not configured"
    if not gbp.connected():
        st = gbp.stored()
        data.set_source(cid, "gbp", "error" if st else "not_connected",
                        (st or {}).get("detail") or "Business Profile isn't connected yet (Sync log → Connect Business Profile)")
        return "not connected"
    try:
        refresh_directory(calls)
    except gbp.NotApproved as e:
        data.set_source(cid, "gbp", "not_connected", str(e))
        return "not approved"
    loc, how, others = match(c, directory())
    if not loc:
        why = ("The profile picked on Setup is no longer in the connected Google account" if how == "manual"
               else f"No Business Profile for {c.get('domain') or c['name']} in any connected Google account")
        data.set_source(cid, "gbp", "not_connected", why)
        data.set_metric(cid, "gbp", "profile", "latest", text=None)
        return "no profile"
    if loc["name"] != c.get("gbp_location"):
        data.log_change(cid, "client", cid, "gbp_location", c.get("gbp_location"), loc["name"], "sync (matched by website)")
        db.x("UPDATE cc_clients SET gbp_location=? WHERE id=?", (loc["name"], cid))

    t = today()
    end, start = t - datetime.timedelta(days=1), t - datetime.timedelta(days=91)
    with gbp.use(loc.get("via")):  # change 87: read through the account that can see this profile
        daily = gbp.daily_metrics(loc["name"], start, end, calls)
        revs, avg, total, revs_all = gbp.reviews(loc["account"], loc["name"], calls)
        posts, posts_all = gbp.local_posts(loc["account"], loc["name"], calls)
        photos, photos_all = gbp.media(loc["account"], loc["name"], calls)

    since = add_months(month_of(t), -(KEEP_MONTHS - 1))
    n_dl = _save_deliverables(cid, loc, revs, posts, photos, since, {"post": posts_all, "photo": photos_all, "reply": revs_all})
    rv = review_summary(revs, avg, total, t)
    last_post = max((gbp.ts(p.get("createTime")) or 0 for p in posts if p.get("state") == "LIVE"), default=0) or None
    profile = {"name": loc["name"], "title": loc.get("title"), "account": loc.get("account_name"), "address": loc.get("address"),
               "website": loc.get("website"), "maps_uri": loc.get("maps_uri"), "place_id": loc.get("place_id"),
               "verified": loc.get("verified"), "open_status": loc.get("open_status"), "how": how, "via": loc.get("via"),
               "website_match": (root_domain(loc.get("website")) == root_domain(c.get("domain"))) if loc.get("website") else None,
               "others": [o.get("title") for o in others], "last_post": last_post,
               "posts_live": sum(1 for p in posts if p.get("state") == "LIVE"), "photos": len(photos)}
    data.set_metric(cid, "gbp", "profile", "latest", text=json.dumps(profile))
    data.set_metric(cid, "gbp", "reviews", "latest", text=json.dumps(rv))
    data.set_metric(cid, "gbp", "daily", "latest", text=json.dumps([{"date": d, **v} for d, v in sorted(daily.items())]))
    if others:
        data.set_source(cid, "gbp", "partial", f"{len(others) + 1} profiles match this website; using {loc.get('title')}. Pick one on Setup.", ok=True)
    else:
        data.set_source(cid, "gbp", "ok", f"{loc.get('title')} · {loc.get('account_name') or loc['account']}"
                        + (f" · via {loc['via']}" if loc.get("via") and loc.get("via") != (gbp.stored() or {}).get("email") else ""), ok=True)
    return f"{loc.get('title')}: {len(revs)} reviews, {len(posts)} posts, {len(photos)} photos, {n_dl} deliverables"


def _local_month(ts_):
    return datetime.datetime.fromtimestamp(ts_, TZ).strftime("%Y-%m")


def _save_deliverables(cid, loc, revs, posts, photos, since, complete):
    rows = []
    for p in posts:
        t0 = gbp.ts(p.get("createTime"))
        if not t0 or _local_month(t0) < since:
            continue
        live = p.get("state") == "LIVE"
        summary = re.sub(r"\s+", " ", p.get("summary") or "").strip()
        rows.append(("post", "gbp_post", p.get("name"), _local_month(t0), (summary[:90] + ("…" if len(summary) > 90 else "")) or
                     (p.get("topicType") or "Post").replace("_", " ").title(), p.get("searchUrl"), t0, live,
                     None if live else ("Rejected by Google" if p.get("state") == "REJECTED" else f"Not live yet ({p.get('state') or 'unknown'})")))
    for m in photos:
        t0 = gbp.ts(m.get("createTime"))
        if m.get("mediaFormat") not in ("PHOTO", "VIDEO") or not t0 or _local_month(t0) < since:
            continue  # videos count toward "photos & videos" (e.g. "12 incl. 2 videos")
        cat = ((m.get("locationAssociation") or {}).get("category") or "photo").replace("_", " ").lower()
        kind = "Video" if m.get("mediaFormat") == "VIDEO" else "Photo"
        rows.append(("photo", "gbp_photo", m.get("name"), _local_month(t0), f"{kind} ({cat})", m.get("googleUrl"), t0, True, None))
    for r in revs:
        rep = r.get("reviewReply") or {}
        t0 = gbp.ts(rep.get("updateTime"))
        if not rep.get("comment") or not t0 or _local_month(t0) < since:
            continue
        rows.append(("reply", "review_reply", "reply:" + str(r.get("reviewId") or r.get("name")), _local_month(t0),
                     f"Reply to a {gbp.stars(r) or '?'}★ review", None, t0, True, None))
    keep = {"post": set(), "photo": set(), "reply": set()}
    t = now()
    for kind, item, ext, month, title, url, t0, ok, note in rows:
        if not ext:
            continue
        keep[kind].add(ext)
        db.upsert("cc_deliverables", {
            "client_id": cid, "item": item, "month": month, "title": title, "status": "published", "url": url,
            "published_at": t0, "verified_at": t0 if ok else None, "verified_how": "Seen on the profile (Google)" if ok else None,
            "check_note": note, "checked_at": t, "source": "gbp", "ext_id": ext, "updated_at": t}, ["client_id", "source", "ext_id"])
    # removed on Google (only when the full list was read): drop ours too, so a deleted post stops counting
    for kind, item in (("post", "gbp_post"), ("photo", "gbp_photo"), ("reply", "review_reply")):
        if not complete[kind]:
            continue
        for d_ in db.q("SELECT id, ext_id FROM cc_deliverables WHERE client_id=? AND source='gbp' AND item=? AND month>=?", (cid, item, since)):
            if d_["ext_id"] not in keep[kind]:
                db.x("DELETE FROM cc_deliverables WHERE id=?", (d_["id"],))
    return len(rows)


def review_summary(revs, avg, total, t):
    """Headline review numbers. Reviewer names aren't kept: a first initial is enough to recognise one."""
    cur = month_of(t)
    waiting = [r for r in revs if not (r.get("reviewReply") or {}).get("comment")]
    oldest = min((gbp.ts(r.get("createTime")) for r in waiting if gbp.ts(r.get("createTime"))), default=None)
    recent = []
    for r in revs[:8]:
        name = ((r.get("reviewer") or {}).get("displayName") or "").strip()
        recent.append({"stars": gbp.stars(r), "text": re.sub(r"\s+", " ", r.get("comment") or "")[:280],
                       "at": gbp.ts(r.get("createTime")), "who": (name[:1] + ".") if name else "Anonymous",
                       "replied": bool((r.get("reviewReply") or {}).get("comment"))})
    return {"average": avg, "total": total, "read": len(revs), "waiting": len(waiting), "oldest_waiting": oldest,
            "new_this_month": sum(1 for r in revs if gbp.ts(r.get("createTime")) and _local_month(gbp.ts(r.get("createTime"))) == cur),
            "recent": recent}
