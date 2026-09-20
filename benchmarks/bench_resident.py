"""Where does the writer's resident memory come from, and what does ``--batch-size`` cost?

Two questions left open at the end of an earlier phase, answered here:

* **The resident footprint.** A run holds tens of megabytes before it has touched any
  data. This walks the import chain step by step so the answer is a measurement rather
  than a guess.
* **``--batch-size`` sensitivity.** It is documented as a memory knob. This checks
  whether it is also a throughput knob, and where the knee is.

Usage::

    python benchmarks/bench_resident.py
    python benchmarks/bench_resident.py --dataset data/b1g.xml
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

DECOMPOSE = """
import json, pathlib, sys
import psutil

process = psutil.Process()
def rss():
    return process.memory_info().rss / (1 << 20)

steps = {"interpreter": round(rss(), 3)}
import yaml
steps["after_yaml"] = round(rss(), 3)
import lxml.etree
steps["after_lxml"] = round(rss(), 3)
from gigaxml.config import parse_config
config = parse_config(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")))
steps["after_gigaxml_config"] = round(rss(), 3)
import pyarrow
steps["after_pyarrow"] = round(rss(), 3)
import pyarrow.parquet
steps["after_pyarrow_parquet"] = round(rss(), 3)
from gigaxml.writers import create_writer
from gigaxml.run import consume_records
from gigaxml.parser.streaming import StreamingRecordReader
steps["after_gigaxml_rest"] = round(rss(), 3)

source, output, batch_size = sys.argv[2], sys.argv[3], int(sys.argv[4])
writer = create_writer(output, config.fields, batch_size=batch_size)
with writer:
    steps["after_writer_open"] = round(rss(), 3)
    reader = StreamingRecordReader(source, config.record_path)
    stats = consume_records(reader, config, writer)
    steps["after_consume"] = round(rss(), 3)
steps["after_close"] = round(rss(), 3)

print(json.dumps({
    "steps": steps,
    "delta_from_interpreter": {k: round(v - steps["interpreter"], 3) for k, v in steps.items()},
    "records": stats.records_processed,
}))
"""

BATCH = """
import json, pathlib, sys, time
import psutil
from gigaxml.config import parse_config
from gigaxml.writers import create_writer
from gigaxml.run import consume_records
from gigaxml.parser.streaming import StreamingRecordReader

process = psutil.Process()
def rss():
    return process.memory_info().rss / (1 << 20)

config = parse_config(json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")))
source, output, batch_size = sys.argv[2], sys.argv[3], int(sys.argv[4])
baseline = rss()
started = time.perf_counter()
writer = create_writer(output, config.fields, batch_size=batch_size)
with writer:
    reader = StreamingRecordReader(source, config.record_path)
    stats = consume_records(reader, config, writer)
elapsed = time.perf_counter() - started
peak = rss()
size = pathlib.Path(source).stat().st_size / (1 << 20)
print(json.dumps({
    "batch_size": batch_size,
    "baseline_mb": round(baseline, 3),
    "peak_mb": round(peak, 3),
    "delta_mb": round(max(0.0, peak - baseline), 3),
    "seconds": round(elapsed, 3),
    "records": stats.records_processed,
    "throughput_mib_s": round(size / elapsed, 1),
    "output_mib": round(pathlib.Path(output).stat().st_size / (1 << 20), 3),
}))
"""

DEFAULT_BATCHES = [500, 1000, 5000, 20000, 100000]


def run_child(script_body: str, work: pathlib.Path, args: list[str]) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    script = work / "child.py"
    script.write_text(script_body, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if completed.returncode != 0:
        return {"error": completed.stderr.strip()[-300:]}
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DATA / "b100m.xml"))
    parser.add_argument("--work", default=None)
    args = parser.parse_args()

    source = pathlib.Path(args.dataset)
    if not source.is_file():
        print(f"missing: {source} -- generate it with `gigaxml generate`")
        return 1
    work_root = pathlib.Path(args.work) if args.work else pathlib.Path(tempfile.mkdtemp())
    config_path = work_root / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(SIX_FIELDS), encoding="utf-8")

    print(f"=== resident footprint: {source.name}, 6 fields, parquet ===")
    decomposition = run_child(
        DECOMPOSE,
        work_root / "decompose",
        [str(config_path), str(source), str(work_root / "decompose" / "out.parquet"), "5000"],
    )
    if "error" in decomposition:
        print("ERROR", decomposition["error"])
        return 1
    deltas = decomposition["delta_from_interpreter"]
    for step, value in decomposition["steps"].items():
        print(f"  {step:<26} {value:>8.3f} MiB   (+{deltas[step]:>6.3f})")

    print()
    print(f"=== batch_size sensitivity: {source.name}, 6 fields ===")
    sensitivity = []
    for batch_size in DEFAULT_BATCHES:
        result = run_child(
            BATCH,
            work_root / f"batch-{batch_size}",
            [
                str(config_path),
                str(source),
                str(work_root / f"batch-{batch_size}" / "out.parquet"),
                str(batch_size),
            ],
        )
        sensitivity.append(result)
        print(json.dumps(result))

    header = f"{'batch':>8} {'delta MiB':>10} {'peak MiB':>9} {'seconds':>8} {'MiB/s':>7}"
    print()
    print(header)
    print("-" * len(header))
    for row in sensitivity:
        if "error" in row:
            print(f"{row.get('batch_size', '?'):>8}  ERROR {row['error'][:60]}")
            continue
        print(
            f"{row['batch_size']:>8} {row['delta_mb']:>10.3f} {row['peak_mb']:>9.3f} "
            f"{row['seconds']:>8.2f} {row['throughput_mib_s']:>7.1f}"
        )

    out = work_root / "results.json"
    out.write_text(
        json.dumps({"decomposition": decomposition, "sensitivity": sensitivity}, indent=2),
        encoding="utf-8",
    )
    print()
    print("raw results:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
