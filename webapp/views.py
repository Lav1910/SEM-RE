"""View models for the Semrush-style screens, computed from a stored audit.json.

Mirrors Semrush AI Visibility's information architecture (Visibility Overview, Competitor Research, Prompt
Research, Topics & Sources tabs) on top of HBM's own data: local questions asked on each AI platform.
Everything is recomputed per platform filter so the numbers always trace back to stored answers.
"""
import collections
import re

from aeo.analyze import PLATFORMS, SOURCE_GROUP
from aeo import scope as scopes
from aeo.engines import ENGINES, ENGINE_LABELS
from . import status as pstatus

INTENT = {"discovery": ("Commercial", "#7C6CF0"), "research": ("Informational", "#0FB2B5"), "brand": ("Navigational", "#F2A93B")}
INTENT_COLORS = {"commercial": "#7C6CF0", "informational": "#0FB2B5", "navigational": "#F2A93B", "transactional": "#18C3A4"}


def intent_of(q):
    """DataWise keyword intent when available, else the question type."""
    if q.get("intent"):
        return q["intent"].capitalize(), INTENT_COLORS.get(q["intent"], "#999")
    return INTENT.get(q.get("kind"), ("—", "#999"))


def band(score):
    if score is None:
        return ("—", "No AI answers measured yet")
    if score < 35:
        return ("Low", "Rarely named in AI answers compared to competitors")
    if score < 70:
        return ("Medium", "Named in some AI answers; competitors appear more often")
    return ("Great", "Named in most AI answers for these questions")


def area_label(q, M):
    if q.get("kind") == "brand":
        return "Brand questions"
    if q.get("area"):
        return q["area"]
    if q["id"].startswith("area_"):
        return q["prompt"].split(" in ")[-1].rstrip("?").strip()
    if q.get("kind") == "brand":
        return "Brand questions"
    return f"{M['city']}, {M['state_code']}"


def areas_for(A):
    main = f"{A['market']['city']}, {A['market']['state_code']}"
    seen = [main] if any(area_label(q, A["market"]) == main for q in A["questions"]) else []
    for q in A["questions"]:
        a = area_label(q, A["market"])
        if a != "Brand questions" and a not in seen:
            seen.append(a)
    return seen


def gauge_geometry(score, cx=120, cy=118, r=90):
    """Points for a 270-degree gauge: track segments (Low 0-35, Medium 35-70, Great 70-100), progress length, marker."""
    import math
    L = 2 * math.pi * r * 0.75

    def pt(frac):
        a = math.radians(135 + 270 * frac)
        return round(cx + r * math.cos(a), 2), round(cy + r * math.sin(a), 2)
    sx, sy = pt(0)
    ex, ey = pt(1)
    seg = lambda a, b: {"dash": f"{round(L * (b - a) - 4, 1)} {round(L, 1)}", "offset": round(-L * a - 2, 1)}  # noqa: E731
    mx, my = pt((score or 0) / 100)
    return {"path": f"M{sx} {sy} A{r} {r} 0 1 1 {ex} {ey}", "L": round(L, 1), "len": round(L * (score or 0) / 100, 1),
            "segs": [seg(0, .35), seg(.35, .70), seg(.70, 1)], "mx": mx, "my": my}


def measured_engines(A):
    """Platforms that gave at least one real result (an answer, or Google showing no AI answer) in this audit."""
    return [e for e in ENGINES if any(a["engine"] == e and a["status"] in ("ok", "no_answer") for a in A.get("answers", []))]


