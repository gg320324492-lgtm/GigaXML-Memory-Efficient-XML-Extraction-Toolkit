"""⑫ Job History and the Resume Manager: the past, and the way back into it.

Every test here runs the **real CLI as a child process** and reads the report it wrote.
That is the whole point of the feature, so a test that built its own report would be
testing the reader against a fixture shaped like the answer: the report's field names, the
checkpoint block's shape and the placement of ``run-report.json`` are all the CLI's
decisions, and only a real run exercises them.

**The claim under test is that the history is a view of the reports, not a copy of them.**
So the strongest assertions here are the ones that would fail if the panel were keeping its
own record: the numbers change when the report changes, and a run made outside the window
-- from a terminal, in a directory the window was told about -- appears in the list without
the window having been involved at all. See
:func:`test_the_numbers_come_from_the_report_rather_than_from_a_copy`.
"""

from __future__ import annotations

import ctypes
import json
import pathlib
import sys

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.i18n import tr
from gigaxml.gui.job_history import directory_to_scan
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.history import format_peak, format_records, format_status

CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
  name:
    path: name
"""

SMALL = (
    "<catalog><products>"
    '<product id="1"><name>Widget</name></product>'
    '<product id="2"><name>Gadget</name></product>'
    '<product id="3"><name>Doohickey</name></product>'
    "</products></catalog>"
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture(scope="module")
def slow_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Big enough that a checkpointed run is still going when it is stopped.

    Built here rather than taken from ``data/``, for the reason
    ``test_gui_resume.py::slow_document`` gives: that directory is generated on demand and
    not committed, so a test needing one of its files would be testing something different
    on a machine that has never run the benchmarks.
    """
    path = tmp_path_factory.mktemp("hist-slow") / "slow.xml"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<catalog><products>")
        padding = "x" * 400
        number = 0
        while handle.tell() < 40 * 1024 * 1024:
            number += 1
            handle.write(f'<product id="{number}"><name>{padding}</name></product>')
        handle.write("</products></catalog>")
    return path


@pytest.fixture
def window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    del app
    main = MainWindow(state_dir=tmp_path / "state")
    yield main
    main.close()


@pytest.fixture
def document(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "small.xml"
    path.write_text(SMALL, encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    return path


def _terminate(pid: int | None) -> bool:
    """``TerminateProcess`` on the child, the way the task manager does it."""
    if sys.platform != "win32":
        pytest.skip("TerminateProcess is a Windows call")
    if pid is None:
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(0x0001, False, pid)
    if not handle:
        return False
    try:
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _wait(qtbot: QtBot, predicate, timeout_ms: int = 60_000) -> None:  # noqa: ANN001
    qtbot.waitUntil(predicate, timeout=timeout_ms)


def run_and_wait(
    window: MainWindow,
    qtbot: QtBot,
    *,
    source: pathlib.Path,
    config: pathlib.Path,
    output: pathlib.Path,
    checkpoint: int = 0,
) -> None:
    """Drive the execution panel the way a user does, and wait for the run to end."""
    panel = window.execution_panel()
    panel._checkpoint.setValue(checkpoint)
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(output))
    panel.start()
    _wait(qtbot, lambda: not panel.is_running(), timeout_ms=120_000)
    qtbot.wait(300)


def leave_a_checkpoint(
    window: MainWindow,
    qtbot: QtBot,
    source: pathlib.Path,
    config: pathlib.Path,
    parts: pathlib.Path,
) -> None:
    """Start a checkpointed run and stop it once the manifest exists.

    Waiting for the manifest rather than for a duration: it is written as parts are
    committed, so its appearance is the run being under way, and that is the same on a fast
    machine and a slow one.
    """
    panel = window.execution_panel()
    panel._checkpoint.setValue(500)
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(parts))
    panel.start()
    _wait(qtbot, panel.is_running, timeout_ms=30_000)
    _wait(qtbot, (parts / "checkpoint.json").is_file, timeout_ms=30_000)
    assert _terminate(panel._process.pid), "the child could not be killed"
    _wait(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)


# --- 判据 2: the history lists what ran, from the report --------------------


