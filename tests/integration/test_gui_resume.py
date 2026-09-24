"""⑦ resuming: saying there is something to resume, and showing why it was refused.

Both halves need a **real child and a real checkpoint**: a stopped run is what leaves a
manifest that is not complete, and the refusal is the CLI's own -- a message written by hand
here would prove nothing about whether the tool's own words reach the screen.
"""

from __future__ import annotations

import ctypes
import pathlib
import sys

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.error_advice import KIND_CHECKPOINT
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.run_report import failure_from_stderr

CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
  name:
    path: name
"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture(scope="module")
def slow_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Big enough that the run is still going when it is stopped.

    Built here rather than taken from ``data/``: that directory is generated on demand and
    not committed, so a test that needed one of its files would be testing something
    different on a machine that has never run the benchmarks.
    """
    path = tmp_path_factory.mktemp("slow") / "slow.xml"
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
    return MainWindow(state_dir=tmp_path / "state")


def wait_until(qtbot: QtBot, predicate, timeout_ms: int = 60_000) -> bool:  # noqa: ANN001
    qtbot.waitUntil(predicate, timeout=timeout_ms)
    return predicate()


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
    wait_until(qtbot, panel.is_running, timeout_ms=30_000)
    wait_until(qtbot, (parts / "checkpoint.json").is_file, timeout_ms=30_000)
    assert _terminate(panel._process.pid), "the child could not be killed"
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=60_000)
    qtbot.wait(300)


def run_to_completion(window: MainWindow, qtbot: QtBot) -> None:
    panel = window.execution_panel()
    panel.start()
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=120_000)
    qtbot.wait(300)


# --- G1: saying there is something to resume ----------------------------------


def test_an_unfinished_checkpoint_is_offered(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The core of this round.** A stopped run leaves a manifest saying the source was not
    consumed, and until now nothing in the panel mentioned it -- the user had parts on disk
    and no way to know the tool would continue them."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    leave_a_checkpoint(window, qtbot, slow_document, config, parts)

    panel = window.execution_panel()
    panel._output.editingFinished.emit()

    assert panel.is_resume_offered(), "the panel said nothing about the run on disk"
    notice = panel.resume_notice_text()
    assert "unfinished run" in notice, notice
    assert "Resume" in notice, notice
    # Where it stopped, from the manifest rather than from counting files here.
    unfinished = panel.unfinished_run_here()
    assert unfinished is not None
    # "part" rather than "parts": the notice agrees with the number, and the run
    # stopped early enough that there is often only one.
    assert f"{len(unfinished.parts):,} part" in notice, notice


def test_a_finished_run_is_not_offered_as_resumable(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A complete manifest is a run that finished, however many parts are sitting there."""
    source = tmp_path / "small.xml"
    source.write_text(
        '<catalog><products><product id="1"><name>A</name></product>'
        '<product id="2"><name>B</name></product></products></catalog>',
        encoding="utf-8",
    )
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    panel = window.execution_panel()
    panel._checkpoint.setValue(1)
    panel._source.setText(str(source))
    panel._config.setText(str(config))
    panel._output.setText(str(parts))

    run_to_completion(window, qtbot)

    assert panel.unfinished_run_here() is None
    assert not panel.is_resume_offered()


def test_an_empty_directory_is_not_offered_as_resumable(window: MainWindow) -> None:
    panel = window.execution_panel()
    panel._checkpoint.setValue(500)
    panel._output.setText("")

    assert panel.unfinished_run_here() is None
    assert not panel.is_resume_offered()


# --- G6: resuming is a choice, never a default --------------------------------