def build(A, platform="all", history=None, vs=None, area=None, clean=False):
    """vs: competitor names to compare against (Semrush "Add competitor"); default = the 3 most-named builders.
    area: limit every figure to one market area's questions (Semrush's country tabs, for a local builder).
    clean (change 24): Sales screens and prospect-facing exports. Platforms with no real results are left out and
    no error or connection labels appear; every figure already comes from real answers only."""
    P, X, M = A["profile"], A["analysis"], A["market"]
    me = P["domain"]
    sc = scopes.for_audit(A)
    is_me = lambda c: scopes.matches(c.get("url") or c.get("domain", ""), sc)  # noqa: E731
    allq = A["questions"]
    if area:
        allq = [q for q in allq if area_label(q, M) == area] or allq
    qs = {q["id"]: q for q in allq}
    measured_set = measured_engines(A)
    engines = [e for e in ENGINES if platform in ("all", e) and (not clean or e in measured_set)]
    ans = [a for a in A["answers"] if a["engine"] in engines and a["question_id"] in qs]
    cells = {(c["question_id"], c["engine"]): c for c in X["cells"]}
    ok_all = [a for a in ans if a["status"] == "ok"]
    ok = [a for a in ok_all if qs[a["question_id"]].get("kind") != "brand"]  # brand questions don't count toward visibility

    def mentioned(a):
        return bool(cells[(a["question_id"], a["engine"])]["mentioned"])

    # ---- main metrics (Semrush: Mentions, Citations, Cited Pages) ----
    mentions = sum(1 for a in ok if mentioned(a))
    citations = sum(1 for a in ok if any(is_me(c) for c in a["citations"]))
    my_pages = collections.Counter(c["url"].split("#")[0] for a in ok for c in a["citations"] if is_me(c))
    tot_w = sum(qs[a["question_id"]]["weight"] for a in ok)
    hit_w = sum(qs[a["question_id"]]["weight"] for a in ok if mentioned(a))
    score = round(100 * hit_w / tot_w) if tot_w else None
    label, caption = band(score)
    gauge = gauge_geometry(score)
    # Semrush "Monthly Audience": search demand behind the questions where the builder is named
    named_q = {a["question_id"] for a in ok if mentioned(a)}
    audience = sum(qs[i].get("volume") or 0 for i in named_q)

    # ---- distribution by LLM ----
    dist = []
    for e in ENGINES:
        if clean and e not in measured_set:
            continue
        eo = [a for a in A["answers"] if a["engine"] == e and a["status"] == "ok" and a["question_id"] in qs
              and qs[a["question_id"]].get("kind") != "brand"]
        measured = any(a["engine"] == e and a["status"] in ("ok", "no_answer") for a in A["answers"])
        ew = sum(qs[a["question_id"]]["weight"] for a in eo)
        eh = sum(qs[a["question_id"]]["weight"] for a in eo if mentioned(a))
        dist.append({"engine": e, "label": ENGINE_LABELS[e], "measured": measured, "visibility": round(100 * eh / ew) if ew else None,
                     "status": pstatus.for_audit(A, e),
                     "mentions": sum(1 for a in eo if mentioned(a)),
                     "pages": len({c["url"] for a in eo for c in a["citations"] if is_me(c)}),
                     "answers": len(eo)})
    dm = sum(d["mentions"] for d in dist) or 0
    dp = sum(d["pages"] for d in dist) or 0
    for d in dist:
        d["mentions_pct"] = round(100 * d["mentions"] / dm) if dm else 0
        d["pages_pct"] = round(100 * d["pages"] / dp) if dp else 0

    # ---- mentions by area (Semrush: by country; HBM: by local market) ----
    def area_of(q):
        return area_label(q, M)
    areas = collections.OrderedDict()
    for a in ok:
        ar = area_of(qs[a["question_id"]])
        areas.setdefault(ar, {"area": ar, "mentions": 0, "answers": 0})
        areas[ar]["answers"] += 1
        areas[ar]["mentions"] += 1 if mentioned(a) else 0
    am = sum(v["mentions"] for v in areas.values())
    by_area = [dict(v, pct=round(100 * v["mentions"] / am) if am else 0) for v in areas.values()]

    # ---- competitor set (Semrush: "Add competitor" drives every gap view) ----
    comp = collections.Counter()
    comp_eng = collections.defaultdict(set)
    for a in ok:
        for n in cells[(a["question_id"], a["engine"])]["competitors"]:
            comp[n] += 1; comp_eng[n].add(a["engine"])
    detected = [n for n, _ in comp.most_common(15)]
    chosen = [n for n in (vs or []) if n in comp] or detected[:3]

    # ---- topics (each local question) ----
    topics, opportunities = [], []
    for q in allq:
        qa = [a for a in ok if a["question_id"] == q["id"]]
        named = [a for a in qa if mentioned(a)]
        comps = collections.Counter(n for a in qa for n in cells[(q["id"], a["engine"])]["competitors"])
        row = {"id": q["id"], "topic": q["prompt"], "keyword": q["keyword"], "volume": q.get("volume"),
               "visibility": round(100 * len(named) / len(qa)) if qa else None, "mentions": len(named), "answers": len(qa),
               "intent": intent_of(q)[0], "intent_color": intent_of(q)[1], "difficulty": q.get("difficulty"),
               "platforms": [ENGINE_LABELS[a["engine"]] for a in named], "top_competitors": [n for n, _ in comps.most_common(3)],
               "gap": [comps.get(c, 0) for c in chosen], "trend": q.get("trend") or []}
        if named:
            topics.append(row)
        elif qa and any(comps.get(c) for c in chosen):
            opportunities.append(row)
    topics.sort(key=lambda r: (-(r["visibility"] or 0), -(r["volume"] or 0)))
    opportunities.sort(key=lambda r: -(r["volume"] or 0))

    # ---- sources (Semrush gap_source_domains: competitors mentioned, URLs, prompts, your mentions) ----
    src = collections.defaultdict(lambda: {"urls": set(), "prompts": set(), "you": 0, "gap": collections.Counter(), "answers": 0})
    for a in ok:
        doms = collections.defaultdict(set)
        for c in a["citations"]:
            if c["domain"] and not is_me(c):
                doms[c["domain"]].add(c["url"])
        named_here = set(cells[(a["question_id"], a["engine"])]["competitors"])
        for d, urls in doms.items():
            r = src[d]; r["urls"] |= urls; r["prompts"].add(a["question_id"]); r["answers"] += 1
            r["you"] += 1 if mentioned(a) else 0
            for c in chosen:
                if c in named_here:
                    r["gap"][c] += 1

    def src_rows(kind):
        out = []
        for d, r in src.items():
            if kind == "cited" and not r["you"]:
                continue
            if kind == "opp" and (r["you"] or not any(r["gap"].values())):
                continue
            plat = PLATFORMS.get(d)
            out.append({"domain": d, "label": plat or d, "type": SOURCE_GROUP.get(plat or "", "Builder or local site"),
                        "count": r["answers"], "you": r["you"], "urls": len(r["urls"]), "prompts": len(r["prompts"]),
                        "gap": [r["gap"].get(c, 0) for c in chosen], "competitors_mentioned": sum(1 for c in chosen if r["gap"].get(c))})
        return sorted(out, key=lambda x: (-x["competitors_mentioned"], -x["count"]))[:30]

    pages = [{"url": u, "path": re.sub(r"^https?://[^/]+", "", u) or "/", "count": n} for u, n in my_pages.most_common()]

    # ---- competitors (share of voice) ----
    sov_total = sum(comp.values()) + mentions
    competitors = [{"name": P["name"], "mentions": mentions, "share": round(100 * mentions / sov_total) if sov_total else 0,
                    "platforms": sum(1 for d in dist if d["mentions"] and d["engine"] in engines), "prospect": True}]
    for n, c in comp.most_common(10):
        competitors.append({"name": n, "mentions": c, "share": round(100 * c / sov_total) if sov_total else 0,
                            "platforms": len(comp_eng[n]), "prospect": False})
    competitors.sort(key=lambda r: -r["mentions"])
    pack = [b for b in X["leaderboard"] if b.get("pack")]
    kpis = dict(X["kpis"])
    if area:  # Maps 3-pack for this area's searches only
        from aeo.analyze import norm_name, name_key
        g_cells = [cells[(i, "google_aio")] for i in qs if (i, "google_aio") in cells and cells[(i, "google_aio")]["in_pack"] is not None]
        kpis["pack"] = sum(1 for c in g_cells if c["in_pack"]) if g_cells else None
        kpis["pack_of"] = len(g_cells) if g_cells else None
        cnt, names = collections.Counter(), {}
        for a in A["answers"]:
            if a["engine"] == "google_aio" and a["question_id"] in qs:
                for p_ in a.get("local_pack") or []:
                    t = norm_name(p_.get("title") or "")
                    if t:
                        k = name_key(t) or t.lower(); cnt[k] += 1; names.setdefault(k, t)
        me_k = name_key(P["name"])
        pack = [{"name": P["name"] if k == me_k else names[k], "pack": n, "prospect": k == me_k} for k, n in cnt.most_common(8)]

    # ---- prompts with answers ----
    prompts = []
    for q in allq:
        rows = []
        for e in engines:
            a = next((x for x in A["answers"] if x["question_id"] == q["id"] and x["engine"] == e), None)
            c = cells.get((q["id"], e))
            if not a or (clean and a["status"] not in ("ok", "no_answer")):
                continue
            rows.append({"engine": e, "label": ENGINE_LABELS[e], "status": a["status"], "text": a.get("text") or "",
                         "mentioned": c["mentioned"] if c else None, "rank": c.get("rank") if c else None,
                         "competitors": [n for n in (c or {}).get("competitors", []) if "." not in n][:6], "citations": a["citations"][:8],
                         "error": a.get("error"), "friendly": pstatus.friendly_error(a.get("error")) if a.get("error") else None,
                         "sources_only": a.get("source") == "semrush"})
        prompts.append({"q": q, "rows": rows})

    trend = []
    for h in history or []:
        trend.append(h)

    return {
        "platform": platform, "clean": clean, "platforms": measured_set if clean else list(ENGINES), "score": score, "band": label, "caption": caption,
        "mentions": mentions, "citations": citations, "cited_pages": len(my_pages), "answers": len(ok),
        "dist": dist, "by_area": by_area, "topics": topics, "opportunities": opportunities,
        "cited_sources": src_rows("cited"), "source_opportunities": src_rows("opp"), "pages": pages,
        "audience": audience, "chosen": chosen, "detected": detected, "gauge": gauge,
        "datawise": A.get("datawise"), "gbp": A["profile"].get("gbp"), "scope": sc,
        "areas": areas_for(A), "area": area, "questions_n": len(allq),
        "competitors": competitors, "pack": pack, "prompts": prompts, "trend": trend,
        "recommendations": X["recommendations"], "checks": A["checks"], "kpis": kpis,
        "locations": A.get("locations") or [],
        "measured_any": any(d["measured"] for d in dist),
    }


