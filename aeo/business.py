"""change 53: what kind of business an audit is for, and which services it sells, read from its website.

Home builders keep the original, tested question set. Any other business (a bathroom and basement remodeler, a
roofer, a pool builder ...) gets questions built from its own services, so the score measures the market it is in.

profile["business"] = {
  "kind": "home_builder" | "service",
  "label": "home remodeling company",          # what the business is
  "noun": "remodeler", "nouns": "remodelers",  # how answers and the report refer to one / many of them
  "customers": "homeowners",                   # who asks the questions
  "services": [{"name": "Bathroom remodeling", "pros": "bathroom remodelers", "added": False}, ...],
  "source": "website" | "website keywords" | "default",
}
"""
import json
import re

from . import llm

BUILDER_WORDS = {  # the builder service keys (site.SERVICE_WORDS) that shape the builder questions, as the picker shows them
    "custom homes": "Custom homes", "new construction": "New construction", "luxury homes": "Luxury homes",
    "build on your lot": "Build on your lot", "ranch homes": "Ranch homes", "condos": "Condos",
}
BUILDER_SIGNAL = re.compile(r"home ?builders?|custom homes?|new (?:construction|homes)|floor ?plans|build(?:ing)? on your (?:own )?(?:lot|land)|"
                            r"semi-custom|move-in ready|quick move-in", re.I)

# fallback when no AI helper is connected: common home-service trades (regex, service name, what people call the pros)
TRADES = [
    (r"bath(?:room)?s? (?:remodel|renovat)|bathroom (?:design|makeover)", "Bathroom remodeling", "bathroom remodelers"),
    (r"basement (?:finish|remodel|renovat)|finished basements?", "Basement finishing", "basement finishing companies"),
    (r"kitchen (?:remodel|renovat)|kitchen (?:design|makeover)", "Kitchen remodeling", "kitchen remodelers"),
    (r"home additions?|room additions?|second.story additions?", "Home additions", "home addition contractors"),
    (r"whole.home (?:remodel|renovat)|home (?:remodel|renovat)|remodel(?:ing|ers?)\b|renovations?\b", "Home remodeling", "remodeling contractors"),
    (r"\broof(?:ing|ers?)\b|roof (?:repair|replacement)", "Roofing", "roofers"),
    (r"\bhvac\b|air condition|furnace|heat pump", "Heating and air conditioning", "HVAC companies"),
    (r"\bplumb(?:ing|ers?)\b", "Plumbing", "plumbers"),
    (r"\belectricians?\b|electrical (?:contractor|services|repair)", "Electrical work", "electricians"),
    (r"landscap(?:ing|ers?)|hardscap", "Landscaping", "landscapers"),
    (r"(?:custom |inground |swimming )pools?|pool (?:build|construct|install)", "Pool installation", "pool builders"),
    (r"window (?:replacement|install)|replacement windows", "Window replacement", "window replacement companies"),
    (r"\bsiding\b", "Siding", "siding contractors"),
    (r"(?:interior|exterior|house) painting|\bpainters?\b", "Painting", "painters"),
    (r"\bflooring\b|hardwood floors?", "Flooring", "flooring installers"),
    (r"\bdecks?\b(?: build| install|ing)|deck builders?|screened porch", "Decks and porches", "deck builders"),
    (r"solar (?:panel|install|energy)", "Solar installation", "solar installers"),
    (r"concrete (?:contractor|driveway|patio)|\bpaving\b", "Concrete work", "concrete contractors"),
    (r"cabinet(?:s|ry) (?:install|refacing|makers?)|custom cabinets", "Custom cabinets", "cabinet makers"),
]
BUILDER_DEFAULT = {"kind": "home_builder", "label": "home builder", "noun": "builder", "nouns": "builders",
                   "customers": "home buyers", "services": [], "source": "default"}


def _clean(s, n=60):
    return re.sub(r"\s+", " ", str(s or "")).strip(" .,;:-")[:n]


def _plural_to_singular(p):
    p = p.strip()
    if p.endswith("companies"):
        return p[:-9] + "company"
    if p.endswith("ies"):
        return p[:-3] + "y"
    return p[:-1] if p.endswith("s") and not p.endswith("ss") else p


