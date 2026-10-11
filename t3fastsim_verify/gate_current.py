#!/usr/bin/env python3
"""Run the CURRENT official developer verifier (qfbench2-track-simulation + qfbench2-common v2.5.1) over output dirs.

Unlike t3/scripts/gate_eval.py this does NOT prepend Agenthon-2026/00_shared_toolkit/common to sys.path (that tree is the
stale toolkit v2.3.1 and lacks digest_members -> ImportError, or worse silently shadows the pinned install). Run it with
the scorer venv, isolated so cwd / PYTHONPATH can never shadow the installed packages:

  ~/.venvs/t3-scorer313/bin/python -I t3/fastsim/tools/gate_current.py --out-root t3/fastsim/refs --units all \
        --save-dir t3/fastsim/scorer_runs/refs_baseline

--out-root  directory holding <unit>/ outputs (single: trace.parquet+message_trace.parquet+events.json;
            batch: sub_XX/...+batch_events.json), e.g. fastsim/refs or fastsim/cand_runs/<tag>
--unit-root unit dirs with the references (default t3/evaluation_references; the units/ copy holds LFS stubs here)
Exit 0 iff every unit is admissible.
"""
import argparse, json, sys, time
from dataclasses import asdict
from pathlib import Path

T3 = Path(__file__).resolve().parents[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-root", required=True)
    ap.add_argument("--unit-root", default=str(T3 / "evaluation_references"))
    ap.add_argument("--units", nargs="+", default=["all"])
    ap.add_argument("--save-dir", required=True)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    import qfbench2_common
    import qfbench2_track_simulation
    from importlib.metadata import version
    from qfbench2_track_simulation.scoring import build_developer_verifier
    print(f"qfbench2-common {version('qfbench2-common')} ({Path(qfbench2_common.__file__).parent}) | "
          f"track pkg {version('qfbench2-track-simulation')} ({Path(qfbench2_track_simulation.__file__).parent})")

    out_root, unit_root, save = Path(a.out_root), Path(a.unit_root), Path(a.save_dir)
    save.mkdir(parents=True, exist_ok=True)
    units = a.units
    if units == ["all"]:
        units = sorted(p.name for p in out_root.iterdir()
                       if p.is_dir() and not p.name.startswith(("._", "t3-EXAMPLE")) and (unit_root / p.name).is_dir())
    bad = 0
    for u in units:
        ctx = {"unit_dir": str(unit_root / u), "output_dir": str(out_root / u)}
        t0 = time.perf_counter()
        try:
            verdict = build_developer_verifier(ctx).run(ctx)
            res = asdict(verdict)
        except Exception as e:  # noqa: BLE001 -- a crash is a failure, not a pass
            res = {"admissible": False, "error": f"{type(e).__name__}: {e}"}
        res["validation_scope"] = "official developer verifier (current pinned toolkit); no C1/C2/C7 / container enforcement"
        (save / f"{u}.gate.json").write_text(json.dumps(res, indent=2, default=str) + "\n")
        admitted = res.get("admissible")
        keys = [k for k in res if k in ("admissible", "labels", "error", "gate_results")]
        if admitted is not True:
            bad += 1
        print(f"{u:42s} {'ADMIT' if admitted is True else 'FAIL '} {time.perf_counter() - t0:6.1f}s "
              + (f"score={res.get('score')} sf={(res.get('detail') or {}).get('stylized_facts')}" if admitted is True else json.dumps({k: res[k] for k in keys}, default=str)[:600]),
              flush=True)
    print(f"gates: {len(units) - bad}/{len(units)} admitted")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
