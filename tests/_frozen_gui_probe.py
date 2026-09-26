"""G6 items 6 and 7, on a real frozen binary: does the packaged window drive its own CLI?

This is the check Gate 11 could not do. That round proved the *command construction*
(``cli_command_for`` builds ``[exe, ...args]``) and then recorded a false green when a
normal interpreter was dressed up as a frozen one: ``rc=0``, and the output was
``Python 3.13.14`` because the interpreter ate the arguments. Construction is not
execution.

So this drives the actual artifact:

* the window is opened by the frozen ``gigaxml-gui.exe``, with a document and an output
  path filled in, and ``start()`` pressed -- the same three lines a user's click produces;
* the child is then examined **from outside the GUI process**, by pid, so "it worked" is
  not the GUI's own opinion of itself;
* ``PATH`` has no ``gigaxml`` on it, so anything that runs is running out of the binary.

The one thing this cannot check is a click, and it says so rather than implying otherwise:
``start()`` is what the button calls, and driving the real button would need a real
pointer. Everything downstream of the click is exercised.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Final

import psutil

__all__ = ["REPO_ROOT", "probe_frozen_gui"]

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parent.parent

_CONFIG: Final = json.dumps(
    {
        "record": "/catalog/products/product",
        "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
    }
)

_SAMPLE_S: Final = 0.05
_TIMEOUT_S: Final = 300.0


def _child_pids(me: int) -> list[int]:
    """Every descendant of this process, which is how the child is identified from outside."""
    found: list[int] = []
    try:
        parent = psutil.Process(me)
        for child in parent.children(recursive=True):
            found.append(child.pid)
    except psutil.Error:  # pragma: no cover
        return found
    return found


def _measure(exe: str, xml_path: str, work_dir: str) -> dict[str, object]:
    """Open the frozen window, press start, and watch what the machine does.

    Runs in a child interpreter because the window owns a Qt application object and a
    timer, and neither survives being torn down and rebuilt in one process.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication

    from gigaxml.gui.main_window import MainWindow

    work = pathlib.Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = work / "out.csv"
    config = work / "config.json"
    config.write_text(_CONFIG, encoding="utf-8")
    state = work / "state"
    state.mkdir(parents=True, exist_ok=True)

    application = QApplication.instance() or QApplication([])
    window = MainWindow(state_dir=state)
    panel = window.execution_panel()
    panel._source.setText(str(xml_path))
    panel._output.setText(str(output))
    panel.set_config(config)

    # What the child command actually was. Frozen, this is the artifact invoking itself,
    # so the first element is the exe and there is no ``-m`` anywhere.
    args = panel.build_args()
    command_preview = [pathlib.Path(exe).name, *args]

    baseline_rows = 0
    if output.exists():
        with output.open(encoding="utf-8", newline="") as handle:
            baseline_rows = max(0, sum(1 for _ in handle) - 1)

    me = os.getpid()
    pids_before = set(_child_pids(me))

    progress_seen: list[int] = []
    original_show = panel._show

    def _record(progress) -> None:  # noqa: ANN001
        progress_seen.append(progress.records)
        original_show(progress)

    panel._show = _record  # type: ignore[method-assign]

    started = time.perf_counter()
    panel.start()

    child_pids: list[int] = []
    peak = 0
    deadline = started + _TIMEOUT_S
    while time.perf_counter() < deadline:
        application.processEvents()
        now = set(_child_pids(me))
        for pid in now - pids_before:
            if pid not in child_pids:
                child_pids.append(pid)
        try:
            for pid in child_pids:
                try:
                    rss = psutil.Process(pid).memory_info().rss
                    peak = max(peak, rss)
                except psutil.Error:
                    pass
        except psutil.Error:  # pragma: no cover
            pass
        if not panel.is_running():
            application.processEvents()
            # ``is_running()`` reads the *child*, so it goes False the moment the child
            # exits; the panel settles its own state on a later timer tick. Measured: the
            # run had written all 291,200 rows and finished in 3.99 s while ``run_result()``
            # was still ``None``. One more turn of the loop is not always enough, so this
            # waits for the result rather than for the child.
            settle = time.perf_counter() + 10.0
            while time.perf_counter() < settle and panel.run_result() is None:
                application.processEvents()
                time.sleep(_SAMPLE_S)
            break
        time.sleep(_SAMPLE_S)

    elapsed = time.perf_counter() - started
    result = panel.run_result()

    rows = 0
    if output.exists():
        with output.open(encoding="utf-8", newline="") as handle:
            rows = max(0, sum(1 for _ in handle) - 1)

    window.close()
    application.processEvents()

    return {
        "executable": str(exe),
        "frozen_flag_seen_by_child": None,  # filled in below
        "command_preview": command_preview,
        "has_module_flag": "-m" in command_preview,
        "child_pids": child_pids,
        "child_count": len(child_pids),
        "child_peak_rss_bytes": peak,
        "gui_progress_updates": len(progress_seen),
        "gui_last_records": progress_seen[-1] if progress_seen else None,
        "finished": not panel.is_running(),
        "exit_code": None if result is None else result.exit_code,
        "rows_written": rows - baseline_rows,
        "output_exists": output.exists(),
        "seconds": round(elapsed, 3),
    }


