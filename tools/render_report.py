#!/usr/bin/env python3
"""Render the HBM AI Visibility Audit (prospect report) from data/sample_audit.json.
Prototype of the production report step: HBM report style (Poppins, teal palette,
cover band, numbered sections, footer on every page). Output: reports/<domain>-aeo-audit.html"""
import json, base64, html, os

A = json.load(open("data/sample_audit.json"))
P, K = A["prospect"], A["kpis"]
LOGO = "data:image/webp;base64," + base64.b64encode(open("data/hbm-logo.webp", "rb").read()).decode()
SHOT = "data:image/jpeg;base64," + base64.b64encode(open("data/site/jdh_home.jpg", "rb").read()).decode()
e = html.escape
fmt = lambda n: "—" if n is None else f"{n:,}"

# Website checks (from tools: REA DOM capture + robots/llms/sitemap fetches, Nov 9 2026)
CHECKS = [
    ("good", "Business details markup", "HomeAndConstructionBusiness markup with street address, map location, hours and service areas."),
    ("bad", "Phone number consistency", "The page shows (513) 449-2856, but the business markup lists 513-986-5792. AI answers and Google can repeat the wrong number."),
    ("good", "Prices published", "Homes “starting from the mid $500s”, with price bands for each series. AI answers can quote this."),
    ("good", "Answers to common questions", "A “Common Questions” section answers cost (mid $500s to $3M+) and timeline (12–24 months)."),
    ("warn", "FAQ markup", "The questions have no FAQPage markup, so machines have to guess where each answer starts and ends."),
    ("warn", "Review markup", "A testimonials page exists, but there is no review or star-rating markup anywhere on the site."),
    ("warn", "Service-area pages", "Ohio and Kentucky state pages exist. There is no page for Dayton or Northern Kentucky, though the markup lists both as service areas."),
    ("good", "AI crawler access", "Nothing blocks AI crawlers. The site has no robots.txt file, so all crawlers are allowed by default."),
    ("good", "Sitemap", "A sitemap index lists 147 pages: plans, communities, galleries, quick move-ins and blog posts."),
]
passed = sum(1 for c in CHECKS if c[0] == "good")

PRIORITIES = [
    ("Get into the Google Maps 3-pack",
     "Justin Doyle Homes was in the Maps 3-pack for 0 of 9 local searches. Wieland Builders was there for 5, Chris Gorman Homes and JK Custom Homes for 4 each. AI answers also cite Google Business Profile listings and Yelp directly.",
     "Audit and rebuild the Google Business Profile: primary category, service areas, weekly photos and posts, Q&amp;A, and a steady review request flow after closings and walkthroughs."),
    ("Fix the phone number mismatch",
     "Two different phone numbers tell Google and AI tools the business details are unreliable, and either one can end up in an answer.",
     "Pick one tracking-safe main number, update the site markup, footer, Google Business Profile, Facebook, Houzz and Yelp so they all match, and keep them matched."),
    ("Build pages for Northern Kentucky and Dayton",
     "Google's AI answer for “custom home builders northern kentucky” cites The Bold Company and Potter House Group. Justin Doyle Homes ranks #12 there and #9 for Dayton, with no page for either area.",
     "Write one page for each area with local communities, lots, price ranges, drive times and past builds, linked from the homepage and marked up with the area served."),
    ("Mark up the FAQ and reviews",
     "The answers AI tools look for are already on the site. Markup makes them easy to lift and attribute to Justin Doyle Homes.",
     "Add FAQPage markup to “Common Questions”, add review and rating markup to the testimonials page, and expand the FAQ with lot, financing and warranty questions buyers ask."),
    ("Show up on the sources AI answers trust",
     "Across these searches, AI answers cited other builders' sites 10 times, and also Google listings, YouTube, Facebook, Yelp and NewHomeSource. Those sources decide who gets named.",
     "Get the business listed and reviewed on Houzz, Yelp and NewHomeSource, publish build walkthrough videos on YouTube, and pitch local “best builders in Cincinnati” lists."),
]

