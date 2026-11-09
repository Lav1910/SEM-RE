#!/usr/bin/env python3
"""Connect Semrush's official MCP server to Claude Code using your API key.

- Prompts for the key with hidden input (it is never printed).
- Tests the key against https://mcp.semrush.com/v2/mcp before saving.
- Backs up ~/.claude.json, then adds a user-level "semrush" MCP server
  that sends the header  Authorization: Apikey <your key>.
"""
import getpass
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

URL = "https://mcp.semrush.com/v2/mcp"
CONFIG = os.path.expanduser("~/.claude.json")


def test_key(key: str, scheme: str) -> bool:
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "connect-semrush", "version": "1"}},
    }).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Authorization": f"{scheme} {key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            text = resp.read(4000).decode("utf-8", "replace")
            ok = resp.status == 200 and '"result"' in text
            print(f"Semrush responded: HTTP {resp.status}" + ("" if ok else " (unexpected reply)"))
            return ok
    except urllib.error.HTTPError as e:
        print(f"Semrush rejected the request: HTTP {e.code} {e.reason}")
    except Exception as e:  # network problems etc.
        print(f"Could not reach Semrush: {e}")
    return False


def main() -> int:
    key = getpass.getpass("Paste your Semrush API key (input is hidden), then press Return: ").strip()
    if not key:
        print("No key entered; nothing changed.")
        return 1
    for prefix in ("apikey ", "bearer "):
        if key.lower().startswith(prefix):
            key = key[len(prefix):].strip()

    scheme = None
    for candidate in ("Apikey", "Bearer"):
        print(f"Testing the key with Semrush ({candidate} header)...")
        if test_key(key, candidate):
            scheme = candidate
            break
    if scheme is None:
        print("Key test failed; ~/.claude.json was NOT changed.")
        print("Check that you copied the whole key from Semrush (Subscription info -> API units),")
        print("and that your Semrush plan includes API/MCP access.")
        return 1

    with open(CONFIG) as f:
        data = json.load(f)
    backup = f"{CONFIG}.semrush.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(CONFIG, backup)

    data.setdefault("mcpServers", {})["semrush"] = {
        "type": "http",
        "url": URL,
        "headers": {"Authorization": f"{scheme} {key}"},
    }
    tmp = CONFIG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)

    print(f"Done. Added the 'semrush' MCP server to {CONFIG}")
    print(f"Backup of the previous config: {backup}")
    print("Start a new Claude Code session to use Semrush.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
