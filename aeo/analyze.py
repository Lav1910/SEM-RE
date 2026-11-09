"""Turn raw answers into the audit: mentions, competitors, cited sources, scores and recommendations.
Deterministic rules only, so every number in the report traces back to a stored answer."""
import collections
import re

from . import scope
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
NAME_RX = re.compile(r"\b((?:[A-Z][\w'’.&-]*|[A-Z]\.)(?:\s+(?:[A-Z][\w'’.&-]*|&|of|and|[A-Z]\.)){0,4}?\s+" + BUILDER_WORDS + r")\b")
GENERIC_WORDS = ["Best", "Top", "Custom", "New", "Luxury", "Local", "Ranch", "Home", "Homes", "Many", "Some", "Other", "These",
                 "Most", "Several", "Quality", "Top-Rated", "Top Rated", "Rated", "National", "Regional", "Production",
                 "Semi-Custom", "Area", "Search", "Google", "Find", "Notable", "Popular", "Reputable", "Established",
                 "Experienced", "Independent", "Smaller", "Larger", "Boutique", "Greater \\w+", "Northern \\w+", "Southern \\w+"]


LEAD_WORDS = {"the", "a", "an", "for", "with", "and", "or", "in", "at", "consider", "try", "contact", "check", "call", "visit",
              "including", "include", "like", "such", "both", "also", "however", "additionally", "options", "notably", "here",
              "some", "many", "see", "ask", "choose", "look", "recommended", "top", "best", "firms", "companies", "builders",
              "while", "if", "when", "as", "other", "local", "reputable", "established", "popular", "well-known", "among"}


def generic_rx(market_words):
    words = GENERIC_WORDS + [re.escape(w) for w in market_words if w]
    return re.compile(r"^(?:The\s+)?(?:" + "|".join(words) + r")(?:\s+\w+)?\s+" + BUILDER_WORDS + r"$", re.I)


def norm_name(n):
    n = re.sub(r"[’']s\b", "", n)
    n = re.sub(r"\b(LLC|Inc|Co)\.?$", "", n).strip(" .,")
    return re.sub(r"\s+", " ", n)


def name_key(n):
    """Key for merging variants: 'J&K Custom Homes' == 'JK Custom Homes', 'Robert Lucke Homes' ~ 'Robert Lucke Group'."""
    n = re.sub(r"[^a-z ]", "", norm_name(n).lower().replace("&", ""))
    n = re.sub(r"\b(the|custom|homes?|builders?|building|construction|homebuilders|group|company|co|inc|llc|communities|design build|contractors)\b", " ", n)
    return re.sub(r"\s+", "", n)


def brand_patterns(profile):
    name = norm_name(profile["name"])
    core = re.sub(r"\s+" + BUILDER_WORDS + r"$", "", name, flags=re.I).strip()
    label = profile["domain"].split(".")[0]
    pats = [re.escape(name)]
    if len(core.split()) >= 2 or len(core) >= 6:
        pats.append(re.escape(core) + r"(?:'s)?\s+" + BUILDER_WORDS)
        pats.append(re.escape(core) + r"\b")
    pats.append(re.escape(label))
    return re.compile("|".join(pats), re.I)


def find_builders(text, known, generic):
    """Builder names in an answer: known candidates (Maps pack titles etc.) plus name-shaped phrases."""
    found = collections.OrderedDict()
    low = text.lower()
    for k in known:
        i = low.find(k.lower())
        if i >= 0:
            found.setdefault(norm_name(k), i)
    for m in NAME_RX.finditer(text):
        n = norm_name(m.group(1))
        words = n.split()
        while len(words) > 2 and words[0].lower().strip(",:") in LEAD_WORDS:
            if words[0] == "The" and len(words) == 3:  # keep "The Bold Company"
                break
            words = words[1:]
        n = " ".join(words)
        if generic.match(n) or len(n) < 6 or len(words) < 2 or (words[0].lower() in LEAD_WORDS and words[0] != "The"):
            continue
        found.setdefault(n, m.start())
    return [n for n, _ in sorted(found.items(), key=lambda x: x[1])]


