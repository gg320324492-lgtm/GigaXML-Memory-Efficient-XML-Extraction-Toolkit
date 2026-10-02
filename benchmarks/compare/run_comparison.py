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
#: reports its peak RSS. The parent supplies no number at all: cross-process readings
#: are not a measurement (see ``BENCHMARK-METHODOLOGY.md`` section 4), so the recorder
#: below parses a line the child printed and would have nothing to record without one.
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
WRAPPER = """import sys

script, *rest = sys.argv[1:]
sys.argv = [script, *rest]
code = 0
try:
    exec(compile(open(script, encoding="utf-8").read(), script, "exec"),
         {"__name__": "__main__", "__file__": script})
except SystemExit as exit_exc:
    code = exit_exc.code or 0
finally:
    from gigaxml.run import peak_rss_mb, peak_rss_source
    # The peak is read *after* the work finished, and that is the whole point: a
    # high-water mark remembers an allocation the allocator has already handed back.
    # Measured here, 2026-10-03, on a child that allocated 256 MiB and dropped the
    # reference: this function read 281.2 MiB where current RSS read 25.8 MiB.
    #
    # It is not read through psutil. `memory_info().peak_wset` is a Windows-only
    # field, so the `getattr(..., None) or info.rss` that used to stand here fell
    # through to *current* RSS everywhere else -- 14.4 MiB reported for a child that
    # allocated 384, on a Linux runner. The comment on that fallback said it was "a
    # different quantity wearing the same label", and then used it anyway.
    # `gigaxml.run.peak_rss_mb` is the same product code the CLI's own run report
    # uses, with a real implementation per platform, and `peak_rss_source` names the
    # counter it read so `peak_rss_method` is reported rather than assumed.
    #
    # Where there is no high-water mark at all, no number is reported. A platform that
    # cannot measure is not a platform whose measurement can be called wrong, and a
    # current-RSS figure filed under the name "peak" is the one thing worse.
    peak, counter = peak_rss_mb(), peak_rss_source()
    if peak is None:
        print(f"__GIGAXML_PEAK__none {counter or 'no high-water mark on this platform'}",
              file=sys.stderr)
    else:
        print(f"__GIGAXML_PEAK__{int(peak * 1048576)} {counter}", file=sys.stderr)
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
    # **``None``, not ``0``.** A platform whose child reported no high-water mark has
    # not been measured, and 0.0 would read downstream as a process that allocated
    # nothing -- a large improvement over every reading ever taken. The same reasoning
    # as ``gigaxml.run.peak_rss_mb`` returning ``None``, and the reason this is a
    # three-state field rather than a number with a caveat in a comment.
    peak_rss_mb: float | None = None
    peak_method = "not measured: the child printed no peak line"
    if peak_lines:
        reported = peak_lines[-1][len(PEAK_MARKER) :].split(maxsplit=1)
        counter = reported[1] if len(reported) > 1 else "counter not named by the child"
        if reported[0] == "none":
            peak_method = f"not measured: {counter}"
        else:
            peak_rss_mb = round(int(reported[0]) / (1024 * 1024), 1)
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


def summarise(wall_times: list[float], peak_rss: list[float | None]) -> dict[str, object]:
    """Median wall clock and median peak, from the runs that succeeded.

    ``None`` in ``peak_rss`` means the child could not report a high-water mark, and it
    is dropped rather than counted as zero. A run with no measured peak gets
    ``peak_rss_median_mb: None`` -- the same three-state rule as the per-run field, and
    the same reason: a median over "the ones that happened to work" would otherwise
    describe a subset as if it were the sweep.
    """
    ordered = sorted(wall_times)
    p95_index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    measured = [value for value in peak_rss if value is not None]
    return {
        "median_s": round(statistics.median(wall_times), 3),
        "min_s": round(min(wall_times), 3),
        "max_s": round(max(wall_times), 3),
        "stdev_s": round(statistics.stdev(wall_times), 3) if len(wall_times) > 1 else 0.0,
        "p95_s": round(ordered[p95_index], 3),
        "peak_rss_median_mb": round(statistics.median(measured), 1) if measured else None,
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
                rss: list[float | None] = [
                    None if r["peak_rss_mb"] is None else float(r["peak_rss_mb"]) for r in ok_runs
                ]
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
