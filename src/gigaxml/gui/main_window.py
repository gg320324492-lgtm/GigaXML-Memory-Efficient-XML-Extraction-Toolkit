"""The main window.

A shell around the panels, with the menus and the status line. The window is arranged so
that adding a panel is adding a tab rather than rearranging everything.

**Drops are handled here as well as in the document panel.** Qt delivers a drop to the
widget under the cursor, and a user aiming at the window's edges, the tab bar or the
status line is aiming at the window, not at the panel inside it. Refusing those would
make the feature work only when the pointer happened to be over one particular widget.
Both handlers go through the same ``open_document`` call, so there is still one way to
open a document.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QAction, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QLabel,
    QMainWindow,
    QTabWidget,
    QWidget,
)

from gigaxml.gui.panels.document import DocumentPanel
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.panels.structure import StructurePanel
from gigaxml.gui.recent_files import RecentFiles, default_state_dir

#: The file the recent-documents list lives in, under the state directory.
RECENT_FILES_NAME = "recent_files.json"


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(self, parent: QWidget | None = None, *, state_dir: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GigaXML")
        self.resize(1100, 720)
        self.setAcceptDrops(True)

        self._recent = RecentFiles((state_dir or default_state_dir()) / RECENT_FILES_NAME)

        self._tabs = QTabWidget(self)
        self._documents = DocumentPanel(self._recent, self)
        self._structure = StructurePanel(self)
        self._execution = ExecutionPanel(self)
        self._tabs.addTab(self._documents, "Document")
        self._tabs.addTab(self._structure, "Structure")
        self._tabs.addTab(self._execution, "Execute")
        self.setCentralWidget(self._tabs)

        # Opening a document anywhere updates the panel that analyses it. Wired here
        # rather than inside either panel: neither should have to know the other exists.
        self._documents.document_changed.connect(self._on_document_changed)

        self._build_menus()
        self.statusBar().showMessage("ready")

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open document…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._documents.choose_document)
        file_menu.addAction(open_action)

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu("&Help")
        about = QAction("&About", self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _on_document_changed(self, path: str) -> None:
        self._structure.set_document(path)
        self.statusBar().showMessage(f"opened {path}", 5000)

    def _show_about(self) -> None:
        from gigaxml import __version__

        self.statusBar().showMessage(f"GigaXML {__version__} — the CLI does the work", 5000)

    # -- dropping on the window --------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt naming)
        """Hand the drag to the document panel, which decides what it will take."""
        self._documents.dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt naming)
        """As above. One implementation, reached from two widgets."""
        self._documents.dropEvent(event)

    # -- accessors used by tests and by later substeps ---------------------

    def document_panel(self) -> DocumentPanel:
        return self._documents

    def structure_panel(self) -> StructurePanel:
        return self._structure

    def execution_panel(self) -> ExecutionPanel:
        return self._execution

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(f"{title}: not built yet")


__all__ = ["RECENT_FILES_NAME", "MainWindow", "placeholder"]
