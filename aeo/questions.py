"""Build the local question set for a builder.

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
    brand = short_brand(profile["name"])
    vals = {"city": market["city"], "st": market["state_code"], "state": market["state"], "brand": brand}
    qs = []
    for qid, p, k, need, kind in TEMPLATES:
        if need and need not in profile["services"]:
            continue
        qs.append({"id": qid, "prompt": p.format(**vals), "keyword": k.format(**vals).lower(), "kind": kind})
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
        qs.insert(3, {"id": AREA_TEMPLATE[0].format(slug=slug), "prompt": AREA_TEMPLATE[1].format(area=a_full),
                      "keyword": AREA_TEMPLATE[2].format(area=(f"{a} {parent}" if parent else a + (" " + market["state"] if len(a.split()) == 1 else "")).lower()),
                      "kind": "discovery"})
    return qs[:max_questions]


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


def loc_slug(loc):
    return re.sub(r"[^a-z0-9]+", "_", loc["label"].lower()).strip("_")


def loc_kw(loc):
    return (f"{loc['name']} {loc['city']}" if loc.get("kind") == "neighbourhood" else f"{loc['name']} {loc['state_code']}").lower()


def for_location(loc, n=3):
    from .locations import market_for
    return [{"id": f"loc_{loc_slug(loc)}_{tid}", "prompt": p.format(area=loc["label"]), "keyword": k.format(kw=loc_kw(loc)),
             "kind": "discovery", "area": loc["label"], "loc": market_for(loc)} for tid, p, k in LOCATION_TEMPLATES[:n]]
