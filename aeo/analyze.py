"""Turn raw answers into the audit: mentions, competitors, cited sources, scores and recommendations.
Deterministic rules only, so every number in the report traces back to a stored answer."""
import collections
import re

from . import business, scope
from .engines import ENGINES, ENGINE_LABELS, domain_of

PLATFORMS = {
    "google.com": "Google (Maps / Business Profile)", "maps.google.com": "Google (Maps / Business Profile)",
    "youtube.com": "YouTube", "facebook.com": "Facebook", "instagram.com": "Instagram", "yelp.com": "Yelp",
    "houzz.com": "Houzz", "reddit.com": "Reddit", "zillow.com": "Zillow", "newhomesource.com": "NewHomeSource",
    "linkedin.com": "LinkedIn", "trulia.com": "Trulia", "realtor.com": "Realtor.com", "redfin.com": "Redfin",
    "bbb.org": "BBB", "angi.com": "Angi", "homeadvisor.com": "HomeAdvisor", "thumbtack.com": "Thumbtack",
    "nahb.org": "NAHB", "guildquality.com": "GuildQuality", "buildzoom.com": "BuildZoom", "nextdoor.com": "Nextdoor",
    "wikipedia.org": "Wikipedia", "expertise.com": "Expertise.com", "threebestrated.com": "Three Best Rated",
}
SOURCE_GROUP = {  # how a builder would recognise the source type
    "Google (Maps / Business Profile)": "Google listings", "YouTube": "Video", "Facebook": "Social", "Instagram": "Social",
    "LinkedIn": "Social", "Nextdoor": "Social", "Reddit": "Forums", "Yelp": "Review sites", "BBB": "Review sites",
    "GuildQuality": "Review sites", "Angi": "Review sites", "HomeAdvisor": "Review sites", "Thumbtack": "Review sites",
    "Houzz": "Home directories", "NewHomeSource": "Home directories", "Zillow": "Real estate sites", "Trulia": "Real estate sites",
    "Realtor.com": "Real estate sites", "Redfin": "Real estate sites", "Expertise.com": "Best-of lists",
    "Three Best Rated": "Best-of lists", "BuildZoom": "Home directories", "NAHB": "Industry", "Wikipedia": "Reference",
}
BUILDER_WORDS = r"(?:Custom\s+Homes|Homes|Home\s+Builders|Builders|Builder|Building(?:\s+Company|\s+Co\.?)?|Construction|Homebuilders|Communities|Design[\s-]Build|Group|Company|Contractors)"
# change 53: the endings of other trades' business names ("Russell Room Remodelers", "Apex Roofing", "Kitchen & Bath Pros")
TRADE_WORDS = (r"(?:Kitchen\s+(?:&|and)\s+Baths?|Bath\s+(?:&|and)\s+Kitchens?|Remodel(?:ing|ers?)|Renovations?|Restoration|Roofing|Roofers|Plumbing|Heating(?:\s+(?:&|and)\s+(?:Air|Cooling))?|HVAC|"
               r"Air|Electric(?:al)?|Landscaping|Landscapes|Pools?(?:\s+(?:&|and)\s+Spas?)?|Windows|Exteriors|Interiors|Design(?:s)?|"
               r"Services|Solutions|Contracting|Pros|Painting|Painters|Flooring|Floors|Basements?|Baths?|Kitchens?|Cabinets|Cabinetry|"
               r"Improvements?|Home\s+Improvement|Builds|Craftsmen|Carpentry|Concrete|Paving|Decks?|Solar|Inc|LLC)")
ALL_WORDS = r"(?:" + BUILDER_WORDS[3:-1] + "|" + TRADE_WORDS[3:-1] + r")"


def name_rx(words=BUILDER_WORDS):
    return re.compile(r"\b((?:[A-Z][\w'’.&-]*|[A-Z]\.)(?:\s+(?:[A-Z][\w'’.&-]*|&|of|and|[A-Z]\.)){0,4}?\s+" + words + r")\b")