def pill(kind):
    lab = {"ai": "AI answer data", "search": "Google search data", "site": "Website check", "rec": "HBM recommendation"}[kind]
    return f'<span class="tag tag-{kind}">{lab}</span>'

def footer(n, total):
    return (f'<footer><span class="f-brand">Home Builder Marketers</span>'
            f'<span class="f-mid">HomeBuilderMarketers.com · {e(P["name"])} · {e(A["pulled"])}</span>'
            f'<span class="f-pg">{n:02d} / {total:02d}</span></footer>')

def ph(num, title, sub, kinds):
    return (f'<div class="ph"><div class="ph-num">{num}</div><div><h2>{title}</h2><p>{sub}</p>'
            f'<div class="tags">{"".join(pill(k) for k in kinds)}</div></div>'
            f'<div class="ph-meta">{e(P["name"])}<span>{e(A["pulled"])}</span></div></div>')

TOTAL = 7
pages = []

# ---------- Page 1: cover ----------
hero = [
    ("Cited in Google AI answers", f'{K["aio_cited"]} <small>of {K["aio_with_sources"]}</small>', "Local searches where Google's AI Overview listed its sources"),
    ("Google Maps 3-pack", f'{K["pack"]} <small>of {K["queries"]}</small>', "Local builder searches checked"),
    ("Top 3 on Google", f'{K["top3"]} <small>of {K["queries"]}</small>', "Same searches, regular results"),
    ("Website readiness", f'{passed} <small>of {len(CHECKS)}</small>', "AI-readiness checks passed"),
]
D = K["domain"]
support = [
    ("Searches showing an AI Overview", f'{fmt(D["aio_keywords"])}', f'of {fmt(D["organic_keywords"])} the site ranks for'),
    ("AI Overviews citing the site", fmt(D["aio_cited"]), "Across all searches the site ranks for"),
    ("Est. monthly Google visits", fmt(D["organic_traffic"]), "Semrush estimate"),
    ("ChatGPT, Gemini, Perplexity", "—", "Not measured in this sample"),
]
summary = (
    f'This audit checks how <b>{e(P["name"])}</b> shows up when people in {e(P["market"])} ask Google, and Google\'s AI, about building a home. '
    f'It uses <b>{K["queries"]} local searches</b> people actually make, with Google results and AI Overview sources from Semrush, plus a check of the website.\n\n'
    f'Google showed an AI Overview for <b>{K["aio_total"]} of the {K["queries"]}</b> searches. Where it listed sources, it cited Justin Doyle Homes <b>{K["aio_cited"]} of {K["aio_with_sources"]}</b> times. '
    f'The site ranks in the top 3 for <b>{K["top3"]}</b> searches but appears in the Google Maps 3-pack for <b>none</b>, where Wieland Builders, Chris Gorman Homes and JK Custom Homes take most spots.\n\n'
    f'The biggest gaps are Maps visibility, a phone number that does not match between the page and its markup, and no pages for Northern Kentucky or Dayton.'
)
pages.append(f'''
<section class="page p1">
 <div class="band">
  <div class="band-top"><img class="logo" src="{LOGO}" alt="Home Builder Marketers"><span class="band-tag">AI Visibility Audit</span></div>
  <div class="band-title">
   <div class="eyebrow">AI answers · Competitors · Sources · Website readiness</div>
   <h1>{e(P["name"])}</h1>
   <div class="period">{e(P["market"])} · Audit date {e(A["pulled"])}</div>
   <div class="cmpctx">Google AI Overviews and Google results via Semrush (latest snapshot) · Website checks run {e(A["pulled"])}</div>
  </div>
 </div>
 <div class="p1-body">
  <div class="hero">{"".join(f'<div class="hk"><div class="hk-l">{l}</div><div class="hk-v">{v}</div><div class="hk-s">{s}</div></div>' for l,v,s in hero)}</div>
  <div class="support">{"".join(f'<div><span>{l}</span><b>{v}</b><i>{s}</i></div>' for l,v,s in support)}</div>
  <div class="summary"><div class="sum-label">Executive summary</div>{"".join(f"<p>{x}</p>" for x in summary.split(chr(10)+chr(10)))}</div>
  <div class="legend"><span>How to read this report</span>{pill("ai")} measured from real AI answers {pill("search")} measured from Google results {pill("site")} checked on the website {pill("rec")} our advice</div>
 </div>
 {footer(1, TOTAL)}
</section>''')