def test_resume_is_not_passed_unless_it_is_ticked(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """**Never turned on for the user.** Continuing somebody's earlier work is a decision,
    and a panel that made it for them would be running a different run than the one they
    asked for."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    panel = window.execution_panel()
    panel._config.setText(str(config))
    panel._checkpoint.setValue(500)

    assert "--resume" not in panel.build_args()

    panel.set_resume(True)
    assert "--resume" in panel.build_args()


def test_resume_is_not_passed_without_checkpointing(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The CLI refuses ``--resume`` without ``--checkpoint-every``, so offering it there
    would be offering an argument the run rejects."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    panel = window.execution_panel()
    panel._config.setText(str(config))
    panel.set_resume(True)

    assert "--resume" not in panel.build_args()
    assert not panel._resume.isEnabled()


# --- G3/G4: the refusal, in the tool's own words ------------------------------


def test_a_changed_source_is_shown_with_both_values(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**Both sha256 values, verbatim.** The CLI writes the message as a sentence followed
    by indented detail, and the detail is the part that can be acted on -- so a panel that
    kept only the first line would turn "here are the two values that differ" into "cannot
    resume", which is exactly what the message exists to avoid."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    leave_a_checkpoint(window, qtbot, slow_document, config, parts)

    # Same records, different bytes: the source is not the one the checkpoint recorded.
    with slow_document.open("a", encoding="utf-8") as handle:
        handle.write(" ")
    panel = window.execution_panel()
    panel.set_resume(True)

    run_to_completion(window, qtbot)

    failure = window.error_panel().failure()
    assert failure is not None, "the refusal was not reported at all"
    assert failure.error_type == "CheckpointError"
    assert failure.message.count("sha256") == 2, failure.message
    assert "source content:" in failure.message, failure.message
    assert "checkpoint:" in failure.message and "now:" in failure.message, failure.message


def test_a_changed_config_is_shown_with_both_values(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The other refusal the tool has, which names two config hashes instead."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    leave_a_checkpoint(window, qtbot, slow_document, config, parts)

    config.write_text(CONFIG.replace("path: name", "path: '@id'"), encoding="utf-8")
    panel = window.execution_panel()
    panel.set_resume(True)

    run_to_completion(window, qtbot)

    failure = window.error_panel().failure()
    assert failure is not None
    assert failure.error_type == "CheckpointError"
    assert "config:" in failure.message, failure.message
    assert failure.message.count("checkpoint:") == 1, failure.message
    assert failure.message.count("now:") == 1, failure.message


def test_the_refusal_gets_advice_that_does_not_repeat_it(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The advice says what to do about it; the message says what differs. Summarising the
    message would lose the two values, which are the only actionable part of it."""
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    leave_a_checkpoint(window, qtbot, slow_document, config, parts)

    config.write_text(CONFIG.replace("path: name", "path: '@id'"), encoding="utf-8")
    panel = window.execution_panel()
    panel.set_resume(True)

    run_to_completion(window, qtbot)

    errors = window.error_panel()
    assert errors.headline_text() == "Resuming was refused"
    assert "message below" in errors.detail_text().lower()


# --- the message, on its own --------------------------------------------------


def test_every_stderr_line_survives_not_just_the_first() -> None:
    """``failure_from_stderr`` used to keep the first non-empty line. For a one-line failure
    that is the whole message; for this one it is the sentence before the answer."""
    lines = [
        "error: cannot resume: the run would not be the same run.",
        "  config:",
        "    checkpoint: aaa",
        "    now:        bbb",
    ]

    failure = failure_from_stderr(lines, 1)

    assert failure.message.splitlines() == [
        "cannot resume: the run would not be the same run.",
        "  config:",
        "    checkpoint: aaa",
        "    now:        bbb",
    ]


def test_the_log_prefix_is_dropped_but_nothing_else_is() -> None:
    """The CLI writes ``error: `` in front of what it says; the report's ``error.message``
    has no prefix, so dropping it keeps the two sources saying the same thing. The
    indentation after it is part of the message and stays."""
    failure = failure_from_stderr(["error: first", "  indented"], 1)

    assert failure.message == "first\n  indented"


def test_an_empty_stderr_still_says_something() -> None:
    failure = failure_from_stderr([], 3)

    assert failure.message == "the run failed with exit code 3"


def test_the_refusals_action_copies_the_checkpoints_path(
    window: MainWindow, qtbot: QtBot, slow_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The advice says to point the panel back at the right pair, or remove the checkpoint
    to start over. Either way the user needs to know where the checkpoint is, and the
    button is what gives it to them -- **copied rather than removed**, because starting over
    means throwing away parts they may have spent an hour on.
    """
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"
    leave_a_checkpoint(window, qtbot, slow_document, config, parts)

    config.write_text(CONFIG.replace("path: name", "path: '@id'"), encoding="utf-8")
    panel = window.execution_panel()
    panel.set_resume(True)
    run_to_completion(window, qtbot)
    assert window.error_panel().advice().kind == KIND_CHECKPOINT
    QApplication.clipboard().setText("")

    window.error_panel().action_requested.emit(KIND_CHECKPOINT)

    assert str(parts) in QApplication.clipboard().text()
    assert parts.is_dir(), "the checkpoint was removed rather than pointed at"
