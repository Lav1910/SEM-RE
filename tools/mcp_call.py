#!/usr/bin/env python3
"""Tiny MCP client for servers registered in ~/.claude.json (http or stdio).
Usage: mcp_call.py <server> list            -> tool names + input schemas (JSON)
       mcp_call.py <server> <tool> '<json>' -> tool result (JSON)
Credentials are read from the config and never printed."""
import json, os, sys, subprocess, urllib.request, select, time
cfg = json.load(open(os.path.expanduser("~/.claude.json")))["mcpServers"][sys.argv[1]]
INIT = {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"hbm-probe","version":"1"}}}

class Http:
    def __init__(s, c): s.c=c; s.sid=None
    def call(s, m, timeout=300):
        h={**s.c.get("headers",{}),"Content-Type":"application/json","Accept":"application/json, text/event-stream"}
        if s.sid: h["Mcp-Session-Id"]=s.sid
        r=urllib.request.urlopen(urllib.request.Request(s.c["url"],data=json.dumps(m).encode(),headers=h,method="POST"),timeout=timeout)
        s.sid=r.headers.get("Mcp-Session-Id") or s.sid; b=r.read().decode()
        if not b.strip(): return None
        if b.lstrip().startswith("{"): return json.loads(b)
        out=None
        for l in b.splitlines():
            if l.startswith("data:"):
                d=json.loads(l[5:])
                if "id" in d: out=d
        return out

class Stdio:
    def __init__(s, c):
        env={"HOME":os.path.expanduser("~"),"PATH":os.path.expanduser("~/.local/node/bin")+":/usr/bin:/bin:/usr/sbin:/sbin",**c.get("env",{}),**{k:v for k,v in os.environ.items() if k.startswith("REA_")}}
        s.p=subprocess.Popen([c["command"],*c.get("args",[])],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env,cwd=os.path.expanduser("~"))
    def call(s, m, timeout=300):
        s.p.stdin.write((json.dumps(m)+"\n").encode()); s.p.stdin.flush()
        if "id" not in m: return None
        end=time.time()+timeout
        while time.time()<end:
            r,_,_=select.select([s.p.stdout],[],[],1)
            if r:
                l=s.p.stdout.readline()
                if not l: break
                try: d=json.loads(l)
                except Exception: continue
                if d.get("id")==m["id"]: return d
        raise TimeoutError("no reply")

c = Http(cfg) if cfg.get("type")=="http" else Stdio(cfg)
c.call(INIT); c.call({"jsonrpc":"2.0","method":"notifications/initialized"})
if sys.argv[2]=="list":
    r=c.call({"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}})
    print(json.dumps([{"name":t["name"],"description":t.get("description",""),"input":t.get("inputSchema")} for t in r["result"]["tools"]],indent=1))
else:
    args=json.loads(sys.argv[3]) if len(sys.argv)>3 else {}
    r=c.call({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":sys.argv[2],"arguments":args}})
    res=r.get("result",r)
    for item in res.get("content",[]) if isinstance(res,dict) else []:
        if item.get("type")=="text": print(item["text"])
    if isinstance(res,dict) and res.get("structuredContent") and not res.get("content"): print(json.dumps(res["structuredContent"],indent=1))
    if isinstance(res,dict) and res.get("isError"): print("[isError]")
    if "error" in r: print(json.dumps(r["error"]))
if isinstance(c,Stdio): c.p.terminate()
