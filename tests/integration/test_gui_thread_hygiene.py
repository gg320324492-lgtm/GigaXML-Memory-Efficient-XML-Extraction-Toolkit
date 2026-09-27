"""No ``CliProcess`` reader thread outlives its owner.

**Why this exists.** A CI segfault was caught in the garbage collector with two
``CliProcess`` reader threads still on the stack -- the collector walking objects whose
threads were mid-``drain_stderr`` and mid-``_read`` while the test that owned them had
finished. The crash itself is not reproduced here, and deliberately not chased: a race
that shows up once in dozens of runs on one interpreter version tells a local
reproduction nothing. What this file does instead is make the *state* the crash was
sitting in -- a reader thread alive after the panel that started its child is done with
it -- a red test on every machine, on every run, instead of a once-in-a-while core dump.

**Two windows of exposure, both closed by the same chains.** A reader thread lives from
``CliProcess.start()`` until the child's pipes are drained and the reader has delivered
its callback. After a normal completion that is over by the time ``run_result()`` says
so; the dangerous case is the window closing while a child is still working, which is
exactly what every panel's ``shutdown()`` exists to terminate. The second test closes
the window mid-run on a document big enough that the child is definitely still alive,
which is what makes it sensitive to a ``shutdown()`` that has stopped killing.
"""

from __future__ import annotations

import pathlib
import sys
import threading

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.main_window import MainWindow

#: The name prefix every reader thread this application creates carries --
#: ``gigaxml-cli-reader`` and the stderr drainer beside it.
_THREAD_PREFIX = "gigaxml-cli-"

TINY_DOCUMENT = """<?xml version="1.0"?>
<catalog><products>
<product id="1"><name>bracket</name></product>
</products></catalog>
"""

PLAIN_CONFIG = """record: /catalog/products/product
fields:
  product_id:
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


def reader_threads() -> list[str]:
    """The live threads this application's CLI readers own, by name."""
    return [
        thread.name for thread in threading.enumerate() if thread.name.startswith(_THREAD_PREFIX)
    ]


def start_run(
    window: MainWindow, source: pathlib.Path, output: pathlib.Path, config: pathlib.Path
) -> None:
    """Configure and start an extraction the way the panel's own controls would."""
    panel = window.execution_panel()
    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(config)
    panel.start()


def test_after_a_run_finishes_no_reader_threads_survive(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A completed run's threads are gone by the time the panel knows it is over."""
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        source = tmp_path / "tiny.xml"
        source.write_text(TINY_DOCUMENT, encoding="utf-8")
        config = tmp_path / "config.yaml"
        config.write_text(PLAIN_CONFIG, encoding="utf-8")
        panel = window.execution_panel()
        start_run(window, source, tmp_path / "out.csv", config)
        # Bounded, not immediate, in both waits: the reader delivers its callback as its
        # last act and the thread's own exit lands a moment after, so "gone" is a state
        # to reach, not an instant to sample. A thread still here when the wait gives up
        # is a leak, not a scheduling delay.
        qtbot.waitUntil(
            lambda: not panel.is_running() and panel.run_result() is not None,
            timeout=60_000,
        )
        qtbot.waitUntil(lambda: not reader_threads(), timeout=15_000)
        assert not reader_threads(), reader_threads()
    finally:
        window.close()
    assert not reader_threads(), reader_threads()


def test_closing_the_window_mid_run_leaves_no_reader_threads(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path, s100_path: pathlib.Path
) -> None:
    """Closing the window while a child works ends the child and its threads.

    This is the test that pins the ``shutdown()`` chains: every panel is wired so that
    closing the window kills its child and waits for the reader to deliver, and a
    shutdown that skips the kill leaves the child -- and both its threads -- alive,
    which this fails on.
    """
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        config = tmp_path / "config.yaml"
        config.write_text(PLAIN_CONFIG, encoding="utf-8")
        panel = window.execution_panel()
        start_run(window, s100_path, tmp_path / "out.csv", config)
        # Observed, not assumed: the child process object exists and the panel calls it
        # running before the window closes, so what the shutdown kills is a live child
        # rather than a spawn in progress.
        qtbot.waitUntil(lambda: panel.is_running() and panel._process is not None, timeout=15_000)
        window.close()
        # **The bound is short on purpose.** ``CliProcess.kill`` waits for the reader to
        # deliver before it returns, so with a correct shutdown the threads are gone in
        # milliseconds; this wait only has to cover scheduling. The document is the
        # 100 MB one -- its extraction runs for seconds after the close -- so a shutdown
        # that no longer kills leaves the threads alive long past three seconds, and the
        # wait times out instead of the leak healing itself inside a generous window.
        # Measured the hard way: with a 15 s bound and a document small enough to finish
        # inside it, a shutdown that never killed anything still passed this test.
        qtbot.waitUntil(lambda: not reader_threads(), timeout=3_000)
        assert not reader_threads(), reader_threads()
    finally:
        window.close()
