#!/usr/bin/env python3
"""Organizer baseline image (built from the public track repo) vs v10m in-image fallback on the same amd64 runner, on routed NaN/huge-size cases.
Also compares each with the stored (arm64-made) adapter reference, and prints numpy/pandas/pyarrow/python versions of both images.
usage: baseline_cmp.py --baseline-image TAG --v10m REF --out file.json"""
import argparse, json, subprocess, sys, tempfile
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare, verify as V  # noqa: E402

import traceback as _tb


def _hook(t, v, tb):
    print("::error title=T3V-BASELINE-CRASH::" + "".join(_tb.format_exception(t, v, tb))[-900:].replace("%", "%25").replace("\n", "%0A"), flush=True)


sys.excepthook = _hook
ap = argparse.ArgumentParser()
ap.add_argument("--baseline-image", required=True)
ap.add_argument("--v10m", "--fallback-image", dest="v10m", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--cases", nargs="*", default=["fzE200213", "fzE200048", "fzE200135", "fzE200207"])
a = ap.parse_args()
work = Path(tempfile.mkdtemp(prefix="t3bc_")); work.chmod(0o755)
VER = "import sys,numpy,pandas,pyarrow;print(sys.version.split()[0],'numpy',numpy.__version__,'pandas',pandas.__version__,'pyarrow',pyarrow.__version__)"
subprocess.run(["docker", "pull", "--platform", "linux/amd64", a.v10m], capture_output=True)
rep = {"versions": {}, "cases": {}}
for lab, img, ep in (("baseline", a.baseline_image, "python"), ("v10m", a.v10m, "/opt/py/bin/python3")):
    r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64", "--entrypoint", ep, img, "-c", VER], capture_output=True, text=True)
    rep["versions"][lab] = (r.stdout + r.stderr).strip()[-300:]


def _root_flags():
    """organizer baseline image is not readable by uid 65534 (known): same flags but run as root inside the container"""
    f = list(V.FLAGS)
    i = f.index("--user")
    return f[:i] + ["--user", "0:0"] + f[i + 2:]


def run(lab, img, case, rep_i):
    d = work / lab / case / str(rep_i)
    (d / "in").mkdir(parents=True); (d / "out").mkdir()
    d.chmod(0o755); (d / "in").chmod(0o755); (d / "out").chmod(0o777)
    sc = HERE / "data" / "routed" / case / "scenario.json"
    (d / "in" / "scenario.json").write_bytes(sc.read_bytes()); (d / "in" / "scenario.json").chmod(0o644)
    r = subprocess.run(["docker", "run", "--rm", "--platform", "linux/amd64"] + (list(V.FLAGS) if lab != "baseline" else _root_flags()) + ["-e", "QFBENCH_SEED=1", "-e", "QFBENCH_NETWORK=none", "-v", f"{d/'in'}:/input:ro", "-v", f"{d/'out'}:/output", img,
                        "simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"], capture_output=True, text=True)
    if r.returncode:
        print("::warning title=T3V-BASELINE-RUN-%s-%s::rc=%s %s" % (lab, case, r.returncode, r.stderr[-300:].replace("\n", " | ").replace("%", "%25")))
    return r.returncode, d / "out"

import traceback


def one_case(c):
    row = {}
    b1 = run("baseline", a.baseline_image, c, 0); b2 = run("baseline", a.baseline_image, c, 1); v1 = run("v10m", a.v10m, c, 0)
    row["rc"] = {"baseline": b1[0], "baseline2": b2[0], "v10m": v1[0]}
    ref = HERE / "data" / "routed" / c
    for name, (x, y) in {"baseline_vs_baseline_rerun": (b1[1], b2[1]), "v10m_vs_baseline": (v1[1], b1[1]), "baseline_vs_arm64_ref": (b1[1], ref)}.items():
        if not (x / "trace.parquet").exists() or not (y / "trace.parquet").exists():
            row[name] = "missing-output"; continue
        ok, text = compare.compare_dirs(y, x)
        row[name] = "IDENTICAL" if ok else "DIFF: " + text[:400].replace("\n", " | ")
    return row


for c in a.cases:
    try:
        rep["cases"][c] = one_case(c)
    except BaseException:  # noqa: BLE001
        rep["cases"][c] = {"error": traceback.format_exc()[-500:]}
    print("T3V-BASELINE-CASE", c, json.dumps(rep["cases"][c]), flush=True)
    Path(a.out).write_text(json.dumps(rep, indent=1))
print("::notice title=T3V-BASELINE-VERSIONS::" + json.dumps(rep["versions"]).replace("%", "%25"))
items = list(rep["cases"].items())
for i in range(0, len(items), 2):
    print("::notice title=T3V-BASELINE-CMP-%d::%s" % (i // 2 + 1, " ;; ".join("%s %s" % (c, json.dumps(r)) for c, r in items[i:i + 2]).replace("%", "%25").replace("\n", " ")))
