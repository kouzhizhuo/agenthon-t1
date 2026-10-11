#!/usr/bin/env python3
"""Crash/stability stress for image digests on the real runner: N repeated runs per unit under the official hardened flags, counting non-zero
exits (with stderr text) and checking byte-identity of all outputs across runs. Emits ::notice annotations (one per digest) and evidence JSON.
  stress.py --image ghcr.io/x/y --track <track checkout> --out evidence/stress.json --digest label=sha256:... [--digest ...]
            [--plan t3-ra02-shock-early=200 t3-as01-base-mix=50 t3-gbatch-hetero-mix=50]"""
import argparse, json, os, shutil, subprocess, sys, tempfile, time, uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify as V  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True)
ap.add_argument("--track", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--digest", action="append", required=True, help="label=sha256:...")
ap.add_argument("--plan", nargs="*", default=["t3-ra02-shock-early=200", "t3-as01-base-mix=50", "t3-gbatch-hetero-mix=50"])
ap.add_argument("--platform", default="linux/amd64")
a = ap.parse_args()
units_dir = Path(a.track).resolve() / "units"
work = Path(tempfile.mkdtemp(prefix="t3stress_"))
work.chmod(0o755)
report = {}
for spec in a.digest:
    label, dig = spec.split("=", 1)
    ref = f"{a.image}@{dig}"
    cfg = Path(tempfile.mkdtemp(prefix="anon_docker_"))
    p = subprocess.run(["docker", "pull", "--platform", a.platform, ref], capture_output=True, text=True, env=dict(os.environ, DOCKER_CONFIG=str(cfg)))
    rep = {"image": ref, "pull_rc": p.returncode, "units": {}}
    for item in a.plan:
        unit, n = item.rsplit("=", 1)
        n = int(n)
        in_dir = work / label / unit / "in"
        args, batch = V.stage(units_dir / unit, in_dir)
        first = (units_dir / unit / ("scenarios/" + json.loads((units_dir / unit / "batch.json").read_text())["subs"][0]["scenario_file"].split("/")[-1])) if batch else units_dir / "scenario.json"
        seed = json.loads(((units_dir / unit / "scenario.json") if not batch else first).read_text()).get("seed")
        codes, crashes, digests = {}, [], set()
        t0 = time.time()
        for i in range(n):
            out = work / label / unit / f"o{i}"
            out.mkdir(parents=True)
            out.chmod(0o777)
            name = "t3s_" + uuid.uuid4().hex[:10]
            cmd = ["docker", "run", "--name", name, "--platform", a.platform] + V.FLAGS + ["-e", f"QFBENCH_SEED={seed}", "-e", "QFBENCH_NETWORK=none",
                   "-v", f"{in_dir}:/input:ro", "-v", f"{out}:/output", ref] + args
            r = subprocess.run(cmd, capture_output=True, text=True)
            st = subprocess.run(["docker", "inspect", "-f", "{{.State.ExitCode}} {{.State.OOMKilled}} {{.State.Error}}", name], capture_output=True, text=True).stdout.strip()
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
            codes[r.returncode] = codes.get(r.returncode, 0) + 1
            if r.returncode != 0:
                crashes.append({"run": i, "rc": r.returncode, "inspect": st, "stderr_tail": r.stderr[-500:], "stdout_tail": r.stdout[-200:]})
            else:
                digests.add(tuple((str(f.relative_to(out)), V.sha(f)) for f in sorted(out.rglob("*.parquet"))))
            shutil.rmtree(out, ignore_errors=True)
        rep["units"][unit] = {"runs": n, "exit_codes": codes, "nonzero": sum(v for k, v in codes.items() if k != 0), "distinct_output_digests": len(digests),
                              "byte_identical_all_ok_runs": len(digests) <= 1, "seconds": round(time.time() - t0, 1), "crashes": crashes[:5]}
        print(f"{label} {unit}: runs={n} exit_codes={codes} distinct_digests={len(digests)}", flush=True)
    report[label] = rep
    parts = [f"{u}: runs={d['runs']} exits={d['exit_codes']} nonzero={d['nonzero']} byte_identical={d['byte_identical_all_ok_runs']}" for u, d in rep["units"].items()]
    crash_txt = " || ".join(f"{u} run{c['run']} rc={c['rc']} inspect=[{c['inspect']}] stderr=[{c['stderr_tail'].strip()[-300:]}]" for u, d in rep["units"].items() for c in d["crashes"][:2])
    msg = "%s pull_rc=%s ; %s%s" % (ref, p.returncode, " ; ".join(parts), (" ; CRASHES: " + crash_txt) if crash_txt else "")
    print("::notice title=T3V-STRESS-%s::" % label + msg.replace("%", "%25").replace("\n", " | "))
Path(a.out).parent.mkdir(parents=True, exist_ok=True)
Path(a.out).write_text(json.dumps(report, indent=1))
