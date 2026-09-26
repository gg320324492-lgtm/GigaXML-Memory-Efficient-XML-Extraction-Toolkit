"""G4's real-machine half: run ``s100.xml`` through the window and compare three numbers.

Gate 11 G4 is "the progress bar agrees with the run report, and the final row count equals
what was actually written". ``test_gui_results.py:165`` already asserts that on a
three-record fixture, which is a real assertion but a small one. This runs the 100 MB
document so the three numbers being compared are large enough for a rounding error or an
off-by-one part to show up.

**Three numbers, and why all three.** The point is not that two of them match; it is that
the GUI, the report on disk and the dataset's own manifest all say the same thing:

* the last ``records`` the GUI was told about,
* the ``rows`` in ``run-report.json``,
* ``record_count`` in ``s100.xml.manifest.json``.

The manifest is the independent third party. Comparing the GUI only to the report would
pass if both were wrong in the same way.

**Not checkpoint mode.** ``run_report.read_outcome`` documents that under checkpointing the
report's ``rows`` is the *last part's* writer, not the whole run -- measured: an eight-record
run in parts of two reports ``rows: 2``. Asserting a whole-run total there would be asserting
something the format does not claim. Plain mode, where the total is the total.

Run directly::

    python -m tests._gui_progress_probe data/s100.xml <work-dir>
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

__all__ = ["REPO_ROOT", "progress_in_subprocess"]

REPO_ROOT: Final = Path(__file__).resolve().parent.parent

_CONFIG: Final = """
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""

_RUN_TIMEOUT_S: Final = 900.0


def _manifest_record_count(xml_path: pathlib.Path) -> int:
    """How many records the dataset says it contains.

    Read from the generator's own manifest rather than counted here: counting would be the
    same code under test, and a bug that dropped records would drop them from both sides.
    """
    manifest = json.loads(xml_path.with_suffix(".xml.manifest.json").read_text("utf-8"))
    return int(manifest["record_count"])


def _measure(xml_path: str, work_dir: str) -> dict[str, object]:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from gigaxml.gui.main_window import MainWindow
    from gigaxml.gui.run_report import read_outcome

    source = Path(xml_path)
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    output = work / "out.csv"
    config = work / "config.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    application = QApplication.instance() or QApplication([])
    window = MainWindow(state_dir=work / "state")
    panel = window.execution_panel()
    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(config)

    # Collected on the panel's own signal, so what is compared is what the *window* was told,
    # not what the child happened to print. The panel clears its buffer as it renders, so
    # this listener is the only place the last value survives.
    seen: list[int] = []
    panel._show = lambda progress: seen.append(progress.records)  # type: ignore[method-assign]

    panel.start()
    deadline = time.perf_counter() + _RUN_TIMEOUT_S
    while time.perf_counter() < deadline:
        application.processEvents()
        if not panel.is_running():
            break
        time.sleep(0.02)
    application.processEvents()

    result = panel.run_result()
    outcome = read_outcome(output, checkpointing=False)
    rows_on_disk = 0
    if output.exists():
        with output.open(encoding="utf-8", newline="") as handle:
            rows_on_disk = max(0, sum(1 for _ in handle) - 1)

    payload: dict[str, object] = {
        "input": str(source),
        "input_mb": round(source.stat().st_size / (1024 * 1024), 3),
        "gui_last_records": seen[-1] if seen else None,
        "gui_updates": len(seen),
        "report_rows": None if outcome is None else outcome.rows,
        "rows_on_disk": rows_on_disk,
        "manifest_record_count": _manifest_record_count(source),
        "exit_code": None if result is None else result.exit_code,
        "checkpointing": False,
    }
    window.close()
    application.processEvents()
    return payload


def progress_in_subprocess(xml_path: str | Path, work_dir: str | Path) -> dict[str, object]:
    """Run the progress probe in a fresh interpreter and parse its JSON line."""
    state_dir = Path(work_dir) / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [sys.executable, "-m", "tests._gui_progress_probe", str(xml_path), str(work_dir)],
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
            f"progress probe exited {completed.returncode}\n"
            f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
        )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: python -m tests._gui_progress_probe <xml> <work-dir>",
            file=sys.stderr,
        )
        return 2
    print(json.dumps(_measure(argv[1], argv[2])))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
