#!/usr/bin/env python3
"""Load-matched A/B summary of several verify.py runs of two digests executed back to back in ONE job (e.g. v4,v5,v5,v4).
usage: ab_summary.py <labelA=dir,dir> <labelB=dir,dir>"""
import json, statistics as st, sys
from pathlib import Path


def load(spec):
    label, dirs = spec.split("=", 1)
    per = {}
    for d in dirs.split(","):
        for u in json.loads((Path(d) / "report.json").read_text())["units"]:
            per.setdefault(u["unit"], []).append((u["window_median_s"], u.get("n_events"), u.get("exact_vs_public_reference")))
    return label, {k: (st.mean(x[0] for x in v), v[0][1], all(x[2] for x in v)) for k, v in per.items()}


def n(m):
    print("::notice title=T3V-AB::" + m.replace("%", "%25"))


(la, A), (lb, B) = load(sys.argv[1]), load(sys.argv[2])
units = sorted(A)
mean = lambda M: st.mean(M[u][1] / M[u][0] for u in units)
tot = lambda M: sum(M[u][0] for u in units)
ratio = [A[u][0] / B[u][0] for u in units]
n("AB %s vs %s (mean of both runs of each digest, same job): roster_mean_events_per_s %s=%.0f %s=%.0f (%s/%s=%.3f) ; total_window_s %s=%.3f %s=%.3f ; per-unit window ratio %s/%s: median %.3f, %s faster on %d/%d units ; all exact: %s=%s %s=%s"
  % (la, lb, la, mean(A), lb, mean(B), lb, la, mean(B) / mean(A), la, tot(A), lb, tot(B), la, lb, st.median(ratio), lb, sum(x > 1 for x in ratio), len(ratio), la, all(A[u][2] for u in units), lb, all(B[u][2] for u in units)))
big = sorted(units, key=lambda u: -A[u][1])[:8]
n("AB largest units (window s %s / %s): " % (la, lb) + " ; ".join("%s %.3f/%.3f" % (u.replace("t3-", ""), A[u][0], B[u][0]) for u in big))
