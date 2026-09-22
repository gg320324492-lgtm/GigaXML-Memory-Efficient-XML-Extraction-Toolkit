"""Shared fixtures for the integration tests.

**The temporary-directory fixture is about hygiene, not about the product.** A panel keeps
one run directory alive at a time -- that is what the preview's rejected tab and the
structure panel's example values read -- and it discards the previous one when the next
run starts. A window that samples a hundred times therefore leaves one directory, which is
the intended behaviour.

The tests, though, are each their own session: every test builds a fresh window, and each
one leaves its last directory behind. Twenty-odd tests that sample add twenty-odd
directories to the system temporary folder per run, which is how a suite quietly becomes a
source of litter. This fixture removes what the test made and nothing else.
"""

from __future__ import annotations

import pathlib
import tempfile
from collections.abc import Iterator

import pytest

from gigaxml.gui.sampling import RUN_PREFIX, discard_run_directory


def _run_directories() -> set[pathlib.Path]:
    return set(pathlib.Path(tempfile.gettempdir()).glob(f"{RUN_PREFIX}*"))


@pytest.fixture(autouse=True)
def _clean_up_run_directories() -> Iterator[None]:
    """Remove the run directories this test created.

    Only directories, and only ones named the way this project names them -- the removal
    goes through the same guarded helper the panels use, so a mistake here cannot reach
    anything that was not ours.
    """
    before = _run_directories()
    yield
    for created in _run_directories() - before:
        discard_run_directory(created)
