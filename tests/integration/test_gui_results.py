"""⑥ results: what a run produced, and what a stopped run left behind.

The interrupted case is built with a **real child process and a real kill** -- a fixture
``.tmp`` written by the test would prove nothing, because the thing being checked is that
the window notices a run that stopped partway, and a run that never happened cannot stop
partway.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Callable

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.results import ResultPanel, count_of, format_elapsed
from gigaxml.gui.run_report import LeftBehind, left_behind, read_outcome
from gigaxml.writers import PARTIAL_SUFFIX

DOCUMENT = """<catalog><products>
<product id="1"><name>Alpha Lamp</name><qty>5</qty></product>
<product id="2"><name>Beta Suite</name><qty>7</qty></product>
<product id="3"><name>Gamma Desk</name><qty>9</qty></product>
</products></catalog>
"""

#: One record whose ``qty`` will not convert, so a quarantining run has something to reject.
MIXED = (
    '<catalog><products><product id="1"><name>A</name><qty>nope</qty></product>'
    '<product id="2"><name>B</name><qty>2</qty></product></products></catalog>'
)

CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
  name:
    path: name
"""

INT_CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
  qty:
    path: qty
    type: int
"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture(scope="module")
def slow_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A document big enough that a run is still going when the test stops it.

    Built here rather than taken from ``data/``: that directory is generated on demand and
    not committed, so a test that needed ``data/b100m.xml`` would be testing something
    different on a machine that has never run the benchmarks.
    """
    path = tmp_path_factory.mktemp("slow") / "slow.xml"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<catalog><products>")
        padding = "x" * 400
        number = 0
        while handle.tell() < 40 * 1024 * 1024:
            number += 1
            handle.write(
                f'<product id="{number}"><name>{padding}</name>'
                f"<tags><tag>t{number}</tag><tag>u{number}</tag></tags></product>"
            )
        handle.write("</products></catalog>")
    return path


@pytest.fixture
def window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    del app
    return MainWindow(state_dir=tmp_path / "state")


def wait_until(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    qtbot.waitUntil(predicate, timeout=timeout_ms)
    return predicate()


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


def run(
    window: MainWindow,
    qtbot: QtBot,
    source: pathlib.Path,
    config: pathlib.Path,
    output: pathlib.Path,
) -> None:
    panel = window.execution_panel()
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(output))
    panel.start()
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)


def documents(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    return (
        write(tmp_path / "doc.xml", DOCUMENT),
        write(tmp_path / "config.yaml", CONFIG),
    )


# --- G1: a run that finished --------------------------------------------------


def test_a_finished_run_shows_what_it_produced(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    source, config = documents(tmp_path)
    output = tmp_path / "out.csv"

    run(window, qtbot, source, config, output)

    results = window.result_panel()
    assert not results.isHidden(), "the panel said nothing about a run that finished"
    assert "3" in results.headline_text(), results.headline_text()
    assert str(output) in results.path_text()
    assert "rejected" in results.detail_text().lower()
    assert window.error_panel().failure() is None


def test_the_summary_numbers_are_the_ones_the_tool_reported(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**Read from the report, not from the text.** The panel's numbers are checked against
    the file the CLI wrote rather than against a literal, so a summary that agreed with a
    hard-coded three would fail the moment the document changed."""
    source, config = documents(tmp_path)
    output = tmp_path / "out.csv"

    run(window, qtbot, source, config, output)

    outcome = window.result_panel().outcome()
    assert outcome is not None
    payload = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
    assert outcome.rows == payload["rows"] == 3
    assert outcome.rejected == payload["rejected"] == 0
    assert outcome.format == payload["format"]
    assert outcome.output == pathlib.Path(payload["output"])


