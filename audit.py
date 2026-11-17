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
from aeo import analyze, business, config, dates, datawise, engines, locations, questions, render, scope, semrush, site  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))


def log(msg):
    print(msg, file=sys.stderr, flush=True)


class AuditError(Exception):
    """A problem the salesperson can fix (bad URL, unknown market)."""


def _typed_location(label, country=None):
    """A place typed on the dashboard, like "Mason, OH" or "Parramatta, NSW"."""
    return locations.typed(label, "entered", country)


def _stepper(progress):
    _p = progress or (lambda stage, pct, **kw: None)

    def step(stage, pct, event=None, engines_state=None):
        try:
            _p(stage, pct, event=event, engines=engines_state)
        except TypeError:  # an older two-argument callback
            _p(stage, pct)
    return step


def prepare(url, city=None, state=None, max_q=14, only=None, use_semrush=True, progress=None, scope_kind="root",
            locations_chosen=None, max_locations=None, skip_brand=False, prior=None, hedge=True, services_chosen=None):
    """Step 1 of an audit (about 30-90 s): read the site, find the market and service cities, build the questions,
    pull search demand and the Google profile, run the site checks. Returns a JSON-safe state for the next steps."""
    t0 = time.time()
    step = _stepper(progress)
    import threading
    chosen = [en for en in engines.ENGINES if not only or en in only]
    live = [en for en in chosen if engines.is_configured(en)]
    shared_lock = threading.Lock()  # the early batches write to the same answers and counters

    def build_qs(prof, mkt):
        out = questions.build(prof, mkt, max_q)
        return [q for q in out if q.get("kind") != "brand"] if skip_brand else out  # change 35: Bulk skips brand questions

    def start_early(qs_, mkt, prof):
        """change 29: start asking the main local questions now; nothing new starts after 150 s (one serverless run)."""
        label = f"{mkt['city']}, {mkt['state_code']}"
        eq = [q for q in qs_ if not q["id"].startswith("area_")]  # area questions may be swapped for per-city sets
        for q in eq:
            if q.get("kind") != "brand":
                q["area"] = label
        est_ = {en: {"done": 0, "total": len(eq), "state": "running" if en in live else "off"} for en in chosen}
        early_ = {"questions": eq, "chosen": chosen, "est": est_, "answers": {}, "market": mkt, "profile": prof, "hedge": hedge}
        th = threading.Thread(target=ask_batch, args=(early_, progress), daemon=True,
                              kwargs={"pct_range": (18, 50), "lock": shared_lock, "deadline": t0 + 150})
        th.start()
        return {"qs": qs_, "early_qs": eq, "early": early_, "est": est_, "thread": th, "market": mkt}

    # change 34: a business audited before already has a known market and questions, so the AI questions start at once
    # (change 53: only when that audit knew the business type and services, and no new services were picked)
    head = None
    pm = (prior or {}).get("market") or {}
    if pm.get("city") and pm.get("state_code") and (not city or city.strip().lower() == pm["city"].lower()) \
            and prior["profile"].get("business") and services_chosen is None:
        head = start_early(build_qs(prior["profile"], pm), pm, prior["profile"])
        step("Asking AI platforms", 4, event=f"Audited before: asking {len(live)} AI platforms right away for {pm['city']}, {pm['state_code']}",
             engines_state={k: dict(v) for k, v in head["est"].items()})
    step("Reading the website", 5, event=f"Opening {url} in a real browser")
    log(f"Reading {url} ...")
    sc = scope.make(url, scope_kind)
    profile = site.read_site(url, start=scope.start_url(sc) if sc["type"] in ("subfolder", "url") else None)
    profile["scope"] = sc
    if sc["type"] == "root":
        profile["domain"] = sc["domain"]
    hs = profile["homepage_status"]
    if not hs or hs in (404, 410):  # no such site or page: the address is wrong
        raise AuditError(f"Could not load the homepage (HTTP {hs or 'no response'}). Check the URL.")
    # change 26: the site turns away automated visitors (change 60: including CDN / bot-check error pages)
    blocked = (hs >= 400 and profile.get("word_count", 0) < 50) or bool(profile.get("error_page"))
    if blocked:
        profile["site_blocked"] = hs
        profile["name"] = site.name_from_domain(profile["domain"])
        step("Reading the website", 12, event=f"{profile['domain']} turns away automated visitors, so the website checks are skipped; "
                                              "the AI platform questions still run")
    else:
        step("Reading the website", 12, event=f"Checked business markup, phone details and {profile['sitemap_count']:,} sitemap pages")
        step("Reading the website", 14, event="Checked robots.txt and llms.txt for AI crawler access")
    # change 53: what the business sells decides the questions; read it alongside the market lookup
    import concurrent.futures as _cf
    biz_pool = _cf.ThreadPoolExecutor(max_workers=1)
    f_biz = biz_pool.submit(business.cached, profile, profile["domain"]) if not blocked else None
    preset = locations.preset_for(profile["domain"])
    if preset and not city:  # change 56: a set main market (HBM: San Diego) when none was typed in
        city, state = preset["city"], preset.get("state") or preset["state_code"]
    market = site.market_from(profile, city, state)
    if (not market["city"] or not market["state"]) and pm.get("city") and pm.get("state_code"):  # the market from the last audit
        market = site.market_from(profile, pm["city"], pm["state_code"])
    if not market["city"] or not market["state"]:  # change 30: look it up instead of stopping
        lc, ls, how = site.lookup_market(profile["name"] or site.name_from_domain(profile["domain"]), profile["domain"])
        if lc:
            market = site.market_from(profile, lc, ls)
            if market["city"] and market["state"]:
                step("Reading the website", 15, event=f"Found the business's market with {how}: {market['city']}, {market['state_code']}")
    if not market["city"] or not market["state"]:
        biz_pool.shutdown(wait=False)
        raise AuditError("This website turns away automated visitors, so we couldn't read the business's city and state. Enter them and run again."
                         if blocked else "Couldn't find the business's city and state on the website. Enter them and run again.")
    step("Reading the website", 16, event=f"Set the search location to {market['city']}, {market['state_code']}")
    log(f"  {profile['name']} · market {market['city']}, {market['state_code']} · areas {profile['areas_served'] or '—'}")
    main_label = f"{market['city']}, {market['state_code']}"
    try:
        biz = f_biz.result(timeout=60) if f_biz else business.detect(profile)
    except Exception:
        biz = business.detect({})  # too slow: the original home-builder set
    biz_pool.shutdown(wait=False)
    profile["business"] = business.apply_choice(biz, services_chosen)
    W = business.words(profile)
    step("Reading the website", 16, event=(f"Reads as {W['a_label']}: " + ", ".join(W["services"][:4]) if W["services"] else f"Reads as {W['a_label']}")
         + (" (services picked on the dashboard)" if services_chosen is not None else ""))
    same_biz = head and _biz_sig(prior["profile"]["business"]) == _biz_sig(profile["business"])
    if head and same_biz and head["market"]["city"].lower() == market["city"].lower() and head["market"]["state_code"] == market["state_code"]:
        run = head  # the head start was right: keep those questions and answers
    else:
        if head:  # the business moved market or changed what it sells since last time: start over
            head["thread"].join()
        run = start_early(build_qs(profile, market), market, profile)
        step("Asking AI platforms", 17, event=f"Asking {len(live)} AI platform{'s' if len(live) != 1 else ''} the main local questions "
                                              "while the rest of the local data loads", engines_state={k: dict(v) for k, v in run["est"].items()})
    qs, early_qs, early, est, t_early = run["qs"], run["early_qs"], run["early"], run["est"], run["thread"]
    # ---- service locations: each city is measured on its own, searched from that city ----
    step("Finding service locations", 17, event="Finding the cities and neighbourhoods this business serves")
    found = locations.find(profile, market)
    cap = max_locations or (2 if max_q <= 6 else config.SETTINGS["MAX_LOCATIONS"])
    if locations_chosen:  # picked on the dashboard: found places plus any typed by the salesperson
        by_label = {l["label"].lower(): l for l in found}
        pool_locs = [found[0]]
        for lab in locations_chosen:
            l = by_label.get(lab.lower()) or _typed_location(lab, engines.country_of(market))
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
    city_kw = {l["label"]: questions.city_keyword(profile["business"]).format(kw=questions.loc_kw(l)).lower() for l in pool_locs[1:9]}
    kws = list(dict.fromkeys([q["keyword"] for q in qs] + list(city_kw.values())))
    pool = ThreadPoolExecutor(max_workers=2)  # Semrush and DataWise run side by side
    ctry = engines.country_of(market)  # change 58: search data from the right country's database
    f_sem = pool.submit(semrush.context, scope.semrush_target(sc), kws, semrush.database(market)) if (use_semrush and semrush.available()) else None
    f_dw = pool.submit(datawise.early, profile, market, kws) if datawise.available() else None
    from aeo import dfs_volume  # change 40: search volumes from DataForSEO when Semrush isn't connected
    f_dfs = pool.submit(dfs_volume.volumes, kws, 25, ctry) if (dfs_volume.available() and not (use_semrush and semrush.available())) else None
    wait_until = time.time() + config.SETTINGS["DATA_WAIT"]  # change 29: a slow data source can't hold the audit up

    def _within(f, label):
        if not f:
            return None
        try:
            return f.result(timeout=max(1, wait_until - time.time()))
        except Exception as ex:  # too slow or failed: carry on without it
            log(f"  {label} skipped: {'took too long' if type(ex).__name__ == 'TimeoutError' else ex}")
            return None
    sem, dw = _within(f_sem, "Semrush"), _within(f_dw, "DataWise")
    dfs_vols = _within(f_dfs, "DataForSEO volumes") or {}
    pool.shutdown(wait=False)
    for err in (sem or {}).get("errors", []):
        log(f"  Semrush warning: {err}")
    vols = dict((sem or {}).get("volumes", {}))
    for k, v in dfs_vols.items():
        if vols.get(k) is None:
            vols[k] = v
    if dw is not None:
        for k, m in dw["keywords"].items():  # DataWise volumes are US figures: not used for Canadian markets
            if vols.get(k) is None and m.get("volume") is not None and ctry == "US":
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
        qs += questions.for_location(l, per_loc, profile["business"])
    if measured:
        step("Preparing questions", 19, event="Measuring " + ", ".join([main_label] + [l["label"] for l in measured]) + " separately")
    questions.apply_volumes(qs, vols, (sem or {}).get("trends", {}))
    step("Preparing questions", 20, event=f"Prepared {len(qs)} questions local {W['customers']} ask")
    # change 29: the city questions start now too, instead of waiting for the rest of this step
    early_ids = {q["id"] for q in early_qs}
    rest_qs = [q for q in qs if q["id"] not in early_ids]
    for q in rest_qs:
        if q.get("kind") != "brand" and not q.get("area"):
            q["area"] = main_label
    with shared_lock:
        for st in est.values():
            st["total"] = len(qs)
            if st["state"] == "done" and st["done"] < st["total"]:
                st["state"] = "running"
    rest = {"questions": rest_qs, "chosen": chosen, "est": est, "answers": early["answers"], "market": market, "profile": profile, "hedge": hedge}
    t_rest = threading.Thread(target=ask_batch, args=(rest, progress), daemon=True,
                              kwargs={"pct_range": (30, 90), "lock": shared_lock, "total": len(qs) * len(chosen), "deadline": t0 + 150})
    t_rest.start()

    if dw is not None:
        for q in qs:
            m = dw["keywords"].get(q["keyword"]) or {}
            q["difficulty"], q["intent"] = m.get("difficulty"), m.get("intent")
            if q.get("volume") is None and m.get("volume") is not None and ctry == "US":
                q["volume"], q["weight"] = m["volume"], max(m["volume"], questions.FLOOR_WEIGHT)
        profile["gbp"] = dw["gbp"]
        profile["gbp_checked"] = True
        for err in dw["errors"]:
            log(f"  DataWise warning: {err}")
    step("Getting search demand and the Google profile", 28, event="Reviewed the Google Business Profile and local search data")
    checks = [] if profile.get("site_blocked") else site.checks(profile, market)

    log(f"Asking {len(qs)} questions on {len(live)} platforms: {', '.join(engines.ENGINE_LABELS[x] for x in live) or 'none configured'}")
    step("Asking AI platforms", 30, event="Asking each AI platform the local questions, with the location set to "
         + (f"{main_label} and each service city" if measured else main_label), engines_state={k: dict(v) for k, v in est.items()})
    t_early.join()  # the answers were coming in alongside everything above
    t_rest.join()
    return {"url": url, "t0": t0, "profile": profile, "market": market, "questions": qs, "sem": sem, "dw": dw, "checks": checks,
            "chosen": chosen, "est": est, "answers": early["answers"], "pool_locs": pool_locs, "hedge": hedge,
            "found": [l for l in found if l not in pool_locs]}


