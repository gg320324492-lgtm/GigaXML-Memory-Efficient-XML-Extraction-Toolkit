"""Process memory measurement helpers for the test suite.

Windows has no ``resource.getrusage``, so RSS is read through ``psutil`` and, for
the true high-water mark, ``GetProcessMemoryInfo`` via ``ctypes``.

Measurements run in a **fresh subprocess**. In-process numbers taken inside
pytest conflate the scan with pytest's own heap, with garbage left behind by
earlier tests and with the coverage tracer, which is more than enough noise to
make a 1.30x ratio meaningless. A subprocess also gives us the OS-level peak
working set rather than a sampled approximation.

``tests/performance/test_memory.py`` consumes :func:`scan_in_subprocess`; this
module is also runnable directly:

    python -m tests._mem data/s100.xml clean
    python -m tests._mem data/s100.xml noclean
    python -m tests._mem --baseline
    python -m tests._mem --pipeline data/s400.xml out.parquet
    python -m tests._mem --inspect data/s400.xml
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Final

import psutil

__all__ = [
    "REPO_ROOT",
    "empty_baseline_mb",
    "inspect_in_subprocess",
    "measure_peak_rss_mb",
    "peak_rss_mb",
    "pipeline_in_subprocess",
    "rss_mb",
    "scan_in_subprocess",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

_MB: Final = 1024 * 1024

#: Record path and fields of the generated catalog, for the pipeline measurement.
_PIPELINE_RECORD_PATH: Final = "/catalog/products/product"
_PIPELINE_FIELDS: Final = {
    "product_id": {"path": "@id"},
    "kind": {"path": "@type"},
    "name": {"path": "name"},
    "category": {"path": "category"},
    "price": {"path": "price", "type": "decimal"},
    "currency": {"path": "price/@currency"},
    "manufacturer": {"path": "manufacturer/name"},
    "country": {"path": "manufacturer/country"},
    "first_tag": {"path": "tags/tag"},
}


class _ProcessMemoryCounters(ctypes.Structure):
    """``PROCESS_MEMORY_COUNTERS`` -- only ``PeakWorkingSetSize`` is used."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _windows_peak_working_set_mb() -> float | None:
    """OS high-water mark for this process, or ``None`` off Windows."""
    if sys.platform != "win32":
        return None
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    ok = ctypes.windll.psapi.GetProcessMemoryInfo(  # type: ignore[attr-defined]
        ctypes.windll.kernel32.GetCurrentProcess(),  # type: ignore[attr-defined]
        ctypes.byref(counters),
        counters.cb,
    )
    if not ok:
        return None
    return counters.PeakWorkingSetSize / _MB


def rss_mb() -> float:
    """Current resident set size of this process, in MiB."""
    return psutil.Process().memory_info().rss / _MB


def peak_rss_mb() -> float:
    """Peak resident set size of this process so far, in MiB.

    Uses the OS high-water mark on Windows (``PeakWorkingSetSize``). Elsewhere it
    degrades to the current RSS, which is the best ``psutil`` exposes portably.
    """
    peak = _windows_peak_working_set_mb()
    if peak is not None:
        return peak
    return rss_mb()


def measure_peak_rss_mb(
    action: Callable[[], None],
    *,
    interval_s: float = 0.002,
) -> tuple[float, float]:
    """Run ``action`` while sampling RSS.

    Returns:
        ``(baseline_mb, peak_delta_mb)``, where the delta is the peak observed
        during ``action`` minus the RSS measured immediately before it.
    """
    baseline = rss_mb()
    peak = baseline
    stop = threading.Event()

    def _sample() -> None:
        nonlocal peak
        while not stop.wait(interval_s):
            current = rss_mb()
            if current > peak:
                peak = current

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        action()
    finally:
        stop.set()
        sampler.join(timeout=1.0)
    return baseline, max(0.0, peak - baseline)


def _run_measurement(path: str, *, clean: bool) -> dict[str, float | int]:
    """Scan ``path`` once and report the memory profile of this process."""
    from gigaxml.parser.streaming import StreamingRecordReader

    size_mb = Path(path).stat().st_size / _MB
    baseline = rss_mb()
    started = time.perf_counter()
    count = 0
    for _record in StreamingRecordReader(path, "/catalog/products/product", _clean=clean):
        count += 1
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "records": count,
        "seconds": round(elapsed, 4),
        "input_mb": round(size_mb, 3),
        "records_per_sec": round(count / elapsed, 1) if elapsed > 0 else 0.0,
        "mb_per_sec": round(size_mb / elapsed, 2) if elapsed > 0 else 0.0,
    }


