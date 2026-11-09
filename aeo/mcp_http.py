"""Minimal client for remote MCP servers over streamable HTTP with a bearer/API-key header (DataWise, etc.)."""
import json
import urllib.request


class MCPClient:
    def __init__(self, url, auth_header, name="hbm-aeo"):
        self.url, self.auth, self.sid = url, auth_header, None
        self._call({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}}})
        self._call({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _call(self, msg, timeout=180):
        h = {"Authorization": self.auth, "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "User-Agent": "hbm-aeo/1.0 (+https://homebuildermarketers.com)"}
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        req = urllib.request.Request(self.url, data=json.dumps(msg).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
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

    def tools(self):
        r = self._call({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        return (r or {}).get("result", {}).get("tools", [])

    def call(self, name, args):
        r = self._call({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": name, "arguments": args}})
        if r and "error" in r:
            raise RuntimeError(str(r["error"])[:300])
        res = (r or {}).get("result") or {}
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        try:
            return json.loads(text)
        except Exception:
            return {"text": text, "isError": res.get("isError")}
