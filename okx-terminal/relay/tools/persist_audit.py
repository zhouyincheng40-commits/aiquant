#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Persist audit: line-count reconciliation for the jsonl files (rows, not bytes).

The naive "expected rows over the file span" is misleading, because the file span also
contains periods when NO relay process was running. This audit therefore works on
CONTIGUOUS RUNS of second/minute slots:

  run                  : maximal sequence of consecutive slots (gap of 1 slot)
  inside_run_missing   : slots missing INSIDE a run  -> REAL data loss (flush/upstream)
  downtime gap (>60 s) : process was not running      -> NOT data loss
  small gap (2..60 s)  : suspicious -> reported separately for inspection
  tail_before_crash    : last row strictly before --crash-at, and its lag in seconds
                         (answers "did the last 5 s of buffer get lost?")

Run: python tools/persist_audit.py --crash-at "2026-10-11 20:02:03"
"""
import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CN = timezone(timedelta(hours=8))
DOWNTIME_GAP = 60      # slots; larger than this = the process simply was not running
SMALL_GAP = 60


def load(path: Path):
    bad = 0
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                bad += 1
    return rows, bad


def to_cn(sec: int) -> str:
    return datetime.fromtimestamp(sec, CN).strftime("%Y-%m-%d %H:%M:%S")


def runs_of(slots, step):
    """Maximal consecutive runs over integer slots (step = slots per unit)."""
    runs = []
    start = prev = slots[0]
    for s in slots[1:]:
        if s - prev == step:
            prev = s
            continue
        runs.append((start, prev))
        start = prev = s
    runs.append((start, prev))
    return runs


def audit(path: Path, crash_sec):
    name = path.name
    rows, bad = load(path)
    if not rows:
        return {"file": name, "rows": 0, "bad_lines": bad}
    kind = name.split("_")[0]
    inst = name.split("_", 1)[1].rsplit("_", 1)[0]
    unit_sec = 60 if kind == "candle" else 1
    if kind == "trades":
        slots = sorted({int(r["ts"]) // 1000 for r in rows})
        unit_sec = 0
    else:
        slots = sorted({(int(r["ts"]) // 1000) if kind == "candle" else int(r["sec"]) for r in rows})

    out = {"file": name, "inst": inst, "kind": kind, "rows": len(rows), "bad_lines": bad,
           "first_cn": to_cn(slots[0]), "last_cn": to_cn(slots[-1])}

    if kind == "trades":
        span = slots[-1] - slots[0]
        out.update({"slots": len(slots), "span_h": round(span / 3600, 2),
                    "rate_per_min": round(len(rows) / max(0.01, span / 60), 2)})
    else:
        rs = runs_of(slots, unit_sec)
        inside_missing = 0
        for a, b in rs:
            inside_missing += (b - a) // unit_sec + 1 - len([s for s in slots if a <= s <= b])
        gaps = []
        for i in range(1, len(rs)):
            g = (rs[i][0] - rs[i - 1][1]) // unit_sec - 1
            gaps.append((g, rs[i - 1][1] + unit_sec, rs[i][0] - unit_sec))
        down = [g for g in gaps if g[0] >= DOWNTIME_GAP]
        small = [g for g in gaps if 0 < g[0] < DOWNTIME_GAP]
        covered = sum((b - a) // unit_sec + 1 for a, b in rs)
        out.update({
            "slots": len(slots), "runs": len(rs),
            "longest_run_min": round(max((b - a) // unit_sec + 1 for a, b in rs) * unit_sec / 60, 1),
            "covered_slots": covered, "inside_run_missing": inside_missing,
            "inside_loss_pct": round(100.0 * inside_missing / covered, 3) if covered else 0,
            "downtime_gaps": len(down), "downtime_missing": sum(g[0] for g in down),
            "small_gaps": len(small), "small_gap_missing": sum(g[0] for g in small),
            "gap_windows": [("%s -> %s" % (to_cn(a), to_cn(b)), g) for g, a, b in down][:8],
        })
    if crash_sec:
        before = [s for s in slots if s < crash_sec]
        if before:
            out["tail_before_crash"] = to_cn(before[-1])
            out["tail_lag_s"] = crash_sec - before[-1]
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=r"D:\量化\okx-terminal\data")
    p.add_argument("--crash-at", default=None, help='local CN time, e.g. "2026-10-11 20:02:03"')
    a = p.parse_args()
    crash_sec = None
    if a.crash_at:
        crash_sec = int(datetime.strptime(a.crash_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CN).timestamp())
    d = Path(a.dir)
    files = sorted(d.glob("*.jsonl"))
    print("== persist audit (row based) : %s ==" % d)
    if crash_sec:
        print("crash reference (CN): %s" % a.crash_at)
    print()
    for f in files:
        r = audit(f, crash_sec)
        print("### %s" % r["file"])
        if r["kind"] == "trades":
            print("    rows=%(rows)d  distinct_ts=%(slots)d  span_h=%(span_h)s  rate=%(rate_per_min)s/min" % r)
        else:
            print("    rows=%(rows)d slots=%(slots)d runs=%(runs)d longest_run=%(longest_run_min)s min" % r)
            print("    covered_slots=%(covered_slots)d  inside_run_missing=%(inside_run_missing)d "
                  "(%(inside_loss_pct)s%%)   <-- REAL loss" % r)
            print("    downtime_gaps=%(downtime_gaps)d (%(downtime_missing)d slots)   "
                  "small_gaps=%(small_gaps)d (%(small_gap_missing)d slots)" % r)
            for w, g in r.get("gap_windows", []):
                print("      downtime: %s  (%d s)" % (w, g))
        if r.get("tail_lag_s") is not None:
            print("    last row before crash: %s   lag=%ss" % (r["tail_before_crash"], r["tail_lag_s"]))
        if r["bad_lines"]:
            print("    UNPARSEABLE LINES: %d" % r["bad_lines"])
        print()
    print("== totals ==")
    tot = sum(r["rows"] for r in (audit(f, crash_sec) for f in files))
    print("total rows across %d files: %d" % (len(files), tot))


if __name__ == "__main__":
    main()
