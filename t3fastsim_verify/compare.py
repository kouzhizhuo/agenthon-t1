#!/usr/bin/env python3
"""Compare two simulate output dirs (trace.parquet + message_trace.parquet).

Checks equal values AND dtypes (schema: column names, order, arrow types; floats bit-exact, NaN==NaN, nulls aligned).
On mismatch prints the first differing row (index / seq) with both rows and CONTEXT rows each side, plus a summary.
If a dir has no trace.parquet but has sub-dirs that do (batch output), every sub-dir is compared.

  compare.py DIR_A DIR_B [--bytes] [--context 3] [--files trace message_trace] [--quiet]
Exit 0 = identical, 1 = any difference / missing file.
Importable: compare_dirs(a, b, ...) -> (ok: bool, report: str).
"""
import argparse, hashlib, io, os, sys
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

FILES = ("trace.parquet", "message_trace.parquet")
SEQ_COLS = ("seq", "order_id", "message_id")  # shown in the header when present


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def _diff_mask(a: pa.ChunkedArray, b: pa.ChunkedArray, n: int):
    """Boolean numpy mask of length n: True where a[i] != b[i] (null-aware, floats bit-exact)."""
    a = a.slice(0, n).combine_chunks()
    b = b.slice(0, n).combine_chunks()
    an, bn = a.is_null().to_numpy(zero_copy_only=False), b.is_null().to_numpy(zero_copy_only=False)
    mask = an != bn
    both = ~(an | bn)
    t = a.type
    if pa.types.is_floating(t):
        wa = a.fill_null(0).to_numpy(zero_copy_only=False)
        wb = b.fill_null(0).to_numpy(zero_copy_only=False)
        ia = np.ascontiguousarray(wa).view(f"u{wa.dtype.itemsize}")
        ib = np.ascontiguousarray(wb).view(f"u{wb.dtype.itemsize}")
        neq = ia != ib
    elif pa.types.is_integer(t) or pa.types.is_boolean(t) or pa.types.is_temporal(t):
        z = pa.scalar(False) if pa.types.is_boolean(t) else None
        neq = pc.not_equal(a.fill_null(z if z is not None else 0) if not pa.types.is_temporal(t)
                           else a.cast(pa.int64()).fill_null(0),
                           b.fill_null(z if z is not None else 0) if not pa.types.is_temporal(t)
                           else b.cast(pa.int64()).fill_null(0)).to_numpy(zero_copy_only=False)
    else:  # strings & anything else: pc.not_equal (null result where either null; masked by `both`)
        neq = pc.fill_null(pc.not_equal(a, b), False).to_numpy(zero_copy_only=False)
    return mask | (both & neq)


def _fmt_rows(t: pa.Table, lo, hi, mark):
    df = t.slice(lo, hi - lo).to_pandas()
    df.index = range(lo, hi)
    lines = df.to_string().split("\n")
    out = [lines[0]]
    for i, ln in zip(range(lo, hi), lines[1:]):
        out.append(("=> " if i == mark else "   ") + ln)
    return "\n".join(out)