NAME_RX = name_rx()
GENERIC_WORDS = ["Best", "Top", "Custom", "New", "Luxury", "Local", "Ranch", "Home", "Homes", "Many", "Some", "Other", "These",
                 "Most", "Several", "Quality", "Top-Rated", "Top Rated", "Rated", "National", "Regional", "Production",
                 "Semi-Custom", "Area", "Search", "Google", "Find", "Notable", "Popular", "Reputable", "Established",
                 "Experienced", "Independent", "Smaller", "Larger", "Boutique", "Greater \\w+", "Northern \\w+", "Southern \\w+",
                 "Bathroom", "Kitchen", "Basement", "Complete", "Full", "Whole", "Interior", "Exterior", "Residential", "Commercial",
                 "Affordable", "Professional", "Licensed", "General", "Trusted", "Certified", "Emergency", "Same-Day", "Your", "Our"]


LEAD_WORDS = {"the", "a", "an", "for", "with", "and", "or", "in", "at", "consider", "try", "contact", "check", "call", "visit",
              "including", "include", "like", "such", "both", "also", "however", "additionally", "options", "notably", "here",
              "some", "many", "see", "ask", "choose", "look", "recommended", "top", "best", "firms", "companies", "builders",
              "while", "if", "when", "as", "other", "local", "reputable", "established", "popular", "well-known", "among", "one",
              "about", "our", "featured", "management", "dedicated", "focused", "exceptional", "luxurious", "years", "true"}


# change 59: words that only describe a business. A "name" made of nothing else is a heading or a phrase from the
# answer ("Expert Austin Custom Home Builders", "Full-Service Design-Build"), not a company.
DESCRIPTORS = set("""best top custom luxury local expert experts high-end highend highly rated top-rated toprated full-service fullservice
premier leading trusted reliable affordable quality professional professionals experienced award-winning awardwinning crafting
reputable established popular notable licensed certified insured family-owned familyowned boutique modern contemporary
the a an and & of for in your our new home homes house houses residential commercial general design build design-build designbuild
builders builder building construction contractor contractors remodel remodeling remodelers remodeler renovation renovations
kitchen kitchens bath baths bathroom bathrooms basement basements finishing roofing roofers plumbing hvac electric electrical
landscaping pools pool services service solutions company companies group pros options choices firms teams team
small medium large semi-custom semicustom production luxury-home luxuryhome specialty specialized specialist specialists
quick-turnaround quickturnaround mid-sized midsized mid-size large-scale small-scale one-stop onestop go-to
exceptional luxurious featured dedicated focused true fully integrated architect-led architectled mid-to-high system-based
beautiful high low right premium elite top-tier toptier upscale high-quality highquality value-focused budget-friendly
builds projects years experience management canadian american national regional independent smaller larger""".split())
# names containing these are organisations, lists or sources, not competing businesses
NOT_A_BUSINESS = re.compile(r"\b(?:Registry|Association|Associations|Council|Board|Department|Directory|Magazine|Guide|Guides|List|Lists|"
                            r"Awards?|Chamber|Institute|Bureau|Ministry|Authority|Reviews?|Ratings?|Forum|Portfolio|Gallery|Projects|Examples)\b", re.I)


def descriptive(name, extra=()):
    """True when every word of the name just describes (no distinctive word), e.g. 'Highly Rated Local Contractors'."""
    words = [re.sub(r"[^a-z&-]", "", w.lower()) for w in re.split(r"[\s/]+", name) if w.strip()]
    words = [w for w in words if w]
    plain = DESCRIPTORS | {w.lower() for w in extra}
    return bool(words) and all(w in plain or w.replace("-", "") in plain for w in words)


def generic_rx(market_words, ends=BUILDER_WORDS):
    words = GENERIC_WORDS + [re.escape(w) for w in market_words if w]
    return re.compile(r"^(?:The\s+)?(?:" + "|".join(words) + r")(?:\s+\w+)?\s+" + ends + r"$", re.I)


