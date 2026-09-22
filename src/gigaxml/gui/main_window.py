"""The main window.

A shell around the panels, with the menus and the status line. The window is arranged so
that adding a panel is adding a tab rather than rearranging everything.

**The tabs are in the order the work is done.** A document is opened, its structure is
analysed, the fields to extract are configured from what was found, the result is
previewed, and then the extraction is run. The window is where the panels are introduced
to each other -- no panel imports another, and each is driven through the same small set
of methods a test would use.

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
from gigaxml.gui.panels.fields import FieldConfigPanel
from gigaxml.gui.panels.preview import PreviewPanel
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
        self._fields = FieldConfigPanel(self)
        self._preview = PreviewPanel(self)
        self._execution = ExecutionPanel(self)
        for panel, title in (
            (self._documents, "Document"),
            (self._structure, "Structure"),
            (self._fields, "Fields"),
            (self._preview, "Preview"),
            (self._execution, "Execute"),
        ):
            self._tabs.addTab(panel, title)
        self.setCentralWidget(self._tabs)

        self._connect_panels()
        self._build_menus()
        self.statusBar().showMessage("ready")

    def _connect_panels(self) -> None:
        """Introduce the panels to each other. Wired here, so none of them knows another.

        Every link below is one panel telling the window that something changed, and the
        window passing the consequence to whichever panel needs it. A panel that reached
        into another directly would be untestable on its own and would make the tabs
        order-dependent.
        """
        self._documents.document_changed.connect(self._on_document_changed)
        self._structure.report_changed.connect(self._on_report_changed)
        self._structure.candidate_changed.connect(self._fields.set_candidate)
        self._fields.config_changed.connect(self._on_fields_changed)

    def _on_document_changed(self, path: str) -> None:
        self._structure.set_document(path)
        self._preview.set_source(path)
        self.statusBar().showMessage(f"opened {path}", 5000)

    def _on_report_changed(self) -> None:
        """Give the field panel what the analysis found: namespaces and the path list.

        The namespaces matter because a field path is resolved against them, and the path
        list is what the path boxes complete against. Both come from the report rather
        than from the user retyping them.
        """
        report = self._structure.report()
        if report is None:
            return
        self._fields.set_namespaces(report.namespaces)
        self._fields.set_path_choices(tuple(entry.path for entry in report.paths))

    def _on_fields_changed(self) -> None:
        """Hand the preview a config, but only one the project has accepted.

        A mapping the CLI would reject is not passed on: the preview would then fail with
        a message the user has already been shown, which reads as the preview being broken
        rather than the config being wrong.
        """
        self._preview.set_config(self._fields.valid_config_dict())

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        open_action = QAction("&Open document…", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._documents.choose_document)
        file_menu.addAction(open_action)

        save_action = QAction("&Save config…", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._fields.choose_save_path)
        file_menu.addAction(save_action)

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

    def field_panel(self) -> FieldConfigPanel:
        return self._fields

    def preview_panel(self) -> PreviewPanel:
        return self._preview

    def execution_panel(self) -> ExecutionPanel:
        return self._execution

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(f"{title}: not built yet")


__all__ = ["RECENT_FILES_NAME", "MainWindow", "placeholder"]
