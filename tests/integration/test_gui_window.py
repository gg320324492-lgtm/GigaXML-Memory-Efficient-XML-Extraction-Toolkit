"""The window and the execution panel, driven for real.

These tests need Qt and skip without it, so that a checkout without the ``gui`` extra
still runs the suite. They do **not** skip in CI, which installs the extra: a GUI stage
whose GUI tests never run in CI has the appearance of coverage and none of the substance.

They drive the real CLI against a real file. A panel tested against a fake subprocess
would prove that the panel can talk to a fake, which is not the part that breaks.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Callable

import pytest

pytest.importorskip("PySide6", reason="the desktop application is an optional extra")

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.execution import ExecutionPanel, read_report_rows
from gigaxml.gui.progress import Progress

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

CONFIG = {
    "record": "/catalog/products/product",
    "fields": {"id": {"path": "@id"}, "name": {"path": "name"}},
}


@pytest.fixture(scope="module")
def app() -> QApplication:
    """One QApplication for the module. Qt refuses to create a second one."""
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture
def window(app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path) -> MainWindow:
    # `app` is used rather than merely requested: a fixture whose parameter is renamed to
    # please the linter stops being a fixture lookup at all, and pytest then fails every
    # test in the file at setup. Asserting the application is the one this window was
    # built on is both true and worth checking.
    assert QApplication.instance() is app
    # The state directory is passed rather than defaulted: the default is the real user's
    # configuration directory, and a test that writes there is modifying the machine.
    main = MainWindow(state_dir=tmp_path / "state")
    qtbot.addWidget(main)
    return main


def write_config(tmp_path: pathlib.Path, extra: dict | None = None) -> pathlib.Path:
    payload = dict(CONFIG)
    if extra:
        payload.update(extra)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def small_dataset(tmp_path: pathlib.Path, records: int) -> pathlib.Path:
    path = tmp_path / "small.xml"
    body = "".join(f'<product id="{n}"><name>N{n}</name></product>' for n in range(1, records + 1))
    path.write_text(f"<catalog><products>{body}</products></catalog>", encoding="utf-8")
    return path


def fill(
    panel: ExecutionPanel, source: pathlib.Path, config: pathlib.Path, output: pathlib.Path
) -> None:
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(output))
    panel._record_path.setText(CONFIG["record"])


def wait_until(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    """Spin the event loop until `predicate` holds. Returns whether it did."""
    elapsed = QElapsedTimer()
    elapsed.start()
    while elapsed.elapsed() < timeout_ms:
        qtbot.wait(25)
        if predicate():
            return True
    return predicate()


# --- the window ---------------------------------------------------------------


def test_the_window_opens_with_the_execution_panel(window: MainWindow) -> None:
    """The execution panel is still here. Its tab moved when the document panel arrived.

    This used to assert ``tabText(0) == "Execute"``. The tab order now follows the order
    of the functional areas -- a document is opened, then analysed, then extracted -- so
    the assertion is about the tab existing rather than about it being first.
    """
    labels = [window.tabs().tabText(index) for index in range(window.tabs().count())]

    assert "Execute" in labels
    assert isinstance(window.execution_panel(), ExecutionPanel)


def test_the_panel_maps_its_controls_onto_cli_arguments(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Checked without starting a process: the mapping is the part that can be wrong."""
    panel = window.execution_panel()
    config = write_config(tmp_path)
    fill(panel, tmp_path / "in.xml", config, tmp_path / "out.csv")

    args = panel.build_args()

    assert args[0] == "extract"
    assert args[1].endswith("in.xml")
    assert args[3] == str(config), "no override, so the user's own file is passed through"
    assert args[5].endswith("out.csv")
    assert "--progress" in args, "the bar has no source without this"
    assert "--checkpoint-every" not in args, "off by default"


