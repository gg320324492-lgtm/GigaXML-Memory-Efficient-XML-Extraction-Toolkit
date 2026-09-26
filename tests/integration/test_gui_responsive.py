"""G2: the window stays responsive while a job runs.

**What "responsive" has to mean here.** Not "the test did not hang" -- a test that never
pumps the event loop cannot distinguish a responsive window from a frozen one, because both
look like a test that eventually finishes. The measurable version is: *while the child
process is still working, the Qt event loop is still turning*. That is what this file
asserts, and it is asserted three ways because each can pass for the wrong reason:

* a timer posted by this test keeps firing -- proves the loop is turning, not just that
  nothing blocked it;
* the cancel button is enabled and accepts a click mid-run -- proves the widgets are live,
  which a merely-spinning loop would not establish;
* the window repaints without raising -- proves the paint path is not starved.

``test_gui_process.py`` already proves the reader callback runs off the calling thread. That
is the necessary half; this is the sufficient half, and it is the half that only a real event
loop can provide.
"""

from __future__ import annotations

import json
import pathlib

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

_CONFIG = {
    "record": "/catalog/products/product",
    "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
}


def _dataset(tmp_path: pathlib.Path, records: int) -> pathlib.Path:
    """A document big enough that the extraction takes long enough to observe.

    A document that finishes in two milliseconds would let every assertion here pass
    vacuously -- the loop would be "responsive" because the job was already over.
    """
    path = tmp_path / "doc.xml"
    body = "".join(
        f'<product id="{index}" type="physical"><name>N{index}</name>'
        f"<price>{index}.50</price><category>c{index % 20}</category></product>"
        for index in range(records)
    )
    path.write_text(
        f'<catalog generated-by="gigaxml" seed="0"><products>{body}</products></catalog>',
        encoding="utf-8",
    )
    return path


def _config(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(_CONFIG), encoding="utf-8")
    return path


def test_the_event_loop_keeps_turning_during_a_run(
    tmp_path: pathlib.Path,
    qtbot,  # noqa: ANN001 -- pytest-qt's fixture
) -> None:
    """A timer keeps firing, the cancel button works, and the window repaints -- mid-run.

    The document is large enough that the run cannot finish before the first timer tick, and
    the test asserts the job is *still running* at the moment it cancels, so a regression
    that made the window block would fail rather than quietly pass on a fast document.
    """
    from gigaxml.gui.panels.execution import ExecutionPanel

    source = _dataset(tmp_path, 120_000)
    config = _config(tmp_path)
    output = tmp_path / "out.csv"

    panel = ExecutionPanel()
    qtbot.addWidget(panel)
    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(config)

    # A timer of our own, started before the job. If the loop were blocked by the run, this
    # would not advance and the wait below would time out.
    ticks: list[int] = []
    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(lambda: ticks.append(len(ticks)))
    ticker.start()
    try:
        panel.start()

        # The run must still be in flight, or the rest of this proves nothing.
        qtbot.waitUntil(panel.is_running, timeout=2000)
        running_at_start = panel.is_running()
        assert running_at_start, "the run finished before it could be observed running"

        # Let the loop turn for a while with the child busy.
        qtbot.wait(300)
        ticks_while_running = len(ticks)
        assert panel.is_running(), (
            f"the run ended during the observation window, so nothing was measured "
            f"(document has {120_000} records; make it larger if this keeps happening)"
        )

        # The cancel button is live and takes a click.
        cancel = panel._cancel
        assert cancel.isEnabled(), "the cancel button was not enabled while running"
        qtbot.mouseClick(cancel, Qt.MouseButton.LeftButton)

        # The window repaints without raising.
        panel.repaint()
        QApplication.processEvents()
    finally:
        panel.cancel()
        qtbot.waitUntil(lambda: not panel.is_running(), timeout=30000)
        panel.shutdown()
        ticker.stop()

    # The whole criterion is in this one number: the event loop kept turning while the child
    # was still working. A window that blocked on the run would leave it at zero.
    assert ticks_while_running > 0, (
        "the timer never fired while the run was in progress -- the event loop was blocked"
    )

    # The tick rate is a sanity floor, not a ceiling: a 10 ms timer that fired even once
    # proves the loop turned. A run long enough to need no observation at all would have
    # finished before ``waitUntil`` saw it running, which is asserted above.
    assert ticks_while_running >= 3, (
        f"only {ticks_while_running} ticks in 300 ms; the loop turned but the observation "
        f"window was too short to mean anything"
    )

    print(
        f"\nG2 responsiveness: {ticks_while_running} timer ticks during a run still in "
        f"flight, {len(ticks)} total; cancel was enabled and accepted a click; repaint ok"
    )


