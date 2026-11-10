#!/usr/bin/env python3
"""Check or set the tool's spending cap.

  python3 spend.py                 # show spend so far and the cap
  python3 spend.py --limit 20      # cap all AI platform spend at $20 (across every audit)
  python3 spend.py --limit 20 --reset   # start a fresh $20 test budget
  python3 spend.py --no-limit      # remove the cap

Spend is tracked with conservative per-answer estimates, replaced by the real cost where the provider reports it
(Perplexity, DataForSEO). Provider dashboards are the final word; this cap is a safety stop on top of them.
"""
import argparse
import os
import sys
if os.name == "nt" and not sys.flags.utf8_mode:  # Windows: run in UTF-8 mode so reports and data files save correctly
    import subprocess
    sys.exit(subprocess.call([sys.executable, "-X", "utf8"] + sys.argv))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aeo import budget  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=float)
ap.add_argument("--no-limit", action="store_true")
ap.add_argument("--reset", action="store_true")
a = ap.parse_args()
if a.no_limit:
    budget.set_limit(None, a.reset)
elif a.limit is not None:
    budget.set_limit(a.limit, a.reset)
d = budget.status()
lim = d.get("limit_usd")
print(f"Spent so far: ${d['spent_usd']:.2f}" + (f" of ${lim:.2f} cap (${max(0, lim - d['spent_usd']):.2f} left)" if lim is not None else " (no cap set)"))
for k, v in sorted(d.get("calls", {}).items()):
    print(f"  {k:15} {v} answers")
