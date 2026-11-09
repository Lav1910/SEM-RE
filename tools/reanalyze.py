#!/usr/bin/env python3
"""Re-run analysis and re-render an existing audit from its saved answers (no new API calls).
Usage: python3 tools/reanalyze.py <audit_dir> [<audit_dir> ...]"""
import json, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from aeo import analyze, render  # noqa: E402

for d in sys.argv[1:]:
    A = json.load(open(os.path.join(d, "audit.json")))
    for a in A["answers"]:  # older runs stored Semrush "no data" as an error
        if a["status"] == "error" and "no data found" in (a.get("error") or "").lower():
            a["status"], a["error"] = "not_measured", "Semrush has no data for this search"
    A["analysis"] = analyze.analyze(A["profile"], A["market"], A["questions"], A["answers"], [tuple(c) for c in A["checks"]], A["semrush"])
    json.dump(A, open(os.path.join(d, "audit.json"), "w"), indent=1)
    open(os.path.join(d, "report.html"), "w").write(render.render(A))
    open(os.path.join(d, "loom.txt"), "w").write(render.talking_points(A))
    k = A["analysis"]["kpis"]
    print(f"{A['profile']['name']}: score {k['score']} · named {k['mentioned']} of {k['answers']} · pack {k['pack']} of {k['pack_of']}")
    print("  leaderboard:", [(b["name"], b["mentions"], b["pack"]) for b in A["analysis"]["leaderboard"]])
