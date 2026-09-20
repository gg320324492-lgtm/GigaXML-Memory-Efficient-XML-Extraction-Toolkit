"""Smoke tests for the benchmark harness.

The harness has rotted once already: a README that said "a script in this repository"
pointed at scripts that had been deleted. These tests are the thing that would have
caught it.

They assert **nothing about performance** -- no thresholds, no comparisons. A benchmark
that fails because a machine was busy is worse than no benchmark. What is asserted is
that the scripts still import, still parse their arguments, and still produce results of
the expected shape. See the note about A11 in the project's conventions: performance is
recorded, never asserted.

The datasets are generated and not committed, so these skip when `data/` is empty.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import types

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
BENCH = REPO / "benchmarks"
DATA = REPO / "data"

#: The smallest generated dataset. Present whenever `gigaxml generate` has been run.
SMALL = DATA / "s10.xml"

SCRIPTS = ["bench_extraction.py", "bench_resident.py", "bench_inspect.py"]

RESULT_KEYS = {"delta_mb", "peak_mb", "throughput_mib_s", "records"}


def load(script: str) -> types.ModuleType:
    """Import a benchmark script by path -- they are scripts, not a package."""
    path = BENCH / script
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", SCRIPTS)
def test_the_script_can_be_imported(script: str) -> None:
    """A syntax error or a bad import is the most likely way for these to rot."""
    module = load(script)

    assert hasattr(module, "main"), f"{script} has no main()"


@pytest.mark.parametrize("script", SCRIPTS)
def test_help_renders(script: str) -> None:
    """argparse blows up at parse time, so --help is a real check.

    It also catches the `%`-formatting trap: a literal percent in a help string makes
    the whole command raise rather than print.
    """
    completed = subprocess.run(
        [sys.executable, str(BENCH / script), "--help"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_the_harness_still_produces_results_of_the_expected_shape(
    tmp_path: pathlib.Path,
) -> None:
    """The end-to-end check: a real run, on the smallest dataset, asserting only shape.

    `--work` is passed explicitly so the harness writes into the test's temporary
    directory. Without it the harness uses a temp directory of its own, which would be
    fine here but would leave the test unable to look at what it produced.
    """
    if not SMALL.is_file():
        pytest.skip(f"{SMALL} not generated; run `gigaxml generate --size 10MB`")

    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH / "bench_extraction.py"),
            "--datasets",
            SMALL.name,
            "--work",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )

    assert completed.returncode == 0, completed.stderr[-600:]

    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    assert len(results) == 2, "one row per config"

    for row in results:
        assert set(row) >= RESULT_KEYS, f"missing keys in {row}"
        assert row["records"] > 0
        assert row["rows"] == row["records"]
        assert row["seconds"] > 0
        assert row["peak_mb"] >= row["baseline_mb"] - 1.0  # peak cannot be below baseline

    labels = {row["label"] for row in results}
    assert labels == {f"{SMALL.stem} / 6 fields", f"{SMALL.stem} / 1 field"}


def test_the_resident_script_reports_the_verify_cost(tmp_path: pathlib.Path) -> None:
    """The figure the README quotes comes from here, so the section has to exist."""
    if not SMALL.is_file():
        pytest.skip(f"{SMALL} not generated; run `gigaxml generate --size 10MB`")

    completed = subprocess.run(
        [
            sys.executable,
            str(BENCH / "bench_resident.py"),
            "--dataset",
            str(SMALL),
            "--skip-sweep",
            "--work",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )

    assert completed.returncode == 0, completed.stderr[-600:]

    results = json.loads((tmp_path / "results.json").read_text(encoding="utf-8"))
    verify = results["verify_parts"]
    assert "error" not in verify, verify.get("error")
    assert verify["parts"] > 0
    assert verify["verify_ms"] > 0
    assert verify["problems"] == []
    assert results["sensitivity"] == [], "--skip-sweep must actually skip the sweep"