def test_a_run_that_rejected_records_says_how_many_and_where(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A quarantining run **succeeds** -- and the count is the only sign that it did not
    take everything, so it goes first in the summary."""
    source = write(tmp_path / "mixed.xml", MIXED)
    config = write(tmp_path / "config.yaml", INT_CONFIG)
    window.execution_panel().set_on_error("quarantine")

    run(window, qtbot, source, config, tmp_path / "out.csv")

    results = window.result_panel()
    outcome = results.outcome()
    assert outcome is not None
    assert outcome.rejected == 1
    assert outcome.rejected_path is not None
    detail = results.detail_text()
    assert "1 record was rejected" in detail, detail
    assert str(outcome.rejected_path) in detail


def start_and_stop(
    window: MainWindow,
    qtbot: QtBot,
    source: pathlib.Path,
    config: pathlib.Path,
    output: pathlib.Path,
    *,
    checkpointing: bool = False,
) -> None:
    """Start a run and stop it once it has written something.

    **Waits for the file that says the run is under way, which differs by mode.** Writing one
    file that is ``<output>.tmp``; checkpointing it is the manifest, because the parts are
    complete the moment each is renamed into place and a ``.tmp`` exists only for the instant
    a part is being written.

    Waiting for the precondition rather than for a duration, because the first version slept
    for a fixed 1.2 seconds and the run finished inside that -- 40MB takes about a second
    here -- so there was nothing to stop and the test was checking a run that had succeeded.
    """
    panel = window.execution_panel()
    panel._checkpoint.setValue(2 if checkpointing else 0)
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(output))

    ready = (
        (output / "checkpoint.json").is_file
        if checkpointing
        else output.with_name(output.name + PARTIAL_SUFFIX).is_file
    )
    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)
    wait_until(qtbot, ready, timeout_ms=30_000)
    panel.cancel()
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)


# --- G2: a run that was stopped -----------------------------------------------


def test_a_stopped_run_says_so_and_points_at_what_it_left(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**A real child, stopped for real.**

    A killed run exits non-zero with nothing on stdout, nothing on stderr, and no report --
    so before this there was nothing to show and the window showed nothing. It looked like
    a run that finished. The half-written output was on disk the whole time.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "stopped.csv"

    partial = output.with_name(output.name + PARTIAL_SUFFIX)
    start_and_stop(window, qtbot, slow_document, config, output)

    assert partial.is_file(), "the child was stopped before it wrote anything to stop"

    results = window.result_panel()
    assert not results.isHidden(), "a stopped run said nothing"
    assert results.is_unfinished()
    assert results.left().path == partial
    assert str(partial) in results.path_text()
    assert results.is_warning(), "a stopped run has to look like one"
    assert window.error_panel().failure() is None, "stopping is not a failure"


def test_the_path_of_the_partial_output_can_be_copied(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The warning is only useful if the file it names can be reached.

    Presses the button rather than emitting the signal: what is being checked is that the
    user can get the path out, and emitting the signal skips the part they do.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "stopped.csv"
    start_and_stop(window, qtbot, slow_document, config, output)
    QApplication.clipboard().setText("")

    window.result_panel()._copy.click()

    assert str(output.with_name(output.name + PARTIAL_SUFFIX)) in QApplication.clipboard().text()


def test_a_run_killed_from_outside_is_treated_the_same_way(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The GUI's Cancel and a hard kill from outside are different paths.**

    ``panel.cancel()`` sets ``run.killed``, so the window knows what happened without
    looking at the disk. ``TerminateProcess`` -- the task manager, or anything else that
    kills the child -- leaves that flag False, and the window has only the exit code and the
    ``.tmp`` to go on. Nothing else tested that path, and it is the one a user hits when a
    run is stopped by something other than this window.

    The ``.tmp`` is empty at this point more often than not: the writer creates it when it
    opens the file and buffers thousands of rows before the first flush. The check is that
    it *exists*, not that it has anything in it.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "killed.csv"
    partial = output.with_name(output.name + PARTIAL_SUFFIX)
    panel = window.execution_panel()
    panel._source.setText(str(slow_document))
    panel._config.setText(str(config))
    panel._output.setText(str(output))

    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)
    wait_until(qtbot, partial.is_file, timeout_ms=30_000)
    assert _terminate(panel._process.pid), "the child could not be killed"
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)

    run = panel.run_result()
    assert run is not None
    assert not run.killed, "this path is the one where the flag is NOT set"

    results = window.result_panel()
    assert results.is_unfinished(), "a hard kill was reported as something else"
    assert results.left().path == partial
    assert window.error_panel().failure() is None


def _terminate(pid: int | None) -> bool:
    """``TerminateProcess`` on the child, the way the task manager does it."""
    import ctypes

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


def test_a_stopped_run_after_a_failed_one_is_not_reported_as_that_failure(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The mirror of the stale manifest, in the ordinary mode.**

    A run that fails writes a report beside the output and nothing removes it. A later run to
    the same output that is killed from outside writes none -- so the report still there is
    the *first* run's, and reading it reports that failure again, advice and all, for a run
    that was stopped. Measured before this: a WriterError followed by a hard kill was shown
    as the WriterError, with this run's ``.tmp`` sitting there unmentioned.

    What tells them apart is that the killed run said nothing. ``warnings`` is stderr without
    the progress events, so a run that failed on its own has some and a run killed from
    outside has none.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "out.csv"

    # First: a failure of the run's own, which leaves a report.
    output.write_text("", encoding="utf-8")
    holder = _hold_exclusively(output)
    try:
        run(window, qtbot, documents(tmp_path)[0], config, output)
    finally:
        holder.close()
    first = window.error_panel().failure()
    assert first is not None and first.had_report, "the first run left no report to go stale"

    # Then: the same output, stopped from outside.
    partial = output.with_name(output.name + PARTIAL_SUFFIX)
    panel = window.execution_panel()
    panel._source.setText(str(slow_document))
    panel._config.setText(str(config))
    panel._output.setText(str(output))
    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)
    wait_until(qtbot, partial.is_file, timeout_ms=30_000)
    assert _terminate(panel._process.pid), "the child could not be killed"
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)

    results = window.result_panel()
    assert results.is_unfinished(), "the first run's failure was reported for this one"
    assert window.error_panel().failure() is None


# --- G4: a clean success has nothing to warn about ----------------------------


def test_a_clean_success_shows_no_warning(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """No ``.tmp``, no warning. A panel that warned on every run would be noise, and the
    one that matters would be lost in it."""
    source, config = documents(tmp_path)
    output = tmp_path / "out.csv"

    run(window, qtbot, source, config, output)

    results = window.result_panel()
    assert not results.is_unfinished()
    assert not results.is_warning(), "a run that finished was shown as a warning"
    assert results.left() is None
    assert not output.with_name(output.name + PARTIAL_SUFFIX).exists()


# --- the two panels are mutually exclusive ------------------------------------


def test_only_one_of_the_two_panels_speaks_at_a_time(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A run either finished, was stopped, or failed. Both panels showing at once would be
    two answers to one question, and the window is what keeps them apart."""
    source, config = documents(tmp_path)

    run(window, qtbot, source, config, tmp_path / "ok.csv")
    assert not window.result_panel().isHidden()
    assert window.error_panel().isHidden()

    # The same output, held open, so the next run fails where it tries to put the file.
    locked = write(tmp_path / "locked.csv", "")
    holder = _hold_exclusively(locked)
    try:
        run(window, qtbot, source, config, locked)
    finally:
        holder.close()
    assert not window.error_panel().isHidden()
    assert window.result_panel().isHidden(), "the failure left the summary on screen"


def test_going_back_to_a_success_clears_the_failure(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    source, config = documents(tmp_path)
    locked = write(tmp_path / "locked.csv", "")
    holder = _hold_exclusively(locked)
    try:
        run(window, qtbot, source, config, locked)
    finally:
        holder.close()
    assert not window.error_panel().isHidden()

    run(window, qtbot, source, config, tmp_path / "ok.csv")

    assert window.error_panel().isHidden(), "the failure stayed on screen"
    assert not window.result_panel().isHidden()


# --- reading it ---------------------------------------------------------------


def test_the_summary_comes_from_the_report(tmp_path: pathlib.Path) -> None:
    """Numbers nobody would write by hand, to show they were read rather than assumed."""
    (tmp_path / "run-report.json").write_text(
        json.dumps(
            {
                "rows": 4321,
                "rejected": 17,
                "rejected_path": str(tmp_path / "rejected.jsonl"),
                "elapsed_seconds": 12.5,
                "output": str(tmp_path / "out.csv"),
                "format": "jsonl",
            }
        ),
        encoding="utf-8",
    )

    outcome = read_outcome(tmp_path / "out.csv")

    assert outcome is not None
    assert (outcome.rows, outcome.rejected) == (4321, 17)
    assert outcome.elapsed_seconds == 12.5
    assert outcome.format == "jsonl"


def test_no_report_reads_as_no_summary(tmp_path: pathlib.Path) -> None:
    assert read_outcome(tmp_path / "out.csv") is None


def test_a_partial_is_found_only_when_it_is_there(tmp_path: pathlib.Path) -> None:
    output = tmp_path / "out.csv"
    assert left_behind(output) is None

    partial = output.with_name(output.name + PARTIAL_SUFFIX)
    partial.write_text("half a file", encoding="utf-8")

    found = left_behind(output)
    assert found is not None and found.path == partial


def test_a_partial_is_not_looked_for_anywhere_but_beside_the_output(
    tmp_path: pathlib.Path,
) -> None:
    """**Beside the output only.** A scan of the disk would find every abandoned run the
    machine has ever had, and none of them would be this one."""
    output = tmp_path / "out.csv"
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    (elsewhere / f"other.csv{PARTIAL_SUFFIX}").write_text("not ours", encoding="utf-8")

    assert left_behind(output) is None


# --- checkpointing: the same three outcomes, a different place on disk ---------


def test_a_finished_checkpointed_run_shows_its_summary(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """``--output`` names a directory in this mode, and the summary says so."""
    source, config = documents(tmp_path)
    parts = tmp_path / "parts"
    window.execution_panel()._checkpoint.setValue(2)

    run(window, qtbot, source, config, parts)

    results = window.result_panel()
    assert not results.is_unfinished()
    outcome = results.outcome()
    assert outcome is not None
    assert outcome.rows == 3
    assert outcome.output == parts
    assert (parts / "checkpoint.json").is_file()


def test_a_stopped_checkpointed_run_points_at_the_parts(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**A checkpointed run leaves no ``.tmp`` to find, and it still stopped.**

    Every part is complete by construction and is renamed into place the moment it is full,
    so a ``.tmp`` exists only for the instant a part is being written -- and a run stopped
    between parts leaves none at all. What says it did not finish is the manifest's
    ``complete`` flag, which is the same thing the CLI reads for its own ``output_complete``
    in this mode. Before this, a stopped checkpointed run was reported as a plain failure
    with an exit code and nothing else.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    parts = tmp_path / "parts"
    panel = window.execution_panel()
    panel._checkpoint.setValue(2)
    panel._source.setText(str(slow_document))
    panel._config.setText(str(config))
    panel._output.setText(str(parts))

    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)
    # The manifest is written as parts are committed, so this is the run being under way.
    wait_until(qtbot, (parts / "checkpoint.json").is_file, timeout_ms=30_000)
    assert _terminate(panel._process.pid), "the child could not be killed"
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)

    results = window.result_panel()
    assert results.is_unfinished(), "a stopped checkpointed run was reported as a failure"
    left = results.left()
    assert left is not None
    assert left.path == parts
    assert left.is_directory
    assert left.recorded_parts is not None and left.recorded_parts >= 1
    assert left.recorded_rows is not None and left.recorded_rows >= 1
    detail = results.detail_text()
    assert "part" in detail and str(left.recorded_parts) in detail
    assert window.error_panel().failure() is None


def test_a_complete_manifest_is_not_reported_as_stopped(tmp_path: pathlib.Path) -> None:
    """The other half: a manifest that says the source was consumed is not an interruption,
    however many parts are sitting there."""
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "part-00000.csv").write_text("id,name\n1,a\n", encoding="utf-8")
    (parts / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": {"path": "x.xml", "size": 1, "sha256": "0" * 64},
                "config": "0" * 64,
                "records_consumed": 1,
                "rejected": 0,
                "parts": [{"name": "part-00000.csv", "rows": 1}],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )

    assert left_behind(parts, checkpointing=True) is None


def test_a_manifest_that_says_incomplete_is_reported_as_stopped(tmp_path: pathlib.Path) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "checkpoint.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": {"path": "x.xml", "size": 1, "sha256": "0" * 64},
                "config": "0" * 64,
                "records_consumed": 7,
                "rejected": 0,
                "parts": [
                    {"name": "part-00000.csv", "rows": 2},
                    {"name": "part-00001.csv", "rows": 2},
                ],
                "complete": False,
            }
        ),
        encoding="utf-8",
    )

    found = left_behind(parts, checkpointing=True)

    assert found is not None
    assert found.path == parts
    assert found.recorded_parts == 2
    assert found.recorded_rows == 4


