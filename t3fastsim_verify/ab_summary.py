#!/usr/bin/env python3
"""Load-matched summary of several verify.py runs of N digests executed back to back in ONE job (e.g. v6,v7s,v7f,v6).
usage: ab_summary.py <labelA=dir,dir> <labelB=dir,dir> [<labelC=dir,dir> ...]   (every later label is compared with the first)"""
import json, statistics as st, sys
from pathlib import Path


def load(spec):
    label, dirs = spec.split("=", 1)
    per = {}
    for d in dirs.split(","):
        for u in json.loads((Path(d) / "report.json").read_text())["units"]:
            per.setdefault(u["unit"], []).append((u["window_median_s"], u.get("n_events"), u.get("exact_vs_public_reference"), u.get("indep_read_ok")))
    return label, {k: (st.mean(x[0] for x in v), v[0][1], all(x[2] for x in v), all(x[3] is not False for x in v)) for k, v in per.items()}


def n(m):
    print("::notice title=T3V-AB::" + m.replace("%", "%25"))


sets = [load(s) for s in sys.argv[1:]]
la, A = sets[0]
units = sorted(A)
mean = lambda M: st.mean(M[u][1] / M[u][0] for u in units)
tot = lambda M: sum(M[u][0] for u in units)
for lb, B in sets[1:]:
    ratio = [A[u][0] / B[u][0] for u in units]
    n("AB %s vs %s (mean of the runs of each digest, same job): roster_mean_events_per_s %s=%.0f %s=%.0f (%s/%s=%.3f) ; total_window_s %s=%.3f %s=%.3f ; per-unit window ratio %s/%s: median %.3f, %s faster on %d/%d units ; all exact: %s=%s %s=%s ; indep readers ok: %s=%s %s=%s"
      % (la, lb, la, mean(A), lb, mean(B), lb, la, mean(B) / mean(A), la, tot(A), lb, tot(B), la, lb, st.median(ratio), lb, sum(x > 1 for x in ratio), len(ratio),
         la, all(A[u][2] for u in units), lb, all(B[u][2] for u in units), la, all(A[u][3] for u in units), lb, all(B[u][3] for u in units)))
big = sorted(units, key=lambda u: -A[u][1])[:8]
n("AB largest units (window s " + " / ".join(l for l, _ in sets) + "): " + " ; ".join("%s " % u.replace("t3-", "") + "/".join("%.3f" % M[u][0] for _, M in sets) for u in big))
