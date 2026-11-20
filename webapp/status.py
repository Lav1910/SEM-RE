"""Plain-language status for each AI platform, from an audit's stored answers or the current configuration."""
import re

from aeo import config, engines


# change 49: what the report and dashboard show; no provider names, tools or setup steps (Admin keeps the technical text)
PLAIN = {"Needs verification": "This platform's account needs verifying before it can answer.",
         "Needs credits": "This platform's account has run out of credit.",
         "Key rejected": "This platform couldn't be reached.",
         "Rate limited": "This platform was busy; some answers were skipped.",
         "Timed out": "This platform didn't answer in time.",
         "No data": "No AI answer was available for this search.",
         "Provider hiccup": "This platform had a temporary problem; some answers were skipped.",
         "Error": "This platform didn't answer."}


def friendly_error(err, plain=False):
    if plain:
        label = friendly_error(err)[0]
        return label, PLAIN[label]
    e = (err or "").lower()
    if "40104" in e or "verify your account" in e:
        return "Needs verification", "The DataForSEO account must be verified at app.dataforseo.com before the API works."
    if re.search(r"\b(40101|40106|50000)\b", e) or "internal se server error" in e or "partial results" in e:
        # change 81: DataForSEO's own temporary failures. "40101" contains "401" and used to read as "Key rejected"
        return "Provider hiccup", "Google's data provider couldn't fetch some results this time; those questions were skipped."
    if "credit" in e or "insufficient_quota" in e or "quota" in e:
        return "Needs credits", "The account has no API credits left. Add credits in the provider's billing page."
    if re.search(r"\b401\b", e) or "invalid api key" in e or "unauthorized" in e:
        return "Key rejected", "The provider rejected the API key. Re-enter it with setup_keys.py."
    if re.search(r"\b429\b", e) or "rate limit" in e:
        return "Rate limited", "The provider asked us to slow down; some answers were skipped."
    if "timed out" in e or "timeout" in e:
        return "Timed out", "The provider didn't answer in time."
    if "no data" in e:
        return "No data", "Semrush has no data for this search."
    return "Error", (err or "")[:160]


def for_audit(A, engine):
    ans = [a for a in A.get("answers", []) if a["engine"] == engine]
    if not ans or all(a["status"] == "not_configured" for a in ans):
        return {"state": "off", "label": "Not connected", "detail": "No key for this platform."}
    ok = [a for a in ans if a["status"] in ("ok", "no_answer")]
    errs = [a for a in ans if a["status"] == "error"]
    via = ""  # change 49: no tool names on the report
    if not ok and errs:
        label, detail = friendly_error(errs[0].get("error"), plain=True)
        return {"state": "bad", "label": label, "detail": detail}
    if errs:
        label, detail = friendly_error(errs[0].get("error"), plain=True)
        return {"state": "warn", "label": f"Measured{via} · {len(errs)} skipped", "detail": detail}
    return {"state": "good", "label": "Measured" + via, "detail": ""}


def current(last_audit=None, plain=False):
    """Live connection status for the dashboard/admin: saved keys plus the latest real call to each platform
    (from aeo.health). A key saved after the last call shows as "Not tested yet" rather than a green tick."""
    import os
    from aeo import health
    H = health.load()
    try:
        keys_t = os.path.getmtime(config.KEYS_FILE)
    except OSError:
        keys_t = 0
    out = {}
    for en in engines.ENGINES:
        if not engines.is_configured(en):
            out[en] = {"state": "off", "label": "Not connected"}
            continue
        h = H.get(en)
        if h and ((h.get("kp") and h["kp"] != health.key_print(en)) or (not h.get("kp") and not h["ok"])):
            h = None  # change 27: the key changed since this result (or an old error from before keys were tracked)
        if not h or h["t"] < keys_t - 1:
            st = {"state": "mute", "label": "Not tested yet"}
            if en == "google_aio" and not config.key("DATAFORSEO_LOGIN"):
                st = {"state": "good", "label": "Connected via Semrush"}
        elif h["ok"]:
            st = {"state": "good", "label": "Connected"}
            if h.get("via") == "semrush":
                st["label"] = "Connected via Semrush"
                if h.get("error"):
                    st = {"state": "warn", "label": "Via Semrush · DataForSEO " + friendly_error(h["error"])[0].lower()}
        else:
            label, detail = friendly_error(h.get("error"))
            st = {"state": "warn" if label == "Provider hiccup" else "bad", "label": label, "detail": detail}  # a passing failure isn't a broken connection
        if plain:  # change 49: the dashboard pills say only how the platform is doing, never which tool it goes through
            st = {"state": st["state"], "label": {"good": "Connected", "warn": "Connected", "mute": "Not tested yet", "off": "Not connected"}.get(st["state"], st["label"])}
        out[en] = st
    return out
