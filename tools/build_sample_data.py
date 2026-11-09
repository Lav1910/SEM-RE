#!/usr/bin/env python3
"""Turn the raw Semrush pulls + website checks for one prospect into the audit JSON
that render_report.py draws. Prototype of the production 'scoring' step."""
import json, glob, re, collections

PROSPECT = "justindoylehomes.com"
PLATFORMS = {  # non-builder sources, grouped the way a builder would recognise them
    "google.com": "Google (Maps / Business Profile)", "youtube.com": "YouTube", "facebook.com": "Facebook",
    "yelp.com": "Yelp", "houzz.com": "Houzz", "reddit.com": "Reddit", "zillow.com": "Zillow",
    "newhomesource.com": "NewHomeSource", "linkedin.com": "LinkedIn", "trulia.com": "Trulia",
    "realtor.com": "Realtor.com", "play.google.com": "Google Play", "library.municode.com": "Municode",
}
NAMES = {  # display names for builder domains seen in this market
    "justindoylehomes.com": "Justin Doyle Homes", "wielandbuilders.com": "Wieland Builders",
    "chrisgormanhomes.com": "Chris Gorman Homes", "jkcustomhomes.net": "JK Custom Homes",
    "robertluckegroup.com": "Robert Lucke Group", "schumacherhomes.com": "Schumacher Homes",
    "mihomes.com": "M/I Homes", "johnhenryhomes.com": "John Henry Homes", "theboldcompany.com": "The Bold Company",
    "potterhousegroup.com": "Potter House Group", "buildlegendary.com": "Legendary Homes",
    "eaglecustomhomes.com": "Eagle Custom Homes", "marondahomes.com": "Maronda Homes",
    "fischerhomes.com": "Fischer Homes", "hueberhomes.com": "Hueber Homes", "diyannihomes.com": "Diyanni Homes",
    "dennisottbuilders.com": "Dennis Ott Builders", "frazierhomes.com": "Frazier Homes",
    "designhomesco.com": "Design Homes", "buildgawhite.com": "G.A. White Homes", "shadetreehomes.com": "Shade Tree Homes",
    "dreeshomes.com": "Drees Homes", "taylorhomes.com": "Taylor Homes", "ryanhomes.com": "Ryan Homes",
}

def norm(d):
    d = d.lower()
    for p in ("www.", "m."):
        if d.startswith(p): d = d[len(p):]
    return d

vol = {}
for line in json.load(open("data/semrush/phrase_these_cincinnati.json"))["data"].splitlines()[1:]:
    k, v = line.split(";")[:2]; vol[k] = int(v)

queries, comp = [], collections.defaultdict(lambda: {"aio": 0, "pack": 0, "top10": 0, "queries": set()})
src_count = collections.Counter()
for f in sorted(glob.glob("data/semrush/serp_*.json")):
    q = f.split("serp_")[1][:-5].replace("_", " ")
    rows = [r.split(";") for r in json.load(open(f))["data"].splitlines()[1:]]
    feats = rows[0][-1].split(",") if rows else []
    aio = list(dict.fromkeys(norm(r[2]) for r in rows if r[1] == "52"))
    pack = list(dict.fromkeys(norm(r[2]) for r in rows if r[1] == "3"))
    org = []
    for r in rows:
        if r[1] == "-1" and norm(r[2]) not in [o for o, _ in org]: org.append((norm(r[2]), int(r[0])))
    me_org = next((p for d, p in org if d == PROSPECT), None)
    queries.append({
        "q": q, "volume": vol.get(q), "aio": "52" in feats, "aio_sources": aio,
        "cited": PROSPECT in aio, "pack": pack, "in_pack": PROSPECT in pack,
        "organic_pos": me_org, "top3": [d for d, p in org[:3]],
    })
    for d in aio: src_count[d] += 1
    for d in set(aio): comp[d]["aio"] += 1; comp[d]["queries"].add(q)
    for d in set(pack): comp[d]["pack"] += 1; comp[d]["queries"].add(q)
    for d, p in org[:10]: comp[d]["top10"] += 1; comp[d]["queries"].add(q)

queries.sort(key=lambda x: -(x["volume"] or 0))
builders = []
for d, c in comp.items():
    if d in PLATFORMS: continue
    score = c["aio"] * 3 + c["pack"] * 2 + c["top10"]  # weight AI answer > Maps pack > organic top 10
    builders.append({"domain": d, "name": NAMES.get(d, d), "aio": c["aio"], "pack": c["pack"], "top10": c["top10"],
                     "score": score, "prospect": d == PROSPECT})
builders.sort(key=lambda b: (-b["score"], b["name"]))

def kind(d):
    if d == PROSPECT: return "This builder"
    if d in PLATFORMS: return PLATFORMS[d]
    return "Other builders' websites"
src_kinds = collections.Counter()
for d, n in src_count.items(): src_kinds[kind(d)] += n

aio_q = [q for q in queries if q["aio"]]
with_src = [q for q in aio_q if q["aio_sources"]]
dom = json.load(open("data/semrush/domain_rank_jdh.json"))
dr = dict(zip(["domain", "organic_keywords", "organic_traffic", "aio_keywords", "aio_cited", "pack_positions"],
              dom["data"].splitlines()[1].split(";")))
cited_kw = [dict(zip(["keyword", "position", "type", "volume", "url", "intent"], l.split(";")))
            for l in json.load(open("data/semrush/jdh_ai_overview_keywords.json"))["data"].splitlines()[1:]]
seen, top_cited = set(), []
for k in cited_kw:
    if k["keyword"] in seen: continue
    seen.add(k["keyword"]); top_cited.append({"keyword": k["keyword"], "volume": int(k["volume"]), "url": k["url"]})

out = {
    "prospect": {"name": "Justin Doyle Homes", "domain": PROSPECT, "market": "Greater Cincinnati",
                 "hq": "Mason, OH", "areas": ["Greater Cincinnati", "Northern Kentucky", "Dayton"],
                 "price": "Mid $500s to $3M+", "founded_years": 17},
    "pulled": "November 9, 2026",
    "kpis": {
        "aio_cited": len([q for q in with_src if q["cited"]]), "aio_with_sources": len(with_src),
        "aio_total": len(aio_q), "queries": len(queries),
        "pack": len([q for q in queries if q["in_pack"]]),
        "top3": len([q for q in queries if q["organic_pos"] and q["organic_pos"] <= 3]),
        "domain": {k: (int(v) if v.isdigit() else v) for k, v in dr.items()},
    },
    "queries": queries, "builders": builders[:8],
    "sources": [{"kind": k, "count": n} for k, n in src_kinds.most_common()],
    "source_domains": [{"domain": d, "label": NAMES.get(d, PLATFORMS.get(d, d)), "count": n} for d, n in src_count.most_common()],
    "top_cited_keywords": top_cited[:8],
}
json.dump(out, open("data/sample_audit.json", "w"), indent=1)
print(json.dumps(out["kpis"], indent=1)); print(out["builders"]); print(out["sources"])
