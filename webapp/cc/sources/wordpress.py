"""WordPress / sitemap connector (change 64): read-only public requests, through the app's SSRF guard.

1. WordPress REST (/wp-json/wp/v2/posts): published posts with their real publish dates.
2. If REST is off or the site isn't WordPress: the posts sitemap. Sitemaps only carry "last modified", which would
   count edited old posts as new, so the first read is saved as a baseline and only URLs that appear later count.
   Under-counting shows as a warning; over-counting would hide a miss, so it isn't allowed.
3. Each post's live check: HTTP 200, no noindex (meta or X-Robots-Tag), canonical pointing at itself.
A site that blocks us (401/403/429, bot walls) is reported as blocked; we never try to get around it.
"""
import html
import json
import re
import urllib.error
import urllib.request

from aeo import netsafe
from ..core import same_url

UA = "Mozilla/5.0 (compatible; HBM-CommandCenter/1.0; +https://homebuildermarketers.com)"


class Blocked(Exception):
    pass


class NotWordPress(Exception):
    pass


def fetch(url, timeout=20, accept="text/html,application/json,application/xml,*/*"):
    """(status, headers, body, final_url). Status 0 = no response."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    try:
        with netsafe.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(3_000_000).decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), "", url
    except Exception:
        return 0, {}, "", url


def posts(domain, after_iso, calls=None, max_pages=5):
    out = []
    for page in range(1, max_pages + 1):
        url = (f"https://{domain}/wp-json/wp/v2/posts?per_page=100&page={page}&orderby=date&order=desc"
               f"&after={after_iso}&_fields=id,date,link,title,status,jetpack_featured_media_url,yoast_head_json.og_image")
        code, hdrs, body, _ = fetch(url, accept="application/json")
        if calls is not None:
            calls[0] += 1
        if code in (401, 403, 429):
            raise Blocked(f"WordPress answered HTTP {code}")
        if code == 400 and page > 1:
            break  # past the last page
        if code != 200:
            raise NotWordPress(f"no WordPress posts feed (HTTP {code or 'no response'})")
        try:
            data = json.loads(body)
        except ValueError:
            raise NotWordPress("posts feed isn't JSON (a security plugin or a non-WordPress site)")
        if not isinstance(data, list):
            raise NotWordPress("posts feed returned something unexpected")
        for p in data:
            og = ((p.get("yoast_head_json") or {}).get("og_image") or [{}])
            img = p.get("jetpack_featured_media_url") or (og[0].get("url") if og and isinstance(og[0], dict) else None)
            out.append({"id": str(p.get("id")), "date": (p.get("date") or "")[:19], "url": p.get("link") or "",
                        "title": html.unescape(re.sub(r"<[^>]+>", "", ((p.get("title") or {}).get("rendered") or ""))),
                        "image": img if isinstance(img, str) and img.startswith(("http://", "https://")) else None})
        total_pages = int((hdrs.get("X-WP-TotalPages") or hdrs.get("x-wp-totalpages") or "1") or 1)
        if page >= total_pages or len(data) < 100:
            break
    return out


LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def sitemap_post_urls(domain, calls=None):
    """Blog-post URLs from the site's sitemaps (posts sitemaps first; otherwise /blog/ URLs). [] if none."""
    for root in (f"https://{domain}/sitemap_index.xml", f"https://{domain}/wp-sitemap.xml", f"https://{domain}/sitemap.xml"):
        code, _, body, _ = fetch(root, accept="application/xml")
        if calls is not None:
            calls[0] += 1
        if code != 200 or "<loc>" not in body:
            continue
        locs = LOC.findall(body)
        if "<sitemapindex" not in body:
            return [u for u in locs if re.search(r"/(blog|news|articles?|posts?)/", u)]
        children = [u for u in locs if re.search(r"post-sitemap|posts-post|sitemap-posts|blog", u)] or []
        urls = []
        for u in children[:10]:
            c, _, b, _ = fetch(u, accept="application/xml")
            if calls is not None:
                calls[0] += 1
            if c == 200:
                urls += LOC.findall(b)
        return list(dict.fromkeys(urls))
    return []


META_ROBOTS = re.compile(r"<meta[^>]+name=[\"']robots[\"'][^>]*>", re.I)
CANON = re.compile(r"<link[^>]+rel=[\"']canonical[\"'][^>]*>", re.I)
HREF = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
CONTENT = re.compile(r"content=[\"']([^\"']+)[\"']", re.I)


OG_IMAGE = re.compile(r"<meta[^>]+(?:property|name)=[\"'](?:og:image(?::secure_url)?|twitter:image)[\"'][^>]*>", re.I)
OG_TITLE = re.compile(r"<meta[^>]+property=[\"']og:title[\"'][^>]*>", re.I)


def page_image(body, base):
    """The post's share image (og:image / twitter:image), absolute, or None. That's the picture the site itself
    chose to represent the post, so it's what the blog cards show."""
    import urllib.parse
    for m in OG_IMAGE.finditer(body or ""):
        c = CONTENT.search(m.group(0))
        if c:
            u = urllib.parse.urljoin(base, html.unescape(c.group(1).strip()))
            if u.startswith(("http://", "https://")):
                return u
    return None


def page_title(body):
    m = OG_TITLE.search(body or "")
    c = CONTENT.search(m.group(0)) if m else None
    if c:
        return html.unescape(c.group(1)).strip()
    m = re.search(r"<title[^>]*>(.*?)</title>", body or "", re.I | re.S)
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else None


def check_live(url):
    """Is this URL live and indexable right now? -> {ok, status, note, image, title}"""
    code, hdrs, body, final = fetch(url)
    res = _judge(url, code, hdrs, body, final)
    res["image"], res["title"] = (page_image(body, final), page_title(body)) if code == 200 else (None, None)
    return res


def _judge(url, code, hdrs, body, final):
    if code != 200:
        return {"ok": False, "status": code, "note": f"HTTP {code}" if code else "no response"}
    xr = (hdrs.get("X-Robots-Tag") or hdrs.get("x-robots-tag") or "").lower()
    m = META_ROBOTS.search(body)
    meta = (CONTENT.search(m.group(0)).group(1).lower() if m and CONTENT.search(m.group(0)) else "")
    if "noindex" in xr or "noindex" in meta:
        return {"ok": False, "status": code, "note": "noindex"}
    c = CANON.search(body)
    canon = HREF.search(c.group(0)).group(1) if c and HREF.search(c.group(0)) else ""
    if canon and not same_url(canon, final) and not same_url(canon, url):
        return {"ok": False, "status": code, "note": f"canonical points elsewhere ({canon[:120]})"}
    return {"ok": True, "status": code, "note": "200, indexable, self-canonical" if canon else "200, indexable (no canonical tag)"}


def home_status(domain):
    code, _, _, _ = fetch(f"https://{domain}/")
    return code
