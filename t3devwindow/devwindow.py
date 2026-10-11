#!/usr/bin/env python3
"""Dev-window proxy on a real runner: the organizer's Dev window is `docker create -> start -> wait -> rm` (README). For each image and unit,
N runs, each step timed with time.monotonic(): create, start (the `docker start` call), wait (start -> container exit), rm, total.
Also the in-container window (inspect StartedAt -> FinishedAt). Runs are interleaved across images/variants (rep-major) so drift hits all alike.
  devwindow.py --track-raw-base URL --out evidence/devwindow.json [--n 10]"""
import argparse, json, os, statistics as st, subprocess, sys, tempfile, time, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

FLAGS = ["--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m", "--cap-drop=ALL", "--security-opt", "no-new-privileges",
         "--pids-limit", "256", "--ulimit", "nproc=256:256", "--ulimit", "nofile=1024:1024", "--cpus=4", "--memory=16g", "--memory-swap=16g", "--network=none"]
IMG = "ghcr.io/kouzhizhuo/agenthon-t3"
IMAGES = {  # label -> digest
    "v8f": "sha256:1149e11c50532c313cd8e297fd30eca9265e5534407f7719d465e529bce962a9",
    "v9f": "sha256:01331787987457f64093c4f0f3caf0ee1b58dbc36a87de9dcdda1b00e2807e26",
}


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def ts_ns(s):
    s = s.strip().rstrip("Z")
    frac = 0
    if "." in s:
        s, f = s.split(".")
        frac = int(f.ljust(9, "0")[:9])
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) * 10**9 + frac