def norm_name(n):
    n = re.split(r"\s+[-–—|]\s+", n)[0]  # change 59: a listing's tagline ("Mik Homes - Kitchen and Bath Remodeling")
    n = re.sub(r"[’']s\b", "", n)
    n = re.sub(r"(?:,?\s*\b(?:LLC|Inc|Co|Ltd|Corp)\.?)+$", "", n).strip(" .,")
    n = re.sub(r"\s*(?:&|\band)$", "", n).strip(" .,")  # "J.S. Brown & Co." -> "J.S. Brown"
    return re.sub(r"\s+", " ", n)


def name_key(n):
    """Key for merging variants: 'J&K Custom Homes' == 'JK Custom Homes', 'Robert Lucke Homes' ~ 'Robert Lucke Group'."""
    n = re.sub(r"[^a-z ]", "", norm_name(n).lower().replace("&", ""))
    n = re.sub(r"\b(the|custom|homes?|builders?|building|construction|homebuilders|group|company|co|inc|llc|communities|design build|contractors|"
               r"remodel(?:ing|ers?)|renovations?|services|solutions|pros)\b", " ", n)
    return re.sub(r"\s+", "", n)


def brand_patterns(profile, ends=BUILDER_WORDS):
    name = norm_name(profile["name"])
    core = re.sub(r"\s+" + ends + r"$", "", name, flags=re.I).strip()
    label = profile["domain"].split(".")[0]
    pats = [re.escape(name)]
    if len(core.split()) >= 2 or len(core) >= 6:
        pats.append(re.escape(core) + r"(?:'s)?\s+" + ends)
        pats.append(re.escape(core) + r"\b")
    pats.append(re.escape(label))
    return re.compile("|".join(pats), re.I)


def find_builders(text, known, generic, rx=None, plain=()):
    """Builder (or other business) names in an answer: known candidates (Maps pack titles etc.) plus name-shaped phrases.
    plain: market and service words that, on their own, don't make a name distinctive (change 59)."""
    found = collections.OrderedDict()
    low = text.lower()
    for k in known:
        i = low.find(k.lower())
        if i >= 0:
            found.setdefault(norm_name(k), i)
    for m in (rx or NAME_RX).finditer(text):
        n = norm_name(m.group(1))
        words = n.split()
        cut = max((i for i, w in enumerate(words[:-1]) if w.endswith(".") and len(w) > 2), default=-1)
        words = words[cut + 1:]  # a match that runs across a sentence end ("... Bath. Also Bathroom Remodeling")
        while len(words) > 2 and words[0].lower().strip(",:") in LEAD_WORDS:
            if words[0] == "The" and len(words) == 3:  # keep "The Bold Company"
                break
            words = words[1:]
        while words and words[-1].lower().strip(",") in ("&", "and", "of", "the", "for"):
            words = words[:-1]  # "J.S. Brown &" -> "J.S. Brown"
        n = " ".join(words)
        if generic.match(n) or len(n) < 6 or len(words) < 2 or (words[0].lower() in LEAD_WORDS and words[0] != "The"):
            continue
        if descriptive(n, plain) or NOT_A_BUSINESS.search(n):
            continue
        found.setdefault(n, m.start())
    names = [n for n, _ in sorted(found.items(), key=lambda x: x[1])]
    # "Ash Creek and Seven Custom Homes" when "Seven Custom Homes" is named on its own: two businesses run together
    names = [n for n in names if not any(o != n and re.search(r"\s(?:and|&)\s" + re.escape(o) + r"$", n) for o in names)]
    return dedupe_names(names, known)


