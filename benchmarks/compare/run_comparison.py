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
import hashlib
import importlib.util
import json
import platform
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import psutil

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent


def _load_provenance() -> ModuleType:
    """Import ``benchmarks/provenance.py`` by path -- benchmarks are scripts, not a package."""
    path = REPO_ROOT / "benchmarks" / "provenance.py"
    spec = importlib.util.spec_from_file_location("gigaxml_benchmark_provenance", path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file is a bug
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256_of(path: Path) -> str:
    """The digest of a whole file, read in chunks because one of these is 4 GB."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


#: The child runs the implementation through this wrapper so **the child itself**
#: reports its peak RSS. Cross-process readings are not usable on this machine:
#: psutil's ``cpu_times()`` of a live foreign process freezes on its first sample,
#: and ``memory_info()`` of a live foreign process returns a frozen ~4.1 MB for a
#: working extraction (pinned down with three independent probes: ctypes
#: ``GetProcessMemoryInfo`` fails outright, a child's own ``GetCurrentProcess``
#: self-read returns zeros, while a child's self-read of ``peak_wset`` -- the
#: mechanism below and the one ``bench_extraction.py`` uses -- returns sane values).
#: The wrapper executes the implementation's source with ``__name__ == "__main__"`` (so
#: its module-level guard and ``sys.argv`` behave exactly as if run directly), prints
#: the peak on a tagged stderr line, and re-raises the implementation's exit code
#: untouched. It **compiles and ``exec``s** the source rather than using
#: ``runpy.run_path``, and the difference is not cosmetic: measured on the 1 GB document,
#: ``runpy`` reported raw_lxml at 297 MB where the same run under ``exec`` reported
#: 22 MB. ``run_path`` keeps the module's frame and its ``sys.path`` edit alive across
#: the extraction, and the allocator's high-water mark lands on that bookkeeping rather
#: than on the work -- a harness that inflates one implementation by 13x is measuring
#: itself.
WRAPPER = """import psutil
import sys

process = psutil.Process()
script, *rest = sys.argv[1:]
sys.argv = [script, *rest]
code = 0
try:
    exec(compile(open(script, encoding="utf-8").read(), script, "exec"),
         {"__name__": "__main__", "__file__": script})
except SystemExit as exit_exc:
    code = exit_exc.code or 0
finally:
    info = process.memory_info()
    # Which counter answered is **reported, not assumed**. The `or info.rss` below is a
    # real fallback for a platform whose psutil build has no `peak_wset`, and a fallback
    # that returns *current* RSS is a different quantity wearing the same label. Before
    # this, the recorder wrote "peak_wset (self-read in child)" unconditionally, so a run
    # that had quietly fallen back would have been filed as a peak measurement. The
    # measurement is unchanged -- same counter, same process, same moment -- but the
    # record now says which one it was, and `peak_rss_method` is read from this line
    # rather than typed in next to it.
    peak_wset = getattr(info, "peak_wset", None)
    if peak_wset:
        peak, counter = peak_wset, "peak_wset"
    else:
        peak, counter = info.rss, "rss (fallback: this platform has no peak_wset)"
    print(f"__GIGAXML_PEAK__{peak} {counter}", file=sys.stderr)
raise SystemExit(code)
"""

PEAK_MARKER = "__GIGAXML_PEAK__"


def run_once(script: Path, input_path: Path, output_path: Path) -> dict[str, object]:
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-c", WRAPPER, str(script), str(input_path), str(output_path)],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    stdout, stderr = process.communicate()
    elapsed = time.perf_counter() - started
    peak_lines = [line for line in stderr.splitlines() if line.startswith(PEAK_MARKER)]
    peak_rss_mb = 0
    peak_method = "unreadable: the child printed no peak line"
    if peak_lines:
        reported = peak_lines[-1][len(PEAK_MARKER) :].split(maxsplit=1)
        peak_rss_mb = round(int(reported[0]) / (1024 * 1024), 1)
        counter = reported[1] if len(reported) > 1 else "peak_wset (counter not named by the child)"
        peak_method = f"{counter}, self-read in the process that did the work"
    output_size = output_path.stat().st_size if output_path.is_file() else 0
    return {
        "wall_s": round(elapsed, 3),
        "exit_code": process.returncode,
        "peak_rss_mb": peak_rss_mb,
        "peak_rss_method": peak_method,
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

    def gigaxml_version() -> str:
        """The version of the code actually measured.

        Read from the package rather than from ``importlib.metadata``: on an editable
        install the recorded distribution is whatever was there when ``pip install -e``
        last ran, and a tree whose ``__version__`` has moved on still reports the old
        number -- which is how a sweep of 1.1.0 recorded itself as 0.1.0.
        """
        try:
            import gigaxml

            return gigaxml.__version__
        except Exception:
            return package_version("gigaxml")

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
        "gigaxml": gigaxml_version(),
    }


def count_data_rows(path: Path) -> int:
    """Data rows in a CSV (header excluded), counted from the file on disk.

    Counting is cheap relative to the extraction (a few tens of milliseconds for a
    750 MB output) and it is the only way to notice an implementation that stopped early
    without failing -- the gap between this and the document's record count is exactly
    what a silent truncation looks like from the outside.
    """
    if not path.is_file():
        return 0
    count = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            count += chunk.count(b"\n")
        # A CSV whose last line has no trailing newline still holds a record: seek to
        # the end rather than reading the file twice.
        if path.stat().st_size:
            handle.seek(-1, 2)
            if handle.read(1) != b"\n":
                count += 1
    return max(0, count - 1)  # minus the header


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

    # The identity block (M17 criterion A). Two of its fields are the reason this is a
    # function rather than a cosmetic addition to the output:
    #
    # * `git_commit` is `rev-parse HEAD`, never a branch name, because a branch moves and
    #   the commit does not -- "measured on main" is worth nothing the day after.
    # * `config_sha256` is taken over the **committed blob**, not the file in the working
    #   tree. With `core.autocrlf=true` those are different files: a fresh clone of
    #   `gigaxml-config.yaml` is 524 bytes of CRLF where the blob is 512 bytes of LF, and
    #   their digests differ. A working-tree hash is true on the machine that took it and
    #   false on every other one, which is worse than recording nothing.
    #
    # `dataset_sha256` is a map keyed by size rather than one string, because this script
    # measures four documents and a single hash would name the wrong one. Hashing them
    # costs a few seconds for the 4 GB file and happens once per size, beside the rows
    # that describe it. The situation this milestone found the existing `results.json` in
    # is the one it prevents: every number real, none of it traceable, because the
    # datasets were deleted and nothing recorded what produced them.
    datasets: dict[str, str] = {}
    for size in args.sizes:
        path = size_paths[size]
        datasets[size] = _sha256_of(path) if path.is_file() else "not measured: the file was absent"

    provenance = _load_provenance()
    identity = provenance.identity(
        REPO_ROOT,
        dataset_sha256=datasets,
        config_sha256=provenance.blob_sha256(REPO_ROOT, "benchmarks/compare/gigaxml-config.yaml"),
        extra={
            "config": "benchmarks/compare/gigaxml-config.yaml",
            "implementations": sorted(IMPLEMENTATIONS),
            "repeats": args.repeats,
        },
    )

    results: dict[str, object] = {
        "schema_version": provenance.SCHEMA_VERSION,
        "identity": identity,
        "environment": environment(),
        "results": {},
    }
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
        by_size[size] = per_impl
        for name in args.implementations:
            script = IMPLEMENTATIONS[name]
            runs: list[dict[str, object]] = []
            for repeat in range(1, args.repeats + 1):
                output_path = scratch / f"cmp-{name}-{size}.csv"
                print(f"[{size}] {name} run {repeat}/{args.repeats} ...", flush=True)
                payload = run_once(script, input_path, output_path)
                payload["repeat"] = repeat
                # The rows the output file actually holds, counted rather than believed:
                # throughput is reported over these, and a shortfall against the
                # document's record count is the signature of an implementation that
                # stopped early. Measured this way on the 4 GB file, pandas wrote
                # 10,485,760 of 11,915,264 rows and exited 0 -- invisible to any check
                # that read only the manifest and the wall clock.
                payload["rows_written"] = count_data_rows(output_path)
                runs.append(payload)
                print(
                    f"    wall={payload['wall_s']}s exit={payload['exit_code']} "
                    f"peak={payload['peak_rss_mb']}MB rows={payload['rows_written']:,}",
                    flush=True,
                )
            ok_runs = [r for r in runs if r["exit_code"] == 0]
            if ok_runs:
                wall = [float(r["wall_s"]) for r in ok_runs]
                rss = [float(r["peak_rss_mb"]) for r in ok_runs]
                rows_written = int(ok_runs[0]["rows_written"])
                summary = summarise(wall, rss)
                summary.update(
                    {
                        # Over the rows the output actually holds, not the rows the
                        # document contains: a truncated run that "processed" everything
                        # must not be credited with a record rate it did not deliver.
                        "records_per_s_median": int(rows_written / summary["median_s"]),
                        "rows_written_median": int(
                            statistics.median([int(r["rows_written"]) for r in ok_runs])
                        ),
                        "rows_in_document": ROWS_PER_SIZE[size],
                        "complete_output": rows_written == ROWS_PER_SIZE[size],
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
            # Checkpoint after every implementation, not once at the end. A full sweep
            # is hours of wall clock, and the 4 GB pandas runs alone sit near this
            # machine's whole memory budget -- a sweep that dies on its last group loses
            # every group that already finished unless the results are on disk. A
            # partial results.json, holding the groups that completed, beats none.
            results["results"] = by_size
            args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")

    results["results"] = by_size
    args.out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
