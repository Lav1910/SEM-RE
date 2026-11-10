"""change 47: every deliverable for one builder as a single, clean Word file (.docx).

Cover, a contents page with clickable links, a short summary, the strategy plan, the technical fixes (with code in a
monospace box), every Content Generator piece, and a closing page. Opens in Word, Google Docs and Pages.
"""
import datetime
import io
import os
import re

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor, Inches

TEAL = RGBColor(0x0A, 0x68, 0x6A)
INK = RGBColor(0x16, 0x30, 0x2F)
MUTED = RGBColor(0x7A, 0x89, 0x8B)
LOGO = os.path.join(os.path.dirname(__file__), "static", "hbm-logo.png")
FONT = "Arial"  # installed everywhere (Word, Google Docs, Pages); Poppins usually is not


def _setup(doc):
    st = doc.styles["Normal"]
    st.font.name = FONT
    st.font.size = Pt(10.5)
    st.font.color.rgb = INK
    st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.25
    for name, size in (("Heading 1", 20), ("Heading 2", 14), ("Heading 3", 12), ("Heading 4", 11)):
        h = doc.styles[name]
        h.font.name = FONT
        h.font.size = Pt(size)
        h.font.bold = True
        h.font.color.rgb = TEAL if name in ("Heading 1", "Heading 2") else INK
        h.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
        h.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
        h.paragraph_format.space_after = Pt(6)
    sec = doc.sections[0]
    sec.left_margin = sec.right_margin = Inches(0.9)
    sec.top_margin = sec.bottom_margin = Inches(0.8)


_bm = [0]


def _bookmark(par, name):
    _bm[0] += 1
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(_bm[0]))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(_bm[0]))
    par._p.insert(0, start)
    par._p.append(end)


def _internal_link(par, text, anchor):
    link = OxmlElement("w:hyperlink")
    link.set(qn("w:anchor"), anchor)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "0A686A")
    rpr.append(color)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "none")
    rpr.append(u)
    run.append(rpr)
    t = OxmlElement("w:t")
    t.text = text
    run.append(t)
    link.append(run)
    par._p.append(link)


def _shade(par, fill="F2F6F6"):
    ppr = par._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    ppr.append(shd)


def _inline(par, text):
    """**bold**, `code`, [text](url) inside a paragraph."""
    pos = 0
    for m in re.finditer(r"\*\*(.+?)\*\*|`([^`]+)`|\[([^\]]+)\]\(([^)]+)\)", text):
        if m.start() > pos:
            par.add_run(text[pos:m.start()])
        if m.group(1):
            par.add_run(m.group(1)).bold = True
        elif m.group(2):
            r = par.add_run(m.group(2))
            r.font.name = "Consolas"
        else:
            par.add_run(m.group(3))
            r = par.add_run(f" ({m.group(4)})")
            r.font.color.rgb = MUTED
        pos = m.end()
    if pos < len(text):
        par.add_run(text[pos:])


def _code(doc, text):
    for line in (text or "").splitlines() or [""]:
        p = doc.add_paragraph()
        r = p.add_run(line or " ")
        r.font.name = "Consolas"
        r.font.size = Pt(9)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.1
        _shade(p)
    doc.add_paragraph()