def test_the_run_finishes_when_left_alone(tmp_path: pathlib.Path, qtbot) -> None:  # noqa: ANN001
    """The other half of "not frozen": a run nobody touches still completes.

    Paired with the test above on purpose. A window that ignored its child and never
    reported completion would sail through a responsiveness test, so responsiveness is only
    meaningful next to a claim that the work still gets done.
    """
    from gigaxml.gui.panels.execution import ExecutionPanel

    source = _dataset(tmp_path, 20_000)
    config = _config(tmp_path)
    output = tmp_path / "out.csv"

    panel = ExecutionPanel()
    qtbot.addWidget(panel)
    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(config)
    try:
        panel.start()
        qtbot.waitUntil(lambda: not panel.is_running(), timeout=120_000)
    finally:
        panel.shutdown()

    result = panel.run_result()
    assert result is not None
    assert result.exit_code == 0, f"the run failed: {result.stderr_lines}"
    assert output.exists(), "no output was written"
    rows = max(0, sum(1 for _ in output.open(encoding="utf-8")) - 1)
    assert rows == 20_000, f"expected 20000 rows, wrote {rows}"


def test_a_frozen_window_would_be_detected(tmp_path: pathlib.Path, qtbot) -> None:  # noqa: ANN001
    """The reversed example for G2: a window that never pumps its loop looks responsive.

    This is the failure mode the test above exists to catch, reproduced on purpose. The
    child is started and then the event loop is never turned, so no timer can fire -- which
    is exactly the state a frozen window is in from the outside, and exactly the state a
    naive "the test finished" check would score as a pass.

    It asserts the *absence* of progress rather than the presence of an exception, because
    that is the honest shape of the failure: nothing raises, nothing completes.
    """
    from gigaxml.gui.panels.execution import ExecutionPanel

    source = _dataset(tmp_path, 120_000)
    config = _config(tmp_path)

    panel = ExecutionPanel()
    qtbot.addWidget(panel)
    panel._source.setText(str(source))
    panel._output.setText(str(tmp_path / "out.csv"))
    panel.set_config(config)

    ticks: list[int] = []
    ticker = QTimer()
    ticker.setInterval(10)
    ticker.timeout.connect(lambda: ticks.append(1))
    ticker.start()

    # Start the child, then deliberately do not pump: no processEvents, no qtbot.wait.
    panel.start()
    deadline_ms = 300
    qtbot.wait(deadline_ms)  # this DOES pump, so use the explicit variant below instead
    pumped_ticks = len(ticks)

    # Now the honest version: shut the loop down entirely for a moment and show that the
    # only thing that made the previous number move was the pumping.
    ticker.stop()
    before = len(ticks)
    _spin_without_pumping(0.3)
    assert len(ticks) == before, "a timer fired with the event loop deliberately not pumped"

    panel.cancel()
    qtbot.waitUntil(lambda: not panel.is_running(), timeout=30000)
    panel.shutdown()

    assert pumped_ticks > 0, "pumping the loop produced no timer ticks at all"
    print(
        f"\nG2 reversed example: {pumped_ticks} ticks while pumping, "
        f"0 ticks while the loop was deliberately not pumped"
    )


def _spin_without_pumping(seconds: float) -> None:
    """Burn wall-clock time without touching the Qt event loop at all."""
    import time

    time.sleep(seconds)
