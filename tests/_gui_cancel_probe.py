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
  type:
    path: "@type"
  name:
    path: name
  category:
    path: category
  price:
    path: price
    type: float
  manufacturer:
    path: manufacturer/name
"""

#: **The six-field configuration is load-bearing.** The probe used to extract two fields,
#: which read 403 MB in about three seconds on the machine this file was written on --
#: faster than the cancel delay it used to sleep for, so the run was over before the cancel
#: landed, the probe reported ``was_running_when_cancelled: false`` and both cancel tests
#: failed a premise instead of a product. The six-field shape is the README's benchmark
#: configuration; the type conversions cut the throughput enough that the cancel lands
#: inside a run of many seconds. The waits below still watch for facts, not clocks.

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

    # The panel always asks the CLI for ``--progress``, and the panel appends every
    # progress object it receives to ``_received`` and hands it to ``_show`` as it
    # renders. Intercepting ``_show`` gives the same "the child has actually begun
    # working" signal the cpu-time check below used to aim at -- and it is a signal this
    # machine can produce, which cpu time is not: psutil 7.2.2 on this Windows returns a
    # **frozen** snapshot for a live process's ``cpu_times()`` (measured: a pure-CPU loop
    # held ``user=0.0156`` -- one scheduling quantum -- for a full second of heavy
    # arithmetic), so a threshold on accumulated cpu time never fires and every wait
    # built on it spins until the run finishes, then cancels a finished process.
    shown: list[object] = []
    panel._show = shown.append  # type: ignore[method-assign]

    # Two waits, because they answer different questions. The first asks "has the child begun
    # working" -- a progress line is that proof: the child has parsed at least a thousand
    # records and reported a count. The second asks "has it been working long enough to have
    # committed something", and exists only under --checkpoint: that is the only mode that
    # commits anything, and the cancel has to land *after* a part is on disk for "committed
    # parts survive" to be tested at all rather than quietly skipped. In plain mode there
    # is nothing to wait for -- the cancel lands the moment the child is working.
    #
    # **Nothing here waits a fixed time.** The probe used to sleep 2.5 s between "busy" and
    # "cancel", calibrated against a run it assumed would last ~15 s; on the machine this
    # runs on, a 403 MB extract -- even with the six-field configuration -- finishes in
    # under three, and both cancel tests failed their own premise: the run was over, the
    # cancel landed on a completed process, and the payload said so honestly
    # (``was_running_when_cancelled: false, exit_code: 0``). Every wait now watches for the
    # fact the test actually needs -- a progress line; a part committed -- and the cancel
    # fires the moment that fact holds, which is a premise, not a race.
    _pump_until(application, panel, lambda: bool(shown), timeout_s=30)
    if checkpoint:
        _pump_until(application, panel, lambda: _parts_on_disk(work) > 0, timeout_s=30)

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
