"""The GUI-probe invariants: the probes reproduce, and the numbers they show are real.

Moved here from ``tests/performance/`` (``test_gui_memory.py`` and ``test_gui_progress.py``)
because these are pass/fail claims, not measurements: the probes must run standalone, the
window's progress must agree with the report, the disk and the manifest, and none of that
belongs behind a marker that no CI leg executes. The *measurements* -- memory deltas,
throughput, the 400 MB scenarios -- stay in ``tests/performance/`` on purpose.

The documents they drive come from the session fixtures, which generate ``data/`` on
demand: nothing here depends on an artefact being present on the machine.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys

import pytest
from pytestqt.qtbot import QtBot

from tests._gui_progress_probe import progress_in_subprocess

#: The 100 MB document the progress criterion drives. Its size is what makes the numbers
#: large enough for an off-by-one or a stale final value to show.
S100_MB = 100.655


def test_the_probe_runs_as_a_standalone_subprocess(
    tmp_path: pathlib.Path, s10_path: pathlib.Path
) -> None:
    """The measurement tool is runnable on its own, the way ``tests._mem`` is.

    Not ceremony: a measurement that can only be reached through pytest is one nobody can
    point at when a number looks wrong. This is the command the evidence file quotes.

    **``GIGAXML_GUI_STATE_DIR`` is deliberately removed from the child's environment.** An
    earlier version of this test passed while the probe was broken, and the reason is worth
    keeping in mind: it went through the library entry point, which sets that variable for
    its own child, so it exercised a different path from the one the evidence file tells a
    reader to type. The audit found it by pasting the documented command and getting a
    ``KeyError``. Stripping the variable is what makes this test the same path.
    """
    env = {key: value for key, value in os.environ.items() if key != "GIGAXML_GUI_STATE_DIR"}
    env["QT_QPA_PLATFORM"] = "offscreen"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests._gui_mem",
            str(s10_path),
            str(tmp_path),
        ],
        cwd=pathlib.Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert completed.returncode == 0, f"probe failed:\n{completed.stderr}"
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["mode"] == "gui"
    assert payload["exit_code"] == 0
    assert payload["rows"] > 0


def test_every_gui_probe_runs_standalone(tmp_path: pathlib.Path, s10_path: pathlib.Path) -> None:
    """All three probes, run the way the evidence files say to run them.

    One probe being reproducible is luck; three being reproducible is a property of how they
    are written. Each is invoked as ``python -m tests.<probe>`` with the state-directory
    variable stripped, because that is what a reader copying a command out of a document will
    have in their environment -- which is to say, nothing this project set for them.

    **Each probe is asserted against its own payload schema, by name.** The per-schema
    facts below are the deterministic ones -- what must hold whichever way the race
    between the document's end and the cancel lands. What is deliberately *not* asserted
    is ``was_running_when_cancelled`` being a specific boolean: whether the cancel lands
    before a short document finishes is a race, and *both* outcomes are legitimate. Only
    the self-consistency is a fact, so the assertions must hold on both branches.

    An earlier version of this paragraph claimed it was measured that the 10 MB document
    finishes first, reporting ``was_running_when_cancelled: false``. That was not true of
    this machine, then or now: measured 3 runs out of 3 on ``s10.xml``, the cancel lands
    mid-run (``was_running=True killed=True exit=1``, ``cancel_took_s=0.002``), and the
    same holds on ``s400.xml`` (7/7). The race is real; that particular outcome of it was
    not, and stating an unverified observation as a measurement is how a stale claim
    outlives the code it described.
    """
    env = {key: value for key, value in os.environ.items() if key != "GIGAXML_GUI_STATE_DIR"}
    env["QT_QPA_PLATFORM"] = "offscreen"
    repo_root = pathlib.Path(__file__).resolve().parents[2]

    probes = {
        "gui_mem": [sys.executable, "-m", "tests._gui_mem", str(s10_path), str(tmp_path / "mem")],
        "gui_progress_probe": [
            sys.executable,
            "-m",
            "tests._gui_progress_probe",
            str(s10_path),
            str(tmp_path / "progress"),
        ],
        "gui_cancel_probe": [
            sys.executable,
            "-m",
            "tests._gui_cancel_probe",
            str(s10_path),
            str(tmp_path / "cancel"),
        ],
    }

    def require(payload: dict[str, object], name: str, key: str) -> object:
        """Read a key the probe's schema promises, naming the probe if it is missing."""
        assert key in payload, f"probe {name}: payload has no {key!r} key: {payload}"
        return payload[key]

    for name, command in probes.items():
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        assert completed.returncode == 0, (
            f"probe {name} failed when run standalone:\n"
            f"command: {' '.join(command[1:])}\n"
            f"stderr:\n{completed.stderr}"
        )
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        assert payload, f"probe {name} produced no payload"

        if name == "gui_mem":
            assert require(payload, name, "finished") is True, f"probe {name} did not finish"
            assert require(payload, name, "exit_code") == 0, f"probe {name} exited nonzero"
            assert require(payload, name, "rows"), f"probe {name} extracted no rows"
            assert require(payload, name, "peak_mb"), f"probe {name} never sampled memory"
        elif name == "gui_progress_probe":
            manifest = require(payload, name, "manifest_record_count")
            assert manifest and manifest > 0, f"probe {name}: manifest says no records"
            assert require(payload, name, "exit_code") == 0, f"probe {name} exited nonzero"
            # At least told once, not more-than-once: on a 10 MB document this machine
            # finishes in well under a second, and whether more than one progress line
            # arrives before the window's pump drains it is a race, not a property. The
            # bar-must-move claim is the 100 MB criterion's (the progress test below),
            # which runs a document long enough to owe several.
            assert int(require(payload, name, "gui_updates")) >= 1, (
                f"probe {name}: the window was never told a progress value"
            )
            assert require(payload, name, "report_rows") == manifest, (
                f"probe {name}: report rows disagree with the manifest"
            )
            assert require(payload, name, "rows_on_disk") == manifest, (
                f"probe {name}: rows on disk disagree with the manifest"
            )
        elif name == "gui_cancel_probe":
            # The cancel either lands (killed, pid dead) or the document finishes first
            # (not killed, exit 0) -- timing decides which, but not both can be claimed,
            # and the target file answers the same either/or: the writer is atomic, so a
            # finished run moves a complete ``out.csv`` into place and a cancelled one
            # leaves it absent with its ``.tmp`` beside it. Asserting ``exists`` True on
            # both branches was a bug of mine -- measured in a loaded full-tree run, where
            # the cancel landed mid-extract and the file legitimately was not there.
            assert require(payload, name, "pid_alive_after_cancel") is False, (
                f"probe {name}: the child survived the cancel"
            )
            killed = require(payload, name, "killed")
            assert killed == require(payload, name, "was_running_when_cancelled"), (
                f"probe {name} claims a cancellation it did not perform, or the reverse"
            )
            assert bool(require(payload, name, "exists")) is (not killed), (
                f"probe {name}: target presence disagrees with the outcome: {payload}"
            )
            if not killed:
                assert require(payload, name, "exit_code") == 0, (
                    f"probe {name}: the run neither was cancelled nor finished cleanly"
                )
        else:  # pragma: no cover - a new probe must declare its own schema here
            raise AssertionError(f"probe {name} has no schema assertions; add them")