def _answer_sources(answers):
    """Per platform, where its answers came from: {"chatgpt": {"dataforseo": 11, "api (fallback)": 1}, ...}."""
    out = {}
    for a in answers:
        if a.get("status") in ("ok", "no_answer"):
            src = a.get("source") or "api"
            out.setdefault(a["engine"], {}).setdefault(src, 0)
            out[a["engine"]][src] += 1
    return out


def _biz_sig(b):
    """Same business type and services: the questions from a head start are still the right ones."""
    b = b or {}
    return (b.get("kind"), tuple(s.get("name", "").lower() for s in b.get("services") or []))


HEDGED = ("chatgpt", "gemini", "perplexity")  # the platforms whose answers sometimes straggle


def _hedged(en, q, loc):
    """change 34: if an answer hasn't come back after HEDGE_AFTER seconds, ask the same question again and use
    whichever reply arrives first, so one slow answer can't hold up the whole audit. Off for Bulk (cost)."""
    from concurrent.futures import FIRST_COMPLETED, wait
    after = config.SETTINGS["HEDGE_AFTER"]
    if after <= 0:
        return engines.ask(en, q, loc)
    pool = ThreadPoolExecutor(max_workers=2)
    first = pool.submit(engines.ask, en, q, loc)
    try:
        done, _ = wait([first], timeout=after)
        if done:
            return first.result()
        second = pool.submit(engines.ask, en, q, loc)
        pending = {first, second}
        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                r = f.result()
                if r.get("status") in ("ok", "no_answer") or not pending:
                    return r
        return first.result()
    finally:
        pool.shutdown(wait=False)