def _builder(profile, source):
    b = dict(BUILDER_DEFAULT, source=source)
    b["services"] = [{"name": BUILDER_WORDS[k], "pros": None, "key": k, "added": False}
                     for k in (profile.get("services") or []) if k in BUILDER_WORDS]
    return b


def _clue(profile):
    return {"name": profile.get("name"), "title": profile.get("title"), "description": profile.get("meta_description"),
            "headings": (profile.get("headings") or [])[:30], "menu": (profile.get("nav_links") or [])[:40],
            "schema_types": profile.get("schema_types") or [], "text": (profile.get("opening_text") or "")[:1800]}


PROMPT = """Read this business's website details and say what it sells. Reply with JSON only, no other words:
{"is_home_builder": true or false,
 "business": "what the business is, short and lowercase, e.g. 'home remodeling company'",
 "noun": "what one such business is called, singular and lowercase, e.g. 'remodeler'",
 "nouns": "the plural, e.g. 'remodelers'",
 "customers": "who buys from them, plural and lowercase, e.g. 'homeowners'",
 "services": [{"name": "Bathroom remodeling", "pros": "bathroom remodelers"}]}
Rules: is_home_builder is true only when the main business is building new homes (custom, semi-custom or production
builders); a remodeler, contractor or other trade is false. List the 3 to 6 main paid services they offer, named the way
a customer would search for them. Put the broad, most-searched services first and narrower ones after (for example
"Bathroom remodeling" before "Tub-to-shower conversion"), and don't list two names for the same service. "pros" is the
plural a customer would type to find businesses that do that service, like "bathroom remodelers", "roofers" or
"basement finishing companies". If the business serves one specific industry or kind of client (for example an agency
that only works with home builders, or a firm that only serves dentists), name every service and "pros" for that
audience, like {"name": "SEO for home builders", "pros": "home builder SEO agencies"}, and say so in "business"
(e.g. "marketing agency for home builders").
Website details (JSON):
"""


def _from_llm(profile):
    for provider in ("gemini", "chatgpt"):
        if not llm.available(provider):
            continue
        try:
            d, _ = llm.json_answer(PROMPT + json.dumps(_clue(profile)), provider, timeout=45)
        except Exception:
            continue
        if not isinstance(d, dict):
            continue
        if d.get("is_home_builder"):
            return _builder(profile, "website")
        services, seen = [], set()
        for s in d.get("services") or []:
            if not isinstance(s, dict):
                continue
            name = _clean(s.get("name"))
            if name and name.lower() not in seen:
                seen.add(name.lower())
                pros = _clean(s.get("pros"), 50).lower() or None
                services.append({"name": name[0].upper() + name[1:], "pros": pros, "added": False})
        nouns = _clean(d.get("nouns"), 40).lower()
        noun = _clean(d.get("noun"), 40).lower() or (_plural_to_singular(nouns) if nouns else "")
        if not services or not noun:
            continue
        return {"kind": "service", "label": _clean(d.get("business"), 60).lower() or noun, "noun": noun,
                "nouns": nouns or noun + "s", "customers": _clean(d.get("customers"), 40).lower() or "customers",
                "services": services[:6], "source": "website"}
    return None


def _from_keywords(profile):
    """No AI helper: decide from the words on the site."""
    head = " ".join(str(profile.get(k) or "") for k in ("name", "title", "meta_description"))
    body = head + " " + " ".join(profile.get("headings") or []) + " " + " ".join(profile.get("nav_links") or []) \
        + " " + (profile.get("opening_text") or "")
    trades = []
    for rx, name, pros in TRADES:
        if re.search(rx, body, re.I) and name not in [t[0] for t in trades]:
            trades.append((name, pros, bool(re.search(rx, head, re.I))))
    builder_head = bool(BUILDER_SIGNAL.search(head))
    trade_head = any(t[2] for t in trades)
    if builder_head or not trades or (BUILDER_SIGNAL.search(body) and not trade_head):
        return _builder(profile, "website keywords" if (builder_head or BUILDER_SIGNAL.search(body)) else "default")
    trades.sort(key=lambda t: not t[2])  # services named in the title or description first
    specific = [t for t in trades if t[0] != "Home remodeling"] or trades
    remodel = any("remodel" in t[0].lower() or t[0] in ("Basement finishing", "Home additions") for t in trades)
    if remodel:
        noun, nouns, label = "remodeler", "remodelers", "home remodeling company"
    else:
        nouns = trades[0][1]
        noun, label = _plural_to_singular(nouns), _plural_to_singular(nouns) + " business"
    return {"kind": "service", "label": label, "noun": noun, "nouns": nouns, "customers": "homeowners",
            "services": [{"name": n, "pros": p, "added": False} for n, p, _ in specific[:6]], "source": "website keywords"}


