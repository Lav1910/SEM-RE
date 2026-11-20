"""Render an audit (audit.json) as the HBM-branded AI Visibility Audit report, plus Loom talking points."""
import base64
import html
import os

from .engines import COUNTRY_ADJ, ENGINES, ENGINE_LABELS, country_of

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e = html.escape


def fmt(n):
    return "—" if n is None else f"{n:,}"


def _logo():
    for p in [os.path.join(ROOT, "data", "hbm-logo.webp"),
              os.path.expanduser("~/.claude/skills/hbm-report/assets/hbm-logo-used-in-builderboss-reports.webp")]:
        if os.path.exists(p):
            return "data:image/webp;base64," + base64.b64encode(open(p, "rb").read()).decode()
    return ""


def pill(kind):
    lab = {"ai": "AI answer data", "search": "Google search data", "site": "Website check", "rec": "HBM recommendation"}[kind]
    return f'<span class="tag tag-{kind}">{lab}</span>'


def _overview(A, ov):
    if ov is None:  # stored report (no audit history at hand): build the same view model the overview screen uses
        from webapp import views
        ov = {"V": views.build(A), "chart": views.chart(A, [])}
    return ov


def overview_html(A, ov=None, filter_label=None):
    """One-page PDF: just the Visibility Overview."""
    from . import overview_page
    ov = _overview(A, ov)
    name, date = A["profile"]["name"], A["date"]
    logo = _logo()
    strip = (f'<div class="ov-strip">{f"<img class={chr(34)}logo{chr(34)} src={chr(34)}{logo}{chr(34)} alt={chr(34)}Home Builder Marketers{chr(34)}>" if logo else "<b>Home Builder Marketers</b>"}'
             f'<span class="band-tag">AEO Snapshot · AI Visibility Overview</span></div>')
    page = (f'<section class="page ov-page">{strip}{overview_page.section(A, ov, filter_label)}'
            f'<footer><span class="f-brand">Home Builder Marketers</span><span class="f-mid">HomeBuilderMarketers.com · {e(name)} · {e(date)}</span>'
            f'<span class="f-pg">AI Visibility Overview</span></footer></section>')
    return _doc(f"{name} AI Visibility Overview", page, overview_page.CSS)


def _doc(title, pages, extra_css=""):
    css = open(os.path.join(ROOT, "tools", "report.css")).read()
    return f'''<!doctype html><meta charset="utf-8">
<title>{e(title)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap">
<style>{css}{extra_css}</style>
<main class="screen">{pages}</main>'''


def wtm(text):
    """'What this means' callout: the plain-English takeaway at the top of a page."""
    return f'<div class="wtm"><span class="wtm-k">What this means</span><p>{text}</p></div>'


