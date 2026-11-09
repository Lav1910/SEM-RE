"""Guard for fetching user-supplied website addresses from the server (blocks SSRF).

Audits fetch whatever site a salesperson types, plus every link in that site's sitemap. Without a guard, an address
like http://169.254.169.254/ or http://localhost:5432/ would make the server read its own cloud metadata or internal
services. Every request and every redirect hop must be http(s), on a normal web port, to a host whose DNS answers are
all public internet addresses.
"""
import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_PORTS = {None, 80, 443, 8080, 8443}


class BlockedURL(urllib.error.URLError):
    pass


def check(url):
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise BlockedURL(f"blocked scheme or host: {url[:80]}")
    try:
        port = p.port
    except ValueError:
        raise BlockedURL("bad port")
    if port not in ALLOWED_PORTS:
        raise BlockedURL(f"blocked port {port}")
    host = p.hostname.strip("[]").lower()
    if host in ("localhost",) or host.endswith((".local", ".internal", ".localhost")):
        raise BlockedURL(f"blocked host {host}")
    try:
        infos = socket.getaddrinfo(host, port or (443 if p.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise BlockedURL(f"can't resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        if not ip.is_global or ip.is_multicast or ip.is_reserved:
            raise BlockedURL(f"blocked private address for {host}")
    return url


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 6

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check(urllib.parse.urljoin(req.full_url, newurl))
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SafeRedirect)


def urlopen(req, timeout=25):
    check(req.full_url if isinstance(req, urllib.request.Request) else req)
    return _opener.open(req, timeout=timeout)
