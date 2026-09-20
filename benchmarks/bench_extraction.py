"""Benchmark harness: three dataset sizes, two configs, peak RSS in a subprocess.

This is the script behind the numbers in the README and in the benchmark report. It is
committed because "reproducible measurement harness" is the claim being made -- a number
nobody else can re-run is not evidence.

Peak RSS is read with psutil inside a fresh interpreter. On Windows there is no
``resource.getrusage``, and measuring in-process would attribute the profiler's own
allocations to the run.

Usage::

    python benchmarks/bench_extraction.py                     # all six runs
    python benchmarks/bench_extraction.py --datasets b100m.xml # one dataset, both configs

The datasets are generated, not committed::

    gigaxml generate --size 100MB -o data/b100m.xml
    gigaxml generate --size 1GB   -o data/b1g.xml
    gigaxml generate --size 4GB   -o data/b4g.xml
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DATA = REPO / "data"
GIGAXML = REPO / ".venv/Scripts/gigaxml.exe"

#: The benchmark config. Covers an attribute, a leaf, a nested path and a type
#: conversion -- four of the shapes a real config uses.
SIX_FIELDS = {
    "record": "/catalog/products/product",
    "fields": {
        "id": {"path": "@id"},
        "type": {"path": "@type"},
        "name": {"path": "name"},
        "category": {"path": "category"},
        "price": {"path": "price", "type": "float"},
        "manufacturer": {"path": "manufacturer/name"},
    },
}

#: The control. One attribute, the cheapest possible config. Reported alongside the
#: benchmark, never instead of it: a single-field config is the easiest member of this
#: family and therefore the most flattering.
ONE_FIELD = {
    "record": "/catalog/products/product",
    "fields": {"id": {"path": "@id"}},
}

DEFAULT_DATASETS = ["b100m.xml", "b1g.xml", "b4g.xml"]

CHILD = """
import json, pathlib, sys, time
import psutil
from gigaxml.config import parse_config
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import consume_records
from gigaxml.writers import create_writer

source, config_path, output, label = sys.argv[1:5]
config = parse_config(json.loads(pathlib.Path(config_path).read_text(encoding="utf-8")))
process = psutil.Process()

# PeakWorkingSetSize is a maximum over the process's whole life, which is the quantity
# being claimed. Reading the current RSS after the work instead would report a number that
# is 10-14% lower and, more importantly, is not a peak at all -- the allocator may well
# have given pages back. psutil exposes it on Windows; elsewhere there is no portable
# equivalent, so it degrades to the current RSS, which is what tests/_mem.py does too.
def _peak_rss_mb() -> float:
    info = process.memory_info()
    peak = getattr(info, "peak_wset", None)
    return (peak if peak is not None else info.rss) / (1 << 20)


def rss():
    return process.memory_info().rss / (1 << 20)

# The baseline is taken *after* every import, so the measurement is about the work and
# not about what pyarrow costs to load. Two deltas taken against different baselines are
# not the same quantity -- see the note in benchmarks/README.md.
baseline = rss()
started = time.perf_counter()
writer = create_writer(output, config.fields)
with writer:
    reader = StreamingRecordReader(source, config.record_path)
    stats = consume_records(reader, config, writer)
elapsed = time.perf_counter() - started
peak = _peak_rss_mb()
print(json.dumps({
    "label": label,
    "baseline_mb": round(baseline, 3),
    "peak_mb": round(peak, 3),
    "delta_mb": round(max(0.0, peak - baseline), 3),
    "records": stats.records_processed,
    "rows": writer.rows_written,
    "seconds": round(elapsed, 3),
    "input_mib": round(pathlib.Path(source).stat().st_size / (1 << 20), 2),
    "output_mib": round(pathlib.Path(output).stat().st_size / (1 << 20), 3),
}))
"""


def run_one(source: pathlib.Path, config: dict, label: str, work: pathlib.Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    script = work / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    output = work / "out.csv"

    completed = subprocess.run(
        [sys.executable, str(script), str(source), str(config_path), str(output), label],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if completed.returncode != 0:
        return {"label": label, "error": completed.stderr.strip()[-300:]}
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    result["throughput_mib_s"] = round(result["input_mib"] / result["seconds"], 1)
    result["rows_per_sec"] = round(result["rows"] / result["seconds"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--datasets",
        nargs="+",
        default=DEFAULT_DATASETS,
        help="dataset filenames under data/ (default: all three)",
    )
    parser.add_argument(
        "--work",
        default=None,
        help="where to write scratch configs and outputs (default: a temp directory)",
    )
    args = parser.parse_args()

    work_root = pathlib.Path(args.work) if args.work else pathlib.Path(tempfile.mkdtemp())
    results = []
    for name in args.datasets:
        source = DATA / name
        if not source.is_file():
            print(f"missing: {source} -- generate it with `gigaxml generate`", flush=True)
            continue
        size_label = name.split(".")[0]
        for config_label, config in (("6 fields", SIX_FIELDS), ("1 field", ONE_FIELD)):
            tag = f"{size_label} / {config_label}"
            print(f"running {tag} ...", flush=True)
            result = run_one(source, config, tag, work_root / f"{size_label}-{config_label[:1]}")
            results.append(result)
            print(json.dumps(result), flush=True)

    print()
    header = (
        f"{'dataset':<10} {'fields':<9} {'input MiB':>10} {'records':>12} "
        f"{'s':>8} {'MiB/s':>7} {'peak':>8} {'delta':>8}"
    )
    print(header)
    print("-" * len(header))
    for row in results:
        if "error" in row:
            print(f"{row['label']:<30} ERROR {row['error'][:60]}")
            continue
        dataset, fields = row["label"].split(" / ")
        print(
            f"{dataset:<10} {fields:<9} {row['input_mib']:>10.2f} {row['records']:>12,} "
            f"{row['seconds']:>8.2f} {row['throughput_mib_s']:>7.1f} "
            f"{row['peak_mb']:>8.3f} {row['delta_mb']:>8.3f}"
        )

    out = work_root / "results.json"
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print()
    print("raw results:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
