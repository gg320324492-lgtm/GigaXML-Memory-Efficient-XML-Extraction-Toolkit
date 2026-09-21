"""The Qt-free half of the GUI: reading progress, and driving the CLI.

These tests deliberately do not import PySide6, and they must keep working in an
environment where it is not installed. That is the whole reason the two modules they
cover exist separately from the window: the interesting failures in a GUI like this are
in the plumbing, and the plumbing is testable without a display, a window, or an event
loop.

The marker that enforces it is a subprocess run with an import hook that refuses
PySide6 -- see ``test_the_gui_modules_do_not_need_qt`` at the bottom.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from gigaxml.gui.cli_process import (
    CliProcess,
    RunResult,
    cli_command,
    progress_lines,
    run_to_completion,
)
from gigaxml.gui.progress import Progress, format_eta, fraction_done, parse_line

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
GIGAXML = REPO / ".venv/Scripts/gigaxml.exe"

CONFIG = {
    "record": "/catalog/products/product",
    "fields": {"id": {"path": "@id"}, "name": {"path": "name"}},
}


def write_config(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def small_dataset(tmp_path: pathlib.Path, records: int) -> pathlib.Path:
    path = tmp_path / "small.xml"
    body = "".join(f'<product id="{n}"><name>N{n}</name></product>' for n in range(1, records + 1))
    path.write_text(f"<catalog><products>{body}</products></catalog>", encoding="utf-8")
    return path


# --- parsing -----------------------------------------------------------------


def test_a_progress_line_parses() -> None:
    line = (
        '{"event": "progress", "records": 120000, "rows": 119997, '
        '"rejected": 3, "elapsed_seconds": 11.24}'
    )
    parsed = parse_line(line)

    assert parsed is not None
    assert parsed.records == 120_000
    assert parsed.rows == 119_997
    assert parsed.rejected == 3
    assert parsed.elapsed_seconds == 11.24
    assert parsed.part is None
    assert parsed.missing_rows == 3


def test_a_checkpointed_line_carries_a_part_number() -> None:
    line = (
        '{"event": "progress", "records": 2000, "rows": 2000, '
        '"rejected": 0, "elapsed_seconds": 1.0, "part": 3}'
    )
    parsed = parse_line(line)

    assert parsed is not None
    assert parsed.part == 3


def test_a_warning_is_not_progress() -> None:
    assert parse_line("warning: --format parquet was ignored") is None
    assert parse_line("") is None
    assert parse_line("   ") is None


def test_a_truncated_line_is_not_progress() -> None:
    """A child killed mid-write leaves half a line. That must not raise."""
    assert parse_line('{"event": "progress", "records": 12') is None


def test_an_unknown_event_is_ignored_rather_than_fatal() -> None:
    """A newer CLI may add event types. An older window should not break on them."""
    assert parse_line('{"event": "something-new", "value": 1}') is None


def test_a_progress_line_without_counts_is_not_usable() -> None:
    """Showing a number nobody sent is worse than showing nothing."""
    assert parse_line('{"event": "progress", "elapsed_seconds": 1.0}') is None
    assert parse_line('{"event": "progress", "records": "many", "rows": 1}') is None


def test_booleans_are_not_counts() -> None:
    """`True` is an int in Python. It is not a record count."""
    assert parse_line('{"event": "progress", "records": true, "rows": 1}') is None


def test_a_json_array_is_not_a_progress_line() -> None:
    assert parse_line("[1, 2, 3]") is None


def test_progress_lines_filters_and_keeps_order() -> None:
    lines = [
        "warning: something",
        '{"event": "progress", "records": 10, "rows": 10, "rejected": 0, "elapsed_seconds": 0.1}',
        "",
        '{"event": "progress", "records": 20, "rows": 20, "rejected": 0, "elapsed_seconds": 0.2}',
    ]

    parsed = progress_lines(lines)

    assert [item.records for item in parsed] == [10, 20]


# --- the percentage and the estimate -----------------------------------------


def test_fraction_is_none_without_a_total() -> None:
    """The user edited the record path, so nothing knows the denominator. Show no bar."""
    assert fraction_done(500, None) is None
    assert fraction_done(500, 0) is None


def test_fraction_is_clamped() -> None:
    assert fraction_done(50, 100) == pytest.approx(0.5)
    assert fraction_done(200, 100) == 1.0
    assert fraction_done(-5, 100) == 0.0


def test_eta_needs_enough_to_go_on() -> None:
    assert format_eta(0, 0, 100) == "", "nothing done yet"
    assert format_eta(10, 50, None) == "", "no total, so no estimate"
    assert format_eta(0, 50, 100) == "", "no elapsed time"


def test_eta_formats_by_magnitude() -> None:
    assert format_eta(1.0, 50, 100) == "1s left"
    assert format_eta(60.0, 50, 100) == "1m 0s left"
    assert format_eta(3600.0, 50, 100) == "1h 0m left"
    assert format_eta(10.0, 100, 100) == "done"


# --- driving the CLI ---------------------------------------------------------


def test_the_command_runs_this_interpreter_as_a_module() -> None:
    """`python -m gigaxml` cannot work, and the console script may not be on PATH."""
    command = cli_command(["extract", "x.xml"])

    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "gigaxml.cli"]
    assert command[3:] == ["extract", "x.xml"]


def test_a_short_run_produces_a_summary_and_no_progress(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 500)
    config = write_config(tmp_path)

    result = run_to_completion(
        ["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]
    )

    assert result.ok, result.stderr_lines
    assert result.summary is not None
    assert result.summary["rows"] == 500
    assert progress_lines(result.stderr_lines) == []


def test_progress_arrives_through_the_callback(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 3000)
    config = write_config(tmp_path)
    seen: list[Progress] = []

    process = CliProcess(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "out.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ],
        on_progress=seen.append,
    )
    process.start()
    result = process.join(timeout=60)

    assert result is not None and result.ok, result.stderr_lines if result else "no result"
    assert [item.records for item in seen][:3] == [1000, 2000, 3000]
    assert seen[-1].records == 3000


def test_callbacks_run_off_the_calling_thread(tmp_path: pathlib.Path) -> None:
    """The window's thread must never be the one reading the pipe.

    A GUI that reads the child's output on its own thread freezes for as long as the run
    takes, which is the thing this whole design exists to avoid.
    """
    source = small_dataset(tmp_path, 2000)
    config = write_config(tmp_path)
    threads: list[str] = []

    def record(_: Progress) -> None:
        threads.append(threading.current_thread().name)

    process = CliProcess(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "out.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ],
        on_progress=record,
    )
    process.start()
    process.join(timeout=60)

    assert threads, "no progress was seen"
    assert all(name != threading.current_thread().name for name in threads)


def test_warnings_are_kept_in_full(tmp_path: pathlib.Path) -> None:
    """The CLI's messages are written to be read. The window must not summarise them away."""
    source = small_dataset(tmp_path, 200)
    config = write_config(tmp_path)

    result = run_to_completion(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "out.parquet"),
            "--format",
            "csv",
        ]
    )

    assert result.summary is not None
    assert result.summary["format"] == "csv", "the format warning fires and the run continues"
    assert result.warnings, "the warning line is available to show the user"