def chart(A, hist):
    """Monthly series for the trend chart: Semrush's Google AI Overview history plus HBM's own audits.
    Months without data stay empty (null) rather than zero."""
    import datetime
    sem = (A.get("semrush") or {}).get("history") or []
    by_audit = {}
    for h in hist or []:
        by_audit[h.get("month")] = h  # latest audit in each month
    months = sorted({m["month"] for m in sem} | {k for k in by_audit if k})[-12:]
    lab = lambda m: datetime.date(int(m[:4]), int(m[5:7]), 1).strftime("%b %Y")  # noqa: E731
    semm = {m["month"]: m for m in sem}

    def col(src, key):
        return [src.get(m, {}).get(key) if src.get(m) else None for m in months]
    return {
        "months": [lab(m) for m in months],
        "main": [
            {"name": "AI Overview citations", "note": "Google, from Semrush", "color": "#0FB2B5", "dash": True, "values": col(semm, "aio_cited")},
            {"name": "Mentions", "note": "HBM audits", "color": "#5B5FD9", "values": col(by_audit, "mentions")},
            {"name": "Citations", "note": "HBM audits", "color": "#18C3A4", "values": col(by_audit, "citations")},
            {"name": "Cited Pages", "note": "HBM audits", "color": "#B37FEB", "values": col(by_audit, "pages")},
        ],
        "audience": [
            {"name": "Monthly Audience", "note": "HBM audits", "color": "#5B5FD9", "values": col(by_audit, "audience")},
            {"name": "Searches showing an AI Overview", "note": "Google, from Semrush", "color": "#0FB2B5", "dash": True, "values": col(semm, "aio_keywords")},
        ],
        "vis": [
            {"name": "AI Visibility score", "note": "HBM audits", "color": "#0A686A", "values": col(by_audit, "score")},
        ],
    }
