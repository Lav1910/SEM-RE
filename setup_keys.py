#!/usr/bin/env python3
"""Add or update the API keys the audit tool uses. Run it in a terminal and type each key yourself.

- Input is hidden and keys are never printed.
- Keys are saved to ~/.config/hbm-aeo/keys.env with file mode 600 (only your user can read it).
- Press Return on any prompt to keep the current value (or leave it unset).
- Each provider is tested with one small request after saving.
"""
import getpass
import os
import sys
if os.name == "nt" and not sys.flags.utf8_mode:  # Windows: run in UTF-8 mode so reports and data files save correctly
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-X", "utf8"] + sys.argv))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aeo import config  # noqa: E402

PROMPTS = [
    ("OPENAI_API_KEY", "OpenAI API key (platform.openai.com > API keys)"),
    ("GEMINI_API_KEY", "Gemini API key (aistudio.google.com > Get API key)"),
    ("PERPLEXITY_API_KEY", "Perplexity API key (perplexity.ai > Settings > API)"),
    ("DATAFORSEO_LOGIN", "DataForSEO API login (app.dataforseo.com > API access)"),
    ("DATAFORSEO_PASSWORD", "DataForSEO API password"),
    ("SEMRUSH_API_KEY", "Semrush API key (only needed on a server; this Mac already has it)"),
    ("DATAWISE_TOKEN", "DataWise personal token (datawiseseo.com > MCP & AI assistants > Create token, starts with dwmcp_)"),
]


def _clean_b64(text):
    """Keep only Base64 characters: terminal paste can add bracketed-paste markers, spaces or stray symbols."""
    import re
    text = re.sub(r"\x1b\[20[01]~|\[20[01]~", "", text)
    return re.sub(r"[^A-Za-z0-9+/=]", "", text)


def main():
    current = config._load_file()
    print("HBM AEO audit: API keys. Press Return to keep the current value.\n")
    from_token = False
    for name, label in PROMPTS:
        state = "set" if current.get(name) else "not set"
        if name == "DATAFORSEO_LOGIN":
            # DataForSEO MCP instructions often carry the credentials as "Authorization: Basic <code>".
            # That code is base64("api_login:api_password"); accept it here so no separate API key is needed.
            tok = getpass.getpass("DataForSEO: paste the Basic code from your MCP instructions, or press Return to type login and password: ").strip()
            tok = _clean_b64(tok.split("Basic", 1)[-1])
            if tok:
                import base64, binascii
                try:
                    login, pw = base64.b64decode(tok + "=" * (-len(tok) % 4)).decode().split(":", 1)
                    current["DATAFORSEO_LOGIN"], current["DATAFORSEO_PASSWORD"] = login.strip(), pw.strip()
                    print("  DataForSEO login and password read from the Basic code.")
                    from_token = True
                    continue
                except (binascii.Error, ValueError, UnicodeDecodeError):
                    print("  That doesn't look like a Basic code; enter the login and password instead.")
        if name == "DATAFORSEO_PASSWORD" and from_token:
            continue
        val = getpass.getpass(f"{label} [{state}]: ").strip()
        import re  # terminal paste can wrap keys in hidden markers or add spaces; no key contains these
        val = re.sub(r"\x1b\[20[01]~|\[20[01]~|[\s|]", "", val)
        if val and name == "DATAFORSEO_LOGIN" and len("".join(val.split())) > 30:
            # the Base64 code pasted into the login prompt: split it into login and password
            import base64, binascii
            raw = _clean_b64(val)
            try:
                login, pw = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode().split(":", 1)
                current["DATAFORSEO_LOGIN"], current["DATAFORSEO_PASSWORD"] = login.strip(), pw.strip()
                print("  Read the login and password from the Base64 code.")
                from_token = True
                continue
            except (binascii.Error, ValueError, UnicodeDecodeError):
                pass
        if val:
            current[name] = val
    os.makedirs(os.path.dirname(config.KEYS_FILE), exist_ok=True)
    tmp = config.KEYS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# HBM AEO audit keys. Do not share or commit this file.\n")
        for name, _ in PROMPTS:
            if current.get(name):
                f.write(f"{name}={current[name]}\n")
    os.replace(tmp, config.KEYS_FILE)
    os.chmod(config.KEYS_FILE, 0o600)
    print(f"\nSaved to {config.KEYS_FILE} (readable only by you).")

    # quick live test: one tiny question per configured platform
    import importlib
    importlib.reload(config)
    from aeo import engines
    importlib.reload(engines)
    q = {"id": "test", "prompt": "Who are the best custom home builders in Cincinnati, OH?", "keyword": "custom home builders cincinnati"}
    market = {"city": "Cincinnati", "state": "Ohio", "state_code": "OH"}
    print("\nTesting each platform with one question ...")
    for en in engines.ENGINES:
        if not engines.is_configured(en):
            print(f"  {engines.ENGINE_LABELS[en]:22} not configured")
            continue
        a = engines.ask(en, q, market)
        msg = {"ok": f"OK ({len(a['citations'])} sources cited)", "no_answer": "connected, no AI answer for this search"}.get(a["status"], a.get("error") or a["status"])
        print(f"  {engines.ENGINE_LABELS[en]:22} {msg}")


if __name__ == "__main__":
    main()
