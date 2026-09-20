"""``inspect`` throughput and where its time goes.

``inspect`` is roughly half the speed of ``extract`` on the same file, and the reason is
structural rather than a hot spot: it maintains several parallel bookkeeping stacks per
element. This measures the throughput on the real datasets and re-profiles so the numbers
in the report are reproducible.

Usage::

    python benchmarks/bench_inspect.py
    python benchmarks/bench_inspect.py --datasets b100m.xml --top 20
"""

from __future__ import annotations

import argparse
import cProfile
import io
import json
import pathlib
import pstats
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
DATA = REPO / "data"

CHILD = """
import json, pathlib, sys, time
import psutil
from gigaxml.inspect import inspect_document

process = psutil.Process()
def rss():
    return process.memory_info().rss / (1 << 20)

source = sys.argv[1]
baseline = rss()
started = time.perf_counter()
report = inspect_document(source)
elapsed = time.perf_counter() - started
size = pathlib.Path(source).stat().st_size / (1 << 20)
print(json.dumps({
    "input_mib": round(size, 2),
    "seconds": round(elapsed, 3),
    "throughput_mib_s": round(size / elapsed, 1),
    "baseline_mb": round(baseline, 3),
    "peak_mb": round(rss(), 3),
    "delta_mb": round(max(0.0, rss() - baseline), 3),
    "candidates": len(report.candidates),
}))
"""


def throughput(source: pathlib.Path, work: pathlib.Path) -> dict:
    work.mkdir(parents=True, exist_ok=True)
    script = work / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(script), str(source)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    if completed.returncode != 0:
        return {"error": completed.stderr.strip()[-300:]}
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    result["path"] = source.name
    return result


def profile(source: pathlib.Path, top: int) -> str:
    from gigaxml.inspect import inspect_document

    profiler = cProfile.Profile()
    profiler.enable()
    inspect_document(source)
    profiler.disable()

    stream = io.StringIO()
    stats = pstats.Stats(profiler, stream=stream)
    stats.sort_stats("tottime")
    stats.print_stats(top)
    return stream.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", nargs="+", default=["b100m.xml", "b1g.xml"])
    parser.add_argument("--top", type=int, default=14)
    parser.add_argument("--work", default=None)
    args = parser.parse_args()

    work = pathlib.Path(args.work) if args.work else pathlib.Path(tempfile.mkdtemp())

    print("=== throughput ===")
    rows = []
    for name in args.datasets:
        source = DATA / name
        if not source.is_file():
            print(f"missing: {source} -- generate it with `gigaxml generate`")
            continue
        result = throughput(source, work / name)
        rows.append(result)
        print(json.dumps(result))

    first = DATA / args.datasets[0]
    print()
    print(f"=== profile: {first.name}, top {args.top} by tottime ===")
    text = profile(first, args.top)
    print(text[:4000])
    (work / "profile.txt").write_text(text, encoding="utf-8")

    (work / "throughput.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print()
    print("raw results:", work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