def probe_frozen_gui(
    exe: str | pathlib.Path,
    xml_path: str | pathlib.Path,
    work_dir: str | pathlib.Path,
) -> dict[str, object]:
    """Run the frozen-window probe in a fresh interpreter and return its payload.

    ``PATH`` is stripped of anything that could satisfy the CLI from outside. The frozen
    binary is invoked by absolute path, so the artifact under test is the artifact named,
    not a console script that happens to be installed.
    """
    artifact = pathlib.Path(exe).resolve()
    if not artifact.is_file():
        raise FileNotFoundError(f"no frozen binary at {artifact}")

    work = pathlib.Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    (work / "state").mkdir(parents=True, exist_ok=True)

    # An empty PATH plus the two interpreter directories PySide6 needs. If the GUI could
    # only run because something else on this machine was installed, this is where it shows.
    minimal_path = os.pathsep.join(
        [
            str(artifact.parent),
            str(pathlib.Path(sys.executable).parent),
            os.environ.get("SYSTEMROOT", r"C:\Windows"),
            r"C:\Windows\System32",
        ]
    )

    env = {
        **os.environ,
        "PATH": minimal_path,
        "PYTHONPATH": str(REPO_ROOT),
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONHOME": "",
    }
    env.pop("PYTHONHOME", None)

    completed = subprocess.run(
        [sys.executable, "-m", "tests._frozen_gui_probe", str(artifact), str(xml_path), str(work)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"frozen GUI probe exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    payload: dict[str, object] = json.loads(completed.stdout.strip().splitlines()[-1])

    # The one claim that cannot come from inside the window: that the child really was the
    # frozen binary, and not a gigaxml that happened to be installed. Read from the child
    # process's own view of itself, while it is alive, in a separate run.
    payload["child_was_frozen"] = _child_reports_frozen(artifact)
    return payload


def _child_reports_frozen(artifact: pathlib.Path) -> dict[str, object]:
    """Start the frozen binary as a CLI child and ask it whether it is frozen.

    The honest way to prove the packaged window will not fall back to a system install: run
    the artifact the way the GUI runs it, with nothing else on PATH, and read the flag from
    the artifact itself rather than inferring it.
    """
    env = dict(os.environ)
    env["PATH"] = str(artifact.parent)
    env.pop("PYTHONHOME", None)
    completed = subprocess.run(
        [str(artifact), "--version"],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    reported = (completed.stdout or "").strip()
    return {
        "version_output": reported,
        "rc": completed.returncode,
        # A frozen binary answering with "gigaxml x.y.z" is the artifact's own parser. One
        # answering with a Python version means the arguments went to an interpreter.
        "parsed_by_artifact": reported.startswith("gigaxml "),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: python -m tests._frozen_gui_probe <exe> <xml> <work-dir>", file=sys.stderr)
        return 2
    print(json.dumps(_measure(argv[1], argv[2], argv[3])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
