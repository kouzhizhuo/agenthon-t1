#!/usr/bin/env python3
"""Print GitHub ::notice annotations for one verify.py output dir (job logs/artifacts need a login, annotations do not).
Stays under 10 notices per step: header, UNITS chunks of 12, roster, gate.   usage: emit_annotations.py <evidence-dir> <label>"""
import json, sys
from pathlib import Path

ev, label = Path(sys.argv[1]), sys.argv[2]
SHORT = len(sys.argv) > 3 and sys.argv[3] == "short"      # header + roster + gate only (annotation budget)
p = ev / "report.json"
if not p.exists():
    print("::error title=T3V::no report.json in " + str(ev)); raise SystemExit


def n(msg):
    print("::notice title=T3V-%s::" % label + msg.replace("%", "%25").replace("\r", "").replace("\n", " | "))


r = json.loads(p.read_text())
pl = r.get("pull") or {}
n("IMAGE " + r["image"] + " id " + r["image_inspect"]["id"] + " " + r["image_inspect"]["os"] + "/" + r["image_inspect"]["arch"] + " labels " + json.dumps(r["image_inspect"]["labels"]) + " volumes " + str(r["image_inspect"]["volumes"]) + " size_bytes " + str(r["image_inspect"].get("size")) + " layers " + str(r["image_inspect"].get("layers"))
  + " ; ANON PULL rc=" + str(pl.get("rc")) + " in " + str(pl.get("seconds")) + "s anonymous=" + str(pl.get("anonymous"))
  + " ; HOST " + r["host"]["uname"] + " nproc=" + str(r["host"]["nproc"]) + " docker=" + r["host"]["docker"] + " load1 " + str(round(r["host"]["loadavg_start"][0], 2)) + "->" + str(round(r["host"]["loadavg_end"][0], 2))
  + " ; ALPINE_TRUE_WINDOW_MEDIAN_S " + str(round(r["alpine_true_window_median_s"], 4)))
rows = ["%s %s %.4f %.0f ex=%s by=%s rp=%s ix=%s f=%s" % (u["unit"].replace("t3-", ""), u.get("n_events"), u["window_median_s"], u.get("events_per_s_window", 0),
        int(bool(u.get("exact_vs_public_reference"))), u.get("byte_identical_to_public_reference"), int(bool(u.get("repeat_files_byte_identical"))), u.get("indep_read_ok"), u["n_failed"]) for u in r["units"]]
K = 12
for i in range(0, 0 if SHORT else len(rows), K):
    n("UNITS[%d-%d] (name events window_s events_per_s exact bytes_eq_public(informational) repeat_bytes_equal indep_readers failed_runs): " % (i + 1, min(i + K, len(rows))) + " ; ".join(rows[i:i + K]))
ro = r.get("roster", {})
n("roster_mean_events_per_s=%.0f total_window_s=%.3f n_units=%s n_exact=%s n_failed_runs=%s non_exact=%s" % (ro.get("roster_mean_events_per_s", 0), ro.get("total_window_s", 0), ro.get("n_units"), ro.get("n_exact"), ro.get("n_failed_runs"), ro.get("non_exact")))
g = r.get("gate_current", "")
n("GATE_CURRENT " + (g.strip().splitlines()[-1] if g.strip() else "none"))