def same_business(a, b):
    """'Creative Bath' / 'Creative Bath Renovations', 'Alair Homes' / 'Alair Homes Vancouver', 'Lanefab Design' /
    'Lanefab Design/Build': one name extends the other and they share the opening word."""
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ra, rb = re.sub(r"[^a-z]", "", a.lower()), re.sub(r"[^a-z]", "", b.lower())  # 'JDL Homes' / 'JDL Homes Vancouver'
    rs, rl = (ra, rb) if len(ra) <= len(rb) else (rb, ra)
    if len(rs) >= 7 and rl.startswith(rs) and a.split()[0].lower() == b.split()[0].lower():
        return True
    fa, fb = re.sub(r"[^a-z]", "", a.split()[0].lower()), re.sub(r"[^a-z]", "", b.split()[0].lower())
    short, long_ = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    return fa == fb and len(short) >= 5 and long_.startswith(short)


def dedupe_names(names, known=()):
    """Keep one entry per business, preferring a known listing title, else the longer, fuller name."""
    known_l = {k.lower() for k in known}
    out = []
    for n in names:
        hit = next((i for i, o in enumerate(out) if same_business(n, o)), None)
        if hit is None:
            out.append(n)
        else:
            o = out[hit]
            if (n.lower() in known_l and o.lower() not in known_l) or (len(n) > len(o) and o.lower() not in known_l):
                out[hit] = n
    return out