def compare_tables(ta: pa.Table, tb: pa.Table, name, context=3):
    """Returns (ok, lines)."""
    L = []
    ok = True
    L.append(f"[{name}] rows A={ta.num_rows} B={tb.num_rows}")
    if ta.num_rows != tb.num_rows:
        ok = False
        L.append(f"[{name}] ROW COUNT DIFFERS (A-B = {ta.num_rows - tb.num_rows})")
    # dtype / schema
    sa = [(f.name, str(f.type)) for f in ta.schema]
    sb = [(f.name, str(f.type)) for f in tb.schema]
    if sa != sb:
        ok = False
        L.append(f"[{name}] SCHEMA DIFFERS")
        da, db = dict(sa), dict(sb)
        for c in sorted(set(da) | set(db)):
            if da.get(c) != db.get(c):
                L.append(f"    column {c!r}: A={da.get(c)} B={db.get(c)}")
        if [c for c, _ in sa] != [c for c, _ in sb] and set(da) == set(db):
            L.append(f"    column ORDER differs: A={[c for c, _ in sa]} B={[c for c, _ in sb]}")
    common = [c for c, _ in sa if c in dict(sb)]
    n = min(ta.num_rows, tb.num_rows)
    first = {}  # col -> first differing row
    counts = {}
    for c in common:
        ca, cb = ta.column(c), tb.column(c)
        if ca.type != cb.type:  # dtype diff already reported; compare through string repr of values
            try:
                cb = cb.cast(ca.type)
            except Exception:
                continue
        m = _diff_mask(ca, cb, n)
        k = int(m.sum())
        if k:
            counts[c] = k
            first[c] = int(np.argmax(m))
    if first:
        ok = False
        row = min(first.values())
        fc = sorted(first, key=lambda c: (first[c], c))[0]
        L.append(f"[{name}] VALUE MISMATCH: first differing row index {row}, first differing column {fc!r}")
        L.append(f"[{name}] differing rows per column (within common {n} rows): " +
                 ", ".join(f"{c}={counts[c]}" for c in common if c in counts))
        L.append(f"[{name}] columns differing at row {row}: " + ", ".join(c for c in common if first.get(c) == row))
        for key in SEQ_COLS:
            if key in ta.column_names:
                L.append(f"[{name}] A.{key}[{row}]={ta.column(key)[row].as_py()}  B.{key}[{row}]="
                         f"{tb.column(key)[row].as_py() if key in tb.column_names else '?'}")
                break
        lo, hi = max(0, row - context), row + context + 1
        L.append(f"--- A (row {row}) ---")
        L.append(_fmt_rows(ta, lo, min(hi, ta.num_rows), row))
        L.append(f"--- B (row {row}) ---")
        L.append(_fmt_rows(tb, lo, min(hi, tb.num_rows), row))
    elif ok is False and ta.num_rows != tb.num_rows:
        k = n  # first differing row is the first row past the shorter table
        L.append(f"[{name}] common prefix of {n} rows is identical; first extra row (index {k}) is in "
                 f"{'A' if ta.num_rows > tb.num_rows else 'B'}:")
        t = ta if ta.num_rows > tb.num_rows else tb
        L.append(_fmt_rows(t, max(0, k - context), min(k + context + 1, t.num_rows), k))
    if ok:
        L.append(f"[{name}] IDENTICAL (values + dtypes)")
    return ok, L


def compare_one(a: Path, b: Path, files=FILES, context=3, bytes_mode=False):
    ok, L = True, []
    for f in files:
        pa_, pb_ = a / f, b / f
        if not pa_.is_file() or not pb_.is_file():
            ok = False
            L.append(f"[{f}] MISSING: A={'ok' if pa_.is_file() else 'absent'} B={'ok' if pb_.is_file() else 'absent'}")
            continue
        if bytes_mode:
            ha, hb = sha256(pa_), sha256(pb_)
            L.append(f"[{f}] sha256 A={ha} B={hb} {'SAME' if ha == hb else 'DIFFERENT'}")
            ok &= ha == hb
            continue
        try:
            ta, tb = pq.read_table(str(pa_)), pq.read_table(str(pb_))
        except Exception as e:  # unreadable parquet
            ok = False
            L.append(f"[{f}] READ ERROR: {e}")
            continue
        o, ls = compare_tables(ta, tb, f, context)
        ok &= o
        L += ls
    return ok, L


def _units(d: Path, files):
    """[(label, dir)]: the dir itself if it holds output files, else its sub-dirs that do."""
    if any((d / f).is_file() for f in files):
        return [("", d)]
    subs = sorted(p for p in d.iterdir() if p.is_dir() and any((p / f).is_file() for f in files))
    return [(p.name, p) for p in subs] if subs else [("", d)]


def compare_dirs(a, b, files=FILES, context=3, bytes_mode=False):
    a, b = Path(a), Path(b)
    ua, ub = _units(a, files), _units(b, files)
    ok, L = True, []
    na, nb = [n for n, _ in ua], [n for n, _ in ub]
    if na != nb:
        ok = False
        L.append(f"SUB-UNIT SETS DIFFER: A={na} B={nb}")
    db = dict(ub)
    for n, d in ua:
        if n not in db:
            continue
        o, ls = compare_one(d, db[n], files, context, bytes_mode)
        ok &= o
        L += [(f"<{n}> " if n else "") + x if not x.startswith(("   ", "=>", "---", " ")) else x for x in ls]
    return ok, "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--bytes", action="store_true", help="sha256 of both files instead of value/dtype compare")
    ap.add_argument("--context", type=int, default=3)
    ap.add_argument("--files", nargs="*", default=["trace", "message_trace"])
    ap.add_argument("--quiet", action="store_true", help="only print on mismatch")
    args = ap.parse_args(argv)
    files = tuple(f if f.endswith(".parquet") else f + ".parquet" for f in args.files)
    ok, rep = compare_dirs(args.a, args.b, files, args.context, args.bytes)
    if not (ok and args.quiet):
        print(rep)
    print("RESULT:", "IDENTICAL" if ok else "DIFFERENT")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
