"""G3 on the real thing: cancel a real 403 MB run part-way and look at the disk.

``test_gui_process.py`` already covers cancellation with small documents and asserts the
child dies. What it cannot show is what a *long* run leaves behind, because a small run is
usually over before the cancel lands. This drives ``data/s400.xml`` -- 403.5 MiB, about
fifteen seconds of work -- cancels it while it is genuinely in flight, and then inspects the
filesystem.

Three claims, each checked against the disk rather than against a return value:

1. the child is gone (``psutil.pid_exists`` false, not merely "we called kill");
2. the target is not half a CSV -- either absent, or a ``.tmp`` beside it, never a truncated
   ``.csv`` that a reader would take for a finished file;
3. with ``--checkpoint-every``, the parts that were committed are still there.

Run directly::

    python -m tests._gui_cancel_probe data/s400.xml <work-dir> [--checkpoint]
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

import psutil

__all__ = ["REPO_ROOT", "cancel_in_subprocess"]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

_MB: Final = 1024 * 1024

_CONFIG: Final = """
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""

#: Cancel this long after the child is observed working. Chosen against a ~15 s run: early
#: enough that the job is certainly still going, and -- with ``--checkpoint`` -- late enough
#: that at least one part has been committed, which is the only way to check that cancelling
#: does not throw away work that was already safely on disk.
_CANCEL_AFTER_S: Final = 2.5

#: How many records per part when checkpointing. Small on purpose: s400 has 1,164,800 records,
#: so a 50,000-record part would not be committed until well after the cancel and the
#: "committed parts survive" claim would go untested rather than tested.
_CHECKPOINT_EVERY: Final = 20_000


def _inspect(output: pathlib.Path) -> dict[str, object]:
    """What is actually on disk at ``output`` right now.

    Deliberately reports sizes as well as names. "No .tmp" is only reassuring if the
    directory is not simply empty, and "the .csv exists" is only alarming if you know how big
    it is supposed to be.
    """
    parent = output.parent
    listing: list[dict[str, object]] = []
    if parent.is_dir():
        for item in sorted(parent.iterdir()):
            listing.append({"name": item.name, "bytes": item.stat().st_size})
    return {
        "target": str(output),
        "exists": output.exists(),
        "bytes": output.stat().st_size if output.exists() else 0,
        "siblings": listing,
    }


def _measure(xml_path: str, work_dir: str, *, checkpoint: bool) -> dict[str, object]:
    """Start a real run, cancel it mid-flight, and report what survived.

    Runs in its own interpreter for the same reason ``tests._mem`` does: the peak-RSS
    high-water mark and pytest's heap are both process-global, and a measurement that has to
    be trusted should not be taken in a process that has already run a thousand tests.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from gigaxml.gui.main_window import MainWindow

    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = work / "out.csv"
    config = work / "config.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    application = QApplication.instance() or QApplication([])
    window = MainWindow(state_dir=work / "state")
    panel = window.execution_panel()
    panel._source.setText(str(xml_path))
    panel._output.setText(str(output))
    panel.set_config(config)
    if checkpoint:
        panel._checkpoint.setValue(_CHECKPOINT_EVERY)

    panel.start()
    process = panel._process
    pid = None if process is None else process.pid

    # Two waits, because they answer different questions. The first asks "has the child begun
    # working" -- cpu time, not mere existence, since cancelling a process that has not read
    # anything would only prove that cancelling a starting process works. The second asks
    # "has it been working long enough to have committed something", which under
    # --checkpoint is the only way the "committed parts survive" claim gets tested at all
    # rather than quietly skipped.
    _pump_until(application, panel, lambda: _child_is_busy(pid), timeout_s=30)
    _pump_until(
        application,
        panel,
        lambda: not panel.is_running(),
        timeout_s=_CANCEL_AFTER_S,
        stop_when=lambda: _child_is_busy(pid) and _parts_on_disk(work) > 0,
    )

    was_running = panel.is_running()
    started_at = time.perf_counter()
    panel.cancel()
    application.processEvents()

    # Wait for the panel to finish handling the cancellation.
    deadline = time.perf_counter() + 60
    while time.perf_counter() < deadline:
        application.processEvents()
        if not panel.is_running():
            break
        time.sleep(0.02)
    cancel_took = time.perf_counter() - started_at

    alive = bool(pid is not None and psutil.pid_exists(pid))
    result = panel.run_result()
    parts = sorted(
        item.name for item in work.rglob("part-*") if item.is_file() and item.suffix != ".tmp"
    )
    payload: dict[str, object] = {
        "mode": "checkpoint" if checkpoint else "plain",
        "input_mb": round(Path(xml_path).stat().st_size / _MB, 3),
        "was_running_when_cancelled": was_running,
        "pid": pid,
        "pid_alive_after_cancel": alive,
        "cancel_took_s": round(cancel_took, 3),
        "killed": None if result is None else result.killed,
        "exit_code": None if result is None else result.exit_code,
        "committed_parts": parts,
        **_inspect(output),
    }
    window.close()
    application.processEvents()
    return payload


def _pump_until(
    application: object,
    panel: object,
    until: object,
    *,
    timeout_s: float,
    stop_when: object = None,
) -> bool:
    """Turn the event loop until ``until()`` holds, the panel stops, or time runs out.

    Every wait in this module goes through here, because a wait that does not pump the Qt
    event loop is not a wait the panel can notice: it finishes on a timer callback, so a loop
    that is not turning means a run that never "finishes" as far as the caller is concerned.
    """
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        application.processEvents()  # type: ignore[attr-defined]
        if until():  # type: ignore[operator]
            return True
        if stop_when is not None and stop_when():  # type: ignore[operator]
            return True
        if not panel.is_running():  # type: ignore[attr-defined]
            return bool(until())  # type: ignore[operator]
        time.sleep(0.02)
    return False


def _parts_on_disk(work: pathlib.Path) -> int:
    """How many checkpoint parts have been committed under ``work``.

    A part is a file the writer finished and the manifest knows about. Counting files alone
    would also count the ``.tmp`` of a part still being written, which is exactly the file
    that is *not* committed.
    """
    return sum(1 for item in work.rglob("part-*") if item.is_file() and item.suffix != ".tmp")


def _child_is_busy(pid: int | None) -> bool:
    """Whether the child exists and has actually done some work.

    ``cpu_times()`` is the honest signal here: a child that exists but has read nothing has
    not started, and cancelling it would only prove that cancelling a starting process
    works.
    """
    if pid is None:
        return False
    try:
        handle = psutil.Process(pid)
        times = handle.cpu_times()
        return (times.user + times.system) > 0.05
    except psutil.Error:
        return False


def cancel_in_subprocess(
    xml_path: str | Path,
    work_dir: str | Path,
    *,
    checkpoint: bool = False,
) -> dict[str, object]:
    """Run the cancellation probe in a fresh interpreter and parse its JSON line."""
    arguments = [str(xml_path), str(work_dir)]
    if checkpoint:
        arguments.append("--checkpoint")

    state_dir = Path(work_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-m", "tests._gui_cancel_probe", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(REPO_ROOT),
            "QT_QPA_PLATFORM": "offscreen",
            "GIGAXML_GUI_STATE_DIR": str(state_dir),
        },
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"cancel probe exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(
            "usage: python -m tests._gui_cancel_probe <xml> <work-dir> [--checkpoint]",
            file=sys.stderr,
        )
        return 2
    checkpoint = len(argv) == 4 and argv[3] == "--checkpoint"
    print(json.dumps(_measure(argv[1], argv[2], checkpoint=checkpoint)))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
