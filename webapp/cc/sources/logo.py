"""Client logo finder (change 64): one read of the homepage plus at most a few image requests, through the SSRF guard.

Where a logo is looked for, best first:
1. WordPress's own site logo (<img class="custom-logo">), which is what the theme shows in the header.
2. The logo in the site's structured data (Organization / LocalBusiness "logo").
3. Any other header image whose class, id, alt or file name says "logo".
4. The site's app icon (apple-touch-icon) or a large icon (192 px and up). Square, but at least it's theirs.
Tiny favicons aren't used: blown up to tile size they look broken, and the initials look cleaner.
A logo someone uploaded by hand is never replaced by a found one.
"""
import re
import urllib.parse
import urllib.request

from aeo import netsafe, site
from .wordpress import UA, fetch

MAX_BYTES = 1_500_000
IMG = re.compile(r"<img\b[^>]*>", re.I)
LINK = re.compile(r"<link\b[^>]*>", re.I)
ATTR = lambda name: re.compile(name + r"""\s*=\s*["']([^"']+)["']""", re.I)  # noqa: E731
SRC_ATTRS = [ATTR(a) for a in ("data-src", "data-lazy-src", "data-orig-src", "src")]
SRCSET = ATTR("srcset")
REL, HREF, SIZES = ATTR("rel"), ATTR("href"), ATTR("sizes")


def sniff(data):
    """Image type from the bytes themselves (never trust the server's Content-Type), or None."""
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    head = data[:2000].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<svg", b"<?xml")) and b"<svg" in head:
        return "image/svg+xml"
    return None


def dims(data, mime):
    """(width, height) read from the file header (PNG, GIF, JPEG, WEBP, ICO, SVG viewBox), or None."""
    import struct
    try:
        if mime == "image/png":
            return struct.unpack(">II", data[16:24])
        if mime == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if mime == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    i += 1
                    continue
                m = data[i + 1]
                if m in (0xC0, 0xC1, 0xC2):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
            return None
        if mime == "image/webp":
            tag = data[12:16]
            if tag == b"VP8X":
                return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
            if tag == b"VP8L":
                v = int.from_bytes(data[21:25], "little")
                return (v & 0x3FFF) + 1, ((v >> 14) & 0x3FFF) + 1
            if tag == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3FFF, h & 0x3FFF
        if mime == "image/x-icon":
            return data[6] or 256, data[7] or 256
        if mime == "image/svg+xml":
            t = data[:4000].decode("utf-8", "ignore")
            m = re.search(r"""viewBox\s*=\s*["'][\d.\-]+[ ,]+[\d.\-]+[ ,]+([\d.]+)[ ,]+([\d.]+)""", t)
            if m:
                return float(m.group(1)), float(m.group(2))
    except Exception:  # noqa: BLE001
        return None
    return None


def brand_tokens(domain, name=None):
    """Words that show a file is the client's own: the domain's name, long words of the business name, its initials."""
    label = re.sub(r"[^a-z0-9]", "", (domain or "").lower().split(".")[0])
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in ("the", "and", "llc", "inc", "co")]
    toks = {label} if len(label) >= 3 else set()
    toks |= {w for w in words if len(w) >= 4}
    if len(words) >= 2:
        toks.add("".join(w[0] for w in words))
    if len(label) >= 3:
        toks.add(label[:3])
    return {t for t in toks if len(t) >= 3}


def _file_words(url, tag=""):
    path = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
    alt = re.search(r"""alt\s*=\s*["']([^"']*)""", tag or "", re.I)
    return re.sub(r"[^a-z0-9]", "", (path + " " + (alt.group(1) if alt else "")).lower())


