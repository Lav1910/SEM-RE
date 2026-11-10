#!/usr/bin/env python3
"""HBM AI Visibility Audit: one builder URL in, one branded report out.

  python3 audit.py https://justindoylehomes.com
  python3 audit.py builder.com --city Cincinnati --state Ohio --questions 12 --engines chatgpt,google_aio

Writes audits/<domain>/<YYYY-MM-DD>/ with audit.json (every raw answer), report.html and loom.txt.
Platforms without keys are skipped and show as "—" in the report; run setup_keys.py to add keys.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
if os.name == "nt" and not sys.flags.utf8_mode:  # Windows: run in UTF-8 mode so reports and data files save correctly
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-X", "utf8"] + sys.argv))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aeo import analyze, config, dates, datawise, engines, locations, questions, render, scope, semrush, site  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class AuditError(Exception):
    """A problem the salesperson can fix (bad URL, unknown market)."""


def _typed_location(label):
    """A place typed on the dashboard, like "Mason, OH"."""
    m = re.match(r"^\s*([A-Za-z .'-]+?),\s*([A-Za-z]{2})\s*$", label or "")
    if not m or m.group(2).upper() not in site.STATE_NAMES:
        return None
    st = m.group(2).upper()
    name = " ".join(w.capitalize() for w in m.group(1).split())
    return {"label": f"{name}, {st}", "name": name, "city": name, "state": site.STATE_NAMES[st], "state_code": st,
            "kind": "city", "main": False, "sources": ["entered"]}


def run(url, city=None, state=None, max_q=14, only=None, use_semrush=True, progress=None, out_dir=None, scope_kind="root",
        locations_chosen=None, max_locations=None):
    """Run one audit. progress(stage, percent, event=..., engines=...) is called as it goes; returns the output folder.
    Events describe what the audit is doing (never results), for the live screen salespeople watch."""
    t0 = time.time()
    _p = progress or (lambda stage, pct, **kw: None)

    def step(stage, pct, event=None, engines_state=None):
        try:
            _p(stage, pct, event=event, engines=engines_state)
        except TypeError:  # an older two-argument callback
            _p(stage, pct)

    step("Reading the website", 5, event=f"Opening {url} in a real browser")
    log(f"Reading {url} ...")
    sc = scope.make(url, scope_kind)
    profile = site.read_site(url, start=scope.start_url(sc) if sc["type"] in ("subfolder", "url") else None)
    profile["scope"] = sc
    if sc["type"] == "root":
        profile["domain"] = sc["domain"]
    if not profile["homepage_status"] or profile["homepage_status"] >= 400:
        raise AuditError(f"Could not load the homepage (HTTP {profile['homepage_status'] or 'no response'}). Check the URL.")
    step("Reading the website", 12, event=f"Checked business markup, phone details and {profile['sitemap_count']:,} sitemap pages")
    step("Reading the website", 14, event="Checked robots.txt and llms.txt for AI crawler access")
    market = site.market_from(profile, city, state)
    if not market["city"] or not market["state"]:
        raise AuditError("Couldn't find the builder's city and state on the website. Enter them and run again.")
    step("Reading the website", 16, event=f"Set the search location to {market['city']}, {market['state_code']}")
    log(f"  {profile['name']} · market {market['city']}, {market['state_code']} · areas {profile['areas_served'] or '—'}")
    qs = questions.build(profile, market, max_q)
    main_label = f"{market['city']}, {market['state_code']}"
    # ---- service locations: each city is measured on its own, searched from that city ----
    step("Finding service locations", 17, event="Finding the cities and neighbourhoods this builder serves")
    found = locations.find(profile, market)
    cap = max_locations or (2 if max_q <= 6 else config.SETTINGS["MAX_LOCATIONS"])
    if locations_chosen:  # picked on the dashboard: found places plus any typed by the salesperson
        by_label = {l["label"].lower(): l for l in found}
        pool_locs = [found[0]]
        for lab in locations_chosen:
            l = by_label.get(lab.lower()) or _typed_location(lab)
            if l and l["label"].lower() != found[0]["label"].lower() and l not in pool_locs:
                pool_locs.append(l)
    else:
        pool_locs = found
    if len(pool_locs) > 1:
        qs = [q for q in qs if not q["id"].startswith("area_")]  # replaced by the per-city sets
    for q in qs:
        if q.get("kind") != "brand":
            q["area"] = main_label
    step("Finding service locations", 18, event=(f"Found {len(found) - 1} more service location{'s' if len(found) != 2 else ''}: "
                                                   + ", ".join(l["label"] for l in found[1:6]) + ("…" if len(found) > 6 else ""))
         if len(found) > 1 else f"No other service locations listed on the site; measuring {main_label}")

    step("Getting search demand and the Google profile", 20, event="Pulling search demand and keyword difficulty")
    from concurrent.futures import ThreadPoolExecutor
    city_kw = {l["label"]: questions.CITY_KEYWORD.format(kw=questions.loc_kw(l)) for l in pool_locs[1:9]}
    kws = list(dict.fromkeys([q["keyword"] for q in qs] + list(city_kw.values())))
    with ThreadPoolExecutor(max_workers=2) as pool:  # Semrush and DataWise run side by side
        f_sem = pool.submit(semrush.context, scope.semrush_target(sc), kws) if (use_semrush and semrush.available()) else None
        f_dw = pool.submit(datawise.early, profile, market, kws) if datawise.available() else None
        sem = f_sem.result() if f_sem else None
        dw = f_dw.result() if f_dw else None
    for err in (sem or {}).get("errors", []):
        log(f"  Semrush warning: {err}")
    vols = dict((sem or {}).get("volumes", {}))
    if dw is not None:
        for k, m in dw["keywords"].items():
            if vols.get(k) is None and m.get("volume") is not None:
                vols[k] = m["volume"]
    # choose the cities to measure: the salesperson's picks, else the highest search demand, up to the cap
    others = pool_locs[1:]
    if not locations_chosen:
        others = sorted(others, key=lambda l: -(vols.get(city_kw.get(l["label"])) or 0))
    measured = others[:max(0, cap - 1)]
    for l in pool_locs:
        l["volume"] = vols.get(city_kw.get(l["label"])) if not l.get("main") else None
        l["measured"] = l.get("main") or l in measured
    per_loc = 1 if max_q <= 6 else 3
    for l in measured:
        qs += questions.for_location(l, per_loc)
    if measured:
        step("Preparing questions", 19, event="Measuring " + ", ".join([main_label] + [l["label"] for l in measured]) + " separately")
    questions.apply_volumes(qs, vols, (sem or {}).get("trends", {}))
    step("Preparing questions", 20, event=f"Prepared {len(qs)} local home-buyer questions")

    if dw is not None:
        for q in qs:
            m = dw["keywords"].get(q["keyword"]) or {}
            q["difficulty"], q["intent"] = m.get("difficulty"), m.get("intent")
            if q.get("volume") is None and m.get("volume") is not None:
                q["volume"], q["weight"] = m["volume"], max(m["volume"], questions.FLOOR_WEIGHT)
        profile["gbp"] = dw["gbp"]
        profile["gbp_checked"] = True
        for err in dw["errors"]:
            log(f"  DataWise warning: {err}")
    step("Getting search demand and the Google profile", 28, event="Reviewed the Google Business Profile and local search data")
    checks = site.checks(profile, market)

    chosen = [en for en in engines.ENGINES if not only or en in only]
    live = [en for en in chosen if engines.is_configured(en)]
    log(f"Asking {len(qs)} questions on {len(live)} platforms: {', '.join(engines.ENGINE_LABELS[x] for x in live) or 'none configured'}")
    jobs = [(en, q) for q in qs for en in chosen]
    done = [0]
    import threading
    lock = threading.Lock()
    est = {en: {"done": 0, "total": len(qs), "state": "running" if en in live else "off"} for en in chosen}

    def ask(j):
        en, q = j
        r = engines.ask(en, q, q.get("loc") or market)
        with lock:
            done[0] += 1
            st = est[en]
            st["done"] += 1
            label = engines.ENGINE_LABELS[en]
            ev = None
            if r["status"] == "error" and "credit" in (r.get("error") or "").lower():
                if st["state"] != "needs_credits":
                    ev = f"Skipped {label}: the account needs API credits"
                st["state"] = "needs_credits"
            elif st["state"] == "running" and st["done"] >= st["total"]:
                st["state"] = "done"
            if r["status"] not in ("ok", "no_answer"):
                pass  # skipped, failed or not connected: no "asked" line
            elif en == "google_aio":
                ev = f"Checked Google AI Overviews for “{q['keyword']}”"
            else:
                ev = f"Asked {label}: {q['prompt']}"
            snapshot = {k: dict(v) for k, v in est.items()}
        step(f"Asking AI platforms ({done[0]} of {len(jobs)})", 30 + int(60 * done[0] / max(1, len(jobs))), event=ev, engines_state=snapshot)
        return r
    step("Asking AI platforms", 30, event="Asking each AI platform the same local questions, with the location set to "
         + (f"{main_label} and each service city" if measured else main_label), engines_state={k: dict(v) for k, v in est.items()})
    with ThreadPoolExecutor(max_workers=config.SETTINGS["WORKERS"]) as pool:
        answers = list(pool.map(ask, jobs))
    step("Analysing answers", 91, event="Identifying the builders each answer recommends")
    step("Analysing answers", 92, event="Mapping the sources AI platforms rely on")
    capped = sum(1 for a in answers if a["status"] == "budget_reached")
    if capped:
        log(f"  Spending cap reached: {capped} answers skipped (python3 spend.py to check or raise the cap)")
    errs = [a for a in answers if a["status"] == "error"]
    for a in errs[:5]:
        log(f"  {engines.ENGINE_LABELS[a['engine']]} error: {a['error']}")

    analysis = analyze.analyze(profile, market, qs, answers, checks, sem)
    if dw is not None:
        # AI mentions database for the builder + up to 4 competitors that have a known domain
        doms = [profile["domain"]] + [b["domain"] for b in analysis["leaderboard"] if b.get("domain") and not b.get("prospect")]
        doms += analyze.competitor_domains(analysis, answers, profile["domain"])
        step("Checking the AI mentions database", 94, event="Comparing the AI mentions database for this builder and its competitors")
        dw["mentions"] = datawise.mentions(list(dict.fromkeys(doms))[:5])
        for err in dw["mentions"]["errors"]:
            log(f"  DataWise warning: {err}")
    audit = {
        "date": dates.long_date(), "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "input_url": url, "profile": profile, "market": market, "questions": qs, "answers": answers,
        "locations": pool_locs + [l for l in found if l not in pool_locs],
        "checks": checks, "semrush": sem, "datawise": dw, "analysis": analysis,
        "models": {"chatgpt": config.SETTINGS["OPENAI_MODEL"] + " + web search", "gemini": config.SETTINGS["GEMINI_MODEL"] + " + Google Search",
                   "perplexity": "Agent API, effort " + config.SETTINGS["PERPLEXITY_EFFORT"],
                   "google_aio": ("Google results via DataForSEO" if config.key("DATAFORSEO_LOGIN") else "Google AI Overview sources and Maps pack via Semrush (US snapshot)"), "google_ai_mode": "Google AI Mode via DataForSEO"},
        "cost_usd_reported": round(sum(a["cost_usd"] or 0 for a in answers), 4),
        "seconds": round(time.time() - t0, 1),
    }
    step("Building the report", 97, event="Writing the report, PDF layout and Loom talking points")
    out_dir = out_dir or os.path.join(ROOT, "audits", profile["domain"], datetime.date.today().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    json.dump(audit, open(os.path.join(out_dir, "audit.json"), "w"), indent=1)
    open(os.path.join(out_dir, "report.html"), "w").write(render.render(audit))
    open(os.path.join(out_dir, "loom.txt"), "w").write(render.talking_points(audit))
    k = analysis["kpis"]
    log(f"Done in {audit['seconds']}s. Score {k['score'] if k['score'] is not None else '—'} · named in {k['mentioned']} of {k['answers']} answers · "
        f"{k['checks_passed']}/{k['checks_total']} checks · Semrush units {(sem or {}).get('units', 0)}")
    from aeo import budget
    b = budget.status()
    if b.get("limit_usd") is not None:
        log(f"Test budget: ${b['spent_usd']:.2f} of ${b['limit_usd']:.2f} used")
    step("Done", 100, event="Report ready")
    print(out_dir)
    return out_dir


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="HBM AI Visibility Audit")
    ap.add_argument("url")
    ap.add_argument("--city"); ap.add_argument("--state")
    ap.add_argument("--questions", type=int, default=14)
    ap.add_argument("--engines", help="comma list: " + ",".join(engines.ENGINES))
    ap.add_argument("--no-semrush", action="store_true")
    ap.add_argument("--locations", help='semicolon-separated "City, ST" places to measure, e.g. "Mason, OH; Loveland, OH" (default: found on the site)')
    ap.add_argument("--scope", choices=sorted(scope.SCOPES), default="root", help="what counts as the builder's site")
    ap.add_argument("--test", action="store_true", help="cheap test run: 6 questions (about $0.50-1.00 across all five platforms)")
    a = ap.parse_args()
    try:
        run(a.url, a.city, a.state, 6 if a.test else a.questions, a.engines.split(",") if a.engines else None, not a.no_semrush, scope_kind=a.scope,
            locations_chosen=[x.strip() for x in a.locations.split(";")] if a.locations else None)
    except AuditError as err:
        raise SystemExit(str(err))
