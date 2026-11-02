"""Benchmark: a full run over generated files, timed per step.

    python bench.py FILES MONTHS DIR [--append N] [--mochi PATH]

Creates FILES small log files (about 2 KiB each) spread over five subfolders and
MONTHS months under DIR, runs one backup, and prints how long each step took.
With --append N it then adds N more files and runs again (the daily case).
"""
import argparse
import json
import os
import shutil
import time
from pathlib import Path

import logs_backup as lb

SUBFOLDERS = ["BURNIN", "ESS", "JTAG", "STM", "Xray"]


def make_files(src, first, count, months):
    for i in range(first, first + count):
        sub = SUBFOLDERS[i % len(SUBFOLDERS)]
        line = f"unit {i} test log line, value={i * 7919 % 100003}\n".encode()
        (src / sub / f"unit{i}_BC{i:07d}_log.txt").write_bytes(line * (2048 // len(line)))
    return [(f"BC{i:07d}", f"2026-{1 + i % months:02d}-15 08:00:00") for i in range(first + count)]


def timed_run(cfg, candidates):
    marks, last = {}, [None, time.perf_counter()]

    def report(step, *_):
        now = time.perf_counter()
        if last[0]:
            marks[last[0]] = marks.get(last[0], 0) + now - last[1]
        last[:] = [step, now]

    start = time.perf_counter()
    summary = lb.run(cfg, lambda *_: candidates, report=report)
    report("end")
    total = time.perf_counter() - start
    steps = ", ".join(f"{s} {t:.1f}s" for s, t in marks.items())
    print(f"  {summary['files_moved']:,} files in {total:.1f}s ({summary['files_moved'] / total:,.0f} files/s): {steps}")
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("files", type=int)
    parser.add_argument("months", type=int)
    parser.add_argument("dir", type=Path)
    parser.add_argument("--append", type=int, default=0)
    parser.add_argument("--mochi", default=os.environ.get("MOCHI_BIN", "mochi"))
    args = parser.parse_args()

    if args.dir.exists():
        shutil.rmtree(args.dir)
    src = args.dir / "src"
    for sub in SUBFOLDERS:
        (src / sub).mkdir(parents=True)
    (args.dir / "config.json").write_text(json.dumps({
        "source_dir": str(src), "output_dir": str(args.dir / "out"), "db_path": str(args.dir / "index.db"),
        "subfolders": SUBFOLDERS, "date_range": ["2026-01-01", "2027-01-01"], "mochi": args.mochi}))
    cfg = lb.Config(args.dir / "config.json")

    print(f"run: {args.files:,} files, {args.months} month(s)")
    timed_run(cfg, make_files(src, 0, args.files, args.months))
    if args.append:
        print(f"append: {args.append:,} more files")
        timed_run(cfg, make_files(src, args.files, args.append, args.months))
    left = sum(len(os.listdir(src / s)) for s in SUBFOLDERS)
    print(f"  files left in source: {left}, staging exists: {cfg.staging_dir.exists()}")


if __name__ == "__main__":
    main()