def score(data, mime, why, url="", tokens=None):
    """Higher is better: what the site declares as its logo, sharp files, wordmarks over square icons; tiny files lose.
    A file named like an icon (favicon, site icon) loses ground; a header image named after the client gains it."""
    d = dims(data, mime)
    if why == "inline header logo":
        base = 520  # its colours may come from the site's stylesheet: only wins over small or missing image files
    elif mime == "image/svg+xml":
        base = 950  # sharp at any size: worth about a 950 px wide image
    elif not d:
        base = 150
    else:
        w, h = d
        if max(w, h) < 64:
            return -1
        base = min(w, 900) + min(h, 400) * 0.3
        if h and 1.4 <= w / h <= 7:
            base += 220  # a full logo (name + mark) reads better on a tile than a square icon
    # what the site itself declares as its logo comes first; header images are a fallback (they can be partners' logos)
    base += {"site logo (WordPress)": 700, "logo in structured data": 600, "header logo image": 140,
             "inline header logo": 120, "other header image": -150}.get(why, 0)
    if why in ("site app icon", "large site icon"):
        base -= 260
    words = _file_words(url)
    if re.search(r"fav|fev|siteicon|icon", words):
        base -= 300
    if tokens is not None and why in ("header logo image", "other header image", "inline header logo"):
        base += 250 if any(t in words for t in tokens) else -200
    return base


def _src(tag):
    m = SRCSET.search(tag)  # the largest file in a srcset is the sharpest version of the same logo
    if m:
        best = sorted(((int(w or 0), u) for u, w in re.findall(r"(\S+)\s+(\d+)w", m.group(1))), reverse=True)
        if best and best[0][0] >= 300:
            return best[0][1]
    for rx in SRC_ATTRS:
        m = rx.search(tag)
        if m and not m.group(1).startswith("data:"):
            return m.group(1).strip()
    m = SRCSET.search(tag)
    if m:  # widest candidate in a srcset
        best = sorted(((int(w or 0), u) for u, w in re.findall(r"(\S+)\s+(\d+)w", m.group(1))), reverse=True)
        if best:
            return best[0][1]
    return None


def inline_svg(page):
    """A logo drawn as inline <svg> in the header (common on modern themes) -> bytes, or None."""
    for m in re.finditer(r"<svg\b[^>]*>.*?</svg>", page[:300000], re.I | re.S):
        tag = m.group(0)
        head = tag[:400].lower()
        before = page[max(0, m.start() - 400):m.start()].lower()
        if ("logo" in head or "logo" in before[-250:]) and not OTHER_BRANDS.search(head + before[-250:]) and "icon-" not in head:
            low = tag.lower()
            if 300 < len(tag) < 200000 and "<script" not in low and "foreignobject" not in low and "<use" not in low and "class=" not in low[400:]:
                svg = tag if "xmlns" in tag[:300] else tag.replace("<svg", '<svg xmlns="http://www.w3.org/2000/svg"', 1)
                return svg.encode()
    return None


# names of other brands that sites show as badges (review widgets, lenders, software, associations): never a client logo
OTHER_BRANDS = re.compile(r"google|trustindex|elfsight|facebook|instagram|yelp|houzz|\bbbb\b|better-?business|jobtread|buildertrend|"
                          r"\buwm\b|formvue|angi\b|homeadvisor|thumbtack|nahb|\bnari\b|guildquality|buildzoom|porch|nextdoor|linkedin|"
                          r"youtube|tiktok|pinterest|twitter|paypal|visa|mastercard|financ|lender|mortgage|warranty|certif|member|"
                          r"review|rating|partner|client|award|badge|sponsor|powered|widget|trust", re.I)


SITE_CDNS = ("wp.com", "wixstatic.com", "squarespace-cdn.com", "squarespace.com", "shopify.com", "godaddysites.com", "img1.wsimg.com")


