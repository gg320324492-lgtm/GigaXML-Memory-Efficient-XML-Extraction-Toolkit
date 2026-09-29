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
    python -m tests._mem --pipeline-stdin out.parquet   # document on standard input
    python -m tests._mem --inspect data/s400.xml
    python -m tests._mem --reject data/s400.xml <work-dir>
    python -m tests._mem --fastforward data/s400.xml <skip>
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import IO, Final

import psutil

from gigaxml.run import peak_rss_mb as _product_peak_rss_mb

__all__ = [
    "REPO_ROOT",
    "empty_baseline_mb",
    "fastforward_in_subprocess",
    "inspect_in_subprocess",
    "measure_peak_rss_mb",
    "peak_rss_mb",
    "pipeline_in_subprocess",
    "pipeline_stdin_in_subprocess",
    "pipeline_via_cli",
    "plain_extract_in_subprocess",
    "rejections_in_subprocess",
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


def rss_mb() -> float:
    """Current resident set size of this process, in MiB."""
    return psutil.Process().memory_info().rss / _MB


def peak_rss_mb() -> float:
    """Peak resident set size of this process so far, in MiB.

    Delegates to :func:`gigaxml.run.peak_rss_mb`, which is the same counter and is now
    the only implementation. This module used to carry its own ``ctypes`` copy, and it
    was silently broken: without declared argument types ctypes truncated the 64-bit
    pseudo-handle ``GetCurrentProcess`` returns, the ``psapi`` call returned false, and
    the fallback below answered every run with *current* RSS. Nothing failed -- a
    measurement that cannot tell peak from current is still a plausible number, which is
    what made it survive. One implementation, and the signatures declared.
    """
    measured = _product_peak_rss_mb()
    if measured is not None:
        return measured
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


def _run_pipeline(xml_path: str | IO[bytes], out_path: str) -> dict[str, float | int]:
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
    # A stream has no length to stat, so the throughput fields read 0.0 rather
    # than carrying a size that was never measured. Memory is unaffected: it does
    # not depend on how the bytes arrived.
    streamed = not isinstance(xml_path, (str, Path))
    size_mb = 0.0 if streamed else Path(xml_path).stat().st_size / _MB
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


def _run_fast_forward(xml_path: str, skip: str) -> dict[str, float | int]:
    """Parse and discard ``skip`` records, touching nothing else.

    This is what ``--resume`` does before it starts working. It must not accumulate:
    the whole point of a streaming fast-forward is that skipping a million records
    costs time and no memory.
    """
    from gigaxml.config import parse_config
    from gigaxml.parser.streaming import StreamingRecordReader

    count = int(skip)
    config = parse_config({"record": _PIPELINE_RECORD_PATH, "fields": {"id": {"path": "@id"}}})

    baseline = rss_mb()
    started = time.perf_counter()
    stream = StreamingRecordReader(xml_path, config.record_path)
    seen = 0
    for _ in stream:
        seen += 1
        if seen >= count:
            break
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "skipped": seen,
        "seconds": round(elapsed, 4),
        "input_mb": round(Path(xml_path).stat().st_size / _MB, 3),
        "records_per_sec": round(seen / elapsed, 1) if elapsed > 0 else 0.0,
    }


def _run_plain_extract(xml_path: str, work_dir: str) -> dict[str, float | int]:
    """Extract every record with the same config the fast-forward uses.

    The comparison Gate 2 asks for is "skipping costs no more memory than processing",
    and that is only meaningful if both measurements use the same config -- a wider
    config costs more per record, which would flatter the fast-forward.
    """
    from gigaxml.config import parse_config
    from gigaxml.parser.streaming import StreamingRecordReader
    from gigaxml.run import consume_records
    from gigaxml.writers import create_writer

    config = parse_config({"record": _PIPELINE_RECORD_PATH, "fields": {"id": {"path": "@id"}}})
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)

    baseline = rss_mb()
    started = time.perf_counter()
    writer = create_writer(work / "out.csv", config.fields)
    with writer:
        reader = StreamingRecordReader(xml_path, config.record_path)
        stats = consume_records(reader, config, writer)
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "records": stats.records_processed,
        "rows": writer.rows_written,
        "seconds": round(elapsed, 4),
        "input_mb": round(Path(xml_path).stat().st_size / _MB, 3),
    }