def render(A, ov=None):
    P, K, M, X = A["profile"], A["analysis"]["kpis"], A["market"], A["analysis"]
    S = A.get("semrush") or {}
    D = S.get("domain") or {}
    date = A["date"]
    name = P["name"]
    from . import business
    W = business.words(A)  # change 53: 'builder' / 'remodeler', 'home buyers' / 'homeowners' ...
    BZ = business.of(A)
    noun, nouns, cust = W["noun"], W["nouns"], W["customers"]
    LOGO = _logo()
    pages = []
    q_by = {q["id"]: q for q in A["questions"]}
    measured = [en for en in ENGINES if X["per_engine"][en]["measured"]]
    ENG = measured  # the question grid only shows platforms with real results (change 24)
    zero = {"measured": True, "score": 0, "mentioned": 0, "answers": 0}  # change 28: a platform that couldn't run reads 0

    def footer(n, total):
        return (f'<footer><span class="f-brand">Home Builder Marketers</span>'
                f'<span class="f-mid">HomeBuilderMarketers.com · {e(name)} · {e(date)}</span>'
                f'<span class="f-pg">{n:02d} / {total:02d}</span></footer>')

    def ph(num, title, sub, kinds):
        return (f'<div class="ph"><div class="ph-num">{num}</div><div><h2>{title}</h2><p>{sub}</p>'
                f'<div class="tags">{"".join(pill(k) for k in kinds)}</div></div>'
                f'<div class="ph-meta">{e(name)}<span>{e(date)}</span></div></div>')

    TOTAL = 8
    # ---------------- cover ----------------
    score = K["score"]
    hero = [
        ("AI visibility score", "—" if score is None else f'{score} <small>/ 100</small>', f"Share of AI answers naming the {noun}, weighted by search demand"),
        ("Named in AI answers", f'{K["mentioned"]} <small>of {K["answers"]}</small>', f'{K["questions"]} local questions × {K["engines_measured"]} AI platforms'),
        ("Google Maps 3-pack", "—" if K["pack"] is None else f'{K["pack"]} <small>of {K["pack_of"]}</small>', "Local searches checked"),
        ("Website readiness", "—", "Not checked: the website turns away automated visitors") if P.get("site_blocked") else
        ("Website readiness", f'{K["checks_passed"]} <small>of {K["checks_total"]}</small>', "AI-readiness checks passed"),
    ]
    support = [(X["per_engine"][en]["label"],
                f'{X["per_engine"][en]["mentioned"]} of {X["per_engine"][en]["answers"]}' if en in measured else "0",
                f"answers naming the {noun}") for en in ENGINES]
    support_html = "".join(f'<div><span>{l}</span><b>{v}</b><i>{s}</i></div>' for l, v, s in support)
    lb = X["leaderboard"]
    leader = next((b for b in lb if not b.get("prospect")), None)
    me_rank = 1 + next((i for i, b in enumerate(lb) if b.get("prospect")), 0)
    topic = "building a home" if W["is_builder"] else e(business.service_phrase(BZ))
    summ = [f'This audit checks how <b>{e(name)}</b> shows up when people around {e(M["city"])}, {e(M["state_code"])} ask AI tools about {topic}. '
            f'We asked <b>{K["questions"]} local questions</b> on <b>{K["engines_measured"]} AI platforms</b> and checked the website, on {e(date)}.']
    if K["answers"] and not K["mentioned"]:
        summ.append(f'{e(name)} <b>wasn&rsquo;t named in any of the {K["answers"]} AI answers</b> to these local questions. '
                    + (f'<b>{e(leader["name"])}</b> came up most often, and {e(name)} ranks <b>#{me_rank}</b> of the {len(lb)} {nouns} compared.' if leader else ""))
    elif K["answers"]:
        summ.append(f'Across <b>{K["answers"]} AI answers</b>, {e(name)} was named in <b>{K["mentioned"]}</b>, by {K["engines_named"]} of {K["engines_measured"]} platforms. '
                    + (f'<b>{e(leader["name"])}</b> came up most often among other {nouns}, and {e(name)} ranks <b>#{me_rank}</b> of the {len(lb)} {nouns} compared.' if leader else ""))
    gaps = [e(r["title"]) for r in X["recommendations"][:3]]
    if gaps:
        summ.append("The biggest opportunities: <b>" + "</b>; <b>".join(gaps) + "</b>.")
    # plain-English takeaways for each page
    missed = K["answers"] - K["mentioned"]
    w1 = (f"Each row is a question local {cust} ask an AI tool. <b>Green</b> means the AI named " + e(name) + f"; <b>red</b> means it recommended other {nouns}. "
          + (f"{e(name)} was left out of <b>{missed} of {K['answers']}</b> answers." if K["answers"] else "No AI answers were measured in this audit."))
    w2 = ((f"When {cust} ask AI for {W['a_label'] if not W['is_builder'] else 'a builder'} near {e(M['city'])}, <b>{e(leader['name'])}</b> is the name that comes up most. "
           f"{e(name)} is <b>#{me_rank} of {len(lb)}</b> {nouns} compared.") if leader else f"No competing {nouns} were named in these answers.")
    top_src = X["source_domains"][0]["label"] if X.get("source_domains") else None
    w3 = ("AI tools build their answers from sites they trust. " + (f"<b>{e(top_src)}</b> was cited most. " if top_src else "")
          + "Being listed, reviewed and mentioned on these sites raises the chance of being recommended.")
    w4 = (f"<b>{K['checks_passed']} of {K['checks_total']}</b> checks passed. Each fix makes it easier for AI tools to understand who "
          f"{e(name)} is, where they {'build' if W['is_builder'] else 'work'} and why {cust} trust them.")
    if P.get("site_blocked"):  # change 26
        w4 = (f"{e(P['domain'])} turns away automated visitors, so the website checks couldn't run in this audit. "
              "Everything else in this report comes from the AI platforms and Google, which don't depend on reading the site.")
    from . import overview_page
    ov = _overview(A, ov)
    pages.append(f'''
<section class="page ov-page">
 <div class="ov-strip">{f'<img class="logo" src="{LOGO}" alt="Home Builder Marketers">' if LOGO else '<b>Home Builder Marketers</b>'}<span class="band-tag">AEO Snapshot</span></div>
 {overview_page.section(A, ov)}
 {footer(1, TOTAL)}
</section>''')
    lead = (f"{e(name)} was named in <b>{K['mentioned']} of {K['answers']}</b> AI answers to questions local {cust} ask"
            + (f" (an AI visibility score of <b>{score}/100</b>)." if score is not None else ".")
            + (f" <b>{e(leader['name'])}</b> is the {noun} AI recommends most often." if leader else "")) if K["answers"] else \
        "No AI platform answers were measured in this audit, so the score isn't available yet; the website checks and Google data still apply."
    pages.append(f'''
<section class="page">
 {ph("", "Summary", "The headline numbers, in plain English", ["ai", "search", "site"])}
 {wtm(lead)}
 <div class="hero">{"".join(f'<div class="hk"><div class="hk-l">{l}</div><div class="hk-v">{v}</div><div class="hk-s">{s}</div></div>' for l, v, s in hero)}</div>
 <div class="support five">{support_html}</div>
 <div class="summary"><div class="sum-label">Executive summary</div>{"".join(f"<p>{x}</p>" for x in summ)}</div>
 <div class="legend"><span>How to read this report</span>{pill("ai")} measured from real AI answers {pill("search")} measured from Google results {pill("site")} checked on the website {pill("rec")} our advice</div>
 {footer(2, TOTAL)}
</section>''')

    # ---------------- 01 matrix ----------------
    cell_by = {(c["question_id"], c["engine"]): c for c in X["cells"]}

    def cell_html(c):
        if c is None or c["status"] not in ("ok", "no_answer"):
            return '<span class="na">—</span>'
        if c["status"] == "no_answer":
            return '<span class="st st-mute">No AI answer</span>'
        if c["mentioned"]:
            r = f' #{c["rank"]}' if c.get("rank") else ""
            return f'<span class="st st-good">Named{r}</span>'
        return '<span class="st st-bad">Not named</span>'
    rows = []
    for q in A["questions"]:
        tds = "".join(f"<td>{cell_html(cell_by.get((q['id'], en)))}</td>" for en in ENG)
        rows.append(f'<tr><td><b>{e(q["prompt"])}</b><span class="srcs">Google search: {e(q["keyword"])}</span></td><td class="r">{fmt(q.get("volume"))}</td>{tds}</tr>')
    eng_cards = "".join(
        f'<div class="pl {"pl-good" if v["measured"] else "pl-mute"}"><b>{v["label"]}</b><span>{"—" if v["score"] is None else v["score"]}</span>'
        f'<i>{("Named in " + str(v["mentioned"]) + (" of " + str(v["answers"]) if v["answers"] else "") + " answers") if v["measured"] else "Not measured"}</i></div>'
        for v in (X["per_engine"][en] if en in measured else dict(X["per_engine"][en], **zero) for en in ENGINES))
    pages.append(f'''
<section class="page">
 {ph("01", "What AI tells local " + cust, "Each local question asked on every AI platform, and whether the " + noun + " was named", ["ai"])}
 {wtm(w1)}
 <div class="sec-l">Visibility score by platform <span>0–100, weighted by search demand</span></div>
 <div class="plats">{eng_cards}</div>
 <div class="tbl-wrap" style="margin-top:18px"><table class="tbl matrix">
  <colgroup><col style="width:34%"><col style="width:9%">{"".join(f'<col style="width:{round(57 / max(1, len(ENG)), 1)}%">' for _ in ENG)}</colgroup>
  <thead><tr><th>Question</th><th class="r">Monthly searches</th>{"".join(f"<th>{ENGINE_LABELS[en]}</th>" for en in ENG)}</tr></thead>
  <tbody>{"".join(rows)}</tbody></table></div>
 <p class="note">“Named #2” means the {noun} was the second business named in that answer. Monthly searches are {COUNTRY_ADJ[country_of(M)]} estimates for the Google search form of each question. “—” means the platform wasn't measured or didn't respond. AI answers change between runs and by location; this is a snapshot from {e(date)} with the location set to {e(M["city"])}, {e(M["state_code"])}.</p>
 {footer(3, TOTAL)}
</section>''')

    # ---------------- 02 competitors ----------------
    mx = max([b["score"] for b in lb] + [1])
    bars = []
    for b in lb:
        w = lambda n, k: f'<i class="seg seg-{k}" style="width:{n / mx * 100:.1f}%"></i>' if n else ""
        bars.append(f'''<div class="lb{' me' if b.get('prospect') else ''}"><div class="lb-n">{e(b["name"])}{' <em>this ' + noun + '</em>' if b.get('prospect') else ''}</div>
     <div class="lb-bar">{w(b["mentions"] * 2, "aio")}{w(b["pack"], "pack")}</div>
     <div class="lb-v two-col"><span title="AI answers naming them">{b["mentions"]}</span><span title="Maps 3-pack spots">{b["pack"]}</span></div></div>''')
    no_data = K["answers"] == 0 and K["pack"] is None
    empty = ('<div class="empty-state"><b>Not measured in this audit</b><p>This page fills in once at least one AI platform '
             'or the Google results feed is connected. Nothing here is shown as zero.</p></div>')
    lead_txt = f"<b>{e(leader['name'])}</b> is the {noun} AI tools and Google Maps surface most for these questions. " if leader else ""
    lb_html = empty if no_data else (
        f'<p class="lede">{lead_txt}{e(name)} ranks <b>#{me_rank}</b> of {len(lb)}.</p>'
        f'<div class="lb-head"><span>{W["Noun"]}</span>'
        '<span class="lb-key"><i class="seg seg-aio"></i>Named in an AI answer ×2 <i class="seg seg-pack"></i>Maps 3-pack spot ×1</span><span class="lb-v two-col"><span>AI</span><span>Maps</span></span></div>'
        + "".join(bars))
    pages.append(f'''
<section class="page">
 {ph("02", "Who AI recommends instead", W["Nouns"] + " named in the same AI answers, and who holds the Maps 3-pack", ["ai", "search"])}
 {wtm(w2)}
 {lb_html}
 <p class="note">{W["Noun"]} names are taken from the answer text and matched against Google Maps listings. Visibility points: 2 per AI answer naming the {noun}, 1 per Maps 3-pack spot.</p>
 {footer(4, TOTAL)}
</section>''')

    # ---------------- 03 sources ----------------
    srcs = X["sources"]
    smx = max([s["count"] for s in srcs] + [1])
    srcbars = "".join(f'<div class="sb"><span>{e(s["group"])}</span><div><i style="width:{s["count"] / smx * 100:.0f}%"></i></div><b>{s["count"]}</b></div>' for s in srcs) \
        or '<p class="note">No cited sources were recorded.</p>'
    dom_rows = "".join(f'<tr><td>{e(s["label"])}<span class="srcs">{e(s["domain"])}</span></td><td class="r">{s["count"]}</td></tr>' for s in X["source_domains"][:10])
    kw = S.get("ai_cited_keywords") or []
    kw_block = ""
    if kw:
        kw_rows = "".join(f'<tr><td>{e(k["keyword"])}<span class="srcs">{e(k["url"].replace("https://", ""))}</span></td><td class="r">{fmt(k["volume"])}</td></tr>' for k in kw[:8])
        kw_block = (f'<div class="sec-l" style="margin-top:22px">Searches where Google\'s AI Overview already cites {e(P["domain"])} '
                    f'<span>top {min(8, len(kw))} of {fmt(D.get("aio_cited"))} by search volume</span></div>'
                    f'<table class="tbl"><thead><tr><th>Search</th><th class="r">Monthly searches</th></tr></thead><tbody>{kw_rows}</tbody></table>')
    sources_html = empty if K["answers"] == 0 else (
        '<div class="two"><div><div class="sec-l">Cited sources by type <span>' + str(sum(s["count"] for s in srcs)) + ' citations</span></div>'
        + srcbars + '<p class="note">Each source is counted once per answer that cited it.</p></div>'
        + '<div><div class="sec-l">Most-cited sites</div><table class="tbl"><thead><tr><th>Site</th><th class="r">Answers</th></tr></thead><tbody>'
        + (dom_rows or '<tr><td>—</td><td class="r">—</td></tr>') + '</tbody></table></div></div>')
    pages.append(f'''
<section class="page">
 {ph("03", "Where AI answers get their information", "The websites the AI platforms cited across all answers", ["ai"])}
 {wtm(w3)}
 {sources_html}
 {kw_block}
 {footer(5, TOTAL)}
</section>''')

    # ---------------- 04 website checks ----------------
    icon = {"good": "✓", "warn": "!", "bad": "✕"}
    lab = {"good": "Pass", "warn": "Improve", "bad": "Fix"}
    chk = "".join(f'<div class="ck ck-{s}"><span class="ck-i">{icon[s]}</span><div><b>{e(t)}</b> <em>{lab[s]}</em><p>{e(d)}</p></div></div>' for s, t, d in A["checks"])
    if P.get("site_blocked"):
        chk = ('<div class="empty-state"><b>Website checks not run</b><p>The website turns away automated visitors, so its homepage, '
               'markup and AI crawler settings couldn&rsquo;t be read for this audit.</p></div>')
    google_block = ""
    if K["pack"] is not None:
        google_block = (f'<div class="support" style="margin-top:18px"><div><span>Maps 3-pack</span><b>{K["pack"]} of {K["pack_of"]}</b><i>local searches</i></div>'
                        f'<div><span>Top 3 on Google</span><b>{K["top3"]} of {K["pack_of"]}</b><i>same searches</i></div>'
                        f'<div><span>Searches showing an AI Overview</span><b>{fmt(D.get("aio_keywords"))}</b><i>of {fmt(D.get("organic_keywords"))} the site ranks for</i></div>'
                        f'<div><span>Est. monthly Google visits</span><b>{fmt(D.get("organic_traffic"))}</b><i>estimate</i></div></div>')
    pages.append(f'''
<section class="page">
 {ph("04", "Website readiness", "What the website gives AI tools to work with", ["site", "search"])}
 {wtm(w4)}
 <div class="sec-l">Website checks <span>{("not run · " + e(P["domain"])) if P.get("site_blocked") else (str(K["checks_passed"]) + " of " + str(K["checks_total"]) + " passed · " + e(P["domain"]) + " homepage, robots.txt, llms.txt and sitemap, " + e(date))}</span></div>
 <div class="checks">{chk}</div>
 {google_block}
 {footer(6, TOTAL)}
</section>''')

    # ---------------- 05 priorities ----------------
    pri = "".join(f'<div class="pri"><div class="pri-n">{i}</div><div><h3>{e(r["title"])}</h3><div class="pri-g"><div><span>Why it matters</span><p>{e(r["why"])}</p></div><div><span>What HBM is doing</span><p>{e(r["how"])}</p></div></div></div></div>'
                  for i, r in enumerate(X["recommendations"], 1))
    pages.append(f'''
<section class="page">
 {ph("05", "What we'd focus on first", "Specific opportunities, in the order we would tackle them", ["rec"])}
 {pri or '<p class="lede">No major gaps found.</p>'}
 {footer(7, TOTAL)}
</section>''')

    # ---------------- 06 notes ----------------
    # change 81: Google's data only covers listed places; a small town is asked from its province or country instead
    wider = sorted({a.get("location") for a in A.get("answers", []) if a.get("engine", "").startswith("google")
                    and a.get("location") and not a["location"].lower().startswith((M.get("city") or "").lower() + ",")})
    google_loc_note = (f" Google AI Overviews, AI Mode and the Maps results don't offer {e(M['city'])} as a search location, so those were "
                       f"set to {e(' / '.join(w.replace(',', ', ') for w in wider))} instead.") if wider else ""
    pages.append(f'''
<section class="page">
 {ph("06", "Report notes and sources", "Where every number came from and how to read it", ["ai", "search", "site"])}
 <div class="notes">
  <div><h4>Questions asked</h4><p>{K["questions"]} questions {"a home buyer" if W["is_builder"] else "a customer"} near {e(M["city"])} might ask, built from the services and areas on {e(P["domain"])}{"" if W["is_builder"] else " (" + e(business.service_phrase(BZ, 6)) + ")"}. Each was asked once per platform with the location set to {e(M["city"])}, {e(M["state_code"])}.{google_loc_note}</p></div>
  <div><h4>AI answer data</h4><p>Collected {e(date)} from {e(", ".join(ENGINE_LABELS[k] for k in measured))}. {"ChatGPT and Gemini answers are the ones their consumer apps (chatgpt.com, the Gemini app) gave, captured through DataForSEO. " if any("dataforseo" in (v or {}) for k, v in (A.get("answer_sources") or {}).items() if k in ("chatgpt", "gemini")) else ""}Answers can differ slightly from what one person sees, since the apps personalize results.</p></div>
  <div><h4>Google search data</h4><p>Maps 3-pack and rankings come from a live Google results page for {e(M["city"])}. Search volumes and site-wide AI Overview counts come from the latest {COUNTRY_ADJ[country_of(M)]} Google search data.</p></div>
  <div><h4>Website checks</h4><p>{"Not run: the website turns away automated visitors." if P.get("site_blocked") else "The homepage, robots.txt, llms.txt and sitemap were read on " + e(date) + ". Nothing on the site was changed."}</p></div>
 </div>
 <div class="close">
  <div class="close-l">{f'<img class="logo" src="{LOGO}" alt="Home Builder Marketers">' if LOGO else ''}</div>
  <div class="close-r"><div class="close-k">Prepared by</div><div class="close-n">Home Builder Marketers</div><div class="close-u">HomeBuilderMarketers.com</div><div class="close-d">Report date {e(date)}</div></div>
 </div>
 <p class="close-src">Sources · {e(" · ".join(ENGINE_LABELS[en] for en in measured) or "Website checks")} · Google search results · Website checks</p>
 <div class="next-hbm"><b>Next step with HBM</b> A 20-minute walkthrough of these findings and the first three fixes, with a plan to get {e(name)} named in more AI answers.</div>
 <p class="note">Figures come directly from each source and are frozen as of the report date. “—” means a figure was not available for this audit; it is never shown as zero.</p>
 {footer(8, TOTAL)}
</section>''')

    return _doc(f"{name} · AEO Snapshot", "".join(pages), overview_page.CSS)


def talking_points(A):
    """Short script for the salesperson's Loom (not part of the client report)."""
    P, K, X = A["profile"], A["analysis"]["kpis"], A["analysis"]
    lb = X["leaderboard"]
    leader = next((b for b in lb if not b.get("prospect")), None)
    lines = [f"Loom talking points: {P['name']} ({A['date']})", ""]
    pts = []
    if K["answers"]:
        pts.append(f"Open on page 1 (the overview): {P['name']} was named in {K['mentioned']} of {K['answers']} AI answers"
                   + (f" (score {K['score']}/100)." if K["score"] is not None else "."))
    if leader:
        pts.append(f"Show section 02: AI tools and Google Maps surface {leader['name']} most often for these questions.")
    if K["pack"] is not None:
        pts.append(f"Maps 3-pack: {P['name']} appeared in {K['pack']} of {K['pack_of']} local searches.")
    pts += [f"{r['title']}: {r['why']}" for r in X["recommendations"][:3]]
    lines += [f"{i}. {t}" for i, t in enumerate(pts, 1)]
    lines.append("")
    lines.append("Close: offer to walk through the fixes on a 20-minute call.")
    return "\n".join(lines)