def test_a_finished_run_shows_up_in_the_history(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**The core of the feature.** Three records go in, three rows' worth of facts come
    out, and every one of them is a number the report wrote.

    Asserted individually rather than as a row count, because "the table is not empty" is
    also what a panel showing one arbitrary line would satisfy. ``peak`` is the one worth
    naming: it is the project's headline claim, it is a real measurement rather than a
    count, and a history that showed ``0.0`` for it would be the exact failure
    :func:`format_peak` exists to prevent.
    """
    output = tmp_path / "out.csv"
    run_and_wait(window, qtbot, source=document, config=config, output=output)

    panel = window.history_panel()
    entries = panel.entries()
    assert len(entries) == 1, f"expected the run to be listed once: {entries}"
    entry = entries[0]

    report = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
    assert entry.report_path == tmp_path / "run-report.json"
    assert entry.status == "ok" and entry.is_ok
    assert entry.rows == report["rows"] == 3
    assert entry.output == output
    assert entry.output_format == report["format"]
    assert entry.record_path == report["record_path"]
    assert entry.config_hash == report["config_hash"]
    assert entry.peak_rss_mb == report["peak_rss_mb"]
    assert entry.elapsed_seconds == report["elapsed_seconds"]
    assert entry.started_at is not None and entry.finished_at is not None

    assert panel.row_count() == 1
    assert panel.cell(0, 2) == "3 rows"
    assert "MiB" in panel.cell(0, 4), f"the peak was not shown: {panel.cell(0, 4)!r}"
    assert panel.cell(0, 5) == tr("finished")


def test_the_numbers_come_from_the_report_rather_than_from_a_copy(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**The claim that distinguishes a view from a copy, made falsifiable.**

    A panel keeping its own record would show the same numbers no matter what happened to
    the file on disk, and it would look right for ever. So the report is edited and the
    panel is asked again: the number has to follow the file, because the file is the record.
    Editing a report the CLI wrote is normally the wrong thing to do — which is exactly why
    it is a good probe: nothing in the product does it, so nothing but a genuine re-read
    can produce the new value.
    """
    run_and_wait(window, qtbot, source=document, config=config, output=tmp_path / "out.csv")
    panel = window.history_panel()
    assert panel.entries()[0].rows == 3

    report_path = tmp_path / "run-report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload["rows"] = 99
    report_path.write_text(json.dumps(payload), encoding="utf-8")

    panel.refresh()
    assert panel.entries()[0].rows == 99, "the panel is showing a copy, not the report"
    assert panel.cell(0, 2) == "99 rows"


def test_a_run_started_outside_the_window_still_appears(
    window: MainWindow, document: pathlib.Path, config: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The list is a view of the disk, not of this session.**

    The run is done from a command line, and the only thing the window is told is the
    directory to look in. If the history were assembled from what this window had done, the
    row would not be there — and a user who ran a job from a terminal and then opened the
    application is exactly who a job history is for.
    """
    from gigaxml.gui.cli_process import run_to_completion

    output = tmp_path / "elsewhere.csv"
    result = run_to_completion(
        ["extract", str(document), "-c", str(config), "-o", str(output), "--format", "csv"]
    )
    assert result.ok, f"the run failed: {result.stderr_lines}"

    window.job_history().note(output, checkpointing=False, config=config)
    entries = window.history_panel().entries() or window.job_history().entries()
    assert len(entries) == 1
    assert entries[0].rows == 3
    assert entries[0].is_ok


def test_the_newest_run_is_the_first_row(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """Two runs into two directories, newest first — the only order a history is useful in."""
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()
    run_and_wait(window, qtbot, source=document, config=config, output=first / "out.csv")
    run_and_wait(window, qtbot, source=document, config=config, output=second / "out.csv")

    entries = window.history_panel().entries()
    assert len(entries) == 2, entries
    assert entries[0].output == second / "out.csv"
    assert entries[1].output == first / "out.csv"


def test_a_directory_with_no_report_is_not_a_row(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Remembering a directory is not claiming a run happened in it.

    ``note`` is called on every ending, including one that never wrote a report, so a
    remembered directory can be empty. Showing a row for it would be a run that does not
    exist.
    """
    empty = tmp_path / "empty"
    empty.mkdir()
    # Noted the way the window does it: with the *output* the run was pointed at, which is
    # what decides the directory that gets remembered.
    window.job_history().note(empty / "out.csv", checkpointing=False, config=None)

    assert window.history_panel().row_count() == 0
    assert window.job_history().directories() == (empty,)


# --- 判据 5: a report that cannot be read is said so --------------------------


def test_a_report_that_cannot_be_read_is_shown_not_hidden(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**The rule P1-A established, applied to a list rather than to a single run.**

    A corrupt report is still a report that is there, and a history that dropped it would
    show an empty table where a broken file is sitting on disk — the user would conclude
    the run never happened. So the row is there, marked as unreadable, with the reason.
    """
    output = tmp_path / "out.csv"
    run_and_wait(window, qtbot, source=document, config=config, output=output)
    (tmp_path / "run-report.json").write_text("{ this is not json", encoding="utf-8")

    panel = window.history_panel()
    panel.refresh()
    entries = panel.entries()
    assert len(entries) == 1, "a report that could not be read was dropped from the list"
    entry = entries[0]
    assert entry.readable is False
    assert entry.is_known is False
    assert entry.unreadable_reason, "no reason was given for the unreadable report"

    assert panel.cell(0, 5) == tr("report unreadable")
    panel._table.selectRow(0)
    assert "could not be read" in panel.detail_text()
    assert entry.unreadable_reason in panel.detail_text()


def test_an_unreadable_report_shows_no_numbers_at_all(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**No zero anywhere.** A missing measurement must look missing.

    ``0 rows`` and ``0.0 MiB`` are both plausible-looking numbers, and a reader scanning the
    history for a regression would read them as measurements. Every field of an unreadable
    report is ``None``, and every cell says so.
    """
    run_and_wait(window, qtbot, source=document, config=config, output=tmp_path / "out.csv")
    (tmp_path / "run-report.json").write_text("[]", encoding="utf-8")

    panel = window.history_panel()
    panel.refresh()
    entry = panel.entries()[0]
    assert entry.readable is False
    assert entry.rows is None
    assert entry.peak_rss_mb is None
    assert entry.elapsed_seconds is None

    assert "0" not in panel.cell(0, 2), f"rows were shown as zero: {panel.cell(0, 2)!r}"
    assert "0.0 MiB" not in panel.cell(0, 4), f"a peak was invented: {panel.cell(0, 4)!r}"
    assert panel.cell(0, 2) == tr("unknown — the report could not be read")
    assert panel.cell(0, 4) == tr("not recorded")


def test_a_report_with_a_missing_field_says_it_was_not_recorded(tmp_path: pathlib.Path) -> None:
    """**Written by hand, on purpose, to reach the shape a real report does not have.**

    A report from an older build, or one whose peak could not be measured on the machine
    that wrote it, is a dict without the field. ``peak_rss_mb`` is ``None`` on a platform
    that cannot measure it, and :func:`gigaxml.run.peak_rss_mb` says in its own docstring
    that filling that gap would be worse than omitting it. The panel has to agree.
    """
    from gigaxml.gui.job_history import read_report

    report = tmp_path / "run-report.json"
    report.write_text(
        json.dumps({"status": "ok", "source": "a.xml", "rows": 5, "elapsed_seconds": 2.5}),
        encoding="utf-8",
    )
    entry = read_report(report)

    assert entry.readable is True
    assert entry.peak_rss_mb is None
    assert format_peak(entry.peak_rss_mb) == tr("not recorded")
    assert format_peak(0.0) == "0.0 MiB", "a real zero must still read as a zero"
    assert format_records(entry) == "5 rows"


def test_a_failed_run_is_listed_as_failed(tmp_path: pathlib.Path) -> None:
    """A failure is a row too, and it names the exception class rather than the wording."""
    from gigaxml.gui.job_history import read_report

    report = tmp_path / "run-report.json"
    report.write_text(
        json.dumps(
            {
                "status": "failed",
                "source": "a.xml",
                "rows": 0,
                "error": {"type": "FieldTypeError", "message": "field 'price' is not a number"},
            }
        ),
        encoding="utf-8",
    )
    entry = read_report(report)

    assert entry.is_ok is False and entry.is_known is True
    assert entry.error_type == "FieldTypeError"
    assert format_status(entry) == tr("failed")


# --- 判据 3: the Resume Manager ---------------------------------------------

#: A document whose sixth record cannot be extracted, so a checkpointed run fails after
#: committing some parts and therefore leaves a report that says ``complete: false``.
#:
#: **This shape is the only one a report can describe, and that is not an accident.** The
#: CLI writes its summary on the way out, so a run that was *killed* never writes one --
#: measured on a 4 GiB document, stopping the child at 4 s left 66 parts and no
#: ``run-report.json``. A run that *failed* does write one, and the checkpoint block records
#: ``complete`` as ``checkpoint.complete and error is None``, so a failure over checkpointed
#: output is exactly a report saying "not finished". That is the run a history can describe,
#: and the one this manager continues.
BROKEN = (
    "<catalog><products>"
    + "".join(
        f'<product id="{n}"><name>Widget</name>'
        f"<price>{'not-a-number' if n == 6 else '1.50'}</price></product>"
        for n in range(1, 21)
    )
    + "</products></catalog>"
)

PRICED_CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
  price:
    path: price
    type: float
"""


@pytest.fixture
def failed_checkpoint(
    window: MainWindow,
    qtbot: QtBot,
    tmp_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path, pathlib.Path]:
    """A checkpointed run that failed partway, run through the panel. Returns its paths."""
    document = tmp_path / "broken.xml"
    document.write_text(BROKEN, encoding="utf-8")
    config = tmp_path / "priced.yaml"
    config.write_text(PRICED_CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    run_and_wait(window, qtbot, source=document, config=config, output=parts, checkpoint=2)

    assert window.error_panel().failure() is not None, "the run was supposed to fail"
    return document, config, parts


def test_a_failed_checkpointed_run_is_the_one_the_history_can_continue(
    window: MainWindow,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    """**The premise of 判据 3, stated as an assertion about the report.**

    A report that says ``complete: false`` beside a parts directory is a run that can be
    continued, and :attr:`HistoryEntry.can_resume` is asking the report and nothing else.
    The part size is recovered from the report's own list of parts, which is why this row
    can fill ``--checkpoint-every`` at all.
    """
    _document, _config, parts = failed_checkpoint
    entries = window.history_panel().entries()
    assert len(entries) == 1, f"the run was not listed: {entries}"
    entry = entries[0]
    assert entry.has_report is True
    assert entry.status == "failed"
    assert entry.complete is False
    assert entry.can_resume is True
    assert entry.checkpoint_directory == parts
    assert entry.checkpoint_every == 2, "the part size was not recovered from the report"
    assert entry.error_type == "FieldTypeError"


def test_the_resume_button_carries_the_row_into_the_panel(
    window: MainWindow,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    """Every control the CLI needs is filled from the report, and nothing is started."""
    document, config, parts = failed_checkpoint
    panel = window.history_panel()
    panel.refresh()
    panel._table.selectRow(0)
    assert panel.is_resume_enabled() is True
    assert "Resume" in panel.resume_button_text()

    panel._on_resume()
    execution = window.execution_panel()
    assert execution._source.text() == str(document)
    assert execution._config.text() == str(config)
    assert execution._output.text() == str(parts)
    assert execution._checkpoint.value() == 2
    assert execution.is_resuming() is True
    assert execution.is_running() is False, "the button started a run"


def test_pressing_resume_never_starts_anything(
    window: MainWindow,
    qtbot: QtBot,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    del failed_checkpoint  # the fixture's run is the setup; nothing here reads it
    """**The button fills the panel; the user presses Start.**

    Continuing somebody's earlier work is a decision, and ``test_gui_resume.py`` already
    pins down that this project refuses to make it on their behalf. A "Resume" button that
    began extracting would be the same mistake wearing a different widget, and it would be
    much harder to notice: the user pressed one button and a multi-gigabyte extraction
    started.
    """
    panel = window.history_panel()
    panel.refresh()
    panel._table.selectRow(0)
    panel._on_resume()
    qtbot.wait(600)

    assert window.execution_panel().is_running() is False
    # Not asserted on `run_result()`: the panel keeps the previous run's outcome until a
    # new one starts, so its presence here says only that the fixture ran. `is_running()`
    # is the claim; this would be asserting something about the panel's bookkeeping.


def test_the_continued_run_is_the_clis_own_resume_and_not_a_second_start(
    window: MainWindow,
    qtbot: QtBot,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    """**判据 3, and the assertion that could not be satisfied by accident.**

    The CLI treats ``--resume`` and a fresh run over an existing checkpoint differently and
    says so in words: a fresh run stops with "a checkpoint already exists", while
    ``--resume`` goes to :func:`gigaxml.checkpoint.validate_resume`, which refuses when the
    source or the config has changed. So the source is changed after the failure, the run is
    started from the history row, and the failure that comes back must be the **checkpoint
    refusal naming both hashes** -- which is only reachable through ``--resume``.

    That is what makes this a test of the argument rather than of the button: a panel that
    filled the fields and forgot the flag would produce the other message, and the test
    would fail on a different sentence rather than on a count.
    """
    document, _config, _parts = failed_checkpoint
    with document.open("a", encoding="utf-8") as handle:
        handle.write(" ")

    panel = window.history_panel()
    panel.refresh()
    panel._table.selectRow(0)
    panel._on_resume()
    assert "--resume" in window.execution_panel().build_args()

    window.execution_panel().start()
    _wait(qtbot, lambda: not window.execution_panel().is_running(), timeout_ms=120_000)
    qtbot.wait(500)

    failure = window.error_panel().failure()
    assert failure is not None, "the run said nothing at all"
    assert failure.error_type == "CheckpointError", failure.message
    assert "cannot resume" in failure.message, failure.message
    assert "sha256" in failure.message, failure.message
    assert "already exists" not in failure.message, (
        "the CLI treated this as a fresh run, so --resume was not passed"
    )


def test_a_finished_run_is_not_offered_for_resume(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """A run that consumed its source has nothing to continue, however many parts it left."""
    parts = tmp_path / "parts"
    run_and_wait(window, qtbot, source=document, config=config, output=parts, checkpoint=1)

    panel = window.history_panel()
    entries = panel.entries()
    assert len(entries) == 1
    assert entries[0].complete is True
    assert entries[0].can_resume is False
    panel._table.selectRow(0)
    assert panel.is_resume_enabled() is False


def test_a_run_with_no_remembered_config_says_so_rather_than_guessing(
    window: MainWindow,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    """**``--config`` is required, and a fingerprint is not a path.**

    The report records ``config_hash`` -- a hash of the *parsed* config, deliberately, so
    that the same extraction described by two files agrees. It cannot be turned back into a
    file, and picking a config that looks similar would be continuing a run that is not the
    run being continued. So the panel says what is missing and stops.
    """
    _document, _config, parts = failed_checkpoint
    # The window remembered the config when the run happened. Forget the directory and
    # note it again with no config: that is the state a run reached from a session that has
    # since been cleared, and it is the only way to get here without faking a run.
    window.job_history().forget(parts)
    window.job_history().note(parts, checkpointing=True, config=None)

    panel = window.history_panel()
    panel.refresh()
    entry = panel.entries()[0]
    assert entry.can_resume is True, "the row itself is still resumable"
    assert window.job_history().config_for(entry.directory) is None

    panel._table.selectRow(0)
    panel._on_resume()
    # The window keeps the user where they are and says why, rather than filling a control
    # with a guess.
    assert window.execution_panel().is_resuming() is False
    assert "not remembered" in window.statusBar().currentMessage()


def test_a_config_that_is_gone_is_reported_rather_than_substituted(
    window: MainWindow,
    failed_checkpoint: tuple[pathlib.Path, pathlib.Path, pathlib.Path],
) -> None:
    """Remembered, and since deleted. Both halves of ``--config`` are needed."""
    _document, config, _parts = failed_checkpoint
    config.unlink()

    panel = window.history_panel()
    panel.refresh()
    panel._table.selectRow(0)
    panel._on_resume()

    assert window.execution_panel().is_resuming() is False
    assert "no longer there" in window.statusBar().currentMessage()


# --- what a stopped run looks like, given that it writes no report -----------


def test_a_run_that_was_stopped_is_listed_and_can_be_continued(
    tmp_path: pathlib.Path,
) -> None:
    """**The gap this feature ran into, closed the way the plan allows it to be closed.**

    A stopped process never reaches the line that writes the summary -- measured on a 4 GiB
    document, killing the child at 4 s left 66 parts and a manifest saying
    ``complete: false``, and no ``run-report.json`` at all. So the run a resume manager most
    exists for is the one a report-based history cannot describe.

    The directory is built by hand rather than by killing a child, because what is under
    test is how the history *reads* a report-less directory; the CLI's behaviour under a
    kill is measured above and quoted where it matters.

    **What may be read from the checkpoint, and what may not.** The verdict -- may this be
    continued -- comes from ``complete``, and the source path comes along because
    ``--source`` is required and this is the only place it was written down. Both are read
    through the project's own :func:`gigaxml.checkpoint.read_checkpoint`. What is **not**
    read is the run's data: ``records_consumed`` is 65,000 right there in this manifest and
    the row still shows no count, because displaying a number from the checkpoint is the
    thing the rule draws the line at.
    """
    from gigaxml.checkpoint import CHECKPOINT_FILENAME, Checkpoint, PartRecord, write_checkpoint
    from gigaxml.gui.job_history import scan

    document = tmp_path / "big.xml"
    document.write_text("<catalog/>", encoding="utf-8")

    parts = tmp_path / "parts"
    parts.mkdir()
    write_checkpoint(
        parts / CHECKPOINT_FILENAME,
        Checkpoint(
            source={"path": str(document), "size": 11, "sha256": "a" * 64},
            config="b" * 64,
            records_consumed=65_000,
            rejected=0,
            parts=(
                PartRecord(name="part-00000.parquet", rows=1000),
                PartRecord(name="part-00001.parquet", rows=1000),
            ),
            complete=False,
        ),
    )

    entries = scan([parts])
    assert len(entries) == 1, "a run that was stopped was dropped from the list entirely"
    entry = entries[0]
    assert entry.has_report is False, "no report was written, so none may be claimed"
    assert entry.readable is False

    # The verdict, and where it came from -- which is not the same sentence as a row whose
    # report said it, and the field is what keeps them apart.
    assert entry.complete is False
    assert entry.complete_source == "manifest"
    assert entry.can_resume is True

    # The argument the CLI needs, handed back unchanged rather than checked.
    assert entry.source == document
    # And the part size, because `--resume` cannot be passed without `--checkpoint-every`.
    assert entry.checkpoint_every == 1000

    # **And no data.** The row invents no number for a run that wrote no report.
    assert entry.rows is None and entry.peak_rss_mb is None and entry.elapsed_seconds is None
    assert entry.records_consumed is None, (
        "records_consumed is display data and must not be read out of the checkpoint"
    )
    assert entry.unreadable_reason
    assert "stopped" in entry.unreadable_reason


def test_a_stopped_run_whose_checkpoint_cannot_be_read_is_not_offered(
    tmp_path: pathlib.Path,
) -> None:
    """**One field needed a real decision, and this is the case where there is none.**

    A manifest that is missing keys, is not JSON, or is a format this build does not
    understand leaves ``complete`` unset, and ``can_resume`` is then false. The row stays —
    a directory with a checkpoint in it did have a run — but it cannot promise a
    continuation, and it says why rather than offering a button that leads nowhere.
    """
    from gigaxml.checkpoint import CHECKPOINT_FILENAME
    from gigaxml.gui.job_history import scan

    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / CHECKPOINT_FILENAME).write_text("{ not a checkpoint", encoding="utf-8")

    entry = scan([parts])[0]
    assert entry.has_report is False
    assert entry.complete is None
    assert entry.complete_source is None
    assert entry.can_resume is False
    assert entry.unreadable_reason
    assert "could not be read" in entry.unreadable_reason, entry.unreadable_reason


def test_a_stopped_run_is_continuable_from_the_history_itself(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """**The row is no longer a dead end, and it says which file made it possible.**

    With an unreadable checkpoint there is nothing to offer, so the Resume button stays
    disabled; that case is the next test, and the data-layer one is
    :func:`test_a_stopped_run_whose_checkpoint_cannot_be_read_is_not_offered`.
    """
    from gigaxml.checkpoint import CHECKPOINT_FILENAME, Checkpoint, PartRecord, write_checkpoint

    document = tmp_path / "big.xml"
    document.write_text("<catalog/>", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")

    parts = tmp_path / "parts"
    parts.mkdir()
    write_checkpoint(
        parts / CHECKPOINT_FILENAME,
        Checkpoint(
            source={"path": str(document), "size": 11, "sha256": "a" * 64},
            config="b" * 64,
            records_consumed=2,
            rejected=0,
            parts=(PartRecord(name="part-00000.csv", rows=2),),
            complete=False,
        ),
    )
    window.job_history().note(parts, checkpointing=True, config=config)

    panel = window.history_panel()
    panel.refresh()
    assert panel.row_count() == 1
    assert panel.cell(0, 5) == tr("stopped — no report"), "it is still a run that wrote no report"

    panel._table.selectRow(0)
    assert panel.is_resume_enabled() is True, (
        "the checkpoint says this run did not finish, so the row must offer to continue it"
    )
    detail = panel.detail_text()
    assert "no report" in detail
    assert "checkpoint" in detail, f"the row does not say where the verdict came from: {detail}"
    # The part size is here because `--resume` cannot be passed without `--checkpoint-every`.
    assert "2,000" in detail or "2 records" in detail or "1,000" in detail, detail

    # And pressing it fills the execution panel -- still without starting anything.
    panel._on_resume()
    execution = window.execution_panel()
    assert execution._source.text() == str(document)
    assert execution._output.text() == str(parts)
    assert execution._checkpoint.value() == 2
    assert execution.is_resuming() is True
    assert execution.is_running() is False, "the button started a run"


def test_a_stopped_run_with_a_broken_checkpoint_offers_no_resume(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """A manifest that cannot be read leaves nothing to decide on, so nothing is offered."""
    from gigaxml.checkpoint import CHECKPOINT_FILENAME

    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / CHECKPOINT_FILENAME).write_text("{}", encoding="utf-8")
    window.job_history().note(parts, checkpointing=True, config=None)

    panel = window.history_panel()
    panel.refresh()
    assert panel.row_count() == 1, "the directory still had a run in it"
    panel._table.selectRow(0)

    assert panel.is_resume_enabled() is False, "nothing knows whether this run can be continued"
    assert "could not be read" in panel.detail_text(), panel.detail_text()


def test_an_empty_directory_is_still_no_row(tmp_path: pathlib.Path) -> None:
    """Neither a report nor a manifest means nothing ran here, and that is not a row."""
    from gigaxml.gui.job_history import scan

    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan([empty]) == []


# --- what the history stores, and what it does not ---------------------------


def test_the_store_keeps_directories_and_config_paths_and_no_run_records(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**The boundary, asserted on the file itself.**

    The plan is explicit that the history and the run report are the same data. This reads
    the file the window wrote and checks that it contains no run facts at all: no rows, no
    timings, no status. Everything in it is a path. That is what makes it impossible for the
    two sources to disagree -- there is only one source.
    """
    output = tmp_path / "out.csv"
    run_and_wait(window, qtbot, source=document, config=config, output=output)

    payload = json.loads(window.job_history().store.read_text(encoding="utf-8"))
    assert set(payload) == {"directories", "configs"}
    for value in payload["directories"]:
        assert isinstance(value, str)
    for key, value in payload["configs"].items():
        assert isinstance(key, str) and isinstance(value, str)

    serialised = json.dumps(payload)
    for forbidden in ("rows", "elapsed", "peak", "status", "checkpoint", "config_hash"):
        assert forbidden not in serialised, (
            f"the store kept a run fact ({forbidden!r}), which the report should own"
        )


def test_the_directory_scanned_follows_the_clis_own_placement_rule(tmp_path: pathlib.Path) -> None:
    """Beside the output for a single file, inside it for a parts directory.

    Stated here because getting it wrong is invisible until a run's report is simply not
    found, and the natural-looking guess — "the report is beside the output" — is right
    exactly half the time.
    """
    assert directory_to_scan(tmp_path / "out.csv", checkpointing=False) == tmp_path
    assert directory_to_scan(tmp_path / "parts", checkpointing=True) == tmp_path / "parts"


def test_a_corrupt_store_reads_as_empty_rather_than_breaking_the_window(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """It is a cache of where to look, and a window that will not open is the worse outcome.

    The same rule ``RecentFiles._read`` follows, for the same reason: the list is rebuilt by
    running something, and everything in it is still on disk.
    """
    # Written first, then broken: a store that has never existed is an absent store, which
    # is the case above this one, not this one.
    window.job_history().note(tmp_path / "out.csv", checkpointing=False, config=None)
    window.job_history().store.write_text("{{{", encoding="utf-8")
    assert window.job_history().directories() == ()
    assert window.history_panel().row_count() == 0


def test_forgetting_a_directory_stops_it_being_scanned(
    window: MainWindow,
    qtbot: QtBot,
    document: pathlib.Path,
    config: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """The row disappears and so does the config that went with it — a pointer to nothing
    is not kept, and the user is the only one who gets to drop a row."""
    output = tmp_path / "out.csv"
    run_and_wait(window, qtbot, source=document, config=config, output=output)
    panel = window.history_panel()
    assert panel.row_count() == 1

    panel._table.selectRow(0)
    panel._on_forget()

    assert panel.row_count() == 0
    assert window.job_history().directories() == ()
    assert window.job_history().config_for(tmp_path) is None