def _run_pipeline(xml_path: str, out_path: str) -> dict[str, float | int]:
    """Read, extract and write one file, and report this process's memory profile.

    This is the output-side counterpart of :func:`_run_measurement`: the reader
    keeps memory flat on the way in, and the writer has to keep it flat on the way
    out. Measuring the whole pipeline in one process is the only way to see the
    writer's contribution, since the reader alone was already shown to be bounded.
    """
    from gigaxml.config import parse_config
    from gigaxml.fields import extract_record
    from gigaxml.parser.streaming import StreamingRecordReader
    from gigaxml.writers import create_writer

    config = parse_config({"record": _PIPELINE_RECORD_PATH, "fields": _PIPELINE_FIELDS})
    size_mb = Path(xml_path).stat().st_size / _MB
    baseline = rss_mb()
    started = time.perf_counter()
    count = 0
    with create_writer(out_path, config.fields) as writer:
        for record in StreamingRecordReader(xml_path, config.record_path):
            writer.write(extract_record(record, config.fields).values)
            count += 1
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "records": count,
        "seconds": round(elapsed, 4),
        "input_mb": round(size_mb, 3),
        "output_mb": round(Path(out_path).stat().st_size / _MB, 3),
        "records_per_sec": round(count / elapsed, 1) if elapsed > 0 else 0.0,
        "mb_per_sec": round(size_mb / elapsed, 2) if elapsed > 0 else 0.0,
    }


def _run_inspect(xml_path: str) -> dict[str, float | int]:
    """Walk one file with ``inspect_document`` and report this process's memory.

    ``inspect`` cannot know the record path in advance, so it has no ``tag=`` filter
    to hide behind and must release every element itself. That makes its memory the
    purest test of the release rule: the path table is the only thing that grows
    with the document, and it is capped.
    """
    from gigaxml.inspect import inspect_document

    size_mb = Path(xml_path).stat().st_size / _MB
    baseline = rss_mb()
    started = time.perf_counter()
    report = inspect_document(xml_path)
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    top = report.candidates[0] if report.candidates else None
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "elements_seen": report.elements_seen,
        "paths": report.tracked_paths,
        "candidates": len(report.candidates),
        "top_candidate": None if top is None else top.path,
        "top_count": 0 if top is None else top.count,
        "seconds": round(elapsed, 4),
        "input_mb": round(size_mb, 3),
        "mb_per_sec": round(size_mb / elapsed, 2) if elapsed > 0 else 0.0,
    }


def scan_in_subprocess(xml_path: str | Path, *, clean: bool = True) -> dict[str, float | int]:
    """Scan ``xml_path`` in a fresh interpreter and return its memory profile.

    Args:
        xml_path: dataset to scan.
        clean: ``True`` uses :class:`StreamingRecordReader` as shipped. ``False``
            disables the element cleanup, which is how the reversed memory test
            proves the cleanup is load-bearing.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    completed = subprocess.run(
        [sys.executable, "-m", "tests._mem", str(xml_path), "clean" if clean else "noclean"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"measurement subprocess exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    line = completed.stdout.strip().splitlines()[-1]
    payload: dict[str, float | int] = json.loads(line)
    return payload


def inspect_in_subprocess(xml_path: str | Path) -> dict[str, float | int]:
    """Run ``inspect_document`` in a fresh interpreter and return its memory profile.

    Args:
        xml_path: dataset to walk.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    return _subprocess_profile(["--inspect", str(xml_path)])


def pipeline_in_subprocess(
    xml_path: str | Path,
    out_path: str | Path,
) -> dict[str, float | int]:
    """Run read+extract+write in a fresh interpreter and return its memory profile.

    Args:
        xml_path: dataset to extract from.
        out_path: output file; the extension picks the writer.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    return _subprocess_profile(["--pipeline", str(xml_path), str(out_path)])


def _subprocess_profile(arguments: list[str]) -> dict[str, float | int]:
    """Run one measurement mode in a fresh interpreter and parse its JSON line."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    completed = subprocess.run(
        [sys.executable, "-m", "tests._mem", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"measurement subprocess exited {completed.returncode} for {arguments}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    line = completed.stdout.strip().splitlines()[-1]
    payload: dict[str, float | int] = json.loads(line)
    return payload


def empty_baseline_mb() -> float:
    """Peak RSS of a fresh interpreter that only imports the package.

    This is the "empty process" reference the roadmap asks for: everything above
    it is attributable to the scan rather than to ``import lxml``.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "tests._mem", "--baseline"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    if completed.returncode != 0:
        raise RuntimeError(f"baseline subprocess failed:\n{completed.stderr}")
    payload: dict[str, float | int] = json.loads(completed.stdout.strip().splitlines()[-1])
    return float(payload["baseline_mb"])


def _main(argv: list[str]) -> int:
    if argv[1:] == ["--baseline"]:
        from gigaxml.parser.streaming import StreamingRecordReader  # noqa: F401

        print(json.dumps({"baseline_mb": round(peak_rss_mb(), 3)}))
        return 0
    if len(argv) == 4 and argv[1] == "--pipeline":
        print(json.dumps(_run_pipeline(argv[2], argv[3])))
        return 0
    if len(argv) == 3 and argv[1] == "--inspect":
        print(json.dumps(_run_inspect(argv[2])))
        return 0
    if len(argv) != 3:
        print(
            "usage: python -m tests._mem <xml-path> <clean|noclean>\n"
            "       python -m tests._mem --baseline\n"
            "       python -m tests._mem --pipeline <xml-path> <out-path>\n"
            "       python -m tests._mem --inspect <xml-path>",
            file=sys.stderr,
        )
        return 2
    payload = _run_measurement(argv[1], clean=argv[2] == "clean")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
