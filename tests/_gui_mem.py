"""GUI-side process memory measurement, in a fresh subprocess.

**Why this module exists at all, given :mod:`tests._mem` already measures memory.**
:mod:`tests._mem` answers "how much memory does the *CLI* take to read this file". Gate 11
G1 asks a different question: "how much memory does the *desktop application* take while a
job runs". Those are different processes, and measuring the wrong one is the trap this
module exists to make hard to fall into.

The trap, concretely: ``MainWindow`` is built *inside* the test process, so a test that
calls :func:`tests._mem.rss_mb` on itself is measuring pytest -- which never parsed a byte
of XML. That number is beautifully small and means nothing. The measurement here instead
runs in a **subprocess** that builds a real ``QApplication`` and a real ``MainWindow``, so
the number reported is the window's, and pytest's heap and the coverage tracer are nowhere
near it. Same reasoning as :mod:`tests._mem`, applied to the GUI.

**What is deliberately *not* measured here.** The GUI does not parse XML -- hard constraint
1, and Gate 11 G5 proves it by showing ``src/gigaxml/gui/`` never imports ``lxml``. So the
interesting question is not "does the window grow by 400 MB", which it cannot, but "does
the window grow *at all* as documents get bigger". That is why the caller runs this twice
with two input sizes and compares the two deltas additively (see the 4b rule: never a
ratio).

Run it directly::

    python -m tests._gui_mem data/s400.xml <work-dir>          # real GUI, real run
    python -m tests._gui_mem data/s400.xml <work-dir> --slurp # the reversed example
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import psutil

if TYPE_CHECKING:  # pragma: no cover - imported for the annotations only
    from PySide6.QtWidgets import QApplication

    from gigaxml.gui.main_window import MainWindow


class _RunningPanel(Protocol):
    """The two things this module needs from a run panel.

    Named rather than typed as the concrete class on purpose: the point of the measurement
    is that it drives the *real* panel through the *real* interface, so the surface used
    here is the whole contract. If a future panel stops answering these two questions the
    measurement should fail to type-check, not quietly measure nothing.
    """

    def start(self) -> None: ...

    def is_running(self) -> bool: ...

    def run_result(self) -> object: ...


__all__ = [
    "REPO_ROOT",
    "gui_memory_in_subprocess",
]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

_MB: Final = 1024 * 1024

#: The config every measurement writes. Two fields is enough to be a real extraction and
#: small enough that the *output* is not what dominates the child's memory.
_CONFIG: Final = """
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""

#: How often the sampler looks at the process while the job runs.
_SAMPLE_INTERVAL_S: Final = 0.02

#: How long to wait for a run to finish before giving up on it.
_RUN_TIMEOUT_S: Final = 1800.0


def _resolve_state_dir() -> pathlib.Path:
    """Where the window keeps its preferences and saved configs.

    **Resolved here rather than read out of the environment**, and that is the whole point:
    an earlier version of this module read ``os.environ["GIGAXML_GUI_STATE_DIR"]`` directly,
    so the probe only worked when a *library* caller had set that variable first. Run from
    the command line -- which is how the evidence file tells a reader to reproduce a number
    -- it raised ``KeyError`` on the first line of work.

    A measurement whose reproduction command crashes is not evidence of anything, so the
    state directory is derived from the working directory the caller already passed in, with
    the environment variable honoured when some caller sets it deliberately.
    """
    override = os.environ.get("GIGAXML_GUI_STATE_DIR")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.cwd() / "state"


