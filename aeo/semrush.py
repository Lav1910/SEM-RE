"""Semrush context through Semrush's official MCP endpoint (https://mcp.semrush.com/v2/mcp, streamable HTTP).

Auth: SEMRUSH_API_KEY (env or ~/.config/hbm-aeo/keys.env), sent as "Authorization: Apikey <key>". On a machine
where Claude Code already has the Semrush server registered, the key is read from ~/.claude.json instead.
Per audit: domain_rank (10 units), keywords where Google's AI Overview cites the domain (~200), and search
volume for the question keywords (~10 per keyword). About 350 units in total.
"""
import json
import os
import threading
import urllib.request

from . import config

ENDPOINT = "https://mcp.semrush.com/v2/mcp"
_local = threading.local()


def _auth_header():
    k = config.key("SEMRUSH_API_KEY")
    if k:
        return "Apikey " + k
    try:
        srv = json.load(open(os.path.expanduser("~/.claude.json"))).get("mcpServers", {}).get("semrush", {})
        return (srv.get("headers") or {}).get("Authorization")
    except Exception:
        return None


def available():
    return bool(_auth_header())


class _Client:
    def __init__(self):
        self.sid = None
        self.auth = _auth_header()
        self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "hbm-aeo", "version": "1"}}})
        self._call({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, msg):
        h = {"Authorization": self.auth, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "hbm-aeo/1.0 (+https://homebuildermarketers.com)"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(ENDPOINT, data=json.dumps(msg).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=180) as r:
            self.sid = r.headers.get("Mcp-Session-Id") or self.sid
            body = r.read().decode()
        if not body.strip():
            return None
        if body.lstrip().startswith("{"):
            return json.loads(body)
        out = None
        for line in body.splitlines():
            if line.startswith("data:"):
                d = json.loads(line[5:])
                if "id" in d:
                    out = d
        return out

    def tool(self, name, args):
        r = self._call({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
        res = (r or {}).get("result") or {}
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        return json.loads(text) if text.strip().startswith("{") else {"message": text or str(r)[:300]}


def _client():
    if getattr(_local, "c", None) is None:
        _local.c = _Client()
    return _local.c


def _report(report, params):
    try:
        d = _client().tool("execute_report", {"report": report, "params": params})
    except Exception:
        _local.c = None  # session expired: reconnect once
        d = _client().tool("execute_report", {"report": report, "params": params})
    if "data" not in d:
        raise RuntimeError(d.get("message") or str(d)[:300])
    lines = d["data"].strip().splitlines()
    head = lines[0].split(";") if lines else []
    raw = [r.split(";") for r in lines[1:]]
    units = (d.get("metadata") or {}).get("usage", {}).get("api_units", 0)
    return [dict(zip(head, r)) for r in raw], raw, units


def database(market):
    """change 58: Semrush keeps a separate database per country ("us", "ca")."""
    from .engines import country_of
    return country_of(market).lower()


def history(domain, res=None, months=12, db="us"):
    """Monthly Google AI Overview history for the domain (Semrush resource_rank_history, ~10 units per month):
    how many of its ranking searches show an AI Overview, and how many of those AI Overviews cite it."""
    try:
        _, raw, u = _report("resource_rank_history", {"database": db, "target": domain, "display_limit": months,
                                                      "export_columns": ["date", "organic_keywords", "organic_traffic",
                                                                         "serp_ai_overview_keywords", "serp_ai_overview_positions"]})
        if res is not None:
            res["units"] += u
        out = []
        for r in raw:
            d = r[0]
            out.append({"month": f"{d[:4]}-{d[4:6]}", "organic_keywords": int(r[1] or 0), "organic_traffic": int(r[2] or 0),
                        "aio_keywords": int(r[3] or 0), "aio_cited": int(r[4] or 0)})
        return sorted(out, key=lambda x: x["month"])
    except Exception as e:
        if res is not None:
            res["errors"].append(f"history: {e}")
        return []


def context(domain, keywords, db="us"):
    """Return Semrush facts for the report, never raising: missing parts become None."""
    res = {"units": 0, "domain": None, "ai_cited_keywords": [], "volumes": {}, "trends": {}, "history": [], "errors": []}
    try:
        _, raw, u = _report("domain_rank", {"database": db, "target": domain, "export_columns": [
            "domain", "organic_keywords", "organic_traffic", "serp_ai_overview_keywords", "serp_ai_overview_positions",
            "serp_local_pack_positions"]})
        res["units"] += u
        if raw:
            vals = raw[0]  # columns repeat names (two "AI overview"), so read by position
            res["domain"] = {"organic_keywords": int(vals[1]), "organic_traffic": int(vals[2]),
                             "aio_keywords": int(vals[3]), "aio_cited": int(vals[4]), "pack_positions": int(vals[5])}
    except Exception as e:
        res["errors"].append(f"domain_rank: {e}")
    res["history"] = history(domain, res, db=db)
    try:
        rows, _, u = _report("resource_organic", {"database": db, "target": domain, "display_positions_type": "serp_features",
                                                  "display_filter": [{"sign": "+", "field": "domain_serp_features", "operation": "equals", "value": "52"}],
                                                  "display_limit": 20, "display_sort": "volume_desc",
                                                  "export_columns": ["keyword", "volume", "url"]})
        res["units"] += u
        seen = set()
        for r in rows:
            k = r.get("Keyword")
            if k and k not in seen:
                seen.add(k)
                res["ai_cited_keywords"].append({"keyword": k, "volume": int(r.get("Search Volume") or 0), "url": r.get("Url", "")})
    except Exception as e:
        res["errors"].append(f"resource_organic: {e}")
    try:
        kws = [k for k in dict.fromkeys(keywords) if k][:100]
        rows, _, u = _report("phrase_these", {"database": db, "phrase": ";".join(kws)})
        res["units"] += u
        res["volumes"] = {r["Keyword"]: int(r.get("Search Volume") or 0) for r in rows}
        # 12 monthly values relative to the peak (Semrush "Trends"), shown as a sparkline like Semrush's volume_trend
        res["trends"] = {r["Keyword"]: [float(x) for x in (r.get("Trends") or "").split(",") if x.strip()] for r in rows}
    except Exception as e:
        res["errors"].append(f"phrase_these: {e}")
    return res
