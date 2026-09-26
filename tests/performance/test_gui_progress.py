"""G4: the progress the window shows is the progress that happened.

``test_gui_results.py`` already asserts ``outcome.rows == payload["rows"] == 3`` on a
three-record fixture. That is a real assertion, and it is also small enough that an
off-by-one at a part boundary or a rounding error in a percentage would never appear. This
runs the 100 MB document, where the numbers are large enough for those mistakes to show.

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

Marked ``performance``: it drives 100 MB and takes seconds.
"""

from __future__ import annotations

import pathlib
from typing import Final

import pytest

from tests._gui_progress_probe import REPO_ROOT, progress_in_subprocess

pytestmark = pytest.mark.performance

#: The 100 MB document. Absolute, because the probe runs with its own working directory.
S100: Final = REPO_ROOT / "data" / "s100.xml"


def test_progress_agrees_with_the_report_and_the_manifest(tmp_path: pathlib.Path) -> None:
    """Four sources, one number. The window, the report, the disk and the manifest agree."""
    payload = progress_in_subprocess(S100, tmp_path / "run")

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

    print(
        f"\nG4 progress: gui={payload['gui_last_records']} report={payload['report_rows']} "
        f"disk={payload['rows_on_disk']} manifest={expected} "
        f"({payload['gui_updates']} GUI updates)"
    )
