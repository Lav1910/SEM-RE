"""Strategy: turn an audit into the deliverables that raise a builder's AI visibility, and write them.

plan(A)      -> ranked deliverables in four lanes (content, technical, local & reviews, sources), each with evidence
fixes(A)     -> technical tasks with the exact fix (snippets built from the site's real data)
generate()   -> writes one content deliverable (FAQ answers, blog drafts, city pages, llms.txt, profile posts, review asks)

Writing uses only facts read from the site, its Google Business Profile and the audit. Anything the builder must
supply (prices, warranty terms, timelines, awards) is written as [confirm: ...] instead of being invented.
"""
import collections
import json
import re

from . import llm
from .engines import ENGINE_LABELS

LANES = {"content": "Content", "technical": "Technical fixes", "local": "Local & reviews", "sources": "Sources AI trusts"}
IMPACT_ORDER = {"High": 0, "Medium": 1, "Low": 2}


# ---------------------------------------------------------------- facts
def facts(A):
    P, M = A["profile"], A["market"]
    gbp = P.get("gbp") or {}
    pages = [u for u in (P.get("sitemap_urls") or []) if u.count("/") <= 5][:40]
    return {
        "builder": P["name"], "website": P.get("url"), "main_market": f"{M['city']}, {M['state_code']}",
        "service_locations": [l["label"] for l in (A.get("locations") or [])] or P.get("areas_served") or [],
        "services_mentioned_on_site": P.get("services") or [], "prices_seen_on_site": P.get("prices_found") or [],
        "phone": (P.get("schema_phones") or P.get("page_phones") or [None])[0],
        "google_rating": gbp.get("rating"), "google_reviews": gbp.get("reviews"), "google_category": gbp.get("category"),
        "site_description": P.get("meta_description"), "homepage_text_excerpt": (P.get("opening_text") or "")[:1500],
        "site_pages": pages,
    }


def _rules():
    return ("Rules: use only the facts given. Never invent prices, years in business, awards, warranty terms, timelines, "
            "team names or statistics. Where a specific detail would help but isn't in the facts, write [confirm: what to add]. "
            "Write for home buyers in plain, warm, confident language. US spelling. No hype words like 'premier' or 'unparalleled'.")


# ---------------------------------------------------------------- evidence helpers
def _named_counts(A):
    """Per question: answers measured, answers naming the builder, competitors named instead."""
    cells = A["analysis"]["cells"]
    out = collections.defaultdict(lambda: {"ok": 0, "me": 0, "comps": collections.Counter()})
    for c in cells:
        if c["status"] != "ok":
            continue
        o = out[c["question_id"]]
        o["ok"] += 1
        o["me"] += 1 if c["mentioned"] else 0
        for b in c["competitors"]:
            o["comps"][b] += 1
    return out


