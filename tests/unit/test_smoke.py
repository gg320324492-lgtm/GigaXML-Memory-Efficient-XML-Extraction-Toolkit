"""Minimal smoke tests: the package imports and exposes its version."""

from __future__ import annotations

import subprocess

import pytest

import gigaxml
from tests._interpreter import gigaxml_script


def test_version_is_exposed() -> None:
    """The package answers with a version string of the shape a release carries.

    The exact value is deliberately not pinned here: that is ``test_version.py``'s job,
    and pinning it in a second file would make a release a two-place edit -- the very
    accident the version tests exist to prevent.
    """
    parts = gigaxml.__version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


@pytest.mark.parametrize("command", ["extract", "inspect", "sample", "generate"])
def test_every_subcommand_help_renders(command: str) -> None:
    """``--help`` must not crash, and a bare ``%`` in a help string makes it.

    argparse runs help text through %-formatting, so a literal percent has to be
    written ``%%``. A single unescaped one turns ``--help`` for that command into a
    traceback -- which is how this test came to exist.
    """
    script = gigaxml_script()

    completed = subprocess.run(
        [str(script), command, "--help"], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "Traceback" not in completed.stderr
