#!/usr/bin/env python3
"""Reader-compatibility check for candidate parquet (own-writer images): the files must read identically through
  (a) the parquet FOOTER (num_rows == sum of row-group rows == reference rows),
  (b) pandas.read_parquet (pyarrow engine) -> DataFrame.equals + identical dtypes (incl. nullable Int64) vs the reference  [what the scorer does],
  (c) polars (independent Rust reader) -> frame equality + schema equality vs the polars read of the reference,
  (d) fastparquet (independent pure-Python reader) -> values equal to the reference read by the same reader.
Run with the scorer venv:  ~/.venvs/t3-scorer313/bin/python -I tools/indep_read.py <ref_dir> <out_dir>   (dirs of single outputs, or batch dirs with sub dirs)
or --pairs pairs.json  ([{"unit":..., "ref":..., "out":...}, ...] -> prints JSON).  Exit 0 iff every check passes."""
import json, sys
from pathlib import Path

import pandas as pd
import polars as pl
import pyarrow.parquet as pq

FILES = ("trace.parquet", "message_trace.parquet")


def files(d):
    d = Path(d)
    if any((d / f).is_file() for f in FILES):
        return [("", d)]
    return [(p.name, p) for p in sorted(d.iterdir()) if p.is_dir() and any((p / f).is_file() for f in FILES)]


EXPECT_CODEC = None        # e.g. "SNAPPY": every column chunk of every row group must carry exactly this compression codec in the footer
INFO_FASTPARQUET = False   # own-writer images (DELTA_BINARY_PACKED pages): fastparquet's delta decoder is known to misread some pages -> informational


def check_file(ref, out):
    bad = []
    pf_r, pf_o = pq.ParquetFile(str(ref)), pq.ParquetFile(str(out))
    n_o = pf_o.metadata.num_rows
    if n_o != sum(pf_o.metadata.row_group(i).num_rows for i in range(pf_o.metadata.num_row_groups)):
        bad.append("footer num_rows != sum(row groups)")
    if n_o != pf_r.metadata.num_rows:
        bad.append(f"footer num_rows {n_o} != ref {pf_r.metadata.num_rows}")
    codecs = {pf_o.metadata.row_group(i).column(j).compression for i in range(pf_o.metadata.num_row_groups) for j in range(pf_o.metadata.num_columns)}
    if EXPECT_CODEC and codecs != {EXPECT_CODEC}:
        bad.append(f"codec {sorted(codecs)} != expected {EXPECT_CODEC}")
    elif codecs:
        bad.append("info: codecs " + ",".join(sorted(codecs)))
    a, b = pd.read_parquet(out), pd.read_parquet(ref)
    if list(a.dtypes.astype(str)) != list(b.dtypes.astype(str)) or list(a.columns) != list(b.columns):
        bad.append(f"pandas dtypes differ: {dict(zip(a.columns, a.dtypes.astype(str)))} vs {dict(zip(b.columns, b.dtypes.astype(str)))}")
    elif not a.equals(b):
        bad.append("pandas DataFrame.equals False")
    pa_, pb_ = pl.read_parquet(out), pl.read_parquet(ref)
    if pa_.schema != pb_.schema:
        bad.append(f"polars schema differs: {pa_.schema} vs {pb_.schema}")
    elif not pa_.equals(pb_, null_equal=True):
        bad.append("polars frame differs")
    try:
        import fastparquet
        fa, fb = fastparquet.ParquetFile(str(out)).to_pandas(), fastparquet.ParquetFile(str(ref)).to_pandas()
        if list(fa.columns) != list(fb.columns) or len(fa) != len(fb) or not fa.equals(fb):
            bad.append(("info: " if INFO_FASTPARQUET else "") + "fastparquet read differs")
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        bad.append(("info: " if INFO_FASTPARQUET else "") + f"fastparquet error {type(e).__name__}")
    return bad


def split(res):
    """-> (failures, info): entries starting with 'info: ' are informational"""
    fail, info = {}, {}
    for k, v in res.items():
        f = [x for x in v if not x.startswith("info: ")]
        i = [x for x in v if x.startswith("info: ")]
        if f:
            fail[k] = f
        if i:
            info[k] = i
    return fail, info


def check_dirs(ref, out):
    res = {}
    ra, oa = dict(files(ref)), dict(files(out))
    if set(ra) != set(oa):
        return {"": [f"sub-dir sets differ {sorted(ra)} vs {sorted(oa)}"]}
    for sub in ra:
        for f in FILES:
            rp, op = ra[sub] / f, oa[sub] / f
            if not rp.is_file() or not op.is_file():
                res[f"{sub}/{f}"] = ["missing"]
                continue
            try:
                bad = check_file(rp, op)
            except Exception as e:  # noqa: BLE001
                bad = [f"reader error {type(e).__name__}: {e}"]
            if bad:
                res[f"{sub}/{f}"] = bad
    return res


def main():
    global INFO_FASTPARQUET, EXPECT_CODEC
    args = [x for x in sys.argv[1:] if x != "--fastparquet-informational"]
    INFO_FASTPARQUET = len(args) != len(sys.argv) - 1
    if "--expect-codec" in args:
        i = args.index("--expect-codec")
        EXPECT_CODEC = args[i + 1]
        del args[i:i + 2]
    if len(args) > 1 and args[0] == "--pairs":
        pairs = json.loads(Path(args[1]).read_text())
        out = {}
        for p in pairs:
            fail, info = split(check_dirs(p["ref"], p["out"]))
            out[p["unit"]] = {"fail": fail, "info": info}
        print(json.dumps(out))
        return 0 if not any(v["fail"] for v in out.values()) else 1
    fail, info = split(check_dirs(args[0], args[1]))
    for k, v in fail.items():
        print("FAIL", k, v)
    for k, v in info.items():
        print("INFO", k, v)
    print("RESULT:", "OK (footer rows, pandas dtypes+values, polars" + ("" if INFO_FASTPARQUET else ", fastparquet") + ")" if not fail else "DIFFERENT")
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