def ask_batch(S, progress=None, deadline=None, pct_range=(30, 90), lock=None, total=None):
    """Step 2: ask the AI platforms the questions not answered yet, in parallel. Stops starting new work at `deadline`
    (epoch seconds) so a serverless request can save and continue in the next one. Returns True when all are answered."""
    import threading
    from concurrent.futures import ThreadPoolExecutor
    step = _stepper(progress)
    qmap = {q["id"]: q for q in S["questions"]}
    todo = [(en, q["id"]) for q in S["questions"] for en in S["chosen"] if f"{en}|{q['id']}" not in S["answers"]]
    total = total or len(S["questions"]) * len(S["chosen"])
    lock = lock or threading.Lock()
    est = S["est"]
    # change 29: a quick "named so far" count for the live progress screen (the final analysis is the exact figure)
    P = S.get("profile") or {}
    name_l = re.sub(r"\s+", " ", (P.get("name") or "").lower()).strip()
    dom_label = (P.get("domain") or "").lower().split(".")[0]

    def named(r):
        t = (r.get("text") or "").lower()
        return bool((name_l and len(name_l) > 3 and name_l in t) or (dom_label and len(dom_label) > 3 and dom_label in t.replace(" ", ""))
                    or any(dom_label and dom_label in (c.get("domain") or "") for c in r.get("citations") or []))

    hedge = S.get("hedge", True)

    def ask(j):
        en, qid = j
        q = qmap[qid]
        loc = q.get("loc") or S["market"]
        r = _hedged(en, q, loc) if hedge and en in HEDGED else engines.ask(en, q, loc)
        with lock:
            S["answers"][f"{en}|{qid}"] = r
            st = est[en]
            st["done"] += 1
            if r["status"] == "ok":
                st["answered"] = st.get("answered", 0) + 1
                st["named"] = st.get("named", 0) + (1 if named(r) else 0)
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
            n = len(S["answers"])
            snapshot = {k: dict(v) for k, v in est.items()}
        lo, hi = pct_range
        step(f"Asking AI platforms ({n} of {total})", lo + int((hi - lo) * n / max(1, total)), event=ev, engines_state=snapshot)
        return r

    # change 29: every platform gets its own lane of up to PER_PLATFORM questions at once, so one slow platform
    # never holds up the others; nothing new starts after the deadline (a serverless run saves and continues later)
    per = max(1, config.SETTINGS["PER_PLATFORM"])
    lanes = {en: threading.BoundedSemaphore(per) for en in S["chosen"]}

    def lane(j):
        with lanes[j[0]]:
            if deadline and time.time() > deadline:
                return None
            return ask(j)
    if todo:
        with ThreadPoolExecutor(max_workers=min(len(todo), per * len({en for en, _ in todo}))) as pool:
            list(pool.map(lane, todo))
    return len(S["answers"]) >= total