def _site(url):
    host = urllib.parse.urlsplit(url).netloc.lower().split(":")[0]
    for cdn in SITE_CDNS:  # website builders keep the site's own uploads on their CDN
        if host == cdn or host.endswith("." + cdn):
            return "site-cdn"
    parts = host[4:].split(".") if host.startswith("www.") else host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def candidates(page, base):
    """Logo URLs in preference order: [(url, why), ...]."""
    out = []

    def add(u, why):
        if u:
            full = urllib.parse.urljoin(base, u.strip())
            if full.startswith(("http://", "https://")) and full not in [x for x, _ in out]:
                out.append((full, why))

    imgs = IMG.findall(page)
    for t in imgs:
        if re.search(r"custom-logo", t, re.I):
            add(_src(t), "site logo (WordPress)")
    for item in site.jsonld(page):
        for node in (item, item.get("publisher") if isinstance(item.get("publisher"), dict) else None):
            if not node:
                continue
            lg = node.get("logo")
            if isinstance(lg, list):
                lg = lg[0] if lg else None
            if isinstance(lg, dict):
                lg = lg.get("url") or lg.get("contentUrl")
            if isinstance(lg, str):
                add(lg, "logo in structured data")
    own = _site(base)
    n = 0
    for t in imgs:  # images marked "logo": only the site's own files, never a widget's or a partner's badge
        if n >= 3:
            break
        if not re.search(r"""(class|id|alt)\s*=\s*["'][^"']*logo|src\s*=\s*["'][^"']*logo""", t, re.I) or OTHER_BRANDS.search(t):
            continue
        u = _src(t)
        if not u or _site(urllib.parse.urljoin(base, u)) not in (own, "site-cdn"):
            continue
        add(u, "header logo image" if n == 0 else "other header image")
        n += 1
    for t in LINK.findall(page):
        rel = (REL.search(t).group(1).lower() if REL.search(t) else "")
        href = HREF.search(t).group(1) if HREF.search(t) else None
        if "apple-touch-icon" in rel:
            add(href, "site app icon")
        elif "icon" in rel:
            sz = SIZES.search(t)
            big = sz and any(int(n) >= 192 for n in re.findall(r"(\d+)x\d+", sz.group(1)))
            if big or (href or "").lower().endswith(".svg"):
                add(href, "large site icon")
    return out


def full_size(url):
    """Builders serve a shrunken copy; ask for the original: Wix "/v1/fill/w_74,h_40/.." -> the file itself,
    Squarespace "?format=300w" -> 2500w, WordPress "logo-300x80.png" stays (the original may not exist)."""
    m = re.match(r"(https?://static\.wixstatic\.com/media/[^/]+)/v1/", url)
    if m:
        return m.group(1)
    if "squarespace-cdn.com" in url:
        return re.sub(r"format=\d+w", "format=2500w", url) if "format=" in url else url + ("&" if "?" in url else "?") + "format=2500w"
    return url


def fetch_image(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "image/*,*/*;q=0.5"})
    try:
        with netsafe.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None, None
            data = r.read(MAX_BYTES + 1)
    except Exception:  # blocked address, timeout, HTTP error: just no logo from here
        return None, None
    if len(data) > MAX_BYTES or len(data) < 100:
        return None, None
    return data, sniff(data)


def find(domain, calls=None, max_tries=6, name=None):
    """-> (bytes, mime, url, why) or None. Tries up to max_tries candidates and keeps the best-scoring one."""
    toks = brand_tokens(domain, name)
    code, _, page, final = fetch(f"https://{domain}/")
    if calls is not None:
        calls[0] += 1
    if code != 200 or not page:
        return None
    best = None
    svg = inline_svg(page)
    if svg and sniff(svg) == "image/svg+xml":
        best = (score(svg, "image/svg+xml", "inline header logo", "", toks), svg, "image/svg+xml", final or f"https://{domain}/", "inline header logo")
    for url, why in candidates(page, final or f"https://{domain}/")[:max_tries]:
        url = full_size(url)
        data, mime = fetch_image(url)
        if calls is not None:
            calls[0] += 1
        if not mime:
            continue
        sc = score(data, mime, why, url, toks)
        if sc >= 0 and (best is None or sc > best[0]):
            best = (sc, data, mime, url, why)
    return best[1:] if best else None
