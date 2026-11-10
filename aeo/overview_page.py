"""The Visibility Overview as a printable report page (page 1 of every report, and the one-page "Overview only" PDF).

Everything is drawn server-side as SVG/HTML (no scripts), so it prints the same in Chrome on a Mac and in the
serverless PDF function. Input `ov` is the view model the on-screen overview uses: {"V": views.build(...),
"chart": views.chart(...)}.
"""
import base64
import html
import math
import os

from .engines import ENGINES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
e = html.escape
BAND = {"Low": ("#E8892B", "#F6B35E", "#FFF6E8"), "Medium": ("#0A9A9D", "#43D3D5", "#EAF5F5"),
        "Great": ("#0F7A55", "#36C08B", "#E6F5EF")}


def _logo_svg(engine):
    p = os.path.join(ROOT, "webapp", "static", "logos", f"{engine}.svg")
    if not os.path.exists(p):
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(open(p, "rb").read()).decode()


def gauge_svg(score, band):
    a, b, _ = BAND.get(band, ("#9AA6A8", "#C5CED0", "#F4F8F8"))
    cx, cy, r = 120, 118, 90
    L = 2 * math.pi * r * 0.75

    def pt(f):
        ang = math.radians(135 + 270 * f)
        return cx + r * math.cos(ang), cy + r * math.sin(ang)
    sx, sy = pt(0); ex, ey = pt(1)
    path = f"M{sx:.2f} {sy:.2f} A{r} {r} 0 1 1 {ex:.2f} {ey:.2f}"
    segs = ""
    for (f0, f1), col in zip(((0, .35), (.35, .70), (.70, 1)), ("#FCEBDD", "#DDF1F1", "#DDF2E8")):
        segs += (f'<path d="{path}" fill="none" stroke="{col}" stroke-width="14" stroke-dasharray="{L * (f1 - f0) - 4:.1f} {L:.1f}" '
                 f'stroke-dashoffset="{-L * f0 - 2:.1f}"/>')
    prog = mark = ""
    if score:
        mx, my = pt(score / 100)
        prog = (f'<defs><linearGradient id="gp" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/>'
                f'</linearGradient></defs><path d="{path}" fill="none" stroke="url(#gp)" stroke-width="14" stroke-linecap="round" '
                f'stroke-dasharray="{L * score / 100:.1f} {L:.1f}"/>')
        mark = f'<circle cx="{mx:.2f}" cy="{my:.2f}" r="8.5" fill="#fff" stroke="{a}" stroke-width="3.5"/>'
    num = "—" if score is None else str(score)
    return (f'<svg viewBox="0 0 240 214" class="ov-gauge" role="img" aria-label="AI visibility {num} out of 100">{segs}{prog}{mark}'
            f'<text x="120" y="122" text-anchor="middle" class="g-n">{num}<tspan class="g-of" dx="2">/100</tspan></text>'
            f'<text x="30" y="210" class="g-t">0</text><text x="210" y="210" class="g-t" text-anchor="end">100</text></svg>')


