"""Keys and settings for the HBM AEO audit tool.

Keys live in ~/.config/hbm-aeo/keys.env (created by setup_keys.py, file mode 600) or in
environment variables of the same name. They are never printed or written to audit output.
"""
import os

KEYS_FILE = os.path.expanduser("~/.config/hbm-aeo/keys.env")

KEY_NAMES = {
    "OPENAI_API_KEY": "OpenAI (ChatGPT answers)",
    "GEMINI_API_KEY": "Google Gemini API (Gemini answers)",
    "PERPLEXITY_API_KEY": "Perplexity Agent API (Perplexity answers)",
    "DATAFORSEO_LOGIN": "DataForSEO login (Google AI Overviews, AI Mode, Maps pack)",
    "DATAFORSEO_PASSWORD": "DataForSEO API password",
    "SEMRUSH_API_KEY": "Semrush API key (search volumes, AI Overview footprint)",
    "DATAWISE_TOKEN": "DataWise personal MCP token (Google AI data, Local Pack)",
}

# Model / configuration defaults, taken from each provider's docs (November 2026).
# Override with environment variables if a provider renames a model.
SETTINGS = {
    "OPENAI_MODEL": os.environ.get("HBM_OPENAI_MODEL", "gpt-6-sol"),  # change 29: about 30% faster than gpt-6-astra in timing tests
    "GEMINI_MODEL": os.environ.get("HBM_GEMINI_MODEL", "gemini-3.8-flash"),
    "GEMINI_API_REVISION": os.environ.get("HBM_GEMINI_API_REVISION", "2026-05-20"),
    "PERPLEXITY_EFFORT": os.environ.get("HBM_PERPLEXITY_EFFORT", "low"),
    "HTTP_TIMEOUT": int(os.environ.get("HBM_HTTP_TIMEOUT", "120")),
    "WORKERS": int(os.environ.get("HBM_WORKERS", "8")),
    "DATA_WAIT": int(os.environ.get("HBM_DATA_WAIT", "30")),  # change 29: seconds to wait for Semrush / DataWise search data
    "PER_PLATFORM": int(os.environ.get("HBM_PER_PLATFORM", "25")),  # change 29: questions asked at once on each platform
    "MAX_LOCATIONS": int(os.environ.get("HBM_MAX_LOCATIONS", "5")),  # main market + up to 4 more cities per audit
}


def _load_file():
    vals = {}
    try:
        with open(KEYS_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return vals


_FILE = _load_file()


def key(name):
    """Return a key value, or None when it is not configured."""
    v = os.environ.get(name) or _FILE.get(name)
    return v or None


def configured():
    return {name: bool(key(name)) for name in KEY_NAMES}
