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


def run_matrix(label, dig):
    """Fallback matrix for an image WITH fallback: string-count single (rc 0, identical), unknown agent_type (rc 1), missing agent_configs (rc 1),
    batch with one string-count sub (rc 0, every sub identical to the organizer adapter's)."""
    ref = f"{a.image}@{dig}"
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", ref], capture_output=True)
    out_rep = {}
    def run(case, mount_dir, args, erc, refdir=None):
        o = work / label / case / "out"
        o.mkdir(parents=True)
        o.chmod(0o777)
        r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64"] + V.FLAGS + ["-e", "QFBENCH_SEED=1", "-e", "QFBENCH_NETWORK=none", "-v", f"{mount_dir}:/input:ro", "-v", f"{o}:/output", ref] + args,
                           capture_output=True, text=True)
        row = {"rc": r.returncode, "expected_rc": erc, "stderr_tail": r.stderr[-200:]}
        if erc == 0 and r.returncode == 0 and refdir is not None:
            ok, text = compare.compare_dirs(refdir, o)
            row["identical_to_organizer_adapter"] = bool(ok)
        row["ok"] = r.returncode == erc and (erc != 0 or refdir is None or row.get("identical_to_organizer_adapter", False))
        out_rep[case] = row
        print("::notice title=T3V-FALLBACK-MATRIX::" + ("%s %s: rc=%s expected=%s identical=%s ok=%s stderr=[%s]" % (label, case, r.returncode, erc, row.get("identical_to_organizer_adapter"), row["ok"], r.stderr[-120:].strip().replace("\n", " | "))).replace("%", "%25"))
    for case, erc in (("unknown_agent_type", 1), ("missing_agent_configs", 1)):
        d = work / label / case / "in"
        d.mkdir(parents=True)
        (d / "scenario.json").write_bytes((HERE / "data" / "fb_single" / f"{case}.json").read_bytes())
        d.chmod(0o755); (d / "scenario.json").chmod(0o644)
        run(case, d, ["simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"], erc)
    bd = work / label / "batch_string_count" / "in"
    bd.mkdir(parents=True)
    import shutil
    shutil.copytree(HERE / "data" / "fb_batch" / "scenarios", bd / "scenarios")
    shutil.copy(HERE / "data" / "fb_batch" / "batch.json", bd / "batch.json")
    for f in [bd] + list(bd.rglob("*")):
        f.chmod(0o755 if f.is_dir() else 0o644)
    run("batch_string_count", bd, ["simulate-batch", "--batch-dir", "/input/scenarios", "--out-dir", "/output"], 0, HERE / "data" / "fb_batch" / "ref")
    return out_rep


for spec in a.digest:
    label, dig, erc_s = spec.split("=")[0], spec.split("=")[1], spec.split("=")[2]
    if erc_s == "matrix":
        rep[label] = run_matrix(label, dig)
        continue
    erc = int(erc_s)
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