# ---------- Page 2: AI answers by search ----------
rows = []
for q in A["queries"]:
    if not q["aio"]:
        aio, cited = '<span class="st st-mute">No AI answer</span>', '<span class="na">—</span>'
    elif not q["aio_sources"]:
        aio, cited = '<span class="st st-info">AI answer</span>', '<span class="na" title="Semrush did not record sources for this answer">Sources not recorded</span>'
    else:
        aio = '<span class="st st-info">AI answer</span>'
        cited = '<span class="st st-good">Cited</span>' if q["cited"] else '<span class="st st-bad">Not cited</span>'
    srcs = ", ".join(e(s) for s in q["aio_sources"] if s != P["domain"]) or "—"
    pos = f'#{q["organic_pos"]}' if q["organic_pos"] else "Not in top 20"
    pack = '<span class="st st-good">In 3-pack</span>' if q["in_pack"] else '<span class="st st-bad">Not in 3-pack</span>'
    rows.append(f'<tr><td><b>{e(q["q"])}</b><span class="srcs">AI sources: {srcs}</span></td><td class="r">{fmt(q["volume"])}</td><td>{aio}</td><td>{cited}</td><td>{pack}</td><td class="r">{pos}</td></tr>')
pages.append(f'''
<section class="page">
 {ph("01", "AI answers for local building searches", "What Google shows when someone searches for a builder in your area", ["ai", "search"])}
 <p class="lede">Google showed an AI Overview, the AI-written answer above the results, for <b>{K["aio_total"]} of {K["queries"]}</b> searches. Justin Doyle Homes is named as a source in <b>{K["aio_cited"]}</b> of them. It is missing from the Maps 3-pack on every search.</p>
 <div class="tbl-wrap"><table class="tbl">
  <colgroup><col style="width:38%"><col style="width:10%"><col style="width:13%"><col style="width:13%"><col style="width:14%"><col style="width:12%"></colgroup>
  <thead><tr><th>Search</th><th class="r">Monthly searches</th><th>AI Overview</th><th>Your site cited</th><th>Maps 3-pack</th><th class="r">Google rank</th></tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>
 <p class="note">Monthly searches are Semrush's US estimates. “Sources not recorded” means Google showed an AI answer but Semrush did not capture which sites it cited. Results can vary by searcher location and change over time.</p>
 {footer(2, TOTAL)}
</section>''')

# ---------- Page 3: competitors ----------
mx = max(b["score"] for b in A["builders"])
bars = []
for b in A["builders"]:
    w = lambda n, k: f'<i class="seg seg-{k}" style="width:{n/mx*100:.1f}%"></i>' if n else ""
    bars.append(f'''<div class="lb{' me' if b['prospect'] else ''}"><div class="lb-n">{e(b["name"])}{' <em>this builder</em>' if b['prospect'] else ''}</div>
     <div class="lb-bar">{w(b["aio"]*3,"aio")}{w(b["pack"]*2,"pack")}{w(b["top10"],"org")}</div>
     <div class="lb-v"><span title="AI Overview citations">{b["aio"]}</span><span title="Maps 3-pack spots">{b["pack"]}</span><span title="Top-10 rankings">{b["top10"]}</span></div></div>''')
