"""Build the local question set for a builder (or, change 53, any other local business).

Each question has a conversational `prompt` (asked to ChatGPT, Gemini, Perplexity, AI Mode) and a
search-style `keyword` (searched on Google for AI Overviews and the Maps pack). Semrush search volume
weights the score; questions keep a floor weight so zero-volume conversational questions still count.
"""
import re

# (id, prompt template, keyword template, needs service, kind)
TEMPLATES = [
    ("best_custom", "Who are the best custom home builders in {city}, {st}?", "best custom home builders {city}", None, "discovery"),
    ("custom", "I want to build a custom home in {city}. Which builders should I talk to?", "custom home builders {city}", None, "discovery"),
    ("home_builders", "What are the top-rated home builders in the {city} area?", "home builders {city}", None, "discovery"),
    ("luxury", "Recommend a luxury home builder in {city}, {st}.", "luxury home builders {city}", "luxury homes", "discovery"),
    ("own_lot", "Which builders in {city} will build a custom home on my own land?", "build on your lot {city}", "build on your lot", "discovery"),
    ("ranch", "Who builds ranch-style homes in the {city} area?", "ranch home builders {city}", "ranch homes", "discovery"),
    ("new_construction", "Where can I find new construction homes in {city}?", "new construction homes {city}", None, "discovery"),
    ("condos", "Who builds luxury condos or townhomes in {city}?", "luxury condos {city}", "condos", "discovery"),
    ("cost", "How much does it cost to build a custom home in {city}, {st}?", "cost to build a custom home in {city}", None, "research"),
    ("timeline", "How long does it take to build a custom home in {city}?", "how long to build a custom home {city}", None, "research"),
    ("brand_good", "Is {brand} a good home builder?", "{brand} reviews", None, "brand"),
    ("brand_vs", "How does {brand} compare with other builders in {city}?", "{brand} vs", None, "brand"),
]
AREA_TEMPLATE = ("area_{slug}", "Who are the best custom home builders in {area}?", "custom home builders {area}", None, "discovery")
FLOOR_WEIGHT = 10


def short_brand(name):
    return re.sub(r",?\s+(LLC|Inc\.?|Co\.?|Ltd\.?)$", "", name).strip()


def build(profile, market, max_questions=14):
    """change 53: home builders get the original set; any other business gets questions about its own services."""
    biz = profile.get("business")
    if biz and biz.get("kind") == "service":
        return build_service(profile, market, biz, max_questions)
    services = profile["services"]
    if biz and biz.get("chosen"):  # the salesperson's picks decide which builder questions apply
        services = [s["key"] for s in biz["services"] if s.get("key")]
    brand = short_brand(profile["name"])
    vals = {"city": market["city"], "st": market["state_code"], "state": market["state"], "brand": brand}
    qs = []
    for qid, p, k, need, kind in TEMPLATES:
        if need and need not in services:
            continue
        qs.append({"id": qid, "prompt": p.format(**vals), "keyword": k.format(**vals).lower(), "kind": kind})
    # services typed into the dropdown that the builder set doesn't cover get their own question, ahead of research
    extra = [s for s in (biz or {}).get("services") or [] if s.get("added") and not s.get("key")]
    for i, s in enumerate(extra[:3]):
        qs.insert(min(len(qs), 3 + i), _svc_best(s, vals))
    _add_areas(qs, profile, market, AREA_TEMPLATE)
    return qs[:max_questions]


def _add_areas(qs, profile, market, template):
    added = 0
    for area in profile["areas_served"]:
        if added >= 2:  # at most two area questions, so the core buyer questions stay in the set
            break
        a = re.sub(r"^Greater\s+", "", area or "").strip()
        a = re.sub(r",\s*[A-Z]{2}$", "", a).strip()          # "Waxhaw, NC" -> "Waxhaw"
        parent = a.split(",")[1].strip() if a.count(",") >= 1 else None
        a = a.split(",")[0].strip()                          # "Myers Park, Charlotte" -> "Myers Park"
        if not a or a.lower() == market["city"].lower():
            continue
        added += 1
        if parent:  # a neighbourhood of the main city
            a_full = f"{a}, {parent}"
        elif len(a.split()) == 1:  # a bare city name: add the state for clarity
            a_full = f"{a}, {market['state_code']}"
        else:
            a_full = a
        slug = re.sub(r"[^a-z0-9]+", "_", a.lower()).strip("_")
        qs.insert(3, {"id": template[0].format(slug=slug), "prompt": template[1].format(area=a_full),
                      "keyword": template[2].format(area=(f"{a} {parent}" if parent else a + (" " + market["state"] if len(a.split()) == 1 else "")).lower()),
                      "kind": "discovery"})


# ---------------------------------------------------------------- change 53: any other business
def _slug(s):
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:40]


def _pros(s, biz=None):
    """What a customer calls the businesses that do this service ('bathroom remodelers'); None when unknown."""
    return (s or {}).get("pros") or None


def _svc_best(s, vals):
    sl, pros = s["name"].lower(), _pros(s)
    if pros:
        return {"id": "svc_best_" + _slug(s["name"]), "prompt": f"Who are the best {pros} in {vals['city']}, {vals['st']}?",
                "keyword": f"{pros} {vals['city']}".lower(), "kind": "discovery", "service": s["name"]}
    return {"id": "svc_best_" + _slug(s["name"]), "prompt": f"Which companies are best for {sl} in {vals['city']}, {vals['st']}?",
            "keyword": f"{sl} {vals['city']}".lower(), "kind": "discovery", "service": s["name"]}