def analyze(profile, market, questions, answers, checks, semrush):
    brand_rx = brand_patterns(profile)
    me = profile["domain"]
    sc = profile.get("scope") or scope.make(me, "root")
    mine = lambda u: bool(u) and scope.matches(u, sc)  # noqa: E731  (cited pages must sit inside the chosen scope)
    on_site = lambda d: bool(d) and scope.matches(d, scope.make(sc["domain"], "root"))  # noqa: E731  (Maps listings link to the site)
    qmap = {q["id"]: q for q in questions}
    generic = generic_rx([market["city"], market["state"], market["state_code"]] +
                         [w for a in profile["areas_served"] + [q.get("area") or "" for q in questions]
                          for w in re.findall(r"[A-Z][a-z]+", a or "")])
    known = sorted({p["title"] for a in answers for p in a.get("local_pack", []) if p.get("title") and "." not in p["title"]},
                   key=len, reverse=True)

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
            cell["mentioned"] = bool(m) or cell["cited"]
            builders = [b for b in find_builders(text, known, generic) if not brand_rx.search(b)]
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
        label = "This builder's website" if d == me else SOURCE_GROUP.get(PLATFORMS.get(d, ""), "Other builders' and local sites")
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
    status = {c[1]: c for c in checks}
    top = [b for b in leaderboard if not b.get("prospect")][:3]
    top_names = ", ".join(b["name"] for b in top) or "other builders"
    google = [c for c in cells if c["engine"] == "google_aio" and c["in_pack"] is not None]
    if google and sum(1 for c in google if c["in_pack"]) <= len(google) // 3:
        pack_leaders = sorted([b for b in leaderboard if b["pack"] and not b.get("prospect")], key=lambda b: -b["pack"])[:3]
        recs.append((10, "Get into the Google Maps 3-pack",
            f"{profile['name']} appeared in the Maps 3-pack for {sum(1 for c in google if c['in_pack'])} of {len(google)} local searches. "
            + (f"{', '.join(b['name'] + ' (' + str(b['pack']) + ')' for b in pack_leaders)} took those spots. " if pack_leaders else "")
            + "AI answers lean on Google Business Profile listings and reviews.",
            "Rebuild the Google Business Profile: primary category, service areas, weekly photos and posts, Q&A, and a steady review request flow after closings."))
    g = profile.get("gbp")
    if g and g.get("reviews") is not None:
        low = sum(v for k, v in (g.get("distribution") or {}).items() if int(k) <= 2)
        if (g.get("rating") or 0) < 4.6 or (g.get("reviews") or 0) < 50 or (g["reviews"] and low / g["reviews"] > 0.1):
            recs.append((8.5, "Grow and protect Google reviews",
                f"The Google Business Profile shows {g['rating']}★ from {g['reviews']} reviews"
                + (f", including {low} rated 1 or 2 stars" if low else "") + ". AI answers and the Maps pack lean on review count and rating.",
                "Ask every closing and walkthrough client for a review, reply to every review (especially the low ones), and add review markup to the site."))
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
            why = f"No AI answer about builders in {names} named {profile['name']}. "
        if page_gap:
            why += status["Service-area pages"][2]
        recs.append((8, "Build pages for each service area", why.strip(),
            "Write one page per area with local communities, lots, price ranges, drive times and past builds, linked from the homepage and marked up with the area served."))
    if any(status.get(k, ("good",))[0] == "warn" for k in ("FAQ markup", "Review markup", "Answers to common questions")):
        recs.append((7, "Make answers and reviews easy for AI to lift",
            "AI tools quote clear question-and-answer content and trust reviews they can read. Several of these are missing or unmarked on the site.",
            "Add or expand an FAQ (cost, timeline, lots, financing, warranty) with FAQPage markup, and add review and rating markup to testimonials."))
    platform_sources = [d for d, n in src_domains.most_common() if d in PLATFORMS and d != profile["domain"]][:5]
    if platform_sources:
        recs.append((6, "Show up on the sources AI answers trust",
            f"Across these answers, AI tools cited {', '.join(PLATFORMS[d] for d in platform_sources)}. Those sources shape which builders get named.",
            "Claim and complete listings on those sites, collect reviews there, publish build walkthrough videos, and pitch local best-builder lists."))
    if status.get("Prices published", ("good",))[0] == "warn":
        recs.append((6, "Publish price ranges", status["Prices published"][2],
            "Add starting prices or price bands per series or plan so AI answers to cost questions can include this builder."))
    weak_engines = [v["label"] for v in per_engine.values() if v["measured"] and v["answers"] and v["mentioned"] == 0]
    if weak_engines:
        recs.append((5, f"Get named in {', '.join(weak_engines)}",
            f"{', '.join(weak_engines)} did not name {profile['name']} in any answer; {top_names} came up instead.",
            "Publish comparison and best-of content on the site, earn mentions on the cited sources above, and track these platforms monthly."))
    recs.sort(key=lambda r: -r[0])
    return [{"title": t, "why": w, "how": h} for _, t, w, h in recs[:5]]