def chart_svg(chart, set_="main", w=520, h=190):
    months = chart.get("months") or []
    series = [s for s in chart.get(set_, []) if any(v is not None for v in s.get("values", []))]
    if not months or not series:
        return '<div class="ov-chart-empty">History builds up as this builder is audited again each month.</div>'
    L, R, T, B = 34, 10, 8, 26
    pw, ph = w - L - R, h - T - B
    mx = max([v for s in series for v in s["values"] if v is not None] + [1])
    step = 1 if mx <= 4 else 10 ** math.floor(math.log10(mx * 1.1))
    for f in (1, 2, 2.5, 5, 10):
        if step * f * 4 >= mx * 1.1:
            top = step * f * 4; break
    else:
        top = mx * 1.2
    if mx <= 4:
        top = 4
    X = lambda i: L + (pw / 2 if len(months) == 1 else pw * i / (len(months) - 1))  # noqa: E731
    Y = lambda v: T + ph - ph * v / top  # noqa: E731
    out = [f'<svg viewBox="0 0 {w} {h}" class="ov-chart" role="img" aria-label="Monthly trend">']
    for i in range(5):
        y = Y(top * i / 4)
        lab = top * i / 4
        out.append(f'<line x1="{L}" x2="{w - R}" y1="{y:.1f}" y2="{y:.1f}" class="c-g"/>'
                   f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" class="c-a">{lab:g}</text>')
    every = max(1, math.ceil(len(months) / 6))
    for i, m in enumerate(months):
        last = len(months) - 1
        if i != last and (i % every or last - i < every):
            continue
        anchor = "start" if i == 0 and len(months) > 1 else ("end" if i == len(months) - 1 and len(months) > 1 else "middle")
        out.append(f'<text x="{X(i):.1f}" y="{h - 6}" text-anchor="{anchor}" class="c-a">{e(m)}</text>')
    for s in series:
        pts = [(X(i), Y(v)) for i, v in enumerate(s["values"]) if v is not None]
        runs, cur = [], []
        for i, v in enumerate(s["values"]):
            if v is None:
                if cur:
                    runs.append(cur); cur = []
            else:
                cur.append((X(i), Y(v)))
        if cur:
            runs.append(cur)
        for run in runs:
            if len(run) > 1:
                d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in run)
                dash = ' stroke-dasharray="6 5"' if s.get("dash") else ""
                out.append(f'<path d="{d}" fill="none" stroke="{s["color"]}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"{dash}/>')
        for x, y in pts:
            out.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.4" fill="#fff" stroke="{s["color"]}" stroke-width="2"/>')
    out.append("</svg>")
    legend = "".join(f'<span><i class="{"dash" if s.get("dash") else ""}" style="--c:{s["color"]}"></i>{e(s["name"])} <em>{e(s.get("note", ""))}</em></span>'
                     for s in series)
    return "".join(out) + f'<div class="ov-legend">{legend}</div>'


ICONS = {
    "pin": '<path d="M12 21s-7-6.2-7-11a7 7 0 0 1 14 0c0 4.8-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>',
    "chat": '<path d="M4 5h16v11H9l-5 4z"/>',
    "link": '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
    "check": '<path d="M12 3l7 3v6c0 4.5-3 7.6-7 9-4-1.4-7-4.5-7-9V6z"/><path d="M9 12l2 2 4-4"/>',
    "star": '<path d="M12 3l2.6 5.6L20 9.5l-4 4 1 5.8L12 16.6 7 19.3l1-5.8-4-4 5.4-.9z"/>',
    "doc": '<path d="M6 3h9l4 4v14H6zM15 3v4h4M9 12h7M9 16h5"/>',
}


def _rec_icon(title):
    t = title.lower()
    key = next((k for w, k in (("maps", "pin"), ("named in", "chat"), ("sources", "link"), ("lift", "check"), ("review", "star"),
                               ("page", "doc")) if w in t), "check")
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="#0A686A" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f'{ICONS[key]}</svg>')


