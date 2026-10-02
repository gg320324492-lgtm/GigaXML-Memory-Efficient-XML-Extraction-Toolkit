"""Minimal smoke tests: the package imports and exposes its version."""

from __future__ import annotations

import subprocess

import pytest

import gigaxml
from tests._interpreter import gigaxml_script


def test_version_is_exposed() -> None:
    """The package answers with a version, and ``--version`` is that same string.

    **The shape of the version is not checked here.** It used to be -- the same three
    lines ``test_version.py`` has -- and that was this file's own stated reason to avoid:
    *"pinning it in a second file would make a release a two-place edit, the very accident
    the version tests exist to prevent."* It was not avoided. Releasing ``2.0.0rc1`` meant
    editing both copies or leaving one red, which is the accident, committed.

    So the check lives in :mod:`tests.unit.test_version` and only there, where the whole
    vocabulary is spelled out and the refusals are pinned beside it. What is left here is
    what a smoke test is for: the attribute exists, it is a non-empty string, and the flag
    that prints it is wired to it. A version of the wrong *shape* is a release problem;
    a version that is not there at all is an import problem, and this is the test for it.
    """
    assert isinstance(gigaxml.__version__, str)
    assert gigaxml.__version__

    script = gigaxml_script()
    completed = subprocess.run(
        [str(script), "--version"], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert gigaxml.__version__ in completed.stdout


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
