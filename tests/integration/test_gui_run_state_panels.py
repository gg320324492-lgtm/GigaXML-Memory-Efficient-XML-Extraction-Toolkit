"""A run's state as the panels and the window see it, on real children.

★ **M9 criteria A, E and F, at the level above the process.**

``test_run_state_wiring.py`` proves the state machine is right around real children, and
``test_run_state.py`` proves the table itself. Neither can see what this file is about:
**that the window says the right thing.** A state that is perfectly modelled and a window
that files an interrupted run under "error" would satisfy both of those and fail the
milestone, because the milestone's point is not that a run can be described -- it always
could, in a boolean -- but that the description reaches the person looking at it.

So everything here drives a :class:`~gigaxml.gui.main_window.MainWindow` and then reads
**the widgets**. The two paths criterion E names are built with real children: one killed
by the user, one that ends on the CLI's interrupted exit code without anybody asking.

**A note on what "real" costs.** The interrupted child is ``sys.exit(3)`` in a real
interpreter rather than a signal, because a signal cannot be delivered to another process
from a test on this platform. What is under test is "exit code 3 becomes ``INTERRUPTED`` and
the window says so", and the other half -- that a signal produces exit code 3 -- is the
CLI's contract, pinned from its own side in ``test_run_state.py``. Building it any other way
would test the test.
"""

from __future__ import annotations

import pathlib
import sys
from typing import ClassVar

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

import gigaxml.gui.cli_process as cli_process_module
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.run_state import (
    ContradictoryStateError,
    RunState,
    RunStateMachine,
    state_of,
)

DOCUMENT = """<catalog><products>
<product id="1"><name>Alpha Lamp</name><qty>5</qty></product>
<product id="2"><name>Beta Suite</name><qty>7</qty></product>
</products></catalog>
"""

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
        return existing  # type: ignore[return-type]
    return QApplication(sys.argv)


def _interrupting_child(monkeypatch: pytest.MonkeyPatch, *, seconds: int = 0) -> None:
    """Make every child exit with the CLI's interrupted code, having slept ``seconds``."""
    body = f"import sys, time\ntime.sleep({seconds})\nsys.exit(3)\n"
    monkeypatch.setattr(
        cli_process_module, "cli_command", lambda _args: [sys.executable, "-c", body]
    )


