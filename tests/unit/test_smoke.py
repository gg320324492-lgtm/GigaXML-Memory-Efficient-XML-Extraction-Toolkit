"""Minimal smoke tests: the package imports and exposes its version."""

from __future__ import annotations

import subprocess

import pytest

import gigaxml
from tests._interpreter import gigaxml_script


def test_version_is_exposed() -> None:
    assert gigaxml.__version__ == "0.1.0"


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
