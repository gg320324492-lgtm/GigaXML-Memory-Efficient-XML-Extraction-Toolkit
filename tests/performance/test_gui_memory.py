"""G1: the desktop application's own memory while a 403 MB job runs.

**The criterion.** Gate 11 G1: the GUI process must not grow by more than 32 MiB while a
403 MB document is extracted through it. ``data/s400.xml`` is 423,048,871 bytes = 403.5 MiB,
measured rather than assumed, and this file uses that dataset rather than generating one.

**Why the GUI cannot grow much, and why that is worth asserting.** The GUI does not parse
XML -- hard constraint 1, proved separately by G5's grep over ``src/gigaxml/gui/``. The
window hands the document to a child process and reads a progress stream back. So the
honest expectation is not "a small number" but "a number that has nothing to do with the
document's size", and the only way to show that is to compare two sizes. Both are measured
here and compared **additively** (see the 4b rule: never a ratio -- a ratio is dominated by
the baseline and would pass an implementation that grows linearly).

**The reversed example is not optional.** :func:`test_the_measurement_can_fail` slurps all
403 MB on purpose. A measurement that cannot fail is not a measurement; without it, a broken
sampler that always returned a small number would satisfy the criterion above just as
happily as the real thing. Both numbers are reported by both tests.

Marked ``performance``: it measures memory, takes minutes, and is not a pass/fail signal for
a build (A11). It is not run in CI.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Final

import pytest

from tests._gui_mem import REPO_ROOT, gui_memory_in_subprocess

#: The gate. Exceeding this fails the test.
DELTA_BUDGET_MB: Final = 32.0

#: The dataset the criterion names, and its size as measured on disk.
S400_MB: Final = 403.5


def _run(size: str, tmp_path: pathlib.Path, *, slurp: bool = False) -> dict[str, object]:
    """Measure one size in a fresh interpreter and return its payload.

    Each size gets its own subprocess because the window is built *inside* the process being
    measured. Measuring the window from pytest would measure pytest, which never parsed a
    byte -- a small, meaningless and permanently green number.
    """
    return gui_memory_in_subprocess(
        REPO_ROOT / "data" / size,
        tmp_path / ("slurp" if slurp else size),
        slurp=slurp,
    )


@pytest.mark.performance
def test_gui_memory_does_not_grow_with_the_document(
    tmp_path: pathlib.Path,
    s400_path: pathlib.Path,
) -> None:
    """The 403 MB run stays under 32 MiB, and 4x the input costs no more than a band.

    Both sizes run for real. The additive comparison is the part that carries meaning:
    ``delta(400MB) <= delta(100MB) + 8 MiB`` would fail an implementation that kept the
    document, while a ratio-based assertion would hide exactly that.
    """
    # The fixture is what guarantees the file the criterion names actually exists on this
    # machine; the measurement below reads it by name from ``data/``.
    del s400_path
    small = _run("s100.xml", tmp_path)
    big = _run("s400.xml", tmp_path)

    for payload, name in ((small, "s100.xml"), (big, "s400.xml")):
        assert payload["finished"] is True, f"{name} did not finish: {payload}"
        assert payload["exit_code"] == 0, f"{name} exited {payload['exit_code']}: {payload}"
        assert payload["rows"] and payload["rows"] > 0, f"{name} wrote no rows: {payload}"

    assert big["input_mb"] == pytest.approx(S400_MB, abs=1.0), (
        f"the criterion names a 403 MB document; measured {big['input_mb']} MiB"
    )

    # The criterion itself: an absolute ceiling, not a ratio. A ratio is dominated by the
    # ~58 MiB baseline and would let a real regression hide behind it.
    assert big["delta_mb"] <= DELTA_BUDGET_MB, (
        f"GUI grew {big['delta_mb']} MiB running {big['input_mb']} MiB, "
        f"budget {DELTA_BUDGET_MB} MiB (baseline {big['baseline_mb']}, peak {big['peak_mb']})"
    )

    # And the property underneath it: four times the input, the same memory.
    growth = float(big["delta_mb"]) - float(small["delta_mb"])
    assert growth <= 8.0, (
        f"GUI memory grew with the document: 100MB delta {small['delta_mb']} MiB, "
        f"400MB delta {big['delta_mb']} MiB, growth {growth:.3f} MiB"
    )

    print(
        "\n".join(
            [
                "",
                "G1 GUI memory (fresh interpreter per size, real window, real child):",
                f"  s100.xml  input={small['input_mb']:>8} MiB  "
                f"baseline={small['baseline_mb']:>7} MiB",
                f"            peak={small['peak_mb']:>7} MiB  "
                f"delta={small['delta_mb']:>6} MiB  rows={small['rows']}",
                f"  s400.xml  input={big['input_mb']:>8} MiB  baseline={big['baseline_mb']:>7} MiB",
                f"            peak={big['peak_mb']:>7} MiB  "
                f"delta={big['delta_mb']:>6} MiB  rows={big['rows']}",
                f"  growth = {growth:.3f} MiB   budget = {DELTA_BUDGET_MB} MiB",
            ]
        )
    )


@pytest.mark.performance
def test_the_measurement_can_fail(tmp_path: pathlib.Path) -> None:
    """The reversed example: 403 MB read into the window on purpose, and it must blow up.

    Without this, "delta is small" is unfalsifiable. This commits the mistake the GUI must
    never make -- an actual ``read_bytes()`` of the whole file -- and requires the number to
    land so far past the budget that no reading of the criterion could call it a pass.
    """
    reversed_payload = _run("s400.xml", tmp_path, slurp=True)

    assert reversed_payload["delta_mb"] > DELTA_BUDGET_MB, (
        "the reversed example stayed under the budget, so this measurement cannot detect "
        f"a window that loads the document: {reversed_payload}"
    )
    # Not just over the line -- over it by an order of magnitude, because a sampler that was
    # merely a little unlucky would still be a useless measurement.
    assert reversed_payload["delta_mb"] > DELTA_BUDGET_MB * 4, (
        f"the reversed example barely exceeded the budget: {reversed_payload}"
    )

    print(
        f"\nG1 reversed example: delta={reversed_payload['delta_mb']} MiB "
        f"(budget {DELTA_BUDGET_MB} MiB, "
        f"{reversed_payload['delta_mb'] / DELTA_BUDGET_MB:.1f}x over) -- the measurement can fail"
    )


@pytest.mark.performance
def test_the_probe_runs_as_a_standalone_subprocess(tmp_path: pathlib.Path) -> None:
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
            str(REPO_ROOT / "data" / "s10.xml"),
            str(tmp_path),
        ],
        cwd=REPO_ROOT,
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


@pytest.mark.performance
def test_every_gui_probe_runs_standalone(tmp_path: pathlib.Path) -> None:
    """All three probes, run the way the evidence files say to run them.

    One probe being reproducible is luck; three being reproducible is a property of how they
    are written. Each is invoked as ``python -m tests.<probe>`` with the state-directory
    variable stripped, because that is what a reader copying a command out of a document will
    have in their environment -- which is to say, nothing this project set for them.

    **Each probe is asserted against its own payload schema, by name.** The per-schema
    facts below are the deterministic ones -- what must hold whichever way the race
    between the document's end and the cancel lands. What is deliberately *not* asserted
    is ``was_running_when_cancelled`` being a specific boolean: on this machine the 10 MB
    document finishes before the cancel lands (measured: the probe reports
    ``was_running_when_cancelled: false, killed: false, exit_code: 0``), and on a slower
    one it would not -- so either outcome is legitimate and only the self-consistency is
    a fact. The assertions must therefore hold on both branches.
    """
    source = REPO_ROOT / "data" / "s10.xml"
    env = {key: value for key, value in os.environ.items() if key != "GIGAXML_GUI_STATE_DIR"}
    env["QT_QPA_PLATFORM"] = "offscreen"

    probes = {
        "gui_mem": [sys.executable, "-m", "tests._gui_mem", str(source), str(tmp_path / "mem")],
        "gui_progress_probe": [
            sys.executable,
            "-m",
            "tests._gui_progress_probe",
            str(source),
            str(tmp_path / "progress"),
        ],
        "gui_cancel_probe": [
            sys.executable,
            "-m",
            "tests._gui_cancel_probe",
            str(source),
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
            cwd=REPO_ROOT,
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
            # bar-must-move claim is the 100 MB criterion's (tests/performance/
            # test_gui_progress.py), which runs a document long enough to owe several.
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