def build_service(profile, market, biz, max_questions=14):
    brand = short_brand(profile["name"])
    vals = {"city": market["city"], "st": market["state_code"], "brand": brand}
    S = (biz.get("services") or [])[:6]
    nouns = biz.get("nouns") or "companies"
    best = [_svc_best(s, vals) for s in S]
    top = {"id": "svc_top", "prompt": f"What are the top-rated {nouns} in the {vals['city']} area?",
           "keyword": f"{nouns} {vals['city']}".lower(), "kind": "discovery"}

    def hire(s):
        sl = s["name"].lower()
        return {"id": "svc_hire_" + _slug(s["name"]), "prompt": f"I need {sl} in {vals['city']}. Which local companies should I call?",
                "keyword": f"{sl} {vals['city']}".lower(), "kind": "discovery", "service": s["name"]}

    def cost(s):
        sl = s["name"].lower()
        return {"id": "svc_cost_" + _slug(s["name"]), "prompt": f"How much does {sl} cost in {vals['city']}, {vals['st']}?",
                "keyword": f"{sl} cost {vals['city']}".lower(), "kind": "research", "service": s["name"]}
    order = []
    if best:
        order.append(best[0])
    order.append(top)
    if len(best) > 1:
        order.append(best[1])
    if S:
        order.append(hire(S[0]))
    if len(best) > 2:
        order.append(best[2])
    if S:
        order.append(cost(S[0]))
    order += best[3:4]
    if len(S) > 1:
        order.append(hire(S[1]))
    order += best[4:]
    if len(S) > 1:
        order.append(cost(S[1]))
    lead = S[0]["name"].lower() if S else biz.get("label") or "this work"
    order += [{"id": "brand_good", "prompt": f"Is {brand} a good choice for {lead}?", "keyword": f"{brand} reviews".lower(), "kind": "brand"},
              {"id": "brand_vs", "prompt": f"How does {brand} compare with other {nouns} in {vals['city']}?", "keyword": f"{brand} vs".lower(), "kind": "brand"}]
    seen, qs = set(), []
    for q in order:  # two services can share a keyword; keep each question id once
        if q["id"] not in seen:
            seen.add(q["id"]); qs.append(q)
    _add_areas(qs, profile, market, area_template(biz))
    return qs[:max_questions]


def area_template(biz):
    if not biz or biz.get("kind") != "service":
        return AREA_TEMPLATE
    s0 = (biz.get("services") or [None])[0]
    who = _pros(s0) or biz.get("nouns") or "companies"
    return ("area_{slug}", f"Who are the best {who} in {{area}}?", f"{who} {{area}}", None, "discovery")


def apply_volumes(qs, volumes, trends=None):
    for q in qs:
        v = volumes.get(q["keyword"])
        q["volume"] = v
        q["trend"] = (trends or {}).get(q["keyword"]) or []
        q["weight"] = max(v or 0, FLOOR_WEIGHT)
    return qs


# A short set per extra service location (change: each city measured on its own, searched from that city)
LOCATION_TEMPLATES = [
    ("best", "Who are the best custom home builders in {area}?", "custom home builders {kw}"),
    ("new", "Where can I find new construction homes in {area}?", "new construction homes {kw}"),
    ("lot", "Which builders in {area} will build a custom home on my own land?", "build on your lot {kw}"),
]
CITY_KEYWORD = "custom home builders {kw}"


def location_templates(biz=None):
    """change 53: the per-city set for this kind of business."""
    if not biz or biz.get("kind") != "service":
        return LOCATION_TEMPLATES
    S = biz.get("services") or []
    nouns = biz.get("nouns") or "companies"
    w0 = _pros(S[0]) if S else None
    w1 = _pros(S[1]) if len(S) > 1 else None
    out = [("best", f"Who are the best {w0 or nouns} in {{area}}?", f"{w0 or nouns} {{kw}}")]
    if w1:
        out.append(("svc2", f"Who are the best {w1} in {{area}}?", f"{w1} {{kw}}"))
    elif len(S) > 1:
        out.append(("svc2", f"Which companies are best for {S[1]['name'].lower()} in {{area}}?", f"{S[1]['name'].lower()} {{kw}}"))
    out.append(("top", f"What are the top-rated {nouns} in {{area}}?", f"{nouns} {{kw}}"))
    return out


def city_keyword(biz=None):
    return location_templates(biz)[0][2]


def loc_slug(loc):
    return re.sub(r"[^a-z0-9]+", "_", loc["label"].lower()).strip("_")


def loc_kw(loc):
    return (f"{loc['name']} {loc['city']}" if loc.get("kind") == "neighbourhood" else f"{loc['name']} {loc['state_code']}").lower()


def for_location(loc, n=3, biz=None):
    from .locations import market_for
    return [{"id": f"loc_{loc_slug(loc)}_{tid}", "prompt": p.format(area=loc["label"]), "keyword": k.format(kw=loc_kw(loc)).lower(),
             "kind": "discovery", "area": loc["label"], "loc": market_for(loc)} for tid, p, k in location_templates(biz)[:n]]