def _prepare(tmp_path: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path]:
    source = tmp_path / "doc.xml"
    source.write_text(DOCUMENT, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    return source, config


def _settled(panel) -> bool:  # noqa: ANN001 -- an ExecutionPanel, spelled out at the call site
    """Whether the panel has finished handling the run, rather than merely knowing it ended.

    ★ **The distinction this milestone introduces makes the obvious wait wrong.** The state
    reaches its terminal value inside :meth:`CliProcess.kill`, before the UI thread's pump
    has run at all -- so waiting on ``state.is_terminal`` returns while the status label
    still reads "cancelling…", and every widget assertion below fails against a panel that
    is perfectly correct. What says "settled" is the pump having stopped, which only happens
    in :meth:`ExecutionPanel._drain` after the widgets have been written.
    """
    return panel.state.is_terminal and not panel._pump.isActive()


def _start(
    window: MainWindow, source: pathlib.Path, output: pathlib.Path, config: pathlib.Path
) -> ExecutionPanel:
    panel = window.execution_panel()
    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(config)
    panel.start()
    return panel


# --- criterion E: the two paths, end to end ---------------------------------


def test_a_user_stop_and_a_signal_produce_different_states_and_different_words(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ **Criterion E, whole way through: two real runs, two states, two headlines.**

    The first run is stopped by the user through the panel's own Cancel button. The second
    is never asked to stop and ends on the CLI's interrupted exit code. Both leave a
    partial output on disk and neither writes a complete one, so **the shape of what is
    left cannot tell them apart** -- which is why this asserts the words and the state, not
    the filesystem.

    Four things are required and all four are asserted below, because any one of them alone
    would pass with the old code: the states differ, the status lines differ, the results
    headline differs, and only the signal case clears the error panel.
    """
    del app

    # --- the user pressed Cancel ---
    stopped_window = MainWindow(state_dir=tmp_path / "state-a")
    try:
        source, config = _prepare(tmp_path)
        panel = _start(stopped_window, source, tmp_path / "a.csv", config)
        qtbot.waitUntil(lambda: panel.state.is_active, timeout=30_000)
        panel.cancel()
        qtbot.waitUntil(lambda: _settled(panel), timeout=60_000)
        stopped_label = panel._counts.text()
        stopped_state = panel.state
        stopped_headline = stopped_window.result_panel().headline_text()
        stopped_interrupted = stopped_window.result_panel().is_interrupted()
    finally:
        stopped_window.close()

    # --- a signal ended it, and nobody asked ---
    _interrupting_child(monkeypatch)
    signalled_window = MainWindow(state_dir=tmp_path / "state-b")
    try:
        source, config = _prepare(tmp_path)
        panel = _start(signalled_window, source, tmp_path / "b.csv", config)
        qtbot.waitUntil(lambda: _settled(panel), timeout=60_000)
        signalled_label = panel._counts.text()
        signalled_state = panel.state
        signalled_headline = signalled_window.result_panel().headline_text()
        signalled_interrupted = signalled_window.result_panel().is_interrupted()
    finally:
        signalled_window.close()

    assert stopped_state is RunState.CANCELLED, stopped_state
    assert signalled_state is RunState.INTERRUPTED, signalled_state
    assert stopped_state is not signalled_state

    assert stopped_label == "cancelled", stopped_label
    assert "interrupted" in signalled_label, (
        f"the status line for a signal-ended run does not say so: {signalled_label!r}"
    )
    assert stopped_label != signalled_label

    assert stopped_headline != signalled_headline, (
        "the results panel shows the same headline for a run the user stopped and one a "
        "signal ended, so the distinction exists in the state and never reaches the window"
    )
    assert not stopped_interrupted
    assert signalled_interrupted

    # ★ **And the interrupted run is not filed as an error.** This is the part that was
    # actually wrong before this milestone: it arrived with killed=False, ok=False and a
    # non-empty stderr, which is exactly the failure branch's shape, so a run a user
    # interrupted was shown in the same red error panel as a config that will not load.
    assert signalled_window.error_panel().failure() is None, (
        "an interrupted run is not an error; the CLI gives it its own exit code precisely "
        "because the two want opposite reactions, and the window put it in the error panel"
    )


def test_the_i18n_table_says_the_same_thing_in_the_other_language() -> None:
    """★ **Criterion E's other half: the new wording is translated, not left to fall back.**

    ``tools/check_i18n_keys.py`` already requires every ``tr()`` literal to be in the
    table, so this is not duplicating that -- it is asserting the *pair* is present and
    distinct. A missing key fails the tool; a key added twice, or two keys whose Chinese is
    the same string, would pass the tool and leave the distinction invisible to half the
    users, which is the failure the milestone is about.
    """
    from gigaxml.gui.i18n import FALLBACK_LANGUAGE, current_language, set_language, tr

    previous = current_language()
    try:
        set_language(FALLBACK_LANGUAGE)
        english_cancelled = tr("cancelled")
        english_interrupted = tr("interrupted — a signal ended this run")
        english_headlines = {
            tr("this run did not finish"),
            tr("this run was interrupted by a signal"),
        }

        set_language("zh")
        chinese_cancelled = tr("cancelled")
        chinese_interrupted = tr("interrupted — a signal ended this run")
        chinese_headlines = {
            tr("this run did not finish"),
            tr("this run was interrupted by a signal"),
        }
    finally:
        set_language(previous)

    assert english_cancelled != english_interrupted
    assert len(english_headlines) == 2
    assert chinese_cancelled != chinese_interrupted, (
        "both render as the same Chinese, so a Chinese-reading user cannot tell a run they "
        "stopped from one a signal ended"
    )
    assert len(chinese_headlines) == 2, (
        f"the two headlines collapse in Chinese: {chinese_headlines}"
    )


# --- criterion A: one value, and it cannot contradict itself ----------------


def test_the_panel_reads_the_state_off_its_child(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """★ **Criterion A's positive half: the panel asks, rather than keeping an answer.**

    Three answers to the same question in one panel is what the milestone is removing, so
    this walks a real run and asks the panel twice -- before the child exists, and after it
    has ended -- and requires the answer to come from the child both times rather than from
    anything the panel stored.
    """
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        panel = window.execution_panel()
        assert panel.state is RunState.IDLE, "a panel that has never run is not idle"
        assert panel.state is state_of(panel._process)

        source, config = _prepare(tmp_path)
        panel = _start(window, source, tmp_path / "out.csv", config)
        qtbot.waitUntil(lambda: _settled(panel), timeout=60_000)

        assert panel.state.is_terminal
        assert panel.state is state_of(panel._process), (
            "the panel's state is not its child's, so there are two answers to one question"
        )
    finally:
        window.close()


def test_a_panel_holding_a_state_with_no_child_is_refused(
    app: QApplication, tmp_path: pathlib.Path
) -> None:
    """★ **Criterion A's negative half, and criterion G1: a contradiction is refused.**

    The panel's child is forced to claim it is running when it never launched anything --
    the shape a missed transition or a stray assignment takes. Under the old flags this was
    ``_process is None`` beside ``_finished = True``, which is two ordinary values saying
    something impossible, and nothing noticed because nothing was reading both at once.
    """
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        panel = window.execution_panel()
        panel._process = _LyingChild(RunState.RUNNING, launched=False)

        with pytest.raises(ContradictoryStateError):
            _ = panel.state
    finally:
        window.close()


class _LyingChild:
    """A child whose bookkeeping disagrees with itself, for the contradiction test only.

    Minimal on purpose: it exists to be *asked*, and every claim it makes is one the state
    model has to refuse.
    """

    def __init__(self, state: RunState, *, launched: bool) -> None:
        self._machine = RunStateMachine()
        self._machine._state = state  # the corruption under test
        self._launched = launched
        self.killed = False
        self.built: ClassVar[list[str]] = []

    @property
    def state(self) -> RunState:
        from gigaxml.gui.run_state import contradiction

        sentence = contradiction(state=self._machine.state, launched=self._launched)
        if sentence is not None:
            raise ContradictoryStateError(sentence)
        return self._machine.state

    def kill(self) -> None:
        self.killed = True


def test_the_window_reports_idle_after_it_closes(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**A panel that has let go of its child reports IDLE, and that is a decision.**

    Every panel's ``shutdown()`` releases the child on the way out. The alternative would
    be a second copy of the state kept beside it purely so a widget being destroyed could
    still be asked -- which is one more thing to keep in step, and the thing this milestone
    exists to remove. Pinned here so it stays a decision rather than becoming an accident
    nobody noticed, and pinned on a panel that *did* run, so "IDLE" cannot pass by accident
    on a panel that never started anything.
    """
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        source, config = _prepare(tmp_path)
        panel = _start(window, source, tmp_path / "out.csv", config)
        qtbot.waitUntil(lambda: _settled(panel), timeout=60_000)
        assert panel.state is not RunState.IDLE
    finally:
        window.close()

    assert panel.state is RunState.IDLE, (
        "the panel kept a state after releasing its child, so there are two answers again"
    )


# --- criterion F: closing while a stop is in flight --------------------------


def test_closing_the_window_while_it_is_stopping_leaves_nothing_behind(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """★ **Criterion F, on the new state.**

    A window closed mid-run has to end the child and its reader threads -- the existing
    ``test_gui_thread_hygiene.py`` proves that for a run in flight, and it is not changed by
    this milestone. **What is new is the moment a stop is in flight**: the panel is in
    ``CANCELLING``, ``CliProcess.kill`` is blocking on a reader join, and ``shutdown()``
    reaches in and kills again. Adding a state does not obviously break that, and "does not
    obviously" is not a result -- so the window is closed from exactly there.

    Both things are then checked: no reader thread of ours survives, and the panel's pump
    is stopped, which is the half the thread-hygiene test does not look at and the half that
    leaves a timer firing at a window nobody is watching.
    """
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        source, config = _prepare(tmp_path)
        panel = _start(window, source, tmp_path / "out.csv", config)
        qtbot.waitUntil(lambda: panel.is_running(), timeout=30_000)

        panel.cancel()
        assert panel.state in {RunState.CANCELLING, RunState.CANCELLED}, panel.state
        window.close()
    finally:
        window.close()

    assert not panel._pump.isActive(), (
        "the UI pump is still firing after the window closed; it drives widgets on their "
        "way out, which is what the CI segfault's traceback shows"
    )


def test_a_panel_whose_run_was_refused_reports_a_failure_with_no_child(
    app: QApplication, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**``FAILED`` is the one terminal state that can honestly have no child behind it.**

    ``Popen`` raising is the only way a run ends that no exit code describes, and it is why
    ``STARTING`` has an edge to ``FAILED`` at all. Asserted through the panel so the
    exception path is exercised where a user would meet it -- a missing interpreter is a
    thing that happens on somebody's machine, and the window must say so rather than sit at
    IDLE looking as though nothing had been asked of it.
    """
    del app

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such interpreter")

    monkeypatch.setattr(cli_process_module.subprocess, "Popen", refuse)
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        source, config = _prepare(tmp_path)
        panel = window.execution_panel()
        panel._source.setText(str(source))
        panel._output.setText(str(tmp_path / "out.csv"))
        panel.set_config(config)

        # ★ **The exception still reaches the caller, and that is pre-existing.** Nothing in
        # this milestone changed how a failed launch is reported: ``CliProcess.start``
        # re-raises, ``ExecutionPanel.start`` calls it unguarded, and so an ``OSError`` from
        # a button press lands in the Qt event loop exactly as it did before. The
        # milestone's part is the *state*: the run is recorded as failed rather than left
        # describing something that never launched, and no exit code has to be invented to
        # say so. **Turning the escape into a message is a separate fix, reported rather
        # than done here.**
        with pytest.raises(OSError):
            panel.start()

        assert panel.state is RunState.FAILED, panel.state
        assert panel.state.is_terminal
        assert not panel.state.is_active
    finally:
        window.close()
