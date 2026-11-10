"""Where a builder builds: service locations found on the website, checked to be real places near the main market.

Sources: areaServed markup, community / area pages (links and sitemap URLs), "Serving X, Y and Z" phrases,
the Google Business Profile address. Gemini checks the list (real US place, near the main market, proper name);
without Gemini, only markup and page-slug places in the main market's state are kept.
"""
import re
import urllib.parse

from .engines import STATE_CODES

MAX_CANDIDATES = 24
SLUG_RX = re.compile(r"/(?:communities|community|neighborhoods?|areas?|service-areas?|areas-we-serve|locations?|where-we-build|cities)/([a-z0-9-]+)/?$", re.I)
NOT_PLACES = {"all", "index", "page", "map", "available", "sold", "coming-soon", "floor-plans", "models", "homes", "lots", "gallery",
              "about", "contact", "blog", "faq", "new", "featured", "current", "past", "our"}
STATE_NAMES = {v: k for k, v in STATE_CODES.items()}


def _title(slug):
    words = [w for w in slug.replace("_", "-").split("-") if w]
    if words and words[-1].lower() in {c.lower() for c in STATE_NAMES} and len(words) > 1:
        words = words[:-1]  # "loveland-oh" -> "Loveland"
    return " ".join(w.capitalize() for w in words)


def candidates(profile, market):
    """Raw place names with where each was found, main market first."""
    out = {}

    def add(name, source, st=None):
        name = re.sub(r"^(?:greater|the)\s+", "", (name or "").strip(" ,.;:-"), flags=re.I)
        name = re.sub(r"\s+(?:area|metro|region|county)$", "", name, flags=re.I)
        m = re.match(r"(.+?),\s*([A-Z]{2})$", name)
        if m and m.group(2) in STATE_NAMES:
            name, st = m.group(1).strip(), m.group(2)
        if not name or len(name) > 40 or not re.search(r"[A-Za-z]", name) or name.lower() in NOT_PLACES:
            return
        k = name.lower()
        if k not in out:
            out[k] = {"name": name, "state_code": st, "sources": []}
        if source not in out[k]["sources"]:
            out[k]["sources"].append(source)
        out[k]["state_code"] = out[k]["state_code"] or st

    add(market["city"], "main market", market["state_code"])
    for a in profile.get("areas_served") or []:
        parts = [x.strip() for x in (a or "").split(",")]
        if len(parts) == 3 and len(parts[2]) == 2:  # "Myers Park, Charlotte, NC"
            add(f"{parts[0]}, {parts[1]}", "website markup", parts[2])
        else:
            add(a, "website markup")
    for path, text in profile.get("location_links") or []:
        m = SLUG_RX.search(path)
        if m:
            add(text if (text and len(text) < 40 and not re.search(r"(?i)view|learn|more|home|click", text)) else _title(m.group(1)), "community pages")
    for u in profile.get("sitemap_urls") or []:
        m = SLUG_RX.search(urllib.parse.urlparse(u).path)
        if m:
            add(_title(m.group(1)), "community pages")
    for ph in profile.get("area_phrases") or []:
        body = re.sub(r"^.*?(?:serving|serve|building in|build in|homes in|communities in|service areas?:?|areas we serve:?|where we build:?)\s+(?:the\s+)?", "", ph, flags=re.I)
        for piece in re.split(r",\s*|\s+and\s+|\s*&\s*|\s+or\s+", body):
            if re.match(r"^[A-Z]", piece.strip()):
                add(piece, "homepage text")
    gbp = profile.get("gbp") or {}
    m = re.search(r",\s*([A-Za-z .'-]+),\s*([A-Z]{2})\b", gbp.get("address") or "")
    if m:
        add(m.group(1), "Google Business Profile", m.group(2))
    return list(out.values())[:MAX_CANDIDATES]


def validate(cands, market, provider="gemini"):
    """Keep real places near the main market, with a proper label. Returns [{label, city, state, state_code, kind, sources}]."""
    from . import llm
    main_label = f"{market['city']}, {market['state_code']}"
    main = {"label": main_label, "name": market["city"], "city": market["city"], "state": market["state"],
            "state_code": market["state_code"], "kind": "city", "main": True, "sources": ["main market"]}
    rest = [c for c in cands if c["name"].lower() != market["city"].lower()]
    if not rest:
        return [main]
    checked = None
    if llm.available(provider):
        prompt = ("A home builder's main market is " + main_label + ". Below are place names found on their website. "
                  "Keep only real US cities, towns, suburbs or neighbourhoods within about 75 miles of the main market. Drop "
                  "community or subdivision brand names, street names, counties, states and anything that is not a place. "
                  "For each kept place give its proper name, the two-letter state, kind (\"city\" or \"neighbourhood\") and, for a "
                  "neighbourhood, the city it is in. Reply with JSON only: "
                  '[{"input": "...", "name": "Indian Hill", "state_code": "OH", "kind": "city", "parent": null}]\n'
                  + "\n".join(f"- {c['name']}" + (f" ({c['state_code']})" if c.get("state_code") else "") for c in rest))
        try:
            checked, _ = llm.json_answer(prompt, provider, timeout=15)  # change 29: fall back to the site's own list if slow
        except Exception:
            checked = None
    out, seen = [main], {market["city"].lower()}
    if isinstance(checked, list):
        by_input = {c["name"].lower(): c for c in rest}
        for j in checked:
            name, st = (j.get("name") or "").strip(), (j.get("state_code") or "").upper()
            if not name or st not in STATE_NAMES or name.lower() in seen:
                continue
            seen.add(name.lower())
            src = (by_input.get((j.get("input") or "").lower()) or by_input.get(name.lower()) or {}).get("sources") or ["website"]
            parent = (j.get("parent") or "").strip() or None
            kind = "neighbourhood" if j.get("kind") == "neighbourhood" and parent else "city"
            out.append({"label": f"{name}, {parent}" if kind == "neighbourhood" else f"{name}, {st}", "name": name,
                        "city": parent if kind == "neighbourhood" else name, "state": STATE_NAMES[st], "state_code": st,
                        "kind": kind, "main": False, "sources": src})
    else:  # no checker: keep markup and page-slug places, assumed in the main market's state
        for c in rest:
            if not ({"website markup", "community pages"} & set(c["sources"])) or c["name"].lower() in seen:
                continue
            st = c.get("state_code") or market["state_code"]
            if st not in STATE_NAMES:
                continue
            seen.add(c["name"].lower())
            out.append({"label": f"{c['name']}, {st}", "name": c["name"], "city": c["name"], "state": STATE_NAMES[st],
                        "state_code": st, "kind": "city", "main": False, "sources": c["sources"]})
    return out[:16]


def find(profile, market):
    return validate(candidates(profile, market), market)


def market_for(loc):
    """The location an AI platform should search from for this place (a neighbourhood searches from its city)."""
    return {"city": loc["city"], "state": loc["state"], "state_code": loc["state_code"]}
