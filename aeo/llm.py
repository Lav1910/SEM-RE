"""Plain text generation (no web search) for helper tasks: validating places, writing strategy content.
Uses Gemini by default; ChatGPT when chosen and the OpenAI account has credits."""
import json
import re
import urllib.request

from . import config


def available(provider="gemini"):
    return bool(config.key("GEMINI_API_KEY" if provider == "gemini" else "OPENAI_API_KEY"))


def text(prompt, provider="gemini", timeout=120):
    """Return (text, cost_usd_estimate). Raises RuntimeError on failure."""
    if provider == "chatgpt":
        body = {"model": config.SETTINGS["OPENAI_MODEL"], "input": prompt}
        req = urllib.request.Request("https://api.openai.com/v1/responses", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + config.key("OPENAI_API_KEY")})
    else:
        body = {"model": config.SETTINGS["GEMINI_MODEL"], "input": prompt}
        req = urllib.request.Request("https://generativelanguage.googleapis.com/v1beta/interactions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": config.key("GEMINI_API_KEY"),
                                              "Api-Revision": config.SETTINGS["GEMINI_API_REVISION"]})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()[:200]}")
    if provider == "chatgpt":
        out = " ".join(c.get("text", "") for it in d.get("output", []) if it.get("type") == "message"
                       for c in it.get("content", []) if c.get("type") == "output_text")
        u = d.get("usage") or {}
        cost = (u.get("input_tokens", 0) * 2.5 + u.get("output_tokens", 0) * 10) / 1e6
    else:
        out = " ".join(c.get("text", "") for st in d.get("steps", []) if st.get("type") == "model_output"
                       for c in st.get("content", []) if c.get("type") == "text")
        u = d.get("usage") or {}
        cost = (u.get("total_input_tokens", u.get("input_tokens", 0)) * 0.3 + u.get("total_output_tokens", u.get("output_tokens", 0)) * 2.5) / 1e6
    if not out.strip():
        raise RuntimeError("empty answer")
    return out.strip(), round(cost, 5)


def json_answer(prompt, provider="gemini", timeout=120):
    out, cost = text(prompt, provider, timeout)
    m = re.search(r"(\{.*\}|\[.*\])", out, re.S)
    if not m:
        raise RuntimeError("no JSON in answer")
    return json.loads(m.group(1)), cost
