"""Costs of the output machinery: resident footprint, batch size, and the resume check.

Three things that are paid for but not obvious from the outside:

* **The resident footprint.** A run holds tens of megabytes before it has touched any
  data. This walks the import chain step by step so the answer is a measurement rather
  than a guess.
* **``--batch-size`` sensitivity.** It is documented as a memory knob. This checks
  whether it is also a throughput knob, and where the knee is.
* **The cost of ``verify_parts``.** A resume counts every part the manifest names before
  it trusts the manifest, which is one pass over the output. That is a real cost paid on
  every resume, and the README quotes it, so a script has to produce it.

Usage::

    python benchmarks/bench_resident.py
    python benchmarks/bench_resident.py --dataset data/b1g.xml
    python benchmarks/bench_resident.py --skip-sweep      # just the two cheaper sections
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
peak = _peak_rss_mb()
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


VERIFY = """
import json, pathlib, subprocess, sys, time
from gigaxml.checkpoint import Checkpoint, PartRecord, read_checkpoint, verify_parts

parts_dir = pathlib.Path(sys.argv[1])
manifest = read_checkpoint(parts_dir / "checkpoint.json")
extension = manifest.parts[-1].name.rsplit(".", 1)[-1] if manifest.parts else "csv"
total_mib = sum(
    (parts_dir / part.name).stat().st_size for part in manifest.parts
) / (1 << 20)

started = time.perf_counter()
problems = verify_parts(manifest, parts_dir, extension)
elapsed = time.perf_counter() - started
print(json.dumps({
    "parts": len(manifest.parts),
    "output_mib": round(total_mib, 3),
    "verify_ms": round(elapsed * 1000, 1),
    "problems": problems,
}))
"""


def run_verify_check(source: pathlib.Path, work: pathlib.Path) -> dict:
    """Build a checkpointed output, then time the check a resume would do."""
    work.mkdir(parents=True, exist_ok=True)
    config_path = work / "config.json"
    config_path.write_text(json.dumps(SIX_FIELDS), encoding="utf-8")
    parts = work / "parts"

    every = max(1000, 290_900 // 12)  # about twelve parts, whatever the dataset size
    built = subprocess.run(
        [
            str(GIGAXML),
            "extract",
            str(source),
            "-c",
            str(config_path),
            "-o",
            str(parts),
            "--checkpoint-every",
            str(every),
            "--format",
            "csv",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if built.returncode != 0:
        return {"error": built.stderr.strip()[-300:]}

    script = work / "verify.py"
    script.write_text(VERIFY, encoding="utf-8")
    measured = subprocess.run(
        [sys.executable, str(script), str(parts)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if measured.returncode != 0:
        return {"error": measured.stderr.strip()[-300:]}
    result = json.loads(measured.stdout.strip().splitlines()[-1])
    result["checkpoint_every"] = every
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(DATA / "b100m.xml"))
    parser.add_argument("--work", default=None)
    parser.add_argument(
        "--skip-sweep",
        action="store_true",
        help="skip the batch-size sweep, which is the slow section",
    )
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

    sensitivity = []
    if args.skip_sweep:
        print()
        print("=== batch_size sensitivity: skipped (--skip-sweep) ===")
    else:
        print()
        print(f"=== batch_size sensitivity: {source.name}, 6 fields ===")
    for batch_size in [] if args.skip_sweep else DEFAULT_BATCHES:
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

    print()
    print(f"=== verify_parts cost: {source.name}, 6 fields, csv parts ===")
    verify_result = run_verify_check(source, work_root / "verify")
    if "error" in verify_result:
        print("ERROR", verify_result["error"])
    else:
        print(json.dumps(verify_result))
        rate = verify_result["output_mib"] / (verify_result["verify_ms"] / 1000)
        print(
            f"  {verify_result['parts']} parts / {verify_result['output_mib']:.3f} MiB "
            f"-> {verify_result['verify_ms']:.1f} ms  ({rate:.0f} MiB/s)"
        )
        print("  This is paid on every resume, and grows with the output, not the input.")

    out = work_root / "results.json"
    out.write_text(
        json.dumps(
            {
                "decomposition": decomposition,
                "sensitivity": sensitivity,
                "verify_parts": verify_result,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print()
    print("raw results:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