def plain_extract_in_subprocess(
    xml_path: str | Path,
    work_dir: str | Path,
) -> dict[str, float | int]:
    """Measure a full extraction with the fast-forward's config, in a fresh interpreter."""
    return _subprocess_profile(["--plain-extract", str(xml_path), str(work_dir)])


def fastforward_in_subprocess(xml_path: str | Path, skip: int) -> dict[str, float | int]:
    """Measure a fast-forward in a fresh interpreter.

    Args:
        xml_path: dataset to walk.
        skip: how many records to read and discard.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    return _subprocess_profile(["--fastforward", str(xml_path), str(skip)])


def _run_rejections(xml_path: str, work_dir: str) -> dict[str, float | int]:
    """Run a quarantine extraction in which *every* record is rejected.

    The config declares the price column as ``int``, and the generated prices look
    like ``5338.63``, so every record fails to convert. That gives one rejection per
    record -- about 291k for the 100MB dataset and 1.16M for the 400MB one -- which is
    the quantity a rejection log has to be flat in. If the log buffered its entries
    instead of streaming them, the 400MB delta would be roughly four hundred times the
    100MB one rather than about the same.
    """
    from gigaxml.config import parse_config
    from gigaxml.parser.streaming import StreamingRecordReader
    from gigaxml.run import RejectionLog, consume_records
    from gigaxml.writers import create_writer

    config = parse_config(
        {
            "record": _PIPELINE_RECORD_PATH,
            "on_error": "quarantine",
            "fields": {
                "product_id": {"path": "@id"},
                "price": {"path": "price", "type": "int"},
            },
        }
    )
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    size_mb = Path(xml_path).stat().st_size / _MB

    baseline = rss_mb()
    started = time.perf_counter()
    rejections = RejectionLog(work / "rejected.jsonl")
    with rejections:
        writer = create_writer(work / "out.csv", config.fields)
        with writer:
            reader = StreamingRecordReader(xml_path, config.record_path)
            stats = consume_records(reader, config, writer, rejections=rejections)
    elapsed = time.perf_counter() - started
    peak = peak_rss_mb()
    return {
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "records": stats.records_processed,
        "rejected": stats.rejected,
        "rejected_log_mb": round((work / "rejected.jsonl").stat().st_size / _MB, 3),
        "rows": writer.rows_written,
        "seconds": round(elapsed, 4),
        "input_mb": round(size_mb, 3),
        "records_per_sec": round(stats.records_processed / elapsed, 1) if elapsed > 0 else 0.0,
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
            f"measurement subprocess exited {completed.returncode} for "
            f"--pipeline-stdin{chr(10)}"
            f"--- stdout ---{chr(10)}{completed.stdout}"
            f"--- stderr ---{chr(10)}{completed.stderr}"
        )
    line = completed.stdout.strip().splitlines()[-1]
    payload: dict[str, float | int] = json.loads(line)
    return payload


def rejections_in_subprocess(
    xml_path: str | Path,
    work_dir: str | Path,
) -> dict[str, float | int]:
    """Run the all-rejections pipeline in a fresh interpreter.

    Args:
        xml_path: dataset to walk.
        work_dir: directory for the output and the rejection log; created if absent.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    return _subprocess_profile(["--reject", str(xml_path), str(work_dir)])


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


def _write_pipeline_config(work: Path) -> Path:
    """The pipeline config, on disk, for runs that go through the CLI."""
    import yaml

    config = work / "pipeline-config.yaml"
    config.write_text(
        yaml.safe_dump(
            {"record": _PIPELINE_RECORD_PATH, "fields": _PIPELINE_FIELDS}, sort_keys=False
        ),
        encoding="utf-8",
    )
    return config


