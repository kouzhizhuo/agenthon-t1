#!/usr/bin/env python3
"""Linux verification of a Track 3 image: anonymous pull by digest, official Development runtime flags, per-unit container
window (docker inspect StartedAt -> FinishedAt, N repeats, first discarded) and correctness vs the PUBLIC references.

  verify.py --image ghcr.io/<owner>/<name> --digest sha256:... --track <track3-simulation-public checkout> --out evidence [--runs 5]
            [--units u1 u2 ...] [--platform linux/amd64] [--skip-pull]
"""
import argparse, hashlib, json, os, platform, shutil, statistics, subprocess, sys, tempfile, time, uuid
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import compare  # noqa: E402

FLAGS = ["--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m", "--cap-drop=ALL",
         "--security-opt", "no-new-privileges", "--pids-limit", "256", "--ulimit", "nproc=256:256", "--ulimit", "nofile=1024:1024",
         "--cpus=4", "--memory=16g", "--memory-swap=16g", "--network=none"]
DEFAULT_UNITS = ["t3-s001-price-time-priority", "t3-as01-base-mix", "t3-gb-base-30agent-30s", "t3-mp05-cancel-churn-newest",
                 "t3-mr-deep-book-state-size", "t3-gb-mega-throughput", "t3-gbatch-dense-3"]


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ts_ns(s):
    s = s.strip().rstrip("Z")
    frac = 0
    if "." in s:
        s, f = s.split(".")
        frac = int(f.ljust(9, "0")[:9])
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) * 10**9 + frac


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def stage(unit_dir, dst):
    dst.mkdir(parents=True)
    if (unit_dir / "scenario.json").is_file():
        shutil.copy(unit_dir / "scenario.json", dst / "scenario.json")
        args, batch = ["simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"], False
    else:
        shutil.copytree(unit_dir / "scenarios", dst / "scenarios")
        shutil.copy(unit_dir / "batch.json", dst / "batch.json")
        args, batch = ["simulate-batch", "--batch-dir", "/input/scenarios", "--out-dir", "/output"], True
    for f in [dst] + list(dst.rglob("*")):
        f.chmod(0o755 if f.is_dir() else 0o644)
    return args, batch