pages.append(f'''
<section class="page">
 {ph("02", "Who shows up instead", "Builders that appear across the same 9 searches", ["ai", "search"])}
 <p class="lede"><b>Wieland Builders</b> leads this market because it shows up in both AI answers and the Maps 3-pack. Justin Doyle Homes is cited in more AI answers than anyone (3), but it never appears in the Maps 3-pack.</p>
 <div class="lb-head"><span>Builder</span><span class="lb-key"><i class="seg seg-aio"></i>AI Overview citation ×3 <i class="seg seg-pack"></i>Maps 3-pack ×2 <i class="seg seg-org"></i>Top-10 ranking ×1</span><span class="lb-v"><span>AI</span><span>Maps</span><span>Top 10</span></span></div>
 {"".join(bars)}
 <p class="note">Visibility points weight an AI answer citation 3, a Maps 3-pack spot 2 and a top-10 ranking 1, across the {K["queries"]} searches on page 01. Directories and social sites are shown on the next page instead.</p>
 {footer(3, TOTAL)}
</section>''')

# ---------- Page 4: sources ----------
smx = max(s["count"] for s in A["sources"])
srcbars = "".join(f'<div class="sb"><span>{e(s["kind"])}</span><div><i style="width:{s["count"]/smx*100:.0f}%"></i></div><b>{s["count"]}</b></div>' for s in A["sources"])
dom_rows = "".join(f'<tr><td>{e(s["label"])}<span class="srcs">{e(s["domain"])}</span></td><td class="r">{s["count"]}</td></tr>' for s in A["source_domains"])
kw_rows = "".join(f'<tr><td>{e(k["keyword"])}<span class="srcs">{e(k["url"].replace("https://",""))}</span></td><td class="r">{fmt(k["volume"])}</td></tr>' for k in A["top_cited_keywords"])
pages.append(f'''
<section class="page">
 {ph("03", "Where AI answers get their information", "The websites Google's AI Overviews cited for these searches", ["ai"])}
 <div class="two">
  <div><div class="sec-l">Cited sources by type <span>{sum(s["count"] for s in A["sources"])} citations</span></div>{srcbars}
   <p class="note">AI answers lean on other builders' websites, then Google listings, video, social and directories. Being on those sources is part of getting named.</p></div>
  <div><div class="sec-l">Every cited site</div><table class="tbl"><thead><tr><th>Site</th><th class="r">Citations</th></tr></thead><tbody>{dom_rows}</tbody></table></div>
 </div>
 <div class="sec-l" style="margin-top:22px">Searches where Google's AI already cites justindoylehomes.com <span>top {len(A["top_cited_keywords"])} of {fmt(D["aio_cited"])} by search volume</span></div>
 <table class="tbl"><thead><tr><th>Search</th><th class="r">Monthly searches</th></tr></thead><tbody>{kw_rows}</tbody></table>
 <p class="note">Of the 20 highest-volume searches where Google's AI cites the site, 16 point to Homearama, virtual tour and community pages, and 4 to “custom home builder” searches. That shows the content AI already trusts.</p>
 {footer(4, TOTAL)}
</section>''')

# ---------- Page 5: platform coverage + website checks ----------
plat = [("Google AI Overviews", "Measured", f'Cited in {K["aio_cited"]} of {K["aio_with_sources"]} local AI answers with sources', "good"),
        ("Google AI Mode", "—", "Not measured in this sample", "mute"),
        ("ChatGPT", "—", "Not measured in this sample", "mute"),
        ("Gemini", "—", "Not measured in this sample", "mute"),
        ("Perplexity", "—", "Not measured in this sample", "mute")]