def _markdown(doc, md, base_level=2):
    """A small Markdown reader for the generated content: headings, lists, tables, code, bold and links."""
    lines = (md or "").splitlines()
    i, table = 0, []

    def flush_table():
        rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
        if rows:
            t = doc.add_table(rows=len(rows), cols=max(len(r) for r in rows))
            t.style = "Light Grid Accent 1"
            for ri, row in enumerate(rows):
                for ci, cell in enumerate(row):
                    t.cell(ri, ci).text = cell.replace("**", "")
            doc.add_paragraph()
        table.clear()
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|"):
            table.append(line); i += 1
            continue
        if table:
            flush_table()
        if line.strip().startswith("```"):
            buf = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            _code(doc, "\n".join(buf))
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)", line)
        if m:
            doc.add_heading(m.group(2).strip(), level=min(4, base_level + len(m.group(1)) - 1))
        elif re.match(r"^\s*[-*]\s+", line):
            _inline(doc.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            _inline(doc.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        elif line.strip():
            _inline(doc.add_paragraph(), line.strip())
        i += 1
    if table:
        flush_table()


def _page_break(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def build(a, A, S, V, contact_email, sender=None):
    _bm[0] = 0
    doc = Document()
    _setup(doc)
    name = A["profile"].get("name") or a["domain"]
    M = A["market"]
    when = datetime.date.fromtimestamp(a["created_at"])

    # ---- cover
    if os.path.exists(LOGO):
        doc.add_picture(LOGO, width=Inches(2.2))
    for _ in range(5):
        doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("AEO SNAPSHOT")
    r.bold = True; r.font.size = Pt(11); r.font.color.rgb = TEAL
    p = doc.add_paragraph()
    r = p.add_run(f"AI visibility plan for {name}")
    r.bold = True; r.font.size = Pt(28); r.font.color.rgb = INK
    p = doc.add_paragraph()
    r = p.add_run(f"{a['domain']}  ·  {M['city']}, {M['state_code']}  ·  {when:%B} {when.day}, {when.year}")
    r.font.size = Pt(12); r.font.color.rgb = MUTED
    doc.add_paragraph()
    p = doc.add_paragraph()
    r = p.add_run("Prepared by Home Builder Marketers")
    r.font.color.rgb = MUTED

    sections = [("summary", "Summary"), ("plan", "Strategy plan"), ("fixes", "Technical fixes")]
    content = [g for g in S["cards"] if any(c["status"] == "done" for c in g["cards"])]
    sections += [(f"content-{g['kind']}", g["label"]) for g in content]
    sections.append(("next", "Next steps"))

    # ---- contents
    _page_break(doc)
    doc.add_heading("Contents", level=1)
    for anchor, label in sections:
        p = doc.add_paragraph()
        _internal_link(p, label, anchor)

    def section(anchor, label):
        _page_break(doc)
        h = doc.add_heading(label, level=1)
        _bookmark(h, anchor)

    # ---- summary
    section("summary", "Summary")
    score = V.get("score")
    D = V.get("demand") or {}
    _inline(doc.add_paragraph(), f"**AI visibility score:** {score if score is not None else '—'} / 100 ({V.get('band', '')}). {V.get('caption', '')}.")
    if D.get("has"):
        _inline(doc.add_paragraph(), f"**Search demand:** {D['total']:,} people search for these questions every month. AI named {name} for "
                                     f"{D['pct']}% of that demand; {100 - D['pct']}% is going to competitors.")
    recs = (V.get("recommendations") or [])[:3]
    if recs:
        doc.add_heading("Top opportunities", level=2)
        for r_ in recs:
            p = doc.add_paragraph(style="List Number")
            p.add_run(r_.get("title", "")).bold = True
            if r_.get("why"):
                p.add_run(" — " + r_["why"])

    # ---- strategy plan
    section("plan", "Strategy plan")
    for it in S["items"]:
        doc.add_heading(it["title"], level=3)
        p = doc.add_paragraph()
        r = p.add_run(f"{it.get('lane_label', '')} · {it.get('impact', '')} impact · {it.get('effort', '')} effort")
        r.font.size = Pt(9); r.font.color.rgb = MUTED
        if it.get("why"):
            doc.add_paragraph(it["why"])
        if it.get("how"):
            _inline(doc.add_paragraph(), "**What to do:** " + it["how"])

    # ---- fixes
    section("fixes", "Technical fixes")
    if not S["fixes"]:
        doc.add_paragraph("Every website check passed. Nothing to fix.")
    for f in S["fixes"]:
        doc.add_heading(f["title"], level=3)
        p = doc.add_paragraph()
        r = p.add_run(f"{f.get('impact', '')} impact · {f.get('effort', '')} effort")
        r.font.size = Pt(9); r.font.color.rgb = MUTED
        if f.get("why"):
            doc.add_paragraph(f["why"])
        if f.get("how"):
            _inline(doc.add_paragraph(), "**Fix:** " + f["how"])
        if f.get("snippet"):
            _code(doc, f["snippet"])

    # ---- content
    for g in content:
        section(f"content-{g['kind']}", g["label"])
        for c in g["cards"]:
            if c["status"] != "done":
                continue
            if len(g["cards"]) > 1:
                doc.add_heading(c["label"], level=2)
            _markdown(doc, c["text"], base_level=2 if len(g["cards"]) <= 1 else 3)

    # ---- close
    section("next", "Next steps")
    doc.add_paragraph("Want Home Builder Marketers to put this live for you? We'll publish the content, add the fixes to the website and "
                      f"track how AI recommends {name} month by month.")
    _inline(doc.add_paragraph(), f"**Email:** {contact_email}")
    if sender and sender.get("name"):
        _inline(doc.add_paragraph(), f"**Your contact:** {sender['name']}" + (f" ({sender['email']})" if sender.get("email") else ""))

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def filename(a, A):
    name = re.sub(r"[^A-Za-z0-9]+", "-", (A["profile"].get("name") or a["domain"])).strip("-")
    return f"{name}-AI-Visibility-Plan-{datetime.date.fromtimestamp(a['created_at']).isoformat()}.docx"