def test_a_manifest_that_cannot_be_read_is_not_a_finished_run(tmp_path: pathlib.Path) -> None:
    """Truncated by a crash, or newer than this build: something is there and it is not a
    finished run, which is what the panel needs to know."""
    parts = tmp_path / "parts"
    parts.mkdir()
    (parts / "checkpoint.json").write_text("{ not json", encoding="utf-8")

    found = left_behind(parts, checkpointing=True)

    assert found is not None
    assert found.path == parts
    assert found.recorded_parts is None, "nothing could be read, so nothing is claimed"


def test_a_stopped_checkpointed_run_with_no_manifest_falls_back_to_the_part_in_flight(
    tmp_path: pathlib.Path,
) -> None:
    parts = tmp_path / "parts"
    parts.mkdir()
    in_flight = write(parts / f"part-00000.csv{PARTIAL_SUFFIX}", "id,name\n")

    found = left_behind(parts, checkpointing=True)

    assert found is not None and found.path == in_flight


def test_a_checkpointed_run_that_failed_is_shown_as_a_failure(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A failed checkpointed run names its kind rather than being called a stop."""
    source, _ = documents(tmp_path)
    wrong = write(
        tmp_path / "wrong.yaml", "record: /nope/nothing\nfields:\n  id:\n    path: '@id'\n"
    )
    parts = tmp_path / "parts"
    window.execution_panel()._checkpoint.setValue(2)

    run(window, qtbot, source, wrong, parts)

    failure = window.error_panel().failure()
    assert failure is not None, "a failed checkpointed run said nothing"
    assert failure.error_type == "RecordPathError"
    assert window.result_panel().isHidden(), "a failure was also reported as a stop"


def test_a_failure_after_a_stopped_run_is_still_a_failure(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The checkpoint analogue of a stale report.**

    A stopped checkpointed run leaves a manifest saying the source was not consumed, and
    nothing removes it. A later run to the same output finds it there and **refuses** --
    running into a checkpoint would throw away work already committed -- so it fails without
    writing a report of its own. Before this, that refusal was reported as "this run did not
    finish", which is the opposite of what happened: the run failed *because* it would not
    discard the earlier one.

    The run's own stderr is what tells the two apart, and ``warnings`` is stderr without the
    progress events, so a run stopped from outside -- which really is a stop -- has none.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    wrong = write(
        tmp_path / "wrong.yaml", "record: /nope/nothing\nfields:\n  id:\n    path: '@id'\n"
    )
    parts = tmp_path / "parts"
    window.execution_panel()._checkpoint.setValue(2)

    start_and_stop(window, qtbot, slow_document, config, parts, checkpointing=True)
    assert window.result_panel().is_unfinished()
    assert (parts / "checkpoint.json").is_file(), "no manifest, so nothing to go stale"

    run(window, qtbot, documents(tmp_path)[0], wrong, parts)

    failure = window.error_panel().failure()
    assert failure is not None, "the stale manifest was taken for this run's answer"
    assert "checkpoint already exists" in failure.message, failure.message
    assert window.result_panel().isHidden()


# --- the panel on its own -----------------------------------------------------


def test_the_panel_is_empty_until_a_run_ends(app: QApplication) -> None:
    del app
    panel = ResultPanel()

    assert panel.outcome() is None
    assert panel.left() is None
    assert not panel.is_unfinished()
    assert panel.headline_text() == ""
    assert panel.isHidden()


def test_a_stopped_run_with_nothing_written_still_says_something(app: QApplication) -> None:
    del app
    panel = ResultPanel()

    panel.show_unfinished(None)

    assert panel.is_unfinished()
    assert panel.is_warning()
    assert panel.headline_text()
    assert panel.detail_text()
    assert panel.copy_text() == "", "nothing to copy, so no button offering to"


def test_a_partial_of_zero_bytes_is_described_as_that(tmp_path: pathlib.Path) -> None:
    """A run stopped before the writer's first flush is a different thing from one stopped
    mid-write, and "0 bytes of it" reads like a mistake rather than a fact."""
    empty = write(tmp_path / f"out.csv{PARTIAL_SUFFIX}", "")

    panel = ResultPanel()
    panel.show_unfinished(LeftBehind(path=empty, recorded_rows=None, recorded_parts=None))

    assert "0 bytes" not in panel.detail_text()
    assert "before anything was written" in panel.detail_text()


def test_a_partial_with_bytes_says_how_many(tmp_path: pathlib.Path) -> None:
    partial = write(tmp_path / f"out.csv{PARTIAL_SUFFIX}", "x" * 2048)

    panel = ResultPanel()
    panel.show_unfinished(LeftBehind(path=partial, recorded_rows=None, recorded_parts=None))

    assert "2 KB" in panel.detail_text()
    assert str(partial) in panel.path_text()


def test_the_elapsed_time_reads_in_the_unit_that_fits() -> None:
    assert format_elapsed(0.004) == "4 ms"
    assert format_elapsed(2.5) == "2.5 s"
    assert format_elapsed(None) is None


def test_a_count_of_one_is_not_a_count_of_ones() -> None:
    assert count_of(1, "row") == "1 row"
    assert count_of(0, "row") == "0 rows"
    assert count_of(1234, "record") == "1,234 records"


def _hold_exclusively(path: pathlib.Path):  # noqa: ANN202 - a Win32 handle
    """Open a file with no sharing, so the CLI cannot move onto it.

    Windows-only, and skipped elsewhere rather than faked: the failure this produces is a
    Windows one, and a test that pretended otherwise would be testing the mock.
    """
    import ctypes
    from ctypes import wintypes

    if sys.platform != "win32":
        pytest.skip("exclusive file locking is a Windows behaviour")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        pytest.skip(f"could not take an exclusive handle: {ctypes.get_last_error()}")

    class Holder:
        def close(self) -> None:
            kernel32.CloseHandle(handle)

    return Holder()


def test_closing_the_window_stops_a_run_that_is_in_flight(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**A window that closes mid-run used to leave the child reading pipes.**

    Qt does not deliver ``closeEvent`` to a child widget, so the panels are shut down from
    the window's own handler. This panel's ``shutdown`` discarded the temporary config and
    nothing else, so the child, its two reader threads and the pump all outlived the window
    -- the same defect the other two panels had, in the panel that starts the most children.
    Those reader threads are what the CI segfault's traceback shows still in their loops.
    """
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "out.csv"
    panel = window.execution_panel()
    panel._source.setText(str(slow_document))
    panel._config.setText(str(config))
    panel._output.setText(str(output))

    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)

    window.close()

    assert not panel.is_running(), "the child outlived the window"
    assert not panel._pump.isActive(), "the pump outlived the window"


def test_the_panel_closed_on_its_own_also_stops_its_run(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The other way in, for the same reason as the window's: a caller that closes a panel
    directly has to get the same work done."""
    config = write(tmp_path / "config.yaml", CONFIG)
    output = tmp_path / "out.csv"
    panel = window.execution_panel()
    panel._source.setText(str(slow_document))
    panel._config.setText(str(config))
    panel._output.setText(str(output))

    panel.start()
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)

    panel.close()

    assert not panel.is_running(), "closing the panel left its child running"
    assert not panel._pump.isActive(), "closing the panel left its pump running"