def analyze(profile, market, questions, answers, checks, semrush):
    W = business.words(profile)
    ends = BUILDER_WORDS if W["is_builder"] else ALL_WORDS  # change 53: other trades' names end differently
    rx = NAME_RX if W["is_builder"] else name_rx(ends)
    brand_rx = brand_patterns(profile, ends)
    me = profile["domain"]
    sc = profile.get("scope") or scope.make(me, "root")
    mine = lambda u: bool(u) and scope.matches(u, sc)  # noqa: E731  (cited pages must sit inside the chosen scope)
    on_site = lambda d: bool(d) and scope.matches(d, scope.make(sc["domain"], "root"))  # noqa: E731  (Maps listings link to the site)
    qmap = {q["id"]: q for q in questions}
    generic = generic_rx([market["city"], market["state"], market["state_code"]] +
                         [w for a in profile["areas_served"] + [q.get("area") or "" for q in questions] + W["services"]
                          for w in re.findall(r"[A-Z][a-z]+", a or "")], ends)
    plain = {w.lower() for w in [market["city"], market["state"], market["state_code"]] + [q.get("area") or "" for q in questions]
             + W["services"] + profile["areas_served"] for w in re.findall(r"[A-Za-z][A-Za-z'-]+", w or "")}
    known = sorted({p["title"] for a in answers for p in (a.get("local_pack") or []) + (a.get("cards") or [])
                    if p.get("title") and "." not in p["title"]}, key=len, reverse=True)

    cells = []  # one per (question, engine)
    comp = collections.defaultdict(lambda: {"mentions": 0, "engines": set(), "questions": set(), "pack": 0, "domain": ""})
    src_domains = collections.Counter()
    for a in answers:
        q = qmap[a["question_id"]]
        cell = {"engine": a["engine"], "question_id": q["id"], "status": a["status"], "mentioned": None, "cited": None,
                "rank": None, "competitors": [], "in_pack": None, "organic_rank": None, "n_citations": len(a["citations"]),
                "error": a.get("error")}
        if a["status"] == "ok":
            text = a["text"] or ""
            cited_domains = [c["domain"] for c in a["citations"]]
            cell["cited"] = any(mine(c.get("url") or c.get("domain")) for c in a["citations"])
            m = brand_rx.search(text)
            cards = a.get("cards") or []  # change 59: the business cards chatgpt.com shows with its answer
            in_cards = any(brand_rx.search(c.get("title") or "") or on_site(c.get("domain")) for c in cards)
            cell["mentioned"] = bool(m) or cell["cited"] or in_cards
            builders = [b for b in find_builders(text, known, generic, rx, plain) if not brand_rx.search(b)]
            have = {name_key(b) for b in builders}
            for c in cards:  # card businesses the text didn't name are still recommended to the reader
                t = norm_name(c.get("title") or "")
                if t and not brand_rx.search(t) and not on_site(c.get("domain")) and name_key(t) not in have:
                    builders.append(t); have.add(name_key(t))
            if m:
                cell["rank"] = 1 + sum(1 for b in builders if text.find(b) < m.start())
            cell["competitors"] = builders[:12]
            for b in builders[:12]:
                comp[b]["mentions"] += 1; comp[b]["engines"].add(a["engine"]); comp[b]["questions"].add(q["id"])
            for d in set(cited_domains):
                src_domains[d] += 1
        if a["engine"] == "google_aio" and a["status"] in ("ok", "no_answer"):
            cell["in_pack"] = any(on_site(p.get("domain")) or brand_rx.search(p.get("title", "")) for p in a["local_pack"])
            cell["organic_rank"] = next((o["rank"] for o in a["organic"] if mine(o.get("url") or o.get("domain"))), None)
            for p in a["local_pack"]:
                if p.get("title") and not brand_rx.search(p["title"]):
                    n = norm_name(p["title"]); comp[n]["pack"] += 1; comp[n]["domain"] = p.get("domain", "")
        cells.append(cell)

    # ---- scores ----
    def counts(c):  # brand questions ("Is X a good builder?") always name X, so they don't count toward visibility
        return c["status"] == "ok" and qmap[c["question_id"]].get("kind") != "brand"

    def score(cs):
        tot = sum(qmap[c["question_id"]]["weight"] for c in cs if counts(c))
        hit = sum(qmap[c["question_id"]]["weight"] for c in cs if counts(c) and c["mentioned"])
        return None if not tot else round(100 * hit / tot)
    per_engine = {}
    for e in ENGINES:
        cs = [c for c in cells if c["engine"] == e]
        ok = [c for c in cs if counts(c)]
        measured = any(c["status"] in ("ok", "no_answer") for c in cs)
        per_engine[e] = {"label": ENGINE_LABELS[e], "measured": measured, "answers": len(ok),
                         "mentioned": sum(1 for c in ok if c["mentioned"]), "cited": sum(1 for c in ok if c["cited"]),
                         "no_answer": sum(1 for c in cs if c["status"] == "no_answer"),
                         "errors": sum(1 for c in cs if c["status"] == "error"), "score": score(cs)}
    overall = score(cells)
    ok_cells = [c for c in cells if counts(c)]
    google = [c for c in cells if c["engine"] == "google_aio" and c["in_pack"] is not None]

    # merge spelling variants and attach Maps-pack domains (Semrush gives domains) to the named builder
    merged = {}
    for n, c in sorted(comp.items(), key=lambda kv: -kv[1]["mentions"]):
        k = name_key(n) or n
        k = next((mk for mk, mv in merged.items() if same_business(n, mv["name"])), k)  # change 59: name variants
        if "." in n:  # a pack entry that is only a domain
            label = n.split(".")[0].replace("-", "")
            k = next((name_key(m) for m in comp if "." not in m and name_key(m) and name_key(m) in label), k)
        if k in merged:
            dst = merged[k]
            dst["mentions"] += c["mentions"]; dst["pack"] += c["pack"]; dst["engines"] |= c["engines"]; dst["questions"] |= c["questions"]
            dst["domain"] = dst["domain"] or c["domain"]
        else:
            merged[k] = {"name": n, "mentions": c["mentions"], "pack": c["pack"], "engines": set(c["engines"]),
                         "questions": set(c["questions"]), "domain": c["domain"]}
    comp = {v["name"]: v for v in merged.values() if not v["name"].replace(".", "").isdigit()}
    for v in comp.values():  # show a real name when the only label was a domain
        if "." in v["name"] and v["mentions"] == 0:
            v["name"] = v["name"].split(".")[0]
    builders = []
    for n, c in comp.items():
        pts = c["mentions"] * 2 + c["pack"]
        builders.append({"name": c["name"], "mentions": c["mentions"], "engines": len(c["engines"]), "pack": c["pack"],
                         "domain": c["domain"], "score": pts})
    me_row = {"name": profile["name"], "mentions": sum(1 for c in ok_cells if c["mentioned"]),
              "engines": sum(1 for e in per_engine.values() if e["mentioned"]),
              "pack": sum(1 for c in google if c["in_pack"]), "prospect": True}
    me_row["score"] = me_row["mentions"] * 2 + me_row["pack"]
    builders = sorted(builders, key=lambda b: (-b["score"], b["name"]))[:7]
    leaderboard = sorted(builders + [me_row], key=lambda b: (-b["score"], b["name"]))

    groups = collections.Counter()
    for d, n in src_domains.items():
        label = (f"This {W['noun']}'s website" if d == me else
                 SOURCE_GROUP.get(PLATFORMS.get(d, ""), f"Other {W['nouns']}' and local sites"))
        groups[label] += n

    kpis = {
        "score": overall, "answers": len(ok_cells), "mentioned": me_row["mentions"],
        "engines_measured": sum(1 for e in per_engine.values() if e["measured"]),
        "engines_named": me_row["engines"], "questions": len(questions),
        "pack": me_row["pack"] if google else None, "pack_of": len(google) if google else None,
        "top3": sum(1 for c in google if c["organic_rank"] and c["organic_rank"] <= 3) if google else None,
        "checks_passed": sum(1 for c in checks if c[0] == "good"), "checks_total": len(checks),
    }
    return {"cells": cells, "per_engine": per_engine, "kpis": kpis, "leaderboard": leaderboard,
            "sources": [{"group": g, "count": n} for g, n in groups.most_common()],
            "source_domains": [{"domain": d, "label": PLATFORMS.get(d, d), "count": n} for d, n in src_domains.most_common(15)],
            "recommendations": recommend(profile, market, questions, cells, checks, src_domains, leaderboard, per_engine)}


