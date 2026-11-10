"""Monthly Google search volumes from DataForSEO Labs (change 40), used when Semrush isn't connected.

One request per audit for all its keywords: about $0.01 plus a hundredth of a cent per keyword, answered in under
a second. Keywords DataForSEO has no data for are simply left out.
"""
import base64
import json
import urllib.request

from . import config, engines

URL = "https://api.dataforseo.com/v3/dataforseo_labs/google/keyword_overview/live"


def available():
    return engines.is_configured("google_ai_mode")  # the same DataForSEO login


def volumes(keywords, timeout=25):
    """{keyword: monthly US searches} for the keywords DataForSEO knows. Never raises; returns {} on any problem."""
    kws = list(dict.fromkeys(k.strip().lower() for k in keywords if k and k.strip()))[:700]
    if not kws or not available():
        return {}
    auth = base64.b64encode(f"{config.key('DATAFORSEO_LOGIN')}:{config.key('DATAFORSEO_PASSWORD')}".encode()).decode()
    req = urllib.request.Request(URL, method="POST", data=json.dumps([{"keywords": kws, "location_code": 2840, "language_code": "en"}]).encode(),
                                 headers={"Authorization": "Basic " + auth, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.load(r)
        task = (d.get("tasks") or [{}])[0]
        try:
            from . import budget
            budget.settle(0.0, task.get("cost"))  # add the real charge to the spend ledger
        except Exception:
            pass
        out = {}
        for res in task.get("result") or []:
            for it in res.get("items") or []:
                v = (it.get("keyword_info") or {}).get("search_volume")
                if it.get("keyword") and v is not None:
                    out[it["keyword"].lower()] = int(v)
        return out
    except Exception:
        return {}
