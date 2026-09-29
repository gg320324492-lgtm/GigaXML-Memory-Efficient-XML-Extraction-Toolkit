"""A performance regression baseline for CI: catch a 50% slowdown, ignore the noise.

The project decided (A11) that performance numbers are recorded, never asserted,
because a suite that fails when a machine is busy is worse than no suite. This is the
loose version of that decision, and it is deliberately loose in the same direction: a
shared CI runner is a different computer from the one that recorded the baseline, it is
noisy, and it may be under load from a neighbouring job. So the check is not "is this
as fast as it was" -- that is the assertion A11 rejected, and it would be red within a
week. It is **"is this still within half of where it was"**, which is the size of
regression worth a failed build and the size nobody ships on purpose.

Two thresholds, and only two:

* **throughput** must be at least :data:`MIN_THROUGHPUT_RATIO` of the recorded
  reference. Measured as the *median* of several runs, because one slow run on a busy
  runner is noise and the median is the statistic that shrugs at it.
* **peak memory** must not exceed :data:`MAX_PEAK_RSS_MB`. This one is absolute, not
  relative, and it is the more meaningful of the two: bounded memory is the property
  the product is sold on, it is a property of the code rather than of the hardware, and
  a 50% regression in throughput is a nuisance while a 50% regression in memory is the
  failure the whole design exists to prevent.

**The measurement is self-read inside the process that does the work.** A parent
reading a child's memory on this machine gets a frozen or absurdly low figure, which
is how a comparison sweep once reported 4.1 MB for every implementation at every size.
The extraction therefore runs here, and reports its own peak.

Usage::

    python benchmarks/perf_baseline.py --update      # record the current numbers
    python benchmarks/perf_baseline.py               # check against the recorded ones
    python benchmarks/perf_baseline.py --size 10MB --repeats 5
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
BASELINE = HERE / "perf-baseline.json"

#: Throughput may fall to this fraction of the recorded reference before the check
#: fails. Half is the point: a change that costs 50% is a change somebody will notice
#: and can defend, and anything gentler is indistinguishable from a slow Tuesday.
MIN_THROUGHPUT_RATIO = 0.60

#: Peak memory ceiling for an extraction, in MiB, over the small generated dataset.
#: Absolute rather than relative because this is the property that must not move: a
#: reader that accumulates shows up here as a hard number rather than as a ratio, and
#: a ratio would have needed a reference to regress *from*.
MAX_PEAK_RSS_MB = 60.0

#: A throughput below this is a failure regardless of the reference -- it exists so a
#: nonsense reference (recorded on a machine that was itself broken, or a typo in the
#: JSON) cannot turn the check into something that always passes.
FLOOR_RECORDS_PER_S = 1_000.0

CONFIG = """\
record: /catalog/products/product
fields:
  id:           {path: '@id'}
  type:         {path: '@type'}
  name:         {path: name}
  category:     {path: category}
  price:        {path: price, type: decimal}
  manufacturer: {path: manufacturer/name}
"""

#: Runs the extraction in a child and prints one JSON line: records, seconds and the
#: peak working set, the last self-read inside that child.
PROBE = """
import json, sys, time
sys.path.insert(0, {repo!r})
import yaml
from gigaxml.config import parse_config
from gigaxml.fields import extract_record
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import peak_rss_mb
from gigaxml.writers import create_writer

config = parse_config(yaml.safe_load({config!r}))
started = time.perf_counter()
records = 0
with create_writer({out!r}, config.fields) as writer:
    for record in StreamingRecordReader({source!r}, config.record_path):
        writer.write(extract_record(record, config.fields).values)
        records += 1