def competitor_domains(analysis, answers, me):
    """Find website domains for the top-named competitors by matching their names to cited domains."""
    root = scope.root_of(me)
    cited = {c["domain"] for a in answers for c in a.get("citations", []) if c.get("domain") and scope.root_of(c["domain"]) != root}
    out = []
    for b in analysis["leaderboard"]:
        if b.get("prospect") or b.get("domain"):
            continue
        k = name_key(b["name"])
        hit = next((d for d in sorted(cited) if k and len(k) >= 4 and k in d.replace("-", "").replace(".", "")), None)
        if hit and hit not in PLATFORMS:
            out.append(hit)
    return out


def recommend(profile, market, questions, cells, checks, src_domains, leaderboard, per_engine):
    recs = []
    W = business.words(profile)
    B = W["is_builder"]
    status = {c[1]: c for c in checks}
    top = [b for b in leaderboard if not b.get("prospect")][:3]
    top_names = ", ".join(b["name"] for b in top) or "other " + W["nouns"]
    google = [c for c in cells if c["engine"] == "google_aio" and c["in_pack"] is not None]
    if google and sum(1 for c in google if c["in_pack"]) <= len(google) // 3:
        pack_leaders = sorted([b for b in leaderboard if b["pack"] and not b.get("prospect")], key=lambda b: -b["pack"])[:3]
        recs.append((10, "Get into the Google Maps 3-pack",
            f"{profile['name']} appeared in the Maps 3-pack for {sum(1 for c in google if c['in_pack'])} of {len(google)} local searches. "
            + (f"{', '.join(b['name'] + ' (' + str(b['pack']) + ')' for b in pack_leaders)} took those spots. " if pack_leaders else "")
            + "AI answers lean on Google Business Profile listings and reviews.",
            "Rebuild the Google Business Profile: primary category, service areas, weekly photos and posts, Q&A, and a steady review request flow "
            + ("after closings." if B else "after each finished project.")))
    g = profile.get("gbp")
    if g and g.get("reviews") is not None:
        low = sum(v for k, v in (g.get("distribution") or {}).items() if int(k) <= 2)
        if (g.get("rating") or 0) < 4.6 or (g.get("reviews") or 0) < 50 or (g["reviews"] and low / g["reviews"] > 0.1):
            recs.append((8.5, "Grow and protect Google reviews",
                f"The Google Business Profile shows {g['rating']}★ from {g['reviews']} reviews"
                + (f", including {low} rated 1 or 2 stars" if low else "") + ". AI answers and the Maps pack lean on review count and rating.",
                ("Ask every closing and walkthrough client" if B else "Ask every client when a project wraps up")
                + " for a review, reply to every review (especially the low ones), and add review markup to the site."))
    if status.get("Phone number consistency", ("good",))[0] == "bad":
        recs.append((9, "Fix the phone number mismatch", status["Phone number consistency"][2],
            "Pick one main number and make the site markup, footer, Google Business Profile, Facebook, Houzz and Yelp all match."))
    if status.get("AI crawler access", ("good",))[0] == "bad":
        recs.append((9, "Let AI tools read the website", status["AI crawler access"][2],
            "Update robots.txt to allow the AI search crawlers while keeping any private areas blocked."))
    area_q = [q for q in questions if q["id"].startswith("area_")]
    measured_areas = [q for q in area_q if any(c["status"] == "ok" for c in cells if c["question_id"] == q["id"])]
    weak_areas = [q for q in measured_areas if not any(c["mentioned"] for c in cells if c["question_id"] == q["id"] and c["status"] == "ok")]
    page_gap = status.get("Service-area pages", ("good",))[0] == "warn"
    if weak_areas or page_gap:
        why = ""
        if weak_areas:
            names = " and ".join(q["prompt"].split(" in ")[-1].rstrip("?") for q in weak_areas)
            why = f"No AI answer about {W['nouns']} in {names} named {profile['name']}. "
        if page_gap:
            why += status["Service-area pages"][2]
        recs.append((8, "Build pages for each service area", why.strip(),
            ("Write one page per area with local communities, lots, price ranges, drive times and past builds"
             if B else "Write one page per area with the services offered there, recent local projects, typical costs and local reviews")
            + ", linked from the homepage and marked up with the area served."))
    if any(status.get(k, ("good",))[0] == "warn" for k in ("FAQ markup", "Review markup", "Answers to common questions")):
        recs.append((7, "Make answers and reviews easy for AI to lift",
            "AI tools quote clear question-and-answer content and trust reviews they can read. Several of these are missing or unmarked on the site.",
            "Add or expand an FAQ (" + ("cost, timeline, lots, financing, warranty" if B else "cost, timeline, the process, permits, warranty")
            + ") with FAQPage markup, and add review and rating markup to testimonials."))
    platform_sources = [d for d, n in src_domains.most_common() if d in PLATFORMS and d != profile["domain"]][:5]
    if platform_sources:
        recs.append((6, "Show up on the sources AI answers trust",
            f"Across these answers, AI tools cited {', '.join(PLATFORMS[d] for d in platform_sources)}. Those sources shape which {W['nouns']} get named.",
            "Claim and complete listings on those sites, collect reviews there, publish " + ("build" if B else "project")
            + " walkthrough videos, and pitch local best-of lists."))
    if status.get("Prices published", ("good",))[0] == "warn":
        recs.append((6, "Publish price ranges", status["Prices published"][2],
            ("Add starting prices or price bands per series or plan" if B else "Add typical price ranges for each service")
            + f" so AI answers to cost questions can include this {W['noun']}."))
    weak_engines = [v["label"] for v in per_engine.values() if v["measured"] and v["answers"] and v["mentioned"] == 0]
    if weak_engines:
        recs.append((5, f"Get named in {', '.join(weak_engines)}",
            f"{', '.join(weak_engines)} did not name {profile['name']} in any answer; {top_names} came up instead.",
            "Publish comparison and best-of content on the site, earn mentions on the cited sources above, and track these platforms monthly."))
    recs.sort(key=lambda r: -r[0])
    return [{"title": t, "why": w, "how": h} for _, t, w, h in recs[:5]]
