#!/usr/bin/env python3
"""Fallback worst case on a real amd64 runner: the organizer adapter (Python fallback, triggered by a harmless string-typed agent count) on gb-mega- and
gb-pop-horizon-shaped scenarios inside the hardened container: window (docker inspect), peak memory (docker stats polling), output size, exit code; vs the 300 s cap."""
import json, os, re, subprocess, sys, tempfile, threading, time, urllib.request, uuid
from datetime import datetime, timezone
from pathlib import Path

FLAGS = ["--user", "65534:65534", "--read-only", "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=64m", "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--pids-limit", "256",
         "--ulimit", "nproc=256:256", "--ulimit", "nofile=1024:1024", "--cpus=4", "--memory=16g", "--memory-swap=16g", "--network=none"]
IMG = "ghcr.io/kouzhizhuo/agenthon-t3"
IMAGES = {"v9f": "sha256:01331787987457f64093c4f0f3caf0ee1b58dbc36a87de9dcdda1b00e2807e26", "v9m": "sha256:07d16a51f6f46d288ae4466bd51d2ecaf4654efba37d40a264563fef405bf54b"}
RAW = "https://raw.githubusercontent.com/Agenthon-2026/track3-simulation-public/1d7e994761f2e9256dd0c572c3592422c8fd0914/units/%s/scenario.json"
UNITS = ["t3-gb-mega-throughput", "t3-gb-pop-horizon-scale", "t3-gb-horizon-240s", "t3-gb-highfreq-40hz-60s"]


def ts_ns(s):
    s = s.strip().rstrip("Z"); frac = 0
    if "." in s:
        s, f = s.split("."); frac = int(f.ljust(9, "0")[:9])
    return int(datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()) * 10**9 + frac


def mem_mib(s):
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", s.strip())
    return float(m.group(1)) * {"KiB": 1 / 1024, "KB": 1 / 1024, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1024}[m.group(2)] if m else None


work = Path(tempfile.mkdtemp(prefix="fw_")); work.chmod(0o755)
cfg = tempfile.mkdtemp(prefix="anon_")
rows = []
for label, dig in IMAGES.items():
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", f"{IMG}@{dig}"], capture_output=True, env=dict(os.environ, DOCKER_CONFIG=cfg))
    for unit in UNITS:
        sc = json.loads(urllib.request.urlopen(RAW % unit, timeout=60).read())
        sc["agent_configs"][0]["count"] = str(sc["agent_configs"][0]["count"])
        d = work / label / unit / "in"; d.mkdir(parents=True); (d / "scenario.json").write_text(json.dumps(sc)); d.chmod(0o755); (d / "scenario.json").chmod(0o644)
        out = work / label / unit / "out"; out.mkdir(); out.chmod(0o777)
        name = "fw_" + uuid.uuid4().hex[:8]
        subprocess.run(["docker", "run", "-d", "--name", name, "--platform", "linux/amd64"] + FLAGS + ["-e", f"QFBENCH_SEED={sc['seed']}", "-e", "QFBENCH_NETWORK=none", "-v", f"{d}:/input:ro", "-v", f"{out}:/output",
                        f"{IMG}@{dig}", "simulate", "--config", "/input/scenario.json", "--out", "/output/trace.parquet"], capture_output=True, text=True)
        peak, stop = [0.0], threading.Event()
        def poll():
            while not stop.is_set():
                r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name], capture_output=True, text=True)
                m = mem_mib(r.stdout.split("/")[0]) if r.stdout else None
                if m: peak[0] = max(peak[0], m)
                time.sleep(1)
        th = threading.Thread(target=poll, daemon=True); th.start()
        rc = int(subprocess.run(["docker", "wait", name], capture_output=True, text=True).stdout.strip() or -1)
        stop.set(); th.join(timeout=5)
        ins = subprocess.run(["docker", "inspect", "-f", "{{.State.StartedAt}} {{.State.FinishedAt}} {{.State.OOMKilled}}", name], capture_output=True, text=True).stdout.split()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        win = (ts_ns(ins[1]) - ts_ns(ins[0])) / 1e9
        size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
        rows.append({"image": label, "unit": unit, "rc": rc, "window_s": round(win, 1), "peak_mem_mib": round(peak[0]), "output_mib": round(size / 2**20, 1), "oom": ins[2], "cap_300s_ok": win < 300 and rc == 0})
        print(rows[-1], flush=True)
Path("evidence").mkdir(exist_ok=True); Path("evidence/fallback_worst.json").write_text(json.dumps(rows, indent=1))
print("::notice title=T3-FALLBACK-WORST::" + " ; ".join(f"{r['image']} {r['unit'].replace('t3-','')}: rc={r['rc']} window={r['window_s']}s peak={r['peak_mem_mib']}MiB out={r['output_mib']}MiB cap_ok={r['cap_300s_ok']}" for r in rows))