plat_html = "".join(f'<div class="pl pl-{k}"><b>{n}</b><span>{v}</span><i>{s}</i></div>' for n, v, s, k in plat)
icon = {"good": "✓", "warn": "!", "bad": "✕"}
lab = {"good": "Pass", "warn": "Improve", "bad": "Fix"}
chk = "".join(f'<div class="ck ck-{s}"><span class="ck-i">{icon[s]}</span><div><b>{t}</b> <em>{lab[s]}</em><p>{d}</p></div></div>' for s, t, d in CHECKS)
pages.append(f'''
<section class="page">
 {ph("04", "AI platforms and website readiness", "Which AI tools this sample covers, and what the website gives them to work with", ["ai", "site"])}
 <div class="sec-l">AI platform coverage in this audit</div>
 <div class="plats">{plat_html}</div>
 <p class="note">This sample measures Google AI Overviews only. The full HBM audit adds ChatGPT, Gemini, Perplexity and Google AI Mode by asking each one the same local questions.</p>
 <div class="sec-l" style="margin-top:20px">Website checks <span>{passed} of {len(CHECKS)} passed · justindoylehomes.com homepage and sitemap, {e(A["pulled"])}</span></div>
 <div class="checks-wrap"><div class="checks">{chk}</div><figure class="shot"><img src="{SHOT}" alt="Justin Doyle Homes homepage as captured for this audit"><figcaption>Homepage as captured for this audit</figcaption></figure></div>
 {footer(5, TOTAL)}
</section>''')

# ---------- Page 6: priorities ----------
pri = "".join(f'<div class="pri"><div class="pri-n">{i}</div><div><h3>{t}</h3><div class="pri-g"><div><span>Why it matters</span><p>{w}</p></div><div><span>What HBM is doing</span><p>{h}</p></div></div></div></div>'
              for i, (t, w, h) in enumerate(PRIORITIES, 1))
pages.append(f'''
<section class="page">
 {ph("05", "What we'd focus on first", "Specific opportunities, in the order we would tackle them", ["rec"])}
 {pri}
 {footer(6, TOTAL)}
</section>''')

# ---------- Page 7: notes ----------
pages.append(f'''
<section class="page">
 {ph("06", "Report notes and sources", "Where every number came from and how to read it", ["ai", "search", "site"])}
 <div class="notes">
  <div><h4>Searches checked</h4><p>{K["queries"]} searches a {e(P["market"])} home buyer might make, chosen from the services and areas listed on justindoylehomes.com and kept only if Semrush shows real search demand.</p></div>
  <div><h4>AI answer data</h4><p>Google AI Overview sources and Maps 3-pack spots come from Semrush's latest US results snapshot, pulled {e(A["pulled"])}. Site-wide counts (searches showing an AI Overview, AI Overviews citing the site) are Semrush domain totals.</p></div>
  <div><h4>Website checks</h4><p>The homepage was loaded in a real Chrome browser and its markup read on {e(A["pulled"])}, along with robots.txt, llms.txt and the sitemap. Nothing on the site was changed.</p></div>
  <div><h4>What this sample does not cover</h4><p>ChatGPT, Gemini, Perplexity and Google AI Mode are not measured here and show as “—”. Google results vary by location, device and day, so treat single positions as a snapshot.</p></div>
 </div>
 <div class="close"><img class="logo" src="{LOGO}" alt="Home Builder Marketers"><div class="close-r"><div class="close-k">Prepared by</div><div class="close-n">Home Builder Marketers</div><div class="close-u">HomeBuilderMarketers.com</div><div class="close-d">Report date: {e(A["pulled"])}<br>Sources: Google AI Overviews (via Semrush) · Google search results (via Semrush) · Website checks</div></div></div>
 <p class="note">Figures come directly from each source and are frozen as of the report date. “—” means a figure was not available for this audit; it is never shown as zero.</p>
 {footer(7, TOTAL)}
</section>''')

CSS = open("tools/report.css").read()
out = f'''<meta charset="utf-8">
<title>{e(P["name"])} AI Visibility Audit</title>
<meta name="claude:viewport-fit-sides" content="cover">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap">
<style>{CSS}</style>
<main class="screen">{"".join(pages)}</main>'''
os.makedirs("reports", exist_ok=True)
path = f'reports/{P["domain"].split(".")[0]}-ai-visibility-audit.html'
open(path, "w").write(out)
print(path, len(out))