def finish(S, progress=None):
    """Step 3: analyse the answers, check the AI mentions database, build the report. Returns (audit, report_html, loom_text)."""
    step = _stepper(progress)
    profile, market, qs, checks, sem, dw = S["profile"], S["market"], S["questions"], S["checks"], S["sem"], S["dw"]
    answers = [S["answers"][f"{en}|{q['id']}"] for q in qs for en in S["chosen"] if f"{en}|{q['id']}" in S["answers"]]
    url, t0, pool_locs, found = S["url"], S["t0"], S["pool_locs"], S["pool_locs"] + S["found"]
    step("Analysing answers", 91, event=f"Identifying the {business.words(profile)['nouns']} each answer recommends")
    step("Analysing answers", 92, event="Mapping the sources AI platforms rely on")
    capped = sum(1 for a in answers if a["status"] == "budget_reached")
    if capped:
        log(f"  Spending cap reached: {capped} answers skipped (python3 spend.py to check or raise the cap)")
    errs = [a for a in answers if a["status"] == "error"]
    for a in errs[:5]:
        log(f"  {engines.ENGINE_LABELS[a['engine']]} error: {a['error']}")

    analysis = analyze.analyze(profile, market, qs, answers, checks, sem)
    if dw is not None:
        # AI mentions database for the business + up to 4 competitors that have a known domain
        doms = [profile["domain"]] + [b["domain"] for b in analysis["leaderboard"] if b.get("domain") and not b.get("prospect")]
        doms += analyze.competitor_domains(analysis, answers, profile["domain"])
        step("Checking the AI mentions database", 94, event="Comparing the AI mentions database for this business and its competitors")
        mpool = ThreadPoolExecutor(max_workers=1)  # change 29: capped like the other search data
        try:
            dw["mentions"] = mpool.submit(datawise.mentions, list(dict.fromkeys(doms))[:5]).result(timeout=config.SETTINGS["DATA_WAIT"])
        except Exception:
            dw["mentions"] = {"google": {}, "chatgpt": {}, "sources": {}, "errors": ["AI mentions database took too long; skipped"]}
        mpool.shutdown(wait=False)
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
        "answer_sources": _answer_sources(answers),  # change 59: chatgpt.com / Gemini app via DataForSEO, or the official API
        "seconds": round(time.time() - t0, 1),
    }
    step("Building the report", 97, event="Writing the report, PDF layout and Loom talking points")
    report_html, loom = render.render(audit), render.talking_points(audit)
    k = analysis["kpis"]
    log(f"Done in {audit['seconds']}s. Score {k['score'] if k['score'] is not None else '—'} · named in {k['mentioned']} of {k['answers']} answers · "
        f"{k['checks_passed']}/{k['checks_total']} checks · Semrush units {(sem or {}).get('units', 0)}")
    from aeo import budget
    b = budget.status()
    if b.get("limit_usd") is not None:
        log(f"Test budget: ${b['spent_usd']:.2f} of ${b['limit_usd']:.2f} used")
    return audit, report_html, loom


def run(url, city=None, state=None, max_q=14, only=None, use_semrush=True, progress=None, out_dir=None, scope_kind="root",
        locations_chosen=None, max_locations=None, services_chosen=None):
    """Run one whole audit in this process (command line, or a server with background workers).
    progress(stage, percent, event=..., engines=...) is called as it goes; returns the output folder."""
    S = prepare(url, city, state, max_q, only, use_semrush, progress, scope_kind, locations_chosen, max_locations,
                services_chosen=services_chosen)
    ask_batch(S, progress)
    audit, report_html, loom = finish(S, progress)
    out_dir = out_dir or os.path.join(ROOT, "audits", audit["profile"]["domain"], datetime.date.today().isoformat())
    os.makedirs(out_dir, exist_ok=True)
    json.dump(audit, open(os.path.join(out_dir, "audit.json"), "w"), indent=1)
    open(os.path.join(out_dir, "report.html"), "w").write(report_html)
    open(os.path.join(out_dir, "loom.txt"), "w").write(loom)
    _stepper(progress)("Done", 100, event="Report ready")
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