def detect(profile):
    """Read the business type and services from a site profile (aeo.site.read_site). Never raises."""
    if profile.get("site_blocked") or (profile.get("word_count") or 0) < 30:
        return dict(_builder(profile, "default"))
    try:
        return _from_llm(profile) or _from_keywords(profile)
    except Exception:
        return _builder(profile, "default")


def _cache_key(domain):
    return "biz_" + re.sub(r"[^a-z0-9.-]+", "_", (domain or "").lower())[:80]


def cached(profile, domain=None, max_age=7 * 86400, fresh=False):
    """detect(), remembered per domain for a week, so the Services dropdown and the audit that follows agree."""
    import time
    from . import kv
    key = _cache_key(domain or profile.get("domain"))
    if not fresh:
        try:
            hit = kv.get(key)
            if hit and time.time() - hit.get("at", 0) < max_age and hit.get("biz"):
                return hit["biz"]
        except Exception:
            pass
    biz = detect(profile)
    if biz.get("source") != "default":
        try:
            kv.update(key, lambda _: {"at": time.time(), "biz": biz}, default={})
        except Exception:
            pass
    return biz


def apply_choice(biz, chosen):
    """The salesperson's picks from the Services dropdown: keep the ticked ones (in their order) and add typed ones."""
    if chosen is None:
        return biz
    biz = dict(biz)
    by_name = {s["name"].lower(): s for s in biz.get("services") or []}
    out, seen = [], set()
    for raw in chosen:
        name = _clean(raw)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        s = by_name.get(name.lower())
        if s:
            out.append(dict(s))
        else:
            key = next((k for k, v in BUILDER_WORDS.items() if v.lower() == name.lower() or k == name.lower()), None)
            out.append({"name": name[0].upper() + name[1:], "pros": None, "added": True, **({"key": key} if key else {})})
    biz["services"] = out[:8]
    biz["chosen"] = True
    if biz.get("kind") == "home_builder" and biz.get("source") == "default" and out and not any(x.get("key") for x in out):
        # the site couldn't be read, but the picks are clearly not home building: ask about those services
        biz.update(kind="service", label="local business", noun="company", nouns="companies", customers="customers")
    return biz


def of(obj):
    """The business record of an audit (A), a profile, or a business dict; older audits are home builders."""
    if not obj:
        return dict(BUILDER_DEFAULT)
    if "kind" in obj and "nouns" in obj:
        return obj
    p = obj.get("profile") if isinstance(obj.get("profile"), dict) else obj
    return (p or {}).get("business") or dict(BUILDER_DEFAULT)


def words(obj):
    """Wording for reports and screens: {{ W.noun }} = 'builder' or 'remodeler', etc."""
    b = of(obj)
    label = b.get("label") or b["noun"]
    return {"kind": b["kind"], "is_builder": b["kind"] == "home_builder", "noun": b["noun"], "nouns": b["nouns"],
            "Noun": b["noun"][:1].upper() + b["noun"][1:], "Nouns": b["nouns"][:1].upper() + b["nouns"][1:],
            "customers": b.get("customers") or "customers", "label": label,
            "a_label": ("an " if label[:1].lower() in "aeiou" else "a ") + label,
            "services": [s["name"] for s in b.get("services") or []]}


def service_phrase(b, n=3):
    """'bathroom remodeling, basement finishing and kitchen remodeling' for prose."""
    names = [s["name"].lower() for s in (b.get("services") or [])[:n]]
    return names[0] if len(names) == 1 else (", ".join(names[:-1]) + " and " + names[-1]) if names else b.get("label", "")