def _build_window() -> tuple[QApplication, MainWindow]:
    """A real ``QApplication`` and a real ``MainWindow``, in this process.

    Imported lazily so that :mod:`psutil` and this module stay importable without Qt --
    the same reason :mod:`tests._mem` defers its own imports.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from gigaxml.gui.main_window import MainWindow

    application = QApplication.instance() or QApplication([])
    state_dir = _resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    window = MainWindow(state_dir=state_dir)
    return application, window


def _drain_until_finished(panel: _RunningPanel, deadline: float) -> bool:
    """Pump the Qt event loop until the run finishes or ``deadline`` passes.

    The panel finishes on a timer callback, so the event loop has to keep turning for the
    run to be observed at all. :meth:`QApplication.processEvents` is the whole mechanism --
    this is the same loop a user's window runs, which is the point: the GUI is not being
    driven by a private test-only path.
    """
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if application is None:  # pragma: no cover - _build_window guarantees one
        raise RuntimeError("no QApplication: the window was never built")
    while time.perf_counter() < deadline:
        application.processEvents()
        if not panel.is_running():
            # One more turn so the panel's own finished-handling runs.
            application.processEvents()
            return True
        time.sleep(_SAMPLE_INTERVAL_S)
    return False


def _measure(
    xml_path: str,
    work_dir: str,
    *,
    slurp: bool,
) -> dict[str, float | int | bool | str]:
    """Build the window, run one real job through it, report this process's memory.

    Args:
        xml_path: the document to extract.
        work_dir: where the output and the temporary config go.
        slurp: the reversed example. Instead of driving the GUI, **read the whole document
            into this process** the way a badly-written panel would. A measurement that
            cannot fail is not a measurement, so the caller needs a run where the number is
            obviously, hugely over the line -- and the only honest way to produce one is to
            commit the mistake on purpose.
    """
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = work / "out.csv"
    config = work / "config.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    input_mb = Path(xml_path).stat().st_size / _MB

    application, window = _build_window()

    if slurp:
        del application, window
        return _measure_slurp(xml_path, input_mb)

    panel = window.execution_panel()
    panel._source.setText(str(xml_path))
    panel._output.setText(str(output))
    panel.set_config(config)

    # The baseline is taken here on purpose: the window exists, the paths are filled in,
    # and no job has started. Opening a file dialog would have raised memory too, and that
    # is not "the cost of running a job".
    baseline = psutil.Process().memory_info().rss / _MB
    peak = baseline
    stop = threading.Event()

    def _sample() -> None:
        nonlocal peak
        process = psutil.Process()
        while not stop.wait(_SAMPLE_INTERVAL_S):
            try:
                current = process.memory_info().rss / _MB
            except psutil.Error:  # pragma: no cover - the process is us; it is alive
                return
            if current > peak:
                peak = current

    sampler = threading.Thread(target=_sample, daemon=True)
    started = time.perf_counter()
    sampler.start()
    try:
        panel.start()
        finished = _drain_until_finished(panel, deadline=started + _RUN_TIMEOUT_S)
    finally:
        stop.set()
        sampler.join(timeout=2.0)

    elapsed = time.perf_counter() - started
    result = panel.run_result()
    rows = 0
    if output.exists():
        with output.open(encoding="utf-8", newline="") as handle:
            rows = max(0, sum(1 for _ in handle) - 1)

    # ``MainWindow`` has no ``shutdown()`` of its own -- by design, since 8A-38 made
    # ``closeEvent`` the single place panels are told to let go. Closing the window is
    # therefore how this process stops owning a child process and a timer.
    window.close()
    application.processEvents()
    return {
        "mode": "gui",
        "input_mb": round(input_mb, 3),
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "seconds": round(elapsed, 3),
        "finished": finished,
        "exit_code": None if result is None else result.exit_code,
        "rows": rows,
    }


def _measure_slurp(xml_path: str, input_mb: float) -> dict[str, float | int | bool | str]:
    """The reversed example: read the whole document into this process, on purpose.

    Not a mock and not an estimate -- an actual ``read()`` of all 400 MB, which is the
    mistake the GUI must never make. Its only job is to be so far over 32 MiB that no
    reasonable reading of the criterion could call it a pass.
    """
    baseline = psutil.Process().memory_info().rss / _MB
    started = time.perf_counter()
    blob = Path(xml_path).read_bytes()
    elapsed = time.perf_counter() - started
    peak = psutil.Process().memory_info().rss / _MB
    return {
        "mode": "slurp",
        "input_mb": round(input_mb, 3),
        "baseline_mb": round(baseline, 3),
        "peak_mb": round(peak, 3),
        # Held on purpose: without the reference the allocation could be collected and the
        # "measurement" would report a rounding error.
        "delta_mb": round(max(0.0, peak - baseline), 3),
        "seconds": round(elapsed, 3),
        "finished": True,
        "exit_code": 0,
        "rows": len(blob),
    }


def gui_memory_in_subprocess(
    xml_path: str | Path,
    work_dir: str | Path,
    *,
    slurp: bool = False,
) -> dict[str, float | int | bool | str]:
    """Run one GUI memory measurement in a fresh interpreter and parse its JSON line.

    Args:
        xml_path: the document to run through the GUI.
        work_dir: a scratch directory for the output and the temporary config.
        slurp: measure the reversed example instead of a real run.

    Raises:
        RuntimeError: the measurement subprocess failed.
    """
    arguments = [str(xml_path), str(work_dir)]
    if slurp:
        arguments.append("--slurp")

    state_dir = Path(work_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "QT_QPA_PLATFORM": "offscreen",
        "GIGAXML_GUI_STATE_DIR": str(state_dir),
    }
    completed = subprocess.run(
        [sys.executable, "-m", "tests._gui_mem", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"GUI measurement subprocess exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    line = completed.stdout.strip().splitlines()[-1]
    payload: dict[str, float | int | bool | str] = json.loads(line)
    return payload


def _main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: python -m tests._gui_mem <xml-path> <work-dir> [--slurp]",
            file=sys.stderr,
        )
        return 2
    slurp = len(argv) == 4 and argv[3] == "--slurp"
    print(json.dumps(_measure(argv[1], argv[2], slurp=slurp)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
