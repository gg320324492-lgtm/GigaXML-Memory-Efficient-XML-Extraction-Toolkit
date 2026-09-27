"""Run every implementation in this directory over the benchmark sizes, N times each.

Usage::

    python benchmarks/compare/run_comparison.py --sizes 100MB 1GB 4GB --repeats 5

For each (implementation, size) pair this runs the implementation's script ``--repeats``
times in its own subprocess, samples the child's peak RSS while it works, and records
wall-clock, rows written and output size per run. The JSON file this writes holds the
raw per-run values *and* the summary statistics (median / min / max / stdev / p95), so
a reader can recompute every summary from the raws.

Every number in REPORT.md comes from this script's output file, produced on the machine
and date recorded in its ``environment`` block.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent

IMPLEMENTATIONS = {
    "raw_lxml": HERE / "raw_lxml.py",
    "xmltodict": HERE / "via_xmltodict.py",
    "pandas": HERE / "via_pandas.py",
    "gigaxml": HERE / "via_gigaxml.py",
}

ROWS_PER_SIZE = {
    # The generator's manifest numbers; the equality check (verify script) is what
    # actually asserts equality between implementations -- these only turn seconds
    # into records/second.
    "100MB": 290_900,
    "1GB": 2_978_816,
    "4GB": 11_915_264,
    "10GB": 29_788_160,
}


def peak_rss_during(pid: int, done: subprocess.Popen) -> int:
    """Sample the child's RSS until it exits; return the high-water mark in bytes."""
    peak = 0
    try:
        handle = psutil.Process(pid)
    except psutil.Error:
        return 0
    while done.poll() is None:
        try:
            rss = handle.memory_info().rss
            peak = max(peak, rss)
        except psutil.Error:
            break  # the child exited between poll() and the sample
        time.sleep(0.05)
    return peak


def run_once(script: Path, input_path: Path, output_path: Path) -> dict[str, object]:
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, str(script), str(input_path), str(output_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    peak = peak_rss_during(process.pid, process)
    stdout, stderr = process.communicate()
    elapsed = time.perf_counter() - started
    output_size = output_path.stat().st_size if output_path.is_file() else 0
    return {
        "wall_s": round(elapsed, 3),
        "exit_code": process.returncode,
        "peak_rss_mb": round(peak / (1024 * 1024), 1),
        "output_bytes": output_size,
        "stdout_tail": stdout.strip().splitlines()[-1] if stdout.strip() else "",
        "stderr_tail": (stderr.strip().splitlines() or [""])[-1][:300],
    }


def summarise(wall_times: list[float], peak_rss: list[float]) -> dict[str, object]:
    ordered = sorted(wall_times)
    p95_index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    return {
        "median_s": round(statistics.median(wall_times), 3),
        "min_s": round(min(wall_times), 3),
        "max_s": round(max(wall_times), 3),
        "stdev_s": round(statistics.stdev(wall_times), 3) if len(wall_times) > 1 else 0.0,
        "p95_s": round(ordered[p95_index], 3),
        "peak_rss_median_mb": round(statistics.median(peak_rss), 1),
    }


def environment() -> dict[str, object]:
    def package_version(name: str) -> str:
        from importlib.metadata import version

        try:
            return version(name)
        except Exception:
            return "not installed"

    return {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "cpu": platform.processor(),
        "machine": platform.machine(),
        "memory_total_gb": round(psutil.virtual_memory().total / (1024**3), 1),
        "os": f"{platform.system()} {platform.release()}",
        "python": platform.python_version(),
        "lxml": package_version("lxml"),
        "xmltodict": package_version("xmltodict"),
        "pandas": package_version("pandas"),
        "gigaxml": package_version("gigaxml"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", default=["100MB", "1GB", "4GB"])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--implementations", nargs="+", default=list(IMPLEMENTATIONS))
    parser.add_argument("--data-dir", type=Path, default=REPO_ROOT / "data")
    parser.add_argument("--out", type=Path, default=HERE / "results.json")
    args = parser.parse_args()

    size_paths = {
        "100MB": args.data_dir / "b100m.xml",
        "1GB": args.data_dir / "b1g.xml",
        "4GB": args.data_dir / "b4g.xml",
        "10GB": args.data_dir / "b10g.xml",
    }

    results: dict[str, object] = {"environment": environment(), "results": {}}
    # The outputs land under <repo>/.scratch/, which a fresh checkout does not have --
    # found by running this in a clean worktree, where every implementation failed on
    # its output's missing parent directory before extracting a single row.
    scratch = args.data_dir.parent / ".scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    by_size = {}
    for size in args.sizes:
        input_path = size_paths[size]
        if not input_path.is_file():
            print(f"skip {size}: {input_path} does not exist", file=sys.stderr)
            continue
        per_impl: dict[str, object] = {}
        for name in args.implementations:
            script = IMPLEMENTATIONS[name]
            runs: list[dict[str, object]] = []
            for repeat in range(1, args.repeats + 1):
                output_path = scratch / f"cmp-{name}-{size}.csv"
                print(f"[{size}] {name} run {repeat}/{args.repeats} ...", flush=True)
                payload = run_once(script, input_path, output_path)
                payload["repeat"] = repeat
                runs.append(payload)
                print(
                    f"    wall={payload['wall_s']}s exit={payload['exit_code']} "
                    f"peak={payload['peak_rss_mb']}MB",
                    flush=True,
                )
            ok_runs = [r for r in runs if r["exit_code"] == 0]
            if ok_runs:
                wall = [float(r["wall_s"]) for r in ok_runs]
                rss = [float(r["peak_rss_mb"]) for r in ok_runs]
                rows_written = ROWS_PER_SIZE[size]
                summary = summarise(wall, rss)
                summary.update(
                    {
                        "records_per_s_median": int(rows_written / summary["median_s"]),
                        "ok_runs": len(ok_runs),
                        "failed_runs": args.repeats - len(ok_runs),
                    }
                )
            else:
                summary = {
                    "records_per_s_median": None,
                    "ok_runs": 0,
                    "failed_runs": args.repeats,
                    "note": "every repeat failed; see runs[].stderr_tail",
                }
            per_impl[name] = {"summary": summary, "runs": runs}
        by_size[size] = per_impl

    results["results"] = by_size
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
