#!/usr/bin/env python3
"""List the DataWise MCP tools and their inputs (token read from the keys file, never printed)."""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aeo import config  # noqa: E402
from aeo.mcp_http import MCPClient  # noqa: E402

tok = config.key("DATAWISE_TOKEN")
if not tok:
    raise SystemExit("No DATAWISE_TOKEN saved yet. Run setup_keys.py first.")
c = MCPClient("https://mcp.datawiseseo.com/mcp", "Bearer " + tok)
for t in c.tools():
    props = (t.get("inputSchema") or {}).get("properties", {})
    inputs = ", ".join(k + ":" + str(v.get("type", "?")) for k, v in props.items())
    print("## " + t["name"] + "\n  " + (t.get("description") or "")[:400] + "\n  inputs: " + inputs)
