"""DataWise (datawiseseo.com) MCP: extra data on top of the AI answers. DataWise wraps DataForSEO.

Per audit (about $0.22 of the DataWise daily budget):
  datawise_keyword_metrics   difficulty + intent for the question keywords (Semrush topics carry both)
  datawise_local_reviews     Google Business Profile: rating, reviews, category, phone, photos
  datawise_ai_mentions x2    DataForSEO LLM Mentions database: mentions + AI search volume for the builder and
                             up to 4 competitors, on Google AI (Mode/Overviews) and ChatGPT (~$0.10 each)
Auth: DATAWISE_TOKEN (a personal MCP token). Every call is optional; failures leave the field empty.
"""
import os
import re

from . import config
from .mcp_http import MCPClient

URL = "https://mcp.datawiseseo.com/mcp"


def available():
    return bool(config.key("DATAWISE_TOKEN")) and os.environ.get("HBM_USE_DATAWISE", "1") != "0"


def _client():
    return MCPClient(URL, "Bearer " + config.key("DATAWISE_TOKEN"))


def _bare(d):
    return re.sub(r"^www\.", "", (d or "").lower())


def early(profile, market, keywords):
    """Calls that don't depend on the AI answers: keyword metrics and the Google Business Profile."""
    out = {"keywords": {}, "gbp": None, "errors": []}
    try:
        c = _client()
    except Exception as e:
        out["errors"].append(f"connect: {e}")
        return out
    try:
        r = c.call("datawise_keyword_metrics", {"keywords": [k for k in dict.fromkeys(keywords) if k][:50]})
        for k in r.get("keywords", []):
            out["keywords"][k["keyword"]] = {"difficulty": k.get("difficulty"), "intent": k.get("intent"),
                                             "volume": k.get("search_volume"), "cpc": k.get("cpc")}
    except Exception as e:
        out["errors"].append(f"keyword_metrics: {e}")
    try:
        # Google's lookup needs the state: "Marcello Homes Charlotte NC" finds it, "Marcello Homes Charlotte" doesn't
        p, r = {}, {}
        tries = [f"{profile['name']} {market['city']} {market['state_code']}"] * 2 + [f"{profile['name']} {market['city']}", profile["name"]]
        for q in tries:  # the lookup sometimes times out, so the most specific query gets a second try
            r = c.call("datawise_local_reviews", {"business_name": q, "limit": 10})
            p = r.get("profile") or {}
            if p.get("title"):
                break
        if p.get("title"):
            dist = {int(k): v for k, v in (p.get("rating_distribution") or {}).items()}
            out["gbp"] = {"title": p.get("title"), "rating": p.get("rating"), "reviews": p.get("reviews_count"),
                          "distribution": dist, "category": p.get("category"), "categories": p.get("additional_categories") or [],
                          "phone": re.sub(r"\D", "", p.get("phone") or "")[-10:], "address": p.get("address"),
                          "claimed": p.get("is_claimed"), "photos": p.get("total_photos"), "place_id": p.get("place_id"),
                          "url": p.get("url"), "matches_site": _bare(re.sub(r"^https?://", "", p.get("url") or "").split("/")[0]) == profile["domain"],
                          "recent": [{"rating": x.get("rating"), "text": (x.get("text") or "")[:400]} for x in (r.get("reviews") or {}).get("reviews", [])[:5]]}
    except Exception as e:
        out["errors"].append(f"local_reviews: {e}")
    return out


def mentions(domains):
    """AI mentions database (DataForSEO LLM Mentions) for up to 5 domains on Google AI and ChatGPT."""
    out = {"google": {}, "chatgpt": {}, "sources": {}, "errors": []}
    doms = [d for d in dict.fromkeys(_bare(x) for x in domains) if d][:5]
    if not doms:
        return out
    try:
        c = _client()
    except Exception as e:
        out["errors"].append(f"connect: {e}")
        return out
    for plat in ("google", "chatgpt"):
        try:
            r = c.call("datawise_ai_mentions", {"domains": doms, "platform": plat, "response_format": "detailed"})
            items = r.get("items") or []
            if not items and len(doms) == 1:
                items = [dict(key=doms[0], **(r.get("metrics") or {}).get("total", {}))]
            for d in doms:  # domains with no records come back empty: that is a real zero, not missing data
                out[plat].setdefault(d, {"mentions": 0, "ai_search_volume": 0})
            for it in items:
                tot = (it.get("platform") or it.get("location") or [{}])[0]
                out[plat][_bare(it.get("key"))] = {"mentions": tot.get("mentions") or 0, "ai_search_volume": tot.get("ai_search_volume") or 0}
                if plat == "google":
                    out["sources"][_bare(it.get("key"))] = [{"domain": _bare(s.get("key")), "mentions": s.get("mentions")}
                                                            for s in (it.get("sources_domain") or [])[:6]]
        except Exception as e:
            out["errors"].append(f"ai_mentions {plat}: {e}")
    return out