def one(image, in_dir, out_root, cmd_args, entrypoint=None, extra=()):
    out = out_root / ("o_" + uuid.uuid4().hex[:8])
    out.mkdir(parents=True)
    out.chmod(0o777)
    name = "dw_" + uuid.uuid4().hex[:10]
    create = ["docker", "create", "--name", name] + (FLAGS if in_dir is not None else ["--network=none"]) + list(extra)
    if in_dir is not None:
        create += ["-e", "QFBENCH_SEED=1", "-e", "QFBENCH_NETWORK=none", "-v", f"{in_dir}:/input:ro", "-v", f"{out}:/output"]
    if entrypoint:
        create += ["--entrypoint", entrypoint]
    create += [image] + cmd_args
    m = time.monotonic
    t0 = m(); r = sh(create); t1 = m()
    if r.returncode != 0:
        return {"error": "create: " + r.stderr[-200:]}
    s = sh(["docker", "start", name]); t2 = m()
    w = sh(["docker", "wait", name]); t3 = m()
    ins = sh(["docker", "inspect", "-f", "{{.State.StartedAt}} {{.State.FinishedAt}}", name]).stdout.split()
    d = sh(["docker", "rm", name]); t4 = m()
    rc = int(w.stdout.strip() or -1)
    row = {"create_s": t1 - t0, "start_s": t2 - t1, "wait_s": t3 - t2, "rm_s": t4 - t3, "total_s": t4 - t0, "rc": rc}
    if len(ins) == 2:
        row["inside_window_s"] = (ts_ns(ins[1]) - ts_ns(ins[0])) / 1e9
    import shutil
    shutil.rmtree(out, ignore_errors=True)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw-base", required=True, help="raw.githubusercontent URL of the track repo at the pinned commit (units/<u>/scenario.json)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--n", type=int, default=10)
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix="dw_")); work.chmod(0o755)
    units = {}
    for key, unit in (("s001", "t3-s001-price-time-priority"), ("mega", "t3-gb-mega-throughput")):
        d = work / key / "in"; d.mkdir(parents=True)
        (d / "scenario.json").write_bytes(urllib.request.urlopen(f"{a.raw_base}/units/{unit}/scenario.json", timeout=60).read())
        for p in (d, d / "scenario.json"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        units[key] = d
    cfg = tempfile.mkdtemp(prefix="anon_docker_")
    env = dict(os.environ, DOCKER_CONFIG=cfg)
    pulled = {}
    for label, dig in IMAGES.items():
        t = time.monotonic(); p = subprocess.run(["docker", "pull", "--platform", "linux/amd64", f"{IMG}@{dig}"], capture_output=True, text=True, env=env)
        pulled[label] = p.returncode == 0
        print("pull", label, p.returncode, round(time.monotonic() - t, 1), flush=True)
    sh(["docker", "pull", "alpine:3.20"])
    sim = ["simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"]
    cases = []   # (name, image, unit key or None, args, entrypoint, extra)
    for label, dig in IMAGES.items():
        if not pulled[label]:
            continue
        for uk in ("s001", "mega"):
            cases.append((f"{label}/{uk}", f"{IMG}@{dig}", uk, sim, None, ()))
    cases.append(("alpine/true", "alpine:3.20", None, ["true"], None, ()))
    v8f = f"{IMG}@{IMAGES['v8f']}"
    VARIANTS = os.environ.get("DW_VARIANTS") == "1"
    # variants on v8f
    if VARIANTS: cases.append(("v8f/s001 +1MB stderr (sh wrapper)", v8f, "s001", ["-c", "yes 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd | head -c 1048576 >&2; simulate --config /input/scenario.json --out /output/trace.parquet"], "sh", ()))
    if VARIANTS: cases.append(("v8f/mega +1MB stderr (sh wrapper)", v8f, "mega", ["-c", "yes 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcd | head -c 1048576 >&2; simulate --config /input/scenario.json --out /output/trace.parquet"], "sh", ()))
    res = {name: [] for name, *_ in cases}
    notes = {"gpus": "--gpus not available on this runner (no NVIDIA runtime)"}
    # storage-opt probe
    pr = sh(["docker", "create", "--storage-opt", "size=1G", "--name", "dw_probe", "alpine:3.20", "true"])
    notes["storage_opt_size_1G"] = "accepted" if pr.returncode == 0 else "rejected: " + pr.stderr.strip()[-160:]
    sh(["docker", "rm", "-f", "dw_probe"])
    notes["storage_driver"] = sh(["docker", "info", "-f", "{{.Driver}} / {{.DriverStatus}}"]).stdout.strip()
    notes["docker"] = sh(["docker", "version", "--format", "{{.Server.Version}}"]).stdout.strip()
    if pr.returncode == 0 and VARIANTS:
        for name, image, uk, args, ep, extra in list(cases):
            if name in ("v8f/s001", "v8f/mega"):
                cases.append((name + " +storage-opt 1G", image, uk, args, ep, ("--storage-opt", "size=1G")))
                res[name + " +storage-opt 1G"] = []
    for rep in range(a.n):
        for name, image, uk, args, ep, extra in cases:
            row = one(image, units[uk] if uk else None, work / "out", args, ep, extra)
            res[name].append(row)
    summary = {}
    for name, rows in res.items():
        ok = [r for r in rows if "error" not in r and r["rc"] == 0]
        def med(k): return round(st.median(r[k] for r in ok), 4) if ok else None
        summary[name] = {"n_ok": len(ok), "n": len(rows), "create_s": med("create_s"), "start_s": med("start_s"), "wait_s": med("wait_s"), "rm_s": med("rm_s"), "total_s": med("total_s"),
                         "total_min": round(min(r["total_s"] for r in ok), 4) if ok else None, "total_max": round(max(r["total_s"] for r in ok), 4) if ok else None,
                         "inside_window_s": round(st.median(r["inside_window_s"] for r in ok if "inside_window_s" in r), 4) if ok else None,
                         "errors": [r["error"] for r in rows if "error" in r][:2]}
        print(f"{name:40s} n={len(ok)}/{len(rows)} create={summary[name]['create_s']} start={summary[name]['start_s']} wait={summary[name]['wait_s']} rm={summary[name]['rm_s']} total={summary[name]['total_s']} (min {summary[name]['total_min']} max {summary[name]['total_max']}) inside={summary[name]['inside_window_s']}", flush=True)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"summary": summary, "notes": notes, "raw": res}, indent=1))
    # annotations (<=10 notices)
    def n(m): print("::notice title=T3-DEVWINDOW::" + m.replace("%", "%25").replace("\n", " | "))
    n("NOTES " + json.dumps(notes))
    names = list(summary)
    for i in range(0, len(names), 4):
        n("MEDIANS(create/start/wait/rm/total s; n_ok) " + " ; ".join(
            f"{k}: {summary[k]['create_s']}/{summary[k]['start_s']}/{summary[k]['wait_s']}/{summary[k]['rm_s']}/{summary[k]['total_s']} (min {summary[k]['total_min']} max {summary[k]['total_max']}) in={summary[k]['inside_window_s']} n={summary[k]['n_ok']}/{summary[k]['n']}" for k in names[i:i + 4]))


if __name__ == "__main__":
    main()