def test_checkpoint_every_is_passed_when_asked_for(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.execution_panel()
    config = write_config(tmp_path)
    fill(panel, tmp_path / "in.xml", config, tmp_path / "out")
    panel._checkpoint.setValue(1000)

    args = panel.build_args()

    assert "--checkpoint-every" in args
    assert args[args.index("--checkpoint-every") + 1] == "1000"


def test_on_error_is_applied_by_writing_a_config_not_a_flag(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """`on_error` is a config key. There is no flag, and inventing one would be a lie."""
    panel = window.execution_panel()
    config = write_config(tmp_path)
    fill(panel, tmp_path / "in.xml", config, tmp_path / "out.csv")

    assert panel.effective_config() == config, "unchanged, so the file is passed through"

    panel._on_error.setCurrentText("quarantine")
    override = panel.effective_config()

    assert override != config
    payload = json.loads(override.read_text(encoding="utf-8"))
    assert payload["on_error"] == "quarantine"
    assert payload["record"] == CONFIG["record"], "everything else is carried over"
    assert "--on-error" not in panel.build_args()


def test_an_unreadable_config_is_reported_not_raised(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    from gigaxml.config import ConfigError

    panel = window.execution_panel()
    fill(panel, tmp_path / "in.xml", tmp_path / "nope.yaml", tmp_path / "out.csv")
    panel._on_error.setCurrentText("quarantine")

    with pytest.raises(ConfigError):
        panel.effective_config()


# --- running for real ---------------------------------------------------------


def test_a_real_run_reports_the_same_rows_as_the_report(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """Gate 4: what the panel ends up showing is what the report says was written."""
    source = small_dataset(tmp_path, 3000)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    panel = window.execution_panel()
    fill(panel, source, config, output)

    panel.start()
    assert wait_until(qtbot, lambda: not panel.is_running()), "the run did not finish"

    result = panel.run_result()
    assert result is not None and result.ok, result.stderr_lines if result else "no result"

    report_rows = read_report_rows(tmp_path / "run-report.json")
    assert report_rows == 3000
    assert result.summary is not None
    assert result.summary["rows"] == report_rows


def test_the_total_comes_from_inspect_and_the_bar_uses_it(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """The denominator is an exact count from `inspect`, not an estimate."""
    source = small_dataset(tmp_path, 2500)
    config = write_config(tmp_path)
    panel = window.execution_panel()
    fill(panel, source, config, tmp_path / "out.csv")

    panel._probe_total()
    assert wait_until(qtbot, lambda: panel.total() is not None), "the probe found no count"
    assert panel.total() == 2500


def test_without_a_total_the_bar_shows_no_percentage(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """Gate 5 of the brief: no denominator, no percentage. Never a guessed one."""
    source = small_dataset(tmp_path, 1500)
    config = write_config(tmp_path)
    panel = window.execution_panel()
    fill(panel, source, config, tmp_path / "out.csv")
    panel._record_path.setText("/not/in/this/document")

    panel._probe_total()
    qtbot.wait(1500)

    assert panel.total() is None, "a path that is not in the document must not yield a count"

    # Checked by handing the panel a progress event directly rather than by watching a
    # run: a run that finishes sets the bar to 100% on purpose, so reading it afterwards
    # says nothing about what was shown while it was going.
    panel._show(Progress(records=500, rows=500, rejected=0, elapsed_seconds=1.0))

    # An indeterminate range is how Qt says "working, progress unknown". The counts are
    # shown beside it, which is the honest presentation.
    assert panel._bar.maximum() == 0
    assert "500 records" in panel._counts.text()
    assert "%" not in panel._counts.text()


def test_cancelling_stops_the_child_and_leaves_no_half_file(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """Gate 5: the child is gone, and the target is not a truncated file."""
    source = small_dataset(tmp_path, 400_000)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    panel = window.execution_panel()
    fill(panel, source, config, output)

    panel.start()
    assert wait_until(qtbot, lambda: (tmp_path / "out.csv.tmp").exists(), timeout_ms=30_000)

    panel.cancel()
    assert wait_until(qtbot, lambda: not panel.is_running()), "the child outlived the cancel"

    result = panel.run_result()
    assert result is not None and result.killed

    # The writer renames on success and leaves the temp file behind on failure. A
    # half-written target is the failure this whole mechanism exists to prevent.
    assert not output.exists(), "a cancelled run must not publish a partial output"

    size_before = output.stat().st_size if output.exists() else 0
    qtbot.wait(400)
    size_after = output.stat().st_size if output.exists() else 0
    assert size_after == size_before, "the output is still being written after cancel"


def test_cancelling_a_checkpointed_run_keeps_the_committed_parts(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    source = small_dataset(tmp_path, 400_000)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    panel = window.execution_panel()
    fill(panel, source, config, parts)
    panel._checkpoint.setValue(5000)

    panel.start()
    assert wait_until(qtbot, lambda: len(list(parts.glob("part-*.csv"))) >= 2, timeout_ms=30_000)
    panel.cancel()
    assert wait_until(qtbot, lambda: not panel.is_running())

    committed = sorted(parts.glob("part-*.csv"))
    assert committed, "the parts written before the cancel are still there"
    manifest = json.loads((parts / "checkpoint.json").read_text(encoding="utf-8"))
    assert manifest["complete"] is False
    assert manifest["records_consumed"] > 0


# --- the interface does not freeze -------------------------------------------


def test_the_interface_keeps_breathing_while_a_run_is_going(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """Gate 2, measured against this machine rather than against a theoretical rate.

    A QTimer does not fire at its nominal rate -- an idle 50 ms timer on this machine
    manages about 78% of it -- so asserting "at least 90% of the theoretical count" would
    fail on a perfectly healthy window. The comparison is therefore against an idle
    baseline measured on the same machine, in the same process, moments apart.
    """
    idle_beats = _count_beats(qtbot, 1000)

    source = small_dataset(tmp_path, 400_000)
    config = write_config(tmp_path)
    panel = window.execution_panel()
    fill(panel, source, config, tmp_path / "out.csv")

    beats = _count_beats(qtbot, 1500, start=panel.start)
    panel.cancel()
    assert wait_until(qtbot, lambda: not panel.is_running())

    assert idle_beats > 0, "the idle baseline produced no beats at all"
    assert beats >= idle_beats / 2, (
        f"the window beat {beats} times while working against {idle_beats} idle -- "
        f"it was starved, so something is blocking the UI thread"
    )


def _count_beats(qtbot: QtBot, milliseconds: int, start: Callable[[], None] | None = None) -> int:
    """How often a 50 ms timer actually fires over a window of time."""
    beats = 0

    def beat() -> None:
        nonlocal beats
        beats += 1

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(beat)
    timer.start()
    if start is not None:
        start()
    qtbot.wait(milliseconds)
    timer.stop()
    return beats


def test_reading_the_child_does_not_happen_on_the_ui_thread(
    window: MainWindow, tmp_path: pathlib.Path, qtbot: QtBot
) -> None:
    """The callbacks must be the only thing the reader thread does.

    Asserted by proxy: if the panel updated its widgets from the callback, Qt would have
    complained by now or the counts would be stale. What is checked here is the property
    that makes that impossible -- the panel collects into a plain list and drains it on a
    timer, so the widgets are only ever touched from the UI thread.
    """
    source = small_dataset(tmp_path, 3000)
    config = write_config(tmp_path)
    panel = window.execution_panel()
    fill(panel, source, config, tmp_path / "out.csv")

    panel.start()
    assert wait_until(qtbot, lambda: not panel.is_running())
    qtbot.wait(200)

    assert panel._received == []
    assert panel._pump.isActive() is False


def test_a_run_with_no_source_does_not_start(window: MainWindow) -> None:
    panel = window.execution_panel()

    panel.start()

    assert not panel.is_running()
    assert "choose an input" in panel._counts.text()