def section(A, ov, filter_label=None):
    """The overview page body (inside a .page)."""
    V, chart = ov["V"], ov["chart"]
    P, M = A["profile"], A["market"]
    score, band = V["score"], V["band"]
    a, _, wash = BAND.get(band, ("#7A898B", "", "#F4F8F8"))
    semh = (A.get("semrush") or {}).get("history") or []
    metrics = [("#5B5FD9", "Mentions", V["mentions"] if V["measured_any"] else "—", "AI answers naming the builder"),
               ("#18C3A4", "Citations", V["citations"] if V["measured_any"] else "—", "answers linking to the site"),
               ("#B37FEB", "Cited pages", V["cited_pages"] if V["measured_any"] else "—", "site pages AI cites"),
               ("#0FB2B5", "AI Overviews", semh[-1]["aio_cited"] if semh else "—", "Google AI Overviews citing the site · Semrush")]
    mt = "".join(f'<div class="ov-m"><span><i style="background:{c}"></i>{lab}</span><b>{val}</b><small>{sub}</small></div>' for c, lab, val, sub in metrics)
    rows = []
    for d in V["dist"]:
        logo = _logo_svg(d["engine"])
        st = d["status"]
        if not d["measured"] or st["state"] in ("off", "bad"):
            mid = f'<span class="ov-st">{e(st["label"])}</span>'
            nums = '<td class="r">—</td><td class="r">—</td><td class="r">—</td>'
        else:
            mid = f'<div class="ov-bar"><i style="width:{d["mentions_pct"]}%"></i></div>'
            nums = (f'<td class="r">{d["mentions_pct"]}%</td><td class="r">{"—" if d["visibility"] is None else d["visibility"]}</td>'
                    f'<td class="r">{d["mentions"]}</td>')
        rows.append(f'<tr><td class="ov-eng">{f"<img src={chr(34)}{logo}{chr(34)} alt={chr(34)}{chr(34)}>" if logo else ""}{e(d["label"])}</td>'
                    f'<td class="ov-mid">{mid}</td>{nums}</tr>')
    recs = V["recommendations"][:3]
    cards = "".join(f'<div class="ov-card"><h4><span>{_rec_icon(r["title"])}</span>{e(r["title"])}</h4><p>{e(r["why"])}</p></div>' for r in recs) \
        or '<div class="ov-card"><h4>No major gaps found</h4><p>Keep publishing local content and collecting reviews.</p></div>'
    where = f'{e(M["city"])}, {e(M["state_code"])}' + (f' · {e(filter_label)}' if filter_label else "")
    return f'''
 <div class="ov-head">
  <div><div class="ov-k">AI visibility overview</div><h1>{e(P["name"])}</h1><p>{e(P["domain"])} · {where} · {e(A["date"])}</p></div>
  <div class="ov-score-chip" style="--a:{a};--w:{wash}"><b>{"—" if score is None else score}</b><span>{e(band)} visibility</span></div>
 </div>
 <div class="ov-top">
  <div class="ov-box ov-g"><div class="ov-t">AI Visibility</div>{gauge_svg(score, band)}
   <div class="ov-band" style="--a:{a};--w:{wash}">{e(band)}</div>
   <div class="ov-key"><span><i style="background:#F2B47A"></i>Low 0–34</span><span><i style="background:#5CC9CB"></i>Medium 35–69</span><span><i style="background:#4CC193"></i>Great 70+</span></div>
   <p class="ov-cap" style="--w:{wash}">{e(V["caption"])}</p></div>
  <div class="ov-box"><div class="ov-t">Main metrics</div><div class="ov-ms">{mt}</div>{chart_svg(chart)}</div>
 </div>
 <div class="ov-box"><div class="ov-t">Distribution by AI platform <span>share of the builder's mentions, visibility score 0–100, and answers naming them</span></div>
  <table class="ov-dist"><thead><tr><th>Platform</th><th></th><th class="r">Share</th><th class="r">Score</th><th class="r">Named</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div>
 <div class="ov-next"><div class="ov-t">What's next?</div><div class="ov-cards">{cards}</div></div>'''


