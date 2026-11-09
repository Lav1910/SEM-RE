"""Plain-language status for each AI platform, from an audit's stored answers or the current configuration."""
from aeo import config, engines


def friendly_error(err):
    e = (err or "").lower()
    if "40104" in e or "verify your account" in e:
        return "Needs verification", "The DataForSEO account must be verified at app.dataforseo.com before the API works."
    if "credit" in e or "insufficient_quota" in e or "quota" in e:
        return "Needs credits", "The account has no API credits left. Add credits in the provider's billing page."
    if "401" in e or "invalid api key" in e or "unauthorized" in e:
        return "Key rejected", "The provider rejected the API key. Re-enter it with setup_keys.py."
    if "429" in e or "rate limit" in e:
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
    via = " via Semrush" if any(a.get("source") == "semrush" for a in ans) else ""
    if not ok and errs:
        label, detail = friendly_error(errs[0].get("error"))
        return {"state": "bad", "label": label, "detail": detail}
    if errs:
        label, detail = friendly_error(errs[0].get("error"))
        return {"state": "warn", "label": f"Measured{via} · {len(errs)} skipped", "detail": detail}
    return {"state": "good", "label": "Measured" + via, "detail": ""}


def current(last_audit=None):
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
            st = {"state": "bad", "label": label, "detail": detail}
        out[en] = st
    return out
