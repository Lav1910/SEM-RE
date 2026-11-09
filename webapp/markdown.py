"""Tiny, safe Markdown-to-HTML for AI answer text: escapes everything first, then adds headings, bold, italics,
bullets, numbered lists, links (http/https only) and paragraphs. No raw HTML from answers ever reaches the page."""
import html
import re

from markupsafe import Markup


def _inline(s):
    s = re.sub(r"\[([^\]]{1,200})\]\((https?://[^)\s]{1,500})\)", r'<a href="\2" target="_blank" rel="noopener nofollow">\1</a>', s)
    s = re.sub(r"\*\*([^*]{1,300})\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<![*\w])\*([^*\n]{1,200})\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"\[confirm:([^\]]{1,200})\]", r'<mark class="confirm" title="The builder must confirm this">[confirm:\1]</mark>', s)
    s = re.sub(r"`([^`]{1,200})`", r"<code>\1</code>", s)
    return s


def _cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def render(text, levels=False):
    """levels: keep heading levels (# -> h2, ## -> h3, ### -> h4) for generated documents."""
    lines = html.escape(text or "", quote=True).splitlines()
    out, lst, table, code = [], None, [], None

    def flush_table():
        if not table:
            return
        rows = [r for r in table if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
        head, body = rows[0], rows[1:]
        out.append('<div class="md-table"><table><thead><tr>' + "".join(f"<th>{_inline(c)}</th>" for c in _cells(head)) + "</tr></thead><tbody>"
                   + "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in _cells(r)) + "</tr>" for r in body) + "</tbody></table></div>")
        table.clear()

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            if code is None:
                flush_table()
                if lst:
                    out.append(f"</{lst}>"); lst = None
                code = []
            else:
                out.append('<pre class="md-code"><code>' + "\n".join(code) + "</code></pre>"); code = None
            continue
        if code is not None:
            code.append(raw)
            continue
        if line.strip().startswith("|") and line.strip().count("|") >= 2:
            if lst:
                out.append(f"</{lst}>"); lst = None
            table.append(line)
            continue
        flush_table()
        m_b = re.match(r"^\s*[*\-•]\s+(.*)", line)
        m_n = re.match(r"^\s*\d+[.)]\s+(.*)", line)
        kind = "ul" if m_b else "ol" if m_n else None
        if lst and kind != lst:
            out.append(f"</{lst}>"); lst = None
        if kind:
            if not lst:
                out.append(f"<{kind}>"); lst = kind
            out.append(f"<li>{_inline((m_b or m_n).group(1))}</li>")
            continue
        if not line.strip():
            continue
        if re.match(r"^\s*(-{3,}|\*{3,})\s*$", line):
            out.append("<hr>"); continue
        h = re.match(r"^\s*(#{1,6})\s+(.*)", line)
        if h:
            lv = min(4, len(h.group(1)) + 1) if levels else 4
            out.append(f"<h{lv}>{_inline(h.group(2))}</h{lv}>")
        else:
            out.append(f"<p>{_inline(line)}</p>")
    flush_table()
    if code is not None:
        out.append('<pre class="md-code"><code>' + "\n".join(code) + "</code></pre>")
    if lst:
        out.append(f"</{lst}>")
    return Markup("".join(out))