CSS = """
.ov-head { display:flex; justify-content:space-between; align-items:flex-end; gap:16px; padding-bottom:14px; border-bottom:2px solid var(--teal); margin-bottom:16px; }
.ov-k { font-size:7.4pt; font-weight:600; letter-spacing:.16em; text-transform:uppercase; color:var(--teal); }
.ov-head h1 { font-size:21pt; font-weight:600; letter-spacing:-.02em; line-height:1.15; margin:2px 0 2px; }
.ov-head p { font-size:8.6pt; color:var(--muted); }
.ov-score-chip { text-align:center; background:var(--w); border-radius:12px; padding:8px 16px; min-width:1.2in; }
.ov-score-chip b { display:block; font-size:24pt; line-height:1; color:var(--a); }
.ov-score-chip span { font-size:7pt; font-weight:600; letter-spacing:.1em; text-transform:uppercase; color:var(--a); }
.ov-top { display:grid; grid-template-columns:2.35in 1fr; gap:12px; margin-bottom:12px; }
.ov-box { border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:12px; break-inside:avoid; }
.ov-top .ov-box { margin-bottom:0; }
.ov-t { font-size:9.6pt; font-weight:600; color:var(--ink); margin-bottom:6px; }
.ov-t span { font-weight:400; color:var(--muted); font-size:7.4pt; margin-left:6px; }
.ov-g { text-align:center; display:flex; flex-direction:column; align-items:center; }
.ov-g .ov-t { align-self:flex-start; }
.ov-gauge { width:1.85in; display:block; }
.ov-gauge .g-n { font:600 44px Poppins, sans-serif; fill:var(--ink); }
.ov-gauge .g-of { font:500 14px Poppins, sans-serif; fill:var(--muted); }
.ov-gauge .g-t { font:500 10px Poppins, sans-serif; fill:#8A989A; }
.ov-band { margin-top:-0.42in; font-size:7.4pt; font-weight:600; letter-spacing:.08em; text-transform:uppercase; color:var(--a); background:var(--w); padding:2px 10px; border-radius:99px; }
.ov-key { display:flex; gap:8px; justify-content:center; flex-wrap:wrap; font-size:6.8pt; color:var(--ink2); margin-top:0.26in; }
.ov-key i { display:inline-block; width:7px; height:7px; border-radius:2px; margin-right:3px; }
.ov-cap { margin-top:8px; background:var(--w); border-radius:8px; padding:7px 9px; font-size:7.6pt; color:var(--ink2); text-align:left; }
.ov-ms { display:grid; grid-template-columns:repeat(4, minmax(0,1fr)); gap:8px; margin-bottom:6px; }
.ov-m span { display:flex; align-items:center; gap:5px; font-size:7.4pt; color:var(--ink); white-space:nowrap; }
.ov-m span i { width:7px; height:7px; border-radius:50%; display:inline-block; }
.ov-m b { display:block; font-size:16pt; font-weight:600; line-height:1.2; color:var(--ink); }
.ov-m small { display:block; font-size:6.4pt; color:var(--muted); line-height:1.3; }
.ov-chart { width:100%; height:auto; display:block; }
.ov-chart .c-g { stroke:#E9F0F0; } .ov-chart .c-a { font:9px Poppins, sans-serif; fill:#7A898B; }
.ov-chart-empty { font-size:8pt; color:var(--muted); border:1px dashed var(--line); border-radius:8px; padding:20px; text-align:center; }
.ov-legend { display:flex; flex-wrap:wrap; gap:3px 12px; font-size:6.8pt; color:var(--ink); margin-top:2px; }
.ov-legend i { display:inline-block; width:12px; height:2.5px; border-radius:2px; background:var(--c); margin-right:4px; vertical-align:2px; }
.ov-legend i.dash { background:repeating-linear-gradient(90deg, var(--c) 0 4px, transparent 4px 7px); }
.ov-legend em { font-style:normal; color:var(--muted); }
.ov-dist { font-size:8.2pt; }
.ov-dist th { font-size:6.6pt; }
.ov-dist td { padding:5px 8px 5px 0; vertical-align:middle; }
.ov-eng { white-space:nowrap; font-weight:500; } .ov-eng img { width:13px; height:13px; vertical-align:-2px; margin-right:7px; }
.ov-mid { width:46%; }
.ov-bar { height:9px; background:var(--soft); border-radius:3px; overflow:hidden; } .ov-bar i { display:block; height:100%; background:#5B5FD9; border-radius:3px; }
.ov-st { font-size:6.8pt; font-weight:600; color:var(--bad); background:var(--bad-bg); padding:2px 8px; border-radius:99px; }
.ov-next { border:1px solid #BFE3E3; border-radius:12px; overflow:hidden; break-inside:avoid; }
.ov-next .ov-t { background:linear-gradient(90deg, #E3F4F4, #EEF8F8); padding:8px 14px; margin:0; color:var(--deep); }
.ov-cards { display:grid; grid-template-columns:repeat(3, minmax(0,1fr)); gap:10px; padding:10px 12px 12px; background:linear-gradient(180deg, #F6FBFB, #fff); }
.ov-card { border:1px solid #D3E9EA; border-radius:10px; padding:10px 11px; background:#fff; }
.ov-card h4 { font-size:8.4pt; font-weight:600; line-height:1.3; margin-bottom:5px; display:flex; gap:6px; }
.ov-card h4 span { flex:none; width:16px; height:16px; border-radius:5px; background:#E1F3F3; display:grid; place-items:center; }
.ov-card h4 span svg { width:11px; height:11px; }
.ov-card p { font-size:7.4pt; color:var(--ink2); line-height:1.5; }
@media screen and (max-width: 8.9in) { .ov-top, .ov-cards { grid-template-columns:1fr; } .ov-ms { grid-template-columns:repeat(2, minmax(0,1fr)); } }
"""
