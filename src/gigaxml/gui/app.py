"""The desktop application's entry point.

This module is the only one that must exist for ``gigaxml-gui`` to work, and it is the
only place that imports Qt at module scope -- everything it imports from here on assumes
Qt is present. That is why the import lives inside :func:`main` rather than at the top:
the friendly error below is only possible if the failure happens where it can be caught.

**It is also the whole frozen executable's front door.** PyInstaller gives us one binary,
so this has to be both the window and the CLI: launched with a command it dispatches to
:func:`gigaxml.cli.main`, launched with nothing it opens the window. That is deliberate
rather than the cheaper-looking alternative of shipping two executables -- the window finds
its CLI by asking the interpreter what it is, and a single binary has no "the other one is
missing" failure mode at all.
"""

from __future__ import annotations

import argparse
import sys


def _cli_flags() -> tuple[str, ...]:
    """The options the top-level parser answers itself, and so never take a subcommand."""
    return ("--version", "-h", "--help")


def _cli_commands() -> frozenset[str]:
    """The CLI's subcommand names, read from its own parser.

    **Read rather than written out.** A second copy of this list is a second thing to
    forget when a subcommand is added, and the failure mode is quiet: the new command would
    open a window instead of running, which looks like a packaging bug rather than a
    dispatcher one.
    """
    from gigaxml.cli import build_parser

    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return frozenset(action.choices)
    return frozenset()


def _wants_cli(argv: list[str]) -> bool:
    """Whether these arguments are addressed to the CLI rather than to the window.

    **A leading option is the signal, not any option.** ``gigaxml-gui somefile.xml`` is a
    double-click with a file, which is a window with a document -- whereas
    ``gigaxml-gui extract --help`` is a question about the CLI. Deciding on the first
    argument keeps the two from bleeding into each other.

    **Every other argument opens the window.** Guessing wrong in that direction is much the
    cheaper mistake: a stray option produces a window, whereas guessing the other way turns a
    double-click into a CLI error and a taskbar entry nobody asked for.

    **One case is genuinely ambiguous, and the subcommand wins.** A lone ``inspect`` is both
    a subcommand name and a perfectly good filename. There is no way to tell them apart, and
    the same is true of every command-line tool that takes subcommands -- ``git status`` is a
    command, not a file. Opening the window for a file therefore needs a path that is not
    also a command name, which in practice means a file that carries an extension or lives
    in a directory: ``gigaxml-gui catalog.xml`` opens a window either way. The cost is that
    a file literally named ``inspect`` opens the CLI instead; the alternative -- preferring
    paths -- would make ``gigaxml-gui extract`` unable to print that subcommand's help, which
    is the more common thing a person does.
    """
    if not argv:
        return False
    first = argv[0]
    return first in _cli_flags() or first in _cli_commands()


def main(argv: list[str] | None = None) -> int:
    """Start the window, or the CLI if these arguments are addressed to it.

    Returns the process exit code.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)

    if _wants_cli(arguments):
        # Imported here, not at the top: the CLI must stay importable in an environment
        # where PySide6 is absent, and this module's own import cost should not decide that.
        from gigaxml.cli import main as cli_main

        return cli_main(arguments)

    return _run_window(arguments)


def _run_window(argv: list[str]) -> int:
    """Start the window. Returns the process exit code.

    Qt is an optional extra -- a 150-200 MB dependency that only somebody using the
    window needs -- so the first thing this does is check whether it is there. A bare
    ``ImportError`` traceback would tell a user nothing they can act on; one line telling
    them the exact command to run is the difference between a bug report and a fix.
    """
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print(
            'error: the GUI needs PySide6. Install it with: pip install -e ".[gui]"',
            file=sys.stderr,
        )
        return 1

    from gigaxml.gui.main_window import MainWindow

    application = QApplication([sys.argv[0], *argv])
    application.setApplicationName("GigaXML")
    application.setApplicationVersion(_version())

    window = MainWindow()
    window.show()
    return application.exec()


def _version() -> str:
    from gigaxml import __version__

    return __version__


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