def test_a_failure_returns_a_nonzero_code_and_keeps_stderr(tmp_path: pathlib.Path) -> None:
    config = write_config(tmp_path)

    result = run_to_completion(
        ["extract", str(tmp_path / "missing.xml"), "-c", str(config), "-o", str(tmp_path / "o.csv")]
    )

    assert not result.ok
    assert result.exit_code != 0
    assert any("error" in line.lower() for line in result.warnings)


def test_kill_really_stops_the_child(tmp_path: pathlib.Path) -> None:
    """Cancelling has to mean the process is gone, not that we stopped listening."""
    source = small_dataset(tmp_path, 400_000)
    config = write_config(tmp_path)
    process = CliProcess(
        ["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]
    )
    process.start()

    # Wait until it is genuinely working rather than merely started.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not (tmp_path / "out.csv.tmp").exists():
        time.sleep(0.05)

    pid = process.pid
    assert pid is not None
    process.kill()

    assert not process.is_running
    result = process.result
    assert result is not None, "the reader thread finished"
    assert result.killed
    assert result.exit_code != 0

    # "Really stopped" is checked by watching the output stop growing, not by asking
    # whether the pid exists: on Windows a terminated pid can still be opened for a
    # short while, so a pid check reports the wrong answer at exactly the moment it
    # matters. A file that has stopped changing is the thing the user cares about.
    assert process.returncode is not None, "the child has an exit code"
    output = tmp_path / "out.csv"
    before = output.stat().st_size if output.exists() else 0
    time.sleep(0.4)
    after = output.stat().st_size if output.exists() else 0
    assert after == before, f"the output grew by {after - before} bytes after kill()"
    assert pid is not None


def test_cancelling_a_checkpointed_run_leaves_the_committed_parts(
    tmp_path: pathlib.Path,
) -> None:
    """What makes a cancel recoverable rather than destructive."""
    source = small_dataset(tmp_path, 400_000)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    process = CliProcess(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "5000",
            "--format",
            "csv",
        ]
    )
    process.start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and len(list(parts.glob("part-*.csv"))) < 2:
        time.sleep(0.05)
    process.kill()

    committed = sorted(parts.glob("part-*.csv"))
    assert committed, "at least one part survived the cancel"
    manifest = json.loads((parts / "checkpoint.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False, "it was interrupted, so it is not complete"
    assert manifest["records_consumed"] > 0


def test_kill_before_start_is_harmless() -> None:
    process = CliProcess(["extract"])

    process.kill()

    assert process.result is None
    assert not process.is_running


def test_starting_twice_is_refused(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 100)
    config = write_config(tmp_path)
    process = CliProcess(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "o.csv")])
    process.start()
    try:
        with pytest.raises(RuntimeError):
            process.start()
    finally:
        process.join(timeout=60)


def test_a_run_result_reports_ok_only_when_it_is() -> None:
    assert RunResult(exit_code=0).ok
    assert not RunResult(exit_code=1).ok
    assert not RunResult(exit_code=0, killed=True).ok


# --- the Qt-free promise -----------------------------------------------------


def test_the_gui_modules_do_not_need_qt() -> None:
    """Run the two modules in an interpreter where importing PySide6 fails.

    This is the check that keeps the split honest. If somebody later adds `import
    PySide6` to either module, the tests above would still pass on a machine with Qt
    installed -- and would start failing on one without it, which is the machine that
    matters for the `gui` extra being optional.
    """
    script = textwrap.dedent(
        """
        import sys

        class Blocker:
            def find_module(self, name, path=None):
                return self if name.split(".")[0] == "PySide6" else None

            def load_module(self, name):
                raise ImportError(f"PySide6 is blocked for this test: {name}")

        sys.meta_path.insert(0, Blocker())

        import gigaxml.gui.cli_process as cli_process
        import gigaxml.gui.progress as progress

        assert progress.parse_line('{"event": "progress", "records": 1, "rows": 1}') is not None
        assert cli_process.cli_command(["extract"])[1:3] == ["-m", "gigaxml.cli"]
        print("ok")
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False, cwd=str(REPO)
    )

    assert completed.returncode == 0, completed.stderr[-600:]
    assert "ok" in completed.stdout
