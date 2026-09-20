"""Minimal smoke tests: the package imports and exposes its version."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import gigaxml


def test_version_is_exposed() -> None:
    assert gigaxml.__version__ == "0.1.0"


@pytest.mark.parametrize("command", ["extract", "inspect", "sample", "generate"])
def test_every_subcommand_help_renders(command: str) -> None:
    """``--help`` must not crash, and a bare ``%`` in a help string makes it.

    argparse runs help text through %-formatting, so a literal percent has to be
    written ``%%``. A single unescaped one turns ``--help`` for that command into a
    traceback -- which is how this test came to exist.
    """
    script = Path(sys.executable).parent / ("gigaxml.exe" if sys.platform == "win32" else "gigaxml")
    if not script.exists():  # pragma: no cover - depends on the environment
        pytest.skip(f"console script not installed at {script}")

    completed = subprocess.run(
        [str(script), command, "--help"], capture_output=True, text=True, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert "Traceback" not in completed.stderr
