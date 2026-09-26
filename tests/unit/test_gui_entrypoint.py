"""How one executable decides between being a window and being the CLI.

The frozen build ships a single binary, so :func:`gigaxml.gui.app.main` has to be both.
That makes a question of routing, and a wrong answer here is not a crash -- it is a window
where somebody asked for a command line, or a taskbar entry where they double-clicked a
file. Both are the kind of wrong that gets reported as "it doesn't work" with no detail.

The cases below are chosen from the shapes a real invocation takes: a double-click, a
drag-onto-the-icon, a command in a terminal, and a file that happens to share a name with a
subcommand.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Final

import pytest

from gigaxml.gui.app import _cli_commands, _cli_flags, _wants_cli

#: (arguments, expected to be the CLI, why this case is here)
CASES: Final = [
    ([], False, "a double-click: no arguments at all"),
    (["catalog.xml"], False, "a file dragged onto the icon"),
    (["a/b/c.xml"], False, "a path with directories in it"),
    (["extract", "a.xml"], True, "the everyday command line"),
    (["inspect", "a.xml"], True, "inspect takes no config file"),
    (["sample", "a.xml"], True, "sampling"),
    (["generate", "--size", "1MB", "-o", "x.xml"], True, "generation"),
    (["--version"], True, "the version flag, which the parser answers itself"),
    (["--help"], True, "the long help"),
    (["-h"], True, "the short help"),
    (["--unknown-flag"], False, "an option we do not know opens a window rather than erroring"),
    (["-x"], False, "a single-dash unknown option"),
    (["", "extract"], False, "an empty first argument is not a subcommand"),
    (["extractor.xml"], False, "a file whose name merely starts like a subcommand"),
    (["--", "extract"], False, "an end-of-options marker is not a subcommand"),
]


@pytest.mark.parametrize(("argv", "expected", "reason"), CASES, ids=lambda v: str(v)[:28])
def test_the_router_sends_each_shape_to_the_right_place(
    argv: list[str], expected: bool, reason: str
) -> None:
    assert _wants_cli(argv) is expected, reason


def test_the_subcommand_list_comes_from_the_parser() -> None:
    """Adding a subcommand must not need a second edit here.

    The failure this prevents is quiet: a new subcommand the router has never heard of would
    open a window instead of running, which looks like a packaging fault rather than a
    routing one.
    """
    from gigaxml.cli import build_parser

    commands = _cli_commands()
    assert commands, "no subcommands were found, so nothing would route to the CLI"
    assert "extract" in commands
    assert "inspect" in commands

    # And the router's own list is a subset of what the parser really accepts.
    import argparse

    accepted = {
        name
        for action in build_parser()._actions
        if isinstance(action, argparse._SubParsersAction)
        for name in action.choices
    }
    assert commands <= accepted, f"the router knows names the parser rejects: {commands - accepted}"


def test_the_flag_list_matches_the_parser() -> None:
    """``--version`` and the two help flags are handled by the parser without a subcommand."""
    from gigaxml.cli import build_parser

    options: set[str] = set()
    for action in build_parser()._actions:
        if action.option_strings:
            options.update(action.option_strings)
    assert set(_cli_flags()) <= options, (
        f"the router forwards flags the parser does not accept: {set(_cli_flags()) - options}"
    )


def test_a_missing_qt_is_reported_rather_than_traced() -> None:
    """The window half still says what to do when PySide6 is not installed.

    Not cosmetic: this is the message a CLI user gets if they install the package without
    the extra and type the GUI's name by hand.
    """
    code = (
        "import builtins, sys\n"
        "real = builtins.__import__\n"
        "def blocked(name, *a, **k):\n"
        "    if name.split('.')[0] == 'PySide6':\n"
        "        raise ImportError('no PySide6')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = blocked\n"
        "from gigaxml.gui.app import main\n"
        "raise SystemExit(main([]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1, completed.stderr
    assert "PySide6" in completed.stderr, completed.stderr
    assert "pip install" in completed.stderr, (
        f"the message does not say how to fix it: {completed.stderr}"
    )
    assert "Traceback" not in completed.stderr, "it died on a traceback instead of saying so"


def test_a_cli_argument_does_not_need_qt() -> None:
    """``--version`` must work in an environment where the window could not start.

    This is the half that keeps the two responsibilities from interfering: routing to the
    CLI happens before Qt is touched at all.
    """
    code = (
        "import builtins\n"
        "real = builtins.__import__\n"
        "def blocked(name, *a, **k):\n"
        "    if name.split('.')[0] == 'PySide6':\n"
        "        raise ImportError('no PySide6')\n"
        "    return real(name, *a, **k)\n"
        "builtins.__import__ = blocked\n"
        "from gigaxml.gui.app import main\n"
        "raise SystemExit(main(['--version']))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    from gigaxml import __version__

    assert completed.stdout.strip() == f"gigaxml {__version__}"
