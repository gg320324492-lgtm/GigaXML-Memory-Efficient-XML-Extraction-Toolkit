"""The desktop application's entry point.

This module is the only one that must exist for ``gigaxml-gui`` to work, and it is the
only place that imports Qt at module scope -- everything it imports from here on assumes
Qt is present. That is why the import lives inside :func:`main` rather than at the top:
the friendly error below is only possible if the failure happens where it can be caught.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
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

    application = QApplication(argv if argv is not None else sys.argv)
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
