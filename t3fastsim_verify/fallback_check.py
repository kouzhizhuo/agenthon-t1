#!/usr/bin/env python3
"""Fallback-path check on the real runner: a scenario the native engine does not parse (string-typed agent count) must (a) fall back to the bundled
Python/ABIDES path and give output identical to the organizer adapter's (reference = parquet produced by the organizer adapter), in images WITH
fallback (expect rc 0), and (b) exit 3 in images without it.   usage: fallback_check.py --image X --out file.json --digest label=sha256:...[:expect_rc] ..."""
import argparse, json, os, subprocess, sys, tempfile, uuid
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare, verify as V  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--digest", action="append", required=True, help="label=sha256:...=expected_rc")
a = ap.parse_args()
data = HERE / "data" / "fzF00000"
work = Path(tempfile.mkdtemp(prefix="t3fb_"))
work.chmod(0o755)
rep = {}
for spec in a.digest:
    label, dig, erc = spec.split("=")[0], spec.split("=")[1], int(spec.split("=")[2])
    ref = f"{a.image}@{dig}"
    inp = work / label / "in"
    inp.mkdir(parents=True)
    (inp / "scenario.json").write_bytes((data / "scenario.json").read_bytes())
    inp.chmod(0o755)
    (inp / "scenario.json").chmod(0o644)
    out = work / label / "out"
    out.mkdir()
    out.chmod(0o777)
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref], capture_output=True)
    seed = json.loads((inp / "scenario.json").read_text())["seed"]
    r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64"] + V.FLAGS + ["-e", f"QFBENCH_SEED={seed}", "-e", "QFBENCH_NETWORK=none", "-v", f"{inp}:/input:ro", "-v", f"{out}:/output", ref,
                        "simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"], capture_output=True, text=True)
    row = {"image": ref, "rc": r.returncode, "expected_rc": erc, "stderr_tail": r.stderr[-300:]}
    if erc == 0 and r.returncode == 0:
        ok, text = compare.compare_dirs(data, out)
        row["identical_to_organizer_adapter"] = bool(ok)
        if not ok:
            row["compare"] = text[:600]
    row["ok"] = r.returncode == erc and (erc != 0 or row.get("identical_to_organizer_adapter", False))
    rep[label] = row
    msg = "FALLBACK-CHECK %s: rc=%s expected=%s identical=%s ok=%s stderr=[%s]" % (label, r.returncode, erc, row.get("identical_to_organizer_adapter"), row["ok"], r.stderr[-160:].strip().replace("\n", " | "))
    print("::notice title=T3V-FALLBACK::" + msg.replace("%", "%25"))
Path(a.out).write_text(json.dumps(rep, indent=1))