def pipeline_via_cli(
    source: str | Path,
    out_path: str | Path,
    work: Path,
    *,
    from_stdin: bool = False,
) -> dict[str, float | int]:
    """Run the real ``gigaxml extract`` and read the peak out of its own run report.

    **This drives the product, not a test helper.** The earlier version of the stdin
    memory check handed ``sys.stdin.buffer`` straight to :func:`_run_pipeline`, which
    exercised :class:`~gigaxml.parser.streaming.StreamingRecordReader` and nothing
    above it. The CLI's own ``-`` branch -- the one line P1 added, and the one a later
    change is most likely to break by making it buffer the stream -- was never run at
    all, so replacing that line with ``io.BytesIO(sys.stdin.buffer.read())`` left every
    test in the repository green while the tool quietly read a 403 MB document into
    memory. A guard has to guard the thing that ships.

    The peak comes from the report the run writes about itself: ``--report`` records
    ``peak_rss_mb`` read *inside that process*, which is the only way to get a true
    number here. A parent reading a child's working set on this machine gets a frozen or
    absurdly low figure, which is how a comparison sweep once reported 4.1 MB for every
    implementation at every size.

    Args:
        source: the document, or the literal ``"-"`` to read standard input.
        out_path: output file; the extension picks the writer.
        work: scratch directory for the config and the report.
        from_stdin: feed the document on standard input rather than naming it.

    Returns:
        The same shape :func:`pipeline_in_subprocess` returns, so the two can be
        compared without special-casing.

    Raises:
        RuntimeError: the CLI exited non-zero, or wrote no usable report.
    """
    config = _write_pipeline_config(work)
    report = work / "cli-report.json"
    command = [
        sys.executable,
        "-m",
        "gigaxml.cli",
        "extract",
        "-" if from_stdin else str(source),
        "-c",
        str(config),
        "-o",
        str(out_path),
        "--report",
        str(report),
    ]
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT)}
    if from_stdin:
        with Path(source).open("rb") as document:
            completed = subprocess.run(
                command,
                cwd=REPO_ROOT,
                stdin=document,
                capture_output=True,
                env=env,
                check=False,
            )
    else:
        completed = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, env=env, check=False
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"gigaxml extract exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n"
            f"--- stderr ---\n{completed.stderr}"
        )
    payload = json.loads(report.read_text(encoding="utf-8"))
    # No `delta_mb` here: the report carries the process's own peak, and subtracting a
    # baseline measured by a *different* process is how you get a number that is neither.
    # Callers that want the increment measure an empty interpreter themselves.
    return {
        "records": int(payload["records"]["accepted"]),
        "seconds": float(payload["elapsed_seconds"]),
        "peak_mb": payload["peak_rss_mb"],
        "input_mb": round(Path(source).stat().st_size / _MB, 3),
        "output_mb": round(Path(out_path).stat().st_size / _MB, 3),
        "rejected": int(payload["records"]["rejected"]),
    }


def pipeline_stdin_in_subprocess(
    out_path: str | Path,
    stdin_data: bytes,
) -> dict[str, float | int]:
    """Measure a piped document: the work happens in a fresh interpreter fed on stdin.

    Args:
        out_path: output file; the extension picks the writer.
        stdin_data: the document bytes to feed the child.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "tests._mem", "--pipeline-stdin", str(out_path)],
        cwd=REPO_ROOT,
        input=stdin_data,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"measurement subprocess exited {completed.returncode} for --pipeline-stdin"
            f"\n--- stdout ---\n{completed.stdout}"
            f"\n--- stderr ---\n{completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


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
    if len(argv) == 3 and argv[1] == "--pipeline-stdin":
        # The document arrives on standard input. The counters are in *this*
        # process and self-read, for the reason the module docstring gives: a
        # parent reading a live child on this machine gets a frozen or absent
        # figure, which is how a sweep once reported 4.1 MB for everything.
        print(json.dumps(_run_pipeline(sys.stdin.buffer, argv[2])))
        return 0
    if len(argv) == 3 and argv[1] == "--inspect":
        print(json.dumps(_run_inspect(argv[2])))
        return 0
    if len(argv) == 4 and argv[1] == "--reject":
        print(json.dumps(_run_rejections(argv[2], argv[3])))
        return 0
    if len(argv) == 4 and argv[1] == "--fastforward":
        print(json.dumps(_run_fast_forward(argv[2], argv[3])))
        return 0
    if len(argv) == 4 and argv[1] == "--plain-extract":
        print(json.dumps(_run_plain_extract(argv[2], argv[3])))
        return 0
    if len(argv) != 3:
        print(
            "usage: python -m tests._mem <xml-path> <clean|noclean>\n"
            "       python -m tests._mem --baseline\n"
            "       python -m tests._mem --pipeline <xml-path> <out-path>\n"
            "       python -m tests._mem --inspect <xml-path>\n"
            "       python -m tests._mem --reject <xml-path> <work-dir>\n"
            "       python -m tests._mem --fastforward <xml-path> <skip>",
            file=sys.stderr,
        )
        return 2
    payload = _run_measurement(argv[1], clean=argv[2] == "clean")
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