elapsed = time.perf_counter() - started
peak = peak_rss_mb()
print(json.dumps({{
    "records": records,
    "seconds": elapsed,
    "peak_mb": peak,
    "rows_written": writer.rows_written,
}}))
"""


def run_once(source: Path, config: str, work: Path) -> dict[str, object]:
    """One extraction in a fresh interpreter, reporting its own peak."""
    out = work / "out.csv"
    probe = PROBE.format(repo=str(REPO_ROOT), config=config, out=str(out), source=str(source))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    if completed.returncode != 0:
        raise SystemExit(
            f"the measurement run failed ({completed.returncode}):\n{completed.stderr[-800:]}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", default="10MB", help="dataset size to generate")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--update", action="store_true", help="record instead of checking")
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as work:
        work_dir = Path(work)
        source = work_dir / "data.xml"
        generated = subprocess.run(
            [
                sys.executable,
                "-m",
                "gigaxml.cli",
                "generate",
                "--size",
                args.size,
                "-o",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )
        if generated.returncode != 0 or not source.is_file():
            raise SystemExit(
                f"could not generate a {args.size} dataset:\n{generated.stderr[-600:]}"
            )

        runs = [run_once(source, CONFIG, work_dir) for _ in range(args.repeats)]

    rates = [int(r["records"]) / float(r["seconds"]) for r in runs]  # type: ignore[operator]
    peaks = [float(r["peak_mb"]) for r in runs]  # type: ignore[arg-type]
    records = {int(r["records"]) for r in runs}  # type: ignore[arg-type]
    if len(records) != 1:
        raise SystemExit(f"the repeats extracted different record counts: {sorted(records)}")

    measured = {
        "records": records.pop(),
        "records_per_s": round(statistics.median(rates), 1),
        "peak_rss_mb": round(max(peaks), 1),
        "repeats": args.repeats,
        "size": args.size,
        "rates": [round(rate, 1) for rate in rates],
        "peaks": [round(peak, 1) for peak in peaks],
        "measured_on": {
            "python": platform.python_version(),
            "machine": platform.machine(),
            "system": platform.system(),
        },
    }

    print(f"records      {measured['records']:,}")
    print(f"throughput   {measured['records_per_s']:,.0f} rec/s (median of {args.repeats})")
    print(f"peak memory  {measured['peak_rss_mb']:.1f} MiB (max of {args.repeats})")
    print(f"runs         {measured['rates']} rec/s, {measured['peaks']} MiB")

    if args.update:
        args.baseline.write_text(
            json.dumps(measured, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nwrote {args.baseline}")
        return 0

    if not args.baseline.is_file():
        raise SystemExit(
            f"{args.baseline} does not exist. Record one with --update, and read what "
            "it says about the machine it came from before trusting it."
        )
    reference = json.loads(args.baseline.read_text(encoding="utf-8"))
    reference_rate = float(reference["records_per_s"])
    required = reference_rate * MIN_THROUGHPUT_RATIO

    print(f"\nreference    {reference_rate:,.0f} rec/s")
    print(f"required     {required:,.0f} rec/s ({MIN_THROUGHPUT_RATIO:.0%} of reference)")

    failures: list[str] = []
    if measured["records_per_s"] < required:
        failures.append(
            f"throughput {measured['records_per_s']:,.0f} rec/s is below the "
            f"{required:,.0f} rec/s floor ({MIN_THROUGHPUT_RATIO:.0%} of the recorded "
            f"{reference_rate:,.0f})"
        )
    if measured["records_per_s"] < FLOOR_RECORDS_PER_S:
        failures.append(
            f"throughput {measured['records_per_s']:,.0f} rec/s is below the absolute "
            f"floor of {FLOOR_RECORDS_PER_S:,.0f} rec/s, which no healthy run reaches"
        )
    if measured["peak_rss_mb"] > MAX_PEAK_RSS_MB:
        failures.append(
            f"peak memory {measured['peak_rss_mb']:.1f} MiB is over the "
            f"{MAX_PEAK_RSS_MB:.1f} MiB ceiling -- this is the regression that matters, "
            "because bounded memory is the property the tool is sold on"
        )
    if measured["records"] != reference.get("records"):
        failures.append(
            f"extracted {measured['records']:,} records against the reference's "
            f"{reference.get('records'):,} -- a performance check on a run that reads a "
            "different amount of data measures nothing"
        )

    if failures:
        print("\nFAIL")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nOK: within the 60% throughput floor and the memory ceiling")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
