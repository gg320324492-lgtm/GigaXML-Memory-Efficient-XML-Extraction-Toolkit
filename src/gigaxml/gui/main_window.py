"""The main window.

A shell around the panels, with the menus and the status line. This substep builds the
execution panel only; the remaining areas are added one at a time, and the window is
arranged so that adding one is adding a tab rather than rearranging everything.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QTabWidget,
    QWidget,
)

from gigaxml.gui.panels.execution import ExecutionPanel


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GigaXML")
        self.resize(1100, 720)

        self._tabs = QTabWidget(self)
        self._execution = ExecutionPanel(self)
        self._tabs.addTab(self._execution, "Execute")
        self.setCentralWidget(self._tabs)

        self._build_menus()
        self.statusBar().showMessage("ready")

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open document…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._execution.choose_source)
        file_menu.addAction(open_action)

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _show_about(self) -> None:
        from gigaxml import __version__

        self.statusBar().showMessage(f"GigaXML {__version__} — the CLI does the work", 5000)

    # -- accessors used by tests and by later substeps ---------------------

    def execution_panel(self) -> ExecutionPanel:
        return self._execution

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(f"{title}: not built yet")
