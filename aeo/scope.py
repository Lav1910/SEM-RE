"""What counts as "the builder's site" for an audit (Semrush-style scope: Root Domain, Subdomain, Subfolder, Exact URL)."""
import re
import urllib.parse

SCOPES = {"root": "Root Domain", "subdomain": "Subdomain", "subfolder": "Subfolder", "url": "Exact URL"}


def _parts(url):
    if not re.match(r"^https?://", url or "", re.I):
        url = "https://" + (url or "")
    p = urllib.parse.urlparse(url)
    host = p.netloc.lower().split("@")[-1].split(":")[0]
    host = re.sub(r"^www\.", "", host)
    path = re.sub(r"/+$", "", p.path or "") or ""
    return host, path


def root_of(host):
    """Registrable domain, good enough for builder sites (handles .co.uk-style two-part suffixes)."""
    bits = host.split(".")
    if len(bits) >= 3 and len(bits[-1]) == 2 and bits[-2] in ("co", "com", "org", "net", "ac", "gov"):
        return ".".join(bits[-3:])
    return ".".join(bits[-2:]) if len(bits) >= 2 else host


def make(url, kind="root"):
    kind = kind if kind in SCOPES else "root"
    host, path = _parts(url)
    sc = {"type": kind, "host": host, "path": path, "domain": root_of(host)}
    if kind == "root":
        sc["label"] = sc["domain"]
    elif kind == "subdomain":
        sc["label"] = host
    elif kind == "subfolder":
        sc["label"] = host + (path or "") + "/"
    else:
        sc["label"] = host + (path or "/")
    return sc


def semrush_target(sc):
    """Semrush accepts a domain, subdomain, subfolder (with trailing slash) or full URL as `target`."""
    if sc["type"] == "root":
        return sc["domain"]
    if sc["type"] == "subdomain":
        return sc["host"]
    if sc["type"] == "subfolder":
        return sc["host"] + sc["path"] + "/"
    return "https://" + sc["host"] + (sc["path"] or "/")


def start_url(sc):
    if sc["type"] in ("subfolder", "url"):
        return "https://" + sc["host"] + (sc["path"] or "") + ("/" if sc["type"] == "subfolder" else "")
    return "https://" + (sc["host"] if sc["type"] == "subdomain" else sc["domain"]) + "/"


def matches(url, sc):
    host, path = _parts(url)
    t = sc.get("type", "root")
    if t == "root":
        return host == sc["domain"] or host.endswith("." + sc["domain"])
    if t == "subdomain":
        return host == sc["host"]
    if t == "subfolder":
        return host == sc["host"] and (path == sc["path"] or path.startswith(sc["path"] + "/"))
    return host == sc["host"] and path == sc["path"]


def for_audit(A):
    """Scope saved with an audit; older audits are treated as Root Domain of their domain."""
    return A["profile"].get("scope") or make(A["profile"]["domain"], "root")
