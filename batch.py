#!/usr/bin/env python3
"""Run audits for a list of prospects, one per line (URL, optionally followed by ",City,State").

  python3 batch.py prospects.txt

Writes each report under audits/<domain>/<date>/ and a summary CSV at audits/batch-<date>.csv.
A failed prospect is logged and skipped so the rest of the list still runs.
"""
import csv
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audit  # noqa: E402


def main(path):
    rows = []
    lines = [l.strip() for l in open(path) if l.strip() and not l.startswith("#")]
    for i, line in enumerate(lines, 1):
        parts = [p.strip() for p in line.split(",")]
        url, city, state = parts[0], (parts[1] if len(parts) > 1 else None), (parts[2] if len(parts) > 2 else None)
        audit.log(f"\n[{i}/{len(lines)}] {url}")
        try:
            out = audit.run(url, city, state)
            A = json.load(open(os.path.join(out, "audit.json")))
            k = A["analysis"]["kpis"]
            rows.append({"url": url, "builder": A["profile"]["name"], "market": f"{A['market']['city']}, {A['market']['state_code']}",
                         "score": k["score"], "named": f"{k['mentioned']}/{k['answers']}", "maps_pack": k["pack"],
                         "checks": f"{k['checks_passed']}/{k['checks_total']}", "report": os.path.join(out, "report.html"), "error": ""})
        except (SystemExit, audit.AuditError) as e:
            rows.append({"url": url, "error": str(e)})
        except Exception as e:
            rows.append({"url": url, "error": f"{type(e).__name__}: {e}"})
    out_csv = os.path.join(audit.ROOT, "audits", f"batch-{datetime.date.today().isoformat()}.csv")
    os.makedirs(os.path.dirname(out_csv), exist_ok=True)
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["url", "builder", "market", "score", "named", "maps_pack", "checks", "report", "error"])
        w.writeheader(); w.writerows(rows)
    print(out_csv)


if __name__ == "__main__":
    main(sys.argv[1])