def test_progress_agrees_with_the_report_and_the_manifest(
    tmp_path: pathlib.Path, qtbot: QtBot, s100_path: pathlib.Path
) -> None:
    """Four sources, one number. The window, the report, the disk and the manifest agree.

    ``test_gui_results.py`` already asserts ``outcome.rows == payload["rows"] == 3`` on a
    three-record fixture. That is a real assertion, and it is also small enough that an
    off-by-one at a part boundary or a rounding error in a percentage would never appear.
    This runs the 100 MB document, where the numbers are large enough for those mistakes
    to show.

    **Four numbers, and the fourth is the point.** The GUI's last reported record count, the
    report's ``rows``, the rows actually on disk, and the dataset manifest's ``record_count``
    must all agree. The manifest is the independent party: comparing the GUI only against the
    report would pass if both were wrong together, which is exactly the failure a progress bar
    is supposed to make impossible.

    **Plain mode, deliberately.** ``run_report.read_outcome`` documents that under checkpointing
    the report's ``rows`` is the *last part's* writer rather than the whole run -- measured: an
    eight-record run in parts of two reports ``rows: 2``. Asserting a whole-run total there would
    be asserting something the format does not promise. The checkpoint half of this criterion is
    covered by the existing small-document test instead.
    """
    del qtbot
    payload = progress_in_subprocess(s100_path, tmp_path / "run")

    assert payload["exit_code"] == 0, f"the run failed: {payload}"
    assert payload["checkpointing"] is False, "this criterion is about non-checkpoint runs"

    expected = payload["manifest_record_count"]
    assert isinstance(expected, int)
    assert expected > 0, f"the manifest reported no records: {payload}"

    # More than one update, so "the GUI was told the right number" cannot be satisfied by a
    # single final value arriving after the work was already done.
    assert payload["gui_updates"] and int(payload["gui_updates"]) > 1, (
        f"the GUI only updated once, so a progress bar that never moved would pass: {payload}"
    )

    assert payload["gui_last_records"] == expected, (
        f"the window's last progress ({payload['gui_last_records']}) does not match the "
        f"dataset ({expected}): {payload}"
    )
    assert payload["report_rows"] == expected, (
        f"the report says {payload['report_rows']}, the dataset has {expected}: {payload}"
    )
    assert payload["rows_on_disk"] == expected, (
        f"{payload['rows_on_disk']} rows are on disk, the dataset has {expected}: {payload}"
    )
    assert payload["input_mb"] == pytest.approx(S100_MB, abs=1.0), (
        f"this criterion is defined on the 100 MB document, got {payload['input_mb']}: {payload}"
    )

    print(
        f"\nG4 progress: gui={payload['gui_last_records']} report={payload['report_rows']} "
        f"disk={payload['rows_on_disk']} manifest={expected} "
        f"({payload['gui_updates']} GUI updates)"
    )
