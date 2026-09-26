"""G3 on a real long run: cancel ``data/s400.xml`` part-way and look at the disk.

``test_gui_process.py`` already covers cancellation against small documents -- its
``test_kill_really_stops_the_child`` and
``test_cancelling_a_checkpointed_run_leaves_the_committed_parts`` are the baseline this
file extends rather than repeats. The gap they cannot close is *timing*: a small document
is usually finished before a cancel lands, so "cancelling a long run leaves nothing
half-written" has never actually been observed on a long run.

Each case runs in a fresh interpreter (``tests/_gui_cancel_probe.py``) and is marked
``performance``, because it drives a 403 MB document and takes seconds.

What is asserted, and why each one alone would pass for the wrong reason:

* ``was_running_when_cancelled`` -- otherwise "the cancel worked" might just mean the job had
  already ended, which proves nothing about cancelling;
* ``pid_alive_after_cancel`` -- ``kill()`` returning is not the child being gone;
* no half-written target -- the atomic-write promise, checked against sizes on disk rather
  than against a return value;
* ``committed_parts`` non-empty -- cancelling must not throw away work already safely
  written, and an empty list would mean the claim went untested rather than tested.

``S400`` is an absolute path because the probe runs with ``cwd`` at the repository root
while pytest's own working directory is wherever it was started. A relative path would pass
locally and fail in CI for no visible reason -- this project has been bitten by that twice.
"""

from __future__ import annotations

import pathlib
from typing import Final

import pytest

from tests._gui_cancel_probe import REPO_ROOT, cancel_in_subprocess

pytestmark = pytest.mark.performance

#: The 403 MB document the criterion names. Absolute, see the module docstring.
S400: Final = REPO_ROOT / "data" / "s400.xml"


def test_cancelling_a_long_run_leaves_no_half_written_target(tmp_path: pathlib.Path) -> None:
    """Plain mode: the child dies, and no truncated CSV is left where a reader would find it."""
    payload = cancel_in_subprocess(S400, tmp_path / "plain")

    assert payload["was_running_when_cancelled"] is True, (
        f"the run was over before the cancel landed, so nothing was measured: {payload}"
    )
    assert payload["pid"] is not None, f"no child was ever started: {payload}"
    assert payload["pid_alive_after_cancel"] is False, (
        f"the child was still alive after cancel: {payload}"
    )
    assert payload["killed"] is True, f"the run was not reported as cancelled: {payload}"

    # A truncated .csv is the failure this whole design is about: a reader cannot tell it
    # from a finished one. Either the file is absent, or it is empty -- never partial.
    siblings = payload["siblings"]
    assert isinstance(siblings, list)
    partial = [
        item
        for item in siblings
        if isinstance(item, dict)
        and str(item.get("name", "")).endswith(".csv")
        and int(item.get("bytes", 0)) > 0
    ]
    assert not partial, f"a non-empty .csv was left beside the target: {partial}"

    print(f"\nG3 plain cancel: {payload}")


def test_cancelling_a_checkpointed_run_keeps_the_committed_parts(
    tmp_path: pathlib.Path,
) -> None:
    """Checkpoint mode: work already on disk survives the cancel."""
    payload = cancel_in_subprocess(S400, tmp_path / "ckpt", checkpoint=True)

    assert payload["was_running_when_cancelled"] is True, f"nothing was measured: {payload}"
    assert payload["pid_alive_after_cancel"] is False, f"the child survived: {payload}"

    parts = payload["committed_parts"]
    assert isinstance(parts, list)
    # The part size is deliberately small in the probe so this list is not empty. Asserting
    # it is the difference between "cancelling keeps committed work" and "cancelling never
    # had any committed work to keep", which look identical from the outside.
    assert parts, (
        "no part was committed before the cancel, so 'committed parts survive' was not "
        f"actually tested: {payload}"
    )

    print(f"\nG3 checkpoint cancel: {payload}")