def gap_questions(A, limit=None):
    """Non-brand questions where AI names competitors more than the builder, biggest demand first."""
    nc = _named_counts(A)
    rows = []
    for q in A["questions"]:
        if q.get("kind") == "brand":
            continue
        o = nc.get(q["id"])
        if not o or not o["ok"]:
            continue
        top = o["comps"].most_common(3)
        if o["me"] < max(1, o["ok"] // 2) and top:
            rows.append({"q": q, "me": o["me"], "ok": o["ok"], "top": [n for n, _ in top]})
    rows.sort(key=lambda r: (-(r["q"].get("weight") or 0), r["me"]))
    return rows[:limit] if limit else rows


def area_gaps(A):
    nc = _named_counts(A)
    by = collections.OrderedDict()
    for q in A["questions"]:
        if q.get("kind") == "brand" or not q.get("area"):
            continue
        o = nc.get(q["id"])
        if not o:
            continue
        b = by.setdefault(q["area"], {"ok": 0, "me": 0, "comps": collections.Counter()})
        b["ok"] += o["ok"]; b["me"] += o["me"]; b["comps"] += o["comps"]
    return by


# ---------------------------------------------------------------- plan
def plan(A):
    P, M, X = A["profile"], A["market"], A["analysis"]
    checks = {c[1]: c for c in A["checks"]}
    items = []

    def add(iid, lane, title, why, impact, effort, gen=None, how=None):
        items.append({"id": iid, "lane": lane, "lane_label": LANES[lane], "title": title, "why": why, "impact": impact,
                      "effort": effort, "generator": gen, "how": how})

    # content
    gaps = gap_questions(A)
    if gaps:
        g = gaps[0]
        add("faq", "content", "Answer local buyers' questions on the site",
            f"AI named {P['name']} in {sum(x['me'] for x in gaps)} of {sum(x['ok'] for x in gaps)} answers to {len(gaps)} buyer questions; "
            f"{', '.join(g['top'][:2])} came up instead. Clear, quotable answers on the site give AI something to cite.",
            "High", "Low", "faq", "Publish an FAQ page (and short FAQ blocks on key pages) with FAQPage markup.")
        add("blog", "content", "Write posts on the topics where competitors get named",
            "Top gaps: " + "; ".join(f"“{x['q']['prompt']}” ({', '.join(x['top'][:2])} named)" for x in gaps[:3]) + ".",
            "High", "Medium", "blog", "One in-depth post per topic, linked from the homepage and service pages.")
    ag = area_gaps(A)
    weak = [(a, b) for a, b in ag.items() if b["ok"] and b["me"] / b["ok"] < .5 and a != f"{M['city']}, {M['state_code']}"]
    locs = [l for l in (A.get("locations") or []) if not l.get("main")]
    if weak or locs:
        ex = weak[0] if weak else None
        why = (f"In {ex[0]}, AI named {P['name']} in {ex[1]['me']} of {ex[1]['ok']} answers"
               + (f"; {ex[1]['comps'].most_common(1)[0][0]} was named {ex[1]['comps'].most_common(1)[0][1]} times." if ex[1]["comps"] else ".")
               if ex else f"The site serves {', '.join(l['label'] for l in locs[:4])} but AI has little local detail to cite.")
        add("city_pages", "content", "Build a page for each service city", why, "High", "Medium", "city_pages",
            "One page per city: communities, lot availability, price range, drive times, past builds, local FAQs.")
    if "Prices published" in checks and checks["Prices published"][0] != "good":
        add("cost_guide", "content", "Publish a cost-to-build guide",
            "Cost is one of the most-asked buyer questions and no prices were found on the site, so AI quotes other builders' figures.",
            "Medium", "Medium", "cost_guide", "A guide with price ranges per home type and what drives cost.")

    # technical
    for f in fixes(A):
        add("fix_" + f["key"], "technical", f["title"], f["why"], f["impact"], f["effort"], f.get("gen"), f["how"])

    # local & reviews
    gbp = P.get("gbp") or {}
    k = X["kpis"]
    if k.get("pack") is not None and k.get("pack_of") and k["pack"] <= k["pack_of"] // 2:
        leaders = [b["name"] for b in X["leaderboard"] if b.get("pack") and not b.get("prospect")][:2]
        add("gbp_posts", "local", "Strengthen the Google Business Profile",
            f"{P['name']} showed in the Maps 3-pack for {k['pack']} of {k['pack_of']} local searches"
            + (f"; {', '.join(leaders)} held the top spots." if leaders else "."),
            "High", "Low", "gbp_posts", "Weekly posts, seeded Q&A, photos of recent builds, every service and city listed.")
    elif gbp:
        add("gbp_posts", "local", "Keep the Google Business Profile active",
            "Google's AI answers and Maps lean on active profiles with fresh posts and answered questions.", "Medium", "Low", "gbp_posts",
            "Weekly posts and seeded Q&A.")
    if not gbp.get("reviews") or (gbp.get("reviews") or 0) < 40:
        add("reviews", "local", "Collect more Google reviews",
            (f"The profile has {gbp.get('reviews')} reviews" if gbp.get("reviews") else "No Google reviews were found")
            + "; AI tools favour builders with many recent reviews.", "High", "Low", "review_requests",
            "Ask every closing and walkthrough client, with a direct review link.")

    # sources
    src = [s for s in X.get("source_domains") or [] if s["domain"] != P["domain"]][:6]
    if src:
        add("sources", "sources", "Get listed on the sites AI cites",
            "AI answers here cited " + ", ".join(f"{s['label']} ({s['count']})" for s in src[:5]) + ".",
            "Medium", "Medium", None, "Claim or complete these listings, add photos and reviews, and pitch local best-builder roundups.")

    items.sort(key=lambda i: (IMPACT_ORDER[i["impact"]], {"Low": 0, "Medium": 1, "High": 2}[i["effort"]]))
    return items


# ---------------------------------------------------------------- technical fixes
AI_BOTS = ["GPTBot", "OAI-SearchBot", "ChatGPT-User", "PerplexityBot", "Google-Extended", "ClaudeBot", "Applebot-Extended"]


def schema_block(A):
    P, M = A["profile"], A["market"]
    gbp = P.get("gbp") or {}
    addr = P.get("address") or {}
    d = {"@context": "https://schema.org", "@type": "HomeAndConstructionBusiness", "name": P["name"], "url": P.get("url")}
    phone = (P.get("schema_phones") or P.get("page_phones") or [None])[0]
    if phone:
        d["telephone"] = f"+1-{phone[:3]}-{phone[3:6]}-{phone[6:]}" if len(phone) == 10 else phone
    if addr:
        d["address"] = {k: v for k, v in addr.items() if k != "@context"}
    else:
        d["address"] = {"@type": "PostalAddress", "addressLocality": M["city"], "addressRegion": M["state_code"], "addressCountry": "US",
                        "streetAddress": "[confirm: street address]", "postalCode": "[confirm: ZIP]"}
    areas = [l["label"] for l in (A.get("locations") or [])] or [f"{M['city']}, {M['state_code']}"]
    d["areaServed"] = [{"@type": "City", "name": a} for a in areas]
    if P.get("same_as"):
        d["sameAs"] = P["same_as"]
    if gbp.get("rating") and gbp.get("reviews"):
        d["aggregateRating"] = {"@type": "AggregateRating", "ratingValue": gbp["rating"], "reviewCount": gbp["reviews"]}
    return '<script type="application/ld+json">\n' + json.dumps(d, indent=2) + "\n</script>"


def fixes(A):
    P = A["profile"]
    out = []
    for status, title, detail in A["checks"]:
        if status == "good":
            continue
        f = {"key": re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_"), "title": title, "why": detail, "status": status,
             "impact": "High" if status == "bad" else "Medium", "effort": "Low", "snippet": None, "how": ""}
        if title == "AI crawler access":
            f.update(title="Let AI crawlers read the site", how="Add these lines to robots.txt (or remove the rules blocking these bots).",
                     snippet="\n".join(f"User-agent: {b}\nAllow: /\n" for b in (P.get("ai_bots_blocked") or AI_BOTS)))
        elif title == "Business details markup":
            f.update(title="Add business details markup", how="Paste this in the <head> of every page (or in the SEO plugin's schema settings).",
                     snippet=schema_block(A))
        elif title == "Phone number consistency":
            f.update(title="Use one phone number everywhere", how="Use the same number in the header, footer, markup and Google profile. "
                     "If call tracking swaps numbers, keep the main number in the markup.")
        elif title == "Review markup":
            f.update(title="Mark up reviews and the rating", how="Add an AggregateRating to the business markup (included in the block "
                     "under “Add business details markup”) and Review markup on the testimonials page.", impact="Medium")
        elif title in ("Answers to common questions", "FAQ markup"):
            f.update(title="Add an FAQ with FAQPage markup", how="Generate the FAQ answers in Content Generator; each comes with the markup to paste.",
                     gen="faq")
        elif title == "Service-area pages":
            f.update(title="Create service-area pages", how="Generate one page per city in Content Generator.", gen="city_pages")
        elif title == "Sitemap":
            f.update(title="Publish an XML sitemap", how="Turn on the sitemap in the SEO plugin (Yoast, Rank Math) and add "
                     "“Sitemap: https://" + P["domain"] + "/sitemap.xml” to robots.txt.")
        elif title == "Prices published":
            f.update(title="Publish price ranges", how="Add starting prices or ranges per home type; generate a cost guide in Content Generator.",
                     gen="cost_guide", effort="Medium")
        elif title == "Google Business Profile":
            f.update(title="Complete the Google Business Profile", how="Fill every field, add services and cities, upload recent build photos.")
        else:
            f["how"] = detail
        out.append(f)
    if not P.get("has_llms_txt"):
        out.append({"key": "llms_txt", "title": "Add an llms.txt file", "status": "warn", "impact": "Low", "effort": "Low",
                    "why": "llms.txt gives AI tools a short, clean summary of the business and its key pages.",
                    "how": "Generate it in Content Generator and upload it to the site root as /llms.txt.", "snippet": None, "gen": "llms_txt"})
    return out


# ---------------------------------------------------------------- generators
GENERATORS = {
    "faq": "FAQ answers", "blog": "Blog post drafts", "city_pages": "City pages", "cost_guide": "Cost-to-build guide",
    "llms_txt": "llms.txt file", "gbp_posts": "Google Business Profile posts & Q&A", "review_requests": "Review request messages",
}


def targets(A, kind):
    """The pieces a generator produces: [(key, label)]."""
    if kind == "blog":
        return [(f"gap{i}", g["q"]["prompt"]) for i, g in enumerate(gap_questions(A, 3))] or [("gap0", "Choosing a custom home builder in " + A["market"]["city"])]
    if kind == "city_pages":
        locs = [l for l in (A.get("locations") or []) if not l.get("main")][:6]
        return [(re.sub(r"[^a-z0-9]+", "_", l["label"].lower()), l["label"]) for l in locs] or \
            [("main", f"{A['market']['city']}, {A['market']['state_code']}")]
    return [("all", GENERATORS[kind])]


def _faq_questions(A):
    qs = [q["prompt"] for q in A["questions"] if q.get("kind") != "brand"]
    extra = [f"Do you build on my lot in {A['market']['city']}?", "What is included in your base price?", "How long does a build take?",
             "Do you offer financing help?", "What warranty do you offer?", "Can I customise a floor plan?"]
    seen, out = set(), []
    for q in qs + extra:
        k = re.sub(r"\W+", " ", q.lower()).strip()
        if k not in seen:
            seen.add(k); out.append(q)
    return out[:16]


def generate(A, kind, key, provider="gemini"):
    """Return (markdown, cost). Raises on failure."""
    F = facts(A)
    base = "Facts about the builder (JSON):\n" + json.dumps(F, indent=1) + "\n\n" + _rules() + "\n\n"
    name = A["profile"]["name"]
    if kind == "faq":
        qs = _faq_questions(A)
        prompt = (base + f"Write website FAQ answers for {name}. Rewrite each buyer question below from the builder's side where needed "
                  "(e.g. 'Who are the best builders in X?' becomes 'Why choose " + name + " in X?'). Each answer: 2-4 sentences, "
                  "starts with a direct answer AI can quote, names the city. Reply with JSON only: "
                  '[{"question": "...", "answer": "..."}]\nQuestions:\n' + "\n".join("- " + q for q in qs))
        data, cost = llm.json_answer(prompt, provider)
        data = [d for d in data if d.get("question") and d.get("answer")]
        md = "\n\n".join(f"### {d['question']}\n\n{d['answer']}" for d in data)
        ld = {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": [
            {"@type": "Question", "name": d["question"], "acceptedAnswer": {"@type": "Answer", "text": re.sub(r"\[confirm:[^\]]*\]", "", d["answer"]).strip()}}
            for d in data]}
        md += ("\n\n---\n\n**FAQPage markup** (paste in the page's <head> once the [confirm] items are filled in):\n\n```html\n"
               '<script type="application/ld+json">\n' + json.dumps(ld, indent=2) + "\n</script>\n```")
        return md, cost
    if kind == "blog":
        g = next((x for i, x in enumerate(gap_questions(A, 3)) if f"gap{i}" == key), None)
        topic = g["q"]["prompt"] if g else dict(targets(A, "blog")).get(key, "Choosing a custom home builder")
        comps = ", ".join(g["top"]) if g else ""
        prompt = (base + f"Write a blog post for {name}'s website that answers this buyer question: “{topic}”. "
                  + (f"AI tools currently name {comps} for it, not {name}. " if comps else "")
                  + "Do not mention competitors. 900-1200 words in Markdown: an H1 title, a 2-sentence direct answer first (quotable by AI), "
                  "then H2 sections with local detail, a short FAQ (3 questions) at the end, and a closing call to action. "
                  "Suggest 2-3 internal links using the site pages given, as [link text](url).")
        return llm.text(prompt, provider, timeout=180)
    if kind == "city_pages":
        loc = dict(targets(A, "city_pages")).get(key) or f"{A['market']['city']}, {A['market']['state_code']}"
        prompt = (base + f"Write a service-area page for {name} about building homes in {loc}. Markdown, 600-800 words: H1 "
                  f"'Custom Home Builder in {loc}', a direct 2-sentence answer first, then sections on communities and lots in {loc} "
                  "([confirm] specific community names unless in the facts), home styles, price range ([confirm] unless in the facts), "
                  "the build process, drive times or nearby areas, 3 local FAQs, and a call to action. Also give a meta title (under 60 "
                  "characters) and meta description (under 155) at the top.")
        return llm.text(prompt, provider, timeout=180)
    if kind == "cost_guide":
        prompt = (base + f"Write a 'Cost to build a custom home in {A['market']['city']}' guide for {name}. Markdown, 800-1000 words: a "
                  "direct answer first, what drives cost (lot, size, finishes, site work, permits), a table of cost ranges by home type with "
                  "every figure as [confirm: $X–$Y] unless prices are in the facts, ways to manage budget, a short FAQ, call to action.")
        return llm.text(prompt, provider, timeout=180)
    if kind == "llms_txt":
        prompt = (base + f"Write an llms.txt file for {name} following the llms.txt convention: '# {name}', a one-paragraph '> ' summary "
                  "(what they build, where, for whom), then '## Key pages' with up to 10 '- [Title](url): one line' entries chosen from the "
                  "site pages given, then '## Service areas' and '## Contact'. Plain Markdown only, no code fences.")
        return llm.text(prompt, provider)
    if kind == "gbp_posts":
        prompt = (base + f"Write Google Business Profile content for {name}: 4 posts (each under 1,200 characters, one per week: a recent build "
                  "[confirm details], a buyer tip, a service-area spotlight, an invitation to talk) and 6 questions with answers to seed in "
                  "the profile's Q&A, using what local buyers ask. Markdown with headings 'Posts' and 'Q&A'.")
        return llm.text(prompt, provider)
    if kind == "review_requests":
        prompt = (base + f"Write review request messages for {name}: a short email and a text message for right after closing, a follow-up "
                  "for 30 days after move-in, and a note for trade partners. Each includes [Google review link]. Friendly, personal, short. "
                  "Markdown with a heading per message.")
        return llm.text(prompt, provider)
    raise ValueError("unknown generator")
