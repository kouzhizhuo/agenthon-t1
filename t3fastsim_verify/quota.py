#!/usr/bin/env python3
"""Quota proxy on the real runner: two images (v10m, v11m), `docker run --cpus=Q` for Q in 2,3,4 (4 = control), all units (single + batch), interleaved
(rep-major, images alternating), reps with the first discarded; FASTSIM_IOSTATS=1 -> user+sys and cgroup cpu.stat (nr_periods, nr_throttled, throttled_usec).
Outputs must be byte-identical across images and quotas.
  quota.py --image ghcr.io/x/y --track <checkout> --out evidence/quota.json --digest label=sha256:... --digest ... [--quotas 2 3 4] [--reps 3]"""
import argparse, json, re, statistics as st, subprocess, sys, tempfile, uuid
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import verify as V  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--image", required=True); ap.add_argument("--track", required=True); ap.add_argument("--out", required=True)
ap.add_argument("--digest", action="append", required=True); ap.add_argument("--quotas", nargs="*", type=float, default=[2, 3, 4])
ap.add_argument("--reps", type=int, default=3); ap.add_argument("--units", nargs="*")
a = ap.parse_args()
units_dir = Path(a.track).resolve() / "units"
work = Path(tempfile.mkdtemp(prefix="t3q_")); work.chmod(0o755)
images = {}
for spec in a.digest:
    lab, dig = spec.split("=", 1); images[lab] = f"{a.image}:{dig[4:]}" if dig.startswith("tag:") else f"{a.image}@{dig}"
    subprocess.run(["docker", "pull", "--platform", "linux/amd64", images[lab]], capture_output=True)
names = a.units or sorted(p.name for p in units_dir.iterdir() if p.is_dir() and ((p / "scenario.json").is_file() or (p / "scenarios").is_dir()))
staged = {}
for u in names:
    staged[u] = V.stage(units_dir / u, work / "in" / u) + (work / "in" / u,)
seeds = {}
for u in names:
    f = units_dir / u / "scenario.json"
    seeds[u] = json.loads(f.read_text()).get("seed", 1) if f.is_file() else 1
RX = {"user": r"user_s=([0-9.]+)", "sys": r"sys_s=([0-9.]+)", "nr_periods": r"nr_periods (\d+)", "nr_throttled": r"nr_throttled (\d+)", "throttled_usec": r"throttled_usec (\d+)"}


def run(img, u, q, tag):
    args, batch, in_dir = staged[u]
    out = work / "out" / tag; out.mkdir(parents=True); out.chmod(0o777)
    name = "t3q_" + uuid.uuid4().hex[:10]
    flags = [("--cpus=%s" % ("%g" % q) if f.startswith("--cpus=") else f) for f in V.FLAGS]
    cmd = ["docker", "run", "--name", name, "--platform", "linux/amd64"] + flags + ["-e", f"QFBENCH_SEED={seeds[u]}", "-e", "QFBENCH_NETWORK=none", "-e", "FASTSIM_IOSTATS=1",
           "-v", f"{in_dir}:/input:ro", "-v", f"{out}:/output", img] + args
    r = V.sh(cmd)
    ins = V.sh(["docker", "inspect", "-f", "{{.State.StartedAt}} {{.State.FinishedAt}} {{.State.ExitCode}}", name])
    V.sh(["docker", "rm", "-f", name])
    s, f, rc = ins.stdout.split()
    row = {"window_s": (V.ts_ns(f) - V.ts_ns(s)) / 1e9, "rc": int(rc)}
    for k, rx in RX.items():
        m = re.findall(rx, r.stderr)
        row[k] = float(m[-1]) if m else None
    n = 0
    try:
        j = json.loads((out / ("batch_events.json" if batch else "events.json")).read_text())
        n = j.get("total_events") if batch else j.get("n_events")
    except Exception:
        pass
    row["n_events"] = n
    row["hashes"] = {str(p.relative_to(out)): V.sha(p) for p in sorted(out.rglob("*.parquet"))}
    subprocess.run(["rm", "-rf", str(out)])
    return row


res = {}
for q in a.quotas:
    for u in names:
        for rep in range(a.reps):
            for lab, img in images.items():
                res.setdefault((q, u, lab), []).append(run(img, u, q, uuid.uuid4().hex[:8]))
rep = {"quotas": {}}
ident = {}
for (q, u, lab), rows in res.items():
    for r in rows:
        ident.setdefault(u, set()).add(json.dumps(r["hashes"], sort_keys=True))
rep["byte_identical_across_images_quotas_reps"] = {u: len(v) == 1 for u, v in ident.items()}
labs = list(images)
for q in a.quotas:
    qr = rep["quotas"]["%g" % q] = {}
    for lab in labs:
        eps, tot, thr, thrus, per, bw, cpu = [], 0.0, 0, 0, 0, {}, 0.0
        for u in names:
            rows = res[(q, u, lab)][1:] or res[(q, u, lab)]
            w = st.median(r["window_s"] for r in rows)
            n = rows[0]["n_events"]
            if n: eps.append(n / w)
            tot += w
            thr += sum(int(r["nr_throttled"] or 0) for r in rows); thrus += sum(int(r["throttled_usec"] or 0) for r in rows); per += sum(int(r["nr_periods"] or 0) for r in rows)
            cpu += st.median((r["user"] or 0) + (r["sys"] or 0) for r in rows)
            if staged[u][1]: bw[u] = round(w, 4)
        qr[lab] = {"roster_mean_eps": sum(eps) / max(1, len(eps)), "sum_window_s": round(tot, 3), "nr_throttled": thr, "throttled_usec": thrus, "nr_periods": per,
                   "sum_cpu_s_median": round(cpu, 3), "batch_windows": bw, "failed_runs": sum(1 for u in names for r in res[(q, u, lab)] if r["rc"])}
    if len(labs) == 2:
        qr["ratio_%s_over_%s" % (labs[1], labs[0])] = qr[labs[1]]["roster_mean_eps"] / qr[labs[0]]["roster_mean_eps"]
Path(a.out).write_text(json.dumps(rep, indent=1))
nonid = [u for u, ok in rep["byte_identical_across_images_quotas_reps"].items() if not ok]
print("::notice title=T3V-QUOTA-IDENT::byte-identical across images x quotas x reps: %d/%d units; non-identical=%s" % (len(names) - len(nonid), len(names), nonid))
for q, qr in rep["quotas"].items():
    msg = "cpus=%s: " % q + " ; ".join("%s mean_eps=%.0f sum_win=%.3fs cpu_s=%.3f throttled=%d/%d periods throttled_usec=%d failed=%d batch=%s" % (
        lab, qr[lab]["roster_mean_eps"], qr[lab]["sum_window_s"], qr[lab]["sum_cpu_s_median"], qr[lab]["nr_throttled"], qr[lab]["nr_periods"], qr[lab]["throttled_usec"], qr[lab]["failed_runs"], qr[lab]["batch_windows"]) for lab in labs)
    ratio = [v for k, v in qr.items() if k.startswith("ratio_")]
    print("::notice title=T3V-QUOTA-%s::%s ; ratio=%s" % (q, msg.replace("%", "%25"), ratio))
