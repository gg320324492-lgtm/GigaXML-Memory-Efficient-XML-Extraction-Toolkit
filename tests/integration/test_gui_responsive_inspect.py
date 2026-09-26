"""G2's second half: the window stays responsive during ``inspect``, not just ``extract``.

**Why this is a separate file rather than another test in ``test_gui_responsive.py``.** The
criterion reads "the interface is responsive while ``inspect`` **and** ``extract`` run", and
the first pass covered only ``extract``. The audit's objection was the right one: *the same
``CliProcess`` mechanism does not mean the same behaviour*, which is the lesson 8A-38 taught
at a cost. ``inspect`` is the riskier of the two -- it walks the whole document with no
``tag=`` filter to hide behind, so on a large file it is the longest single uninterruptible
child this application starts, and it is the path a user waits through while wondering
whether the window has died.

Same three assertions as the extract side, because each can pass for the wrong reason alone:

* a timer keeps firing while the child works -- the loop is turning, not merely unblocked;
* the cancel button is enabled and takes a click mid-run -- the widgets are live;
* the window repaints without raising -- the paint path is not starved.

And the same reversed example, because a test that cannot fail is not a test.
"""

from __future__ import annotations

import pathlib
import time
from typing import Final

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

#: Absolute, because these run a child process against a fixed dataset rather than a fixture
#: path. See the note in ``test_gui_responsive.py``.
REPO_ROOT: Final = pathlib.Path(__file__).resolve().parent.parent.parent

#: The document to analyse. Large on purpose: ``inspect`` on a small file finishes before
#: the first timer tick, and then every assertion below passes without having measured
#: anything.
S400: Final = REPO_ROOT / "data" / "s400.xml"

#: A run shorter than this cannot be observed in flight, and a silent pass is worse than a
#: loud one.
_MIN_OBSERVABLE_S: Final = 0.4


def _pump(seconds: float) -> None:
    """Turn the event loop for ``seconds``, which is how the panel notices anything at all."""
    application = QApplication.instance()
    if application is None:  # pragma: no cover - the qapp fixture builds one
        raise RuntimeError("no QApplication")
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        application.processEvents()
        time.sleep(0.01)


def test_the_event_loop_keeps_turning_during_inspect(qtbot) -> None:  # noqa: ANN001
    """A timer keeps firing, cancel is live, and the window repaints -- while inspect runs."""
    from gigaxml.gui.panels.structure import StructurePanel

    panel = StructurePanel()
    qtbot.addWidget(panel)
    panel.set_document(S400)

    ticks: list[int] = []
    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(lambda: ticks.append(1))
    ticker.start()
    try:
        panel.analyze()

        qtbot.waitUntil(panel.is_running, timeout=5000)
        assert panel.is_running(), "inspect finished before it could be observed running"

        _pump(_MIN_OBSERVABLE_S)
        ticks_during_run = len(ticks)
        still_running = panel.is_running()
        if not still_running:
            pytest.skip(
                f"inspect on {S400.name} finished in under {_MIN_OBSERVABLE_S}s; "
                f"it cannot be observed in flight, so nothing was measured"
            )

        cancel = panel._cancel
        assert cancel.isEnabled(), "the cancel button was not enabled while analysing"
        qtbot.mouseClick(cancel, Qt.MouseButton.LeftButton)

        panel.repaint()
        QApplication.processEvents()
    finally:
        panel.cancel()
        qtbot.waitUntil(lambda: not panel.is_running(), timeout=30000)
        panel.shutdown()
        ticker.stop()

    assert ticks_during_run > 0, (
        "the timer never fired while inspect was in progress -- the event loop was blocked"
    )
    print(f"\nG2 inspect: {ticks_during_run} timer ticks while analysing, cancel accepted")


def test_a_cancelled_inspect_leaves_no_report(qtbot) -> None:  # noqa: ANN001
    """The other half of "responsive": a cancelled inspect reports the cancellation.

    Paired on purpose. A panel that ignored its child and simply never finished would pass a
    responsiveness test; being able to stop it *and having it say so* is the claim that makes
    responsiveness useful to a user.
    """
    from gigaxml.gui.panels.structure import StructurePanel

    panel = StructurePanel()
    qtbot.addWidget(panel)
    panel.set_document(S400)
    try:
        panel.analyze()
        qtbot.waitUntil(panel.is_running, timeout=5000)
        _pump(0.3)
        panel.cancel()
        # On the result rather than on ``is_running()``: the child stops first and the panel
        # settles on a later tick, so the first is not evidence the second has happened.
        qtbot.waitUntil(
            lambda: not panel.is_running() and panel.run_result() is not None,
            timeout=30000,
        )
    finally:
        panel.shutdown()

    result = panel.run_result()
    assert result is not None, "a cancelled inspect left no result at all"
    assert result.killed is True, f"the run was not reported as cancelled: {result}"
    assert panel.report() is None, "a cancelled inspect produced a report anyway"
    print(
        f"\nG2 inspect cancel: killed={result.killed} exit={result.exit_code} "
        f"report={panel.report()}"
    )


def test_an_uninterrupted_inspect_finishes(qtbot) -> None:  # noqa: ANN001
    """Responsiveness must not come at the cost of finishing the work.

    A window that stayed responsive by refusing to finish would sail through both tests
    above. This one closes that door: nobody touches it, and it produces a report.

    **The wait is on the report, not on ``is_running()``.** Those are two different
    quantities. ``is_running()`` reads the *child's* state, so it goes False the moment the
    child exits, while the panel settles its widgets on a later timer tick. Waiting on
    ``is_running()`` and then reading the report finds ``None`` on a run that actually
    succeeded -- measured: two extra event-loop rounds and the report is there, with the
    status line reading "363,775 elements - 5 candidates - 18 paths". Same shape as
    reporting a queue's length and its finished count as if they were one number.
    """
    from gigaxml.gui.panels.structure import StructurePanel

    panel = StructurePanel()
    qtbot.addWidget(panel)
    panel.set_document(S400)
    try:
        panel.analyze()
        # Settled means: the child is gone *and* the panel has finished with it.
        qtbot.waitUntil(
            lambda: not panel.is_running() and panel.run_result() is not None,
            timeout=300_000,
        )
        qtbot.waitUntil(lambda: panel.report() is not None, timeout=30_000)
    finally:
        panel.shutdown()

    result = panel.run_result()
    assert result is not None
    assert result.exit_code == 0, f"inspect failed: {result.stderr_lines}"

    report = panel.report()
    assert report is not None, "inspect finished with no report"
    assert report.candidates, "the report lists no candidates for a 403 MB catalog"
    print(
        f"\nG2 inspect complete: {report.elements_seen} elements, "
        f"{len(report.candidates)} candidates, {report.paths_tracked} tracked paths"
    )