def run_once(image, in_dir, out_dir, args, seed, official=True, platform_=None):
    name = "t3v_" + uuid.uuid4().hex[:10]
    out_dir.mkdir(parents=True)
    out_dir.chmod(0o777)
    cmd = ["docker", "run", "--name", name] + (FLAGS if official else ["--network=none"])
    cmd += ["-e", f"QFBENCH_SEED={seed}", "-e", "QFBENCH_NETWORK=none"]
    if platform_:
        cmd += ["--platform", platform_]
    cmd += ["-v", f"{in_dir}:/input:ro", "-v", f"{out_dir}:/output", image] + args
    t0 = time.perf_counter()
    r = sh(cmd)
    host = time.perf_counter() - t0
    ins = sh(["docker", "inspect", "-f", "{{.State.StartedAt}} {{.State.FinishedAt}} {{.State.ExitCode}}", name])
    sh(["docker", "rm", "-f", name])
    a, b, rc = ins.stdout.split()
    return {"window_s": (ts_ns(b) - ts_ns(a)) / 1e9, "host_s": host, "rc": int(rc), "stderr_tail": r.stderr[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--track", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--units", nargs="*", default=DEFAULT_UNITS, help="unit names, or 'all' = every t3-* unit of the track repo (single + batch)")
    ap.add_argument("--platform", default=None)
    ap.add_argument("--skip-pull", action="store_true")
    ap.add_argument("--expect-codec", default=None, help="require this parquet codec in every column chunk footer")
    ap.add_argument("--fastparquet-informational", action="store_true", help="own-writer images: a fastparquet read mismatch is informational")
    ap.add_argument("--work", default=None, help="scratch dir (must be visible to the docker daemon)")
    a = ap.parse_args()
    out = Path(a.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    work = Path(a.work or tempfile.mkdtemp(prefix="t3v_")).resolve()
    work.mkdir(parents=True, exist_ok=True)
    work.chmod(0o755)
    ref = f"{a.image}@{a.digest}"
    units_dir = Path(a.track).resolve() / "units"
    if a.units == ["all"]:
        a.units = sorted(p.name for p in units_dir.iterdir() if p.is_dir() and p.name.startswith("t3-")
                         and ((p / "scenario.json").is_file() or (p / "batch.json").is_file()))
    rep = {"image": ref, "host": {"uname": " ".join(platform.uname()), "nproc": os.cpu_count(), "loadavg_start": os.getloadavg(),
                                   "docker": sh(["docker", "version", "--format", "{{.Server.Version}} {{.Server.Os}}/{{.Server.Arch}}"]).stdout.strip()}}
    # ---- anonymous pull by digest (empty DOCKER_CONFIG = no credentials)
    if not a.skip_pull:
        cfg = Path(tempfile.mkdtemp(prefix="anon_docker_"))
        env = dict(os.environ, DOCKER_CONFIG=str(cfg))
        t0 = time.perf_counter()
        p = subprocess.run(["docker", "pull"] + (["--platform", a.platform] if a.platform else []) + [ref], capture_output=True, text=True, env=env)
        rep["pull"] = {"rc": p.returncode, "seconds": round(time.perf_counter() - t0, 1), "tail": (p.stdout + p.stderr)[-400:], "anonymous": True}
        print("pull rc", p.returncode, flush=True)
        if p.returncode:
            (out / "report.json").write_text(json.dumps(rep, indent=1))
            return 2
    ins = json.loads(sh(["docker", "image", "inspect", ref]).stdout)[0]
    c = ins["Config"]
    rep["image_inspect"] = {"id": ins["Id"], "arch": ins["Architecture"], "os": ins["Os"], "labels": c.get("Labels"), "volumes": c.get("Volumes"),
                            "entrypoint": c.get("Entrypoint"), "user": c.get("User"), "repo_digests": ins.get("RepoDigests"), "size": ins.get("Size"),
                            "layers": len(ins.get("RootFS", {}).get("Layers", []))}
    # ---- container-start floor
    fl = []
    for _ in range(a.runs):
        nm = "t3v_" + uuid.uuid4().hex[:10]
        sh(["docker", "run", "--name", nm, "--network=none", "alpine:3.20", "true"])
        i = sh(["docker", "inspect", "-f", "{{.State.StartedAt}} {{.State.FinishedAt}}", nm])
        sh(["docker", "rm", "-f", nm])
        x, y = i.stdout.split()
        fl.append((ts_ns(y) - ts_ns(x)) / 1e9)
    rep["alpine_true_window_median_s"] = statistics.median(fl[1:] or fl)
    # ---- units
    rows = []
    outroot = out / "outputs"
    for u in a.units:
        ud = units_dir / u
        in_dir = work / u / "in"
        args, batch = stage(ud, in_dir)
        sdir = ud / ("scenarios/" + json.loads((ud / "batch.json").read_text())["subs"][0]["scenario_file"].split("/")[-1]) if batch else ud / "scenario.json"
        seed = json.loads(sdir.read_text()).get("seed")
        runs, last_out = [], None
        for i in range(a.runs):
            o = work / u / f"out{i}"
            runs.append(run_once(ref, in_dir, o, args, seed, True, a.platform))
            last_out = o
        meas = runs[1:] if len(runs) > 1 else runs
        bad = [r for r in runs if r["rc"] != 0]
        row = {"unit": u, "batch": batch, "runs": a.runs, "n_failed": len(bad), "window_median_s": statistics.median(r["window_s"] for r in meas),
               "window_min_s": min(r["window_s"] for r in meas), "window_max_s": max(r["window_s"] for r in meas),
               "host_median_s": statistics.median(r["host_s"] for r in meas), "windows_s": [round(r["window_s"], 4) for r in runs]}
        if bad:
            row["stderr_tail"] = bad[0]["stderr_tail"]
        else:
            ev = json.loads((ud / "events.json").read_text())["n_events"] if not batch else sum(s["n_events"] for s in json.loads((ud / "batch.json").read_text())["subs"])
            row["n_events"] = ev
            row["events_per_s_window"] = ev / row["window_median_s"]
            dst = outroot / u
            shutil.copytree(last_out, dst)
            refdir = ud if not batch else ud / "checks" / "reference_data"
            ok, text = compare.compare_dirs(refdir, dst)
            row["exact_vs_public_reference"] = bool(ok)
            try:                                   # independent readers: footer rows, pandas dtypes+values, polars, fastparquet
                import indep_read
                indep_read.INFO_FASTPARQUET = a.fastparquet_informational
                indep_read.EXPECT_CODEC = a.expect_codec
                bad_, info_ = indep_read.split(indep_read.check_dirs(refdir, dst))
                if info_:
                    row["indep_read_info"] = info_
                row["indep_read_ok"] = not bad_
                if bad_:
                    row["indep_read_failures"] = bad_
            except ImportError:
                row["indep_read_ok"] = None
            files = [(dst / f, refdir / f) for f in ("trace.parquet", "message_trace.parquet")] if not batch else \
                [(dst / s["sub"] / f, refdir / s["sub"] / f) for s in json.loads((ud / "batch.json").read_text())["subs"] for f in ("trace.parquet", "message_trace.parquet")]
            row["byte_identical_to_public_reference"] = f"{sum(sha(x) == sha(y) for x, y in files)}/{len(files)}"
            # repeat stability: compare the first and last run outputs
            first = work / u / "out0"
            row["repeat_files_byte_identical"] = all(sha(p) == sha(last_out / p.relative_to(first)) for p in sorted(first.rglob("*.parquet")))
            if not ok:
                row["compare_text"] = text[:1500]
            shutil.rmtree(work / u, ignore_errors=True)
        rows.append(row)
        print(f"{u}: window {row['window_median_s']:.3f}s exact={row.get('exact_vs_public_reference')} bytes={row.get('byte_identical_to_public_reference')}", flush=True)
    rep["units"] = rows
    good = [r for r in rows if r.get("events_per_s_window")]
    rep["roster"] = {"n_units": len(rows), "n_exact": sum(bool(r.get("exact_vs_public_reference")) for r in rows),
                     "n_failed_runs": sum(r["n_failed"] for r in rows),
                     "roster_mean_events_per_s": sum(r["events_per_s_window"] for r in good) / max(1, len(rows)),
                     "total_window_s": sum(r["window_median_s"] for r in rows),
                     "non_exact": [r["unit"] for r in rows if not r.get("exact_vs_public_reference")]}
    rep["host"]["loadavg_end"] = os.getloadavg()
    # ---- current scorer on the container outputs
    g = subprocess.run([sys.executable, str(HERE / "gate_current.py"), "--out-root", str(outroot), "--unit-root", str(units_dir),
                        "--units", "all", "--save-dir", str(out / "gate")], capture_output=True, text=True)
    rep["gate_current"] = g.stdout[-3000:] + g.stderr[-500:]
    (out / "report.json").write_text(json.dumps(rep, indent=1))
    md = [f"### {ref}", f"image id `{rep['image_inspect']['id']}` {rep['image_inspect']['os']}/{rep['image_inspect']['arch']}; labels {rep['image_inspect']['labels']}; volumes {rep['image_inspect']['volumes']}",
          f"host: {rep['host']['uname']} nproc={rep['host']['nproc']} load1 {rep['host']['loadavg_start'][0]:.2f}->{rep['host']['loadavg_end'][0]:.2f}; alpine-true window {rep['alpine_true_window_median_s']:.3f}s",
          "", "| unit | events | window s (median of runs 2..N) | min | max | events/s | exact vs public ref | bytes == public | repeat bytes equal | failed |", "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['unit']} | {r.get('n_events')} | {r['window_median_s']:.3f} | {r['window_min_s']:.3f} | {r['window_max_s']:.3f} | {r.get('events_per_s_window', 0):,.0f} | {r.get('exact_vs_public_reference')} | {r.get('byte_identical_to_public_reference')} | {r.get('repeat_files_byte_identical')} | {r['n_failed']} |")
    md += ["", "roster: " + json.dumps(rep["roster"]), "", "gate_current (current scorer, developer verifier):", "```", g.stdout.strip().splitlines()[-1] if g.stdout.strip() else g.stderr[-300:], "```"]
    (out / "summary.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    ok = all(r["n_failed"] == 0 and r.get("exact_vs_public_reference") and r.get("indep_read_ok") is not False for r in rows) and g.returncode == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
