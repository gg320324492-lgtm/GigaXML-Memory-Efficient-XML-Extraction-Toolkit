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

from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_FREE_TARGET,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
)
from gigaxml.gui.panels.document import DocumentPanel
from gigaxml.gui.panels.errors import ErrorPanel
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.panels.fields import FieldConfigPanel
from gigaxml.gui.panels.preview import PreviewPanel
from gigaxml.gui.panels.structure import StructurePanel
from gigaxml.gui.recent_files import RecentFiles, default_state_dir
from gigaxml.gui.run_report import failure_from_stderr, read_failure

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
        self._errors = ErrorPanel(self)
        # The error panel lives with the run rather than in a tab of its own: it is about
        # the thing that just happened in this tab, and it is empty until something fails.
        self._execute_tab = QWidget(self)
        execute_layout = QVBoxLayout(self._execute_tab)
        execute_layout.addWidget(self._execution, 1)
        execute_layout.addWidget(self._errors)
        for panel, title in (
            (self._documents, "Document"),
            (self._structure, "Structure"),
            (self._fields, "Fields"),
            (self._preview, "Preview"),
            (self._execute_tab, "Execute"),
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
        self._structure.candidate_changed.connect(self._on_candidate_changed)
        self._fields.config_changed.connect(self._on_fields_changed)
        self._execution.finished.connect(self._on_run_finished)
        self._execution.start_failed.connect(self._on_run_start_failed)
        self._errors.action_requested.connect(self._on_error_action)

    def _on_run_start_failed(self, message: str, error_type: str) -> None:
        """A run that never started, because the config would not load.

        There is no report, but there is still a kind: the loader raised in this process,
        so the exception's class is in hand even though nothing was written down. Passing
        it through is what lets a bad namespace prefix get its own advice rather than the
        general one -- and it is dispatch by type, which is the same rule the report path
        follows. The message is the project's own, which is why it is shown verbatim.
        """
        self._errors.show_failure(
            failure_from_stderr([message], None, error_type=error_type), [message]
        )

    def _on_run_finished(self) -> None:
        """Say what went wrong, if anything did.

        The report is where the CLI's own classification lives -- ``error.type``, the
        exception class name -- and it is written on failure as well as on success. When
        there is no report, the run died before the CLI could create one: that is the
        config that will not load, and there is no type to dispatch on. It is still shown,
        with the message and the raw output, rather than swallowed for being unclassified.
        """
        run = self._execution.run_result()
        if run is None or run.killed or run.ok:
            self._errors.clear()
            return
        failure = read_failure(
            self._execution.output_path(),
            checkpointing=self._execution.is_checkpointing(),
        )
        if failure is None:
            failure = failure_from_stderr(list(run.stderr_lines), run.exit_code)
        self._errors.show_failure(failure, list(run.stderr_lines))

    def _on_error_action(self, kind: str) -> None:
        """Do the thing the advice suggested. The panel does not know what any of it means.

        Every branch ends somewhere the user can see: a setting changed, a path on the
        clipboard, or a different tab. An advice button that only re-worded the message
        would be decoration.
        """
        if kind == KIND_QUARANTINE:
            self._execution.set_on_error("quarantine")
            self._tabs.setCurrentWidget(self._execute_tab)
            self.statusBar().showMessage("on_error set to quarantine", 5000)
            return
        if kind == KIND_FREE_TARGET:
            failure = self._errors.failure()
            target = failure.partial_path if failure is not None else None
            if target is not None:
                QApplication.clipboard().setText(str(target))
                self.statusBar().showMessage(f"copied {target}", 5000)
            return
        if kind == KIND_NAMESPACES:
            self._tabs.setCurrentWidget(self._structure)
            # The panel is empty until the document has been analysed, and the advice says
            # the prefixes are listed there. Saying which of the two states the user has
            # landed in is the difference between arriving somewhere and arriving at
            # nothing -- the panel itself cannot tell them, because an unanalysed document
            # and a document with no namespaces look the same on screen.
            if self._structure.report() is None:
                self.statusBar().showMessage(
                    "press Analyse on this tab to see what the document declares", 8000
                )
            else:
                self.statusBar().showMessage("the document's namespaces are listed here", 5000)
            return
        if kind == KIND_CHECK_CONFIG:
            self._tabs.setCurrentWidget(self._fields)
            self.statusBar().showMessage("the config is in the Fields tab", 5000)

    def _on_document_changed(self, path: str) -> None:
        self._structure.set_document(path)
        self._preview.set_source(path)
        # The field panel needs it too: the CLI cannot write a config for a candidate
        # without the document that candidate came from.
        self._fields.set_source(path)
        self.statusBar().showMessage(f"opened {path}", 5000)

    def _on_candidate_changed(self, candidate: object) -> None:
        """Pass the candidate, and its number in the report, to the field panel.

        The number is not the row the user clicked. The candidate table sorts itself, so
        the view row and the report index are different numbers -- and it is the report
        index, 1-based, that ``inspect --generate-config`` takes.
        """
        self._fields.set_candidate(candidate, self._structure.selected_candidate_index())

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

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Let the panels give up what is not the user's to clean up.

        Qt does not deliver ``closeEvent`` to a child widget: closing the window hides and
        destroys the panels rather than closing them. Without this, a temporary config the
        execution panel wrote would outlive the window that needed it.
        """
        self._execution.shutdown()
        super().closeEvent(event)

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

    def error_panel(self) -> ErrorPanel:
        return self._errors

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(f"{title}: not built yet")


__all__ = ["RECENT_FILES_NAME", "MainWindow", "placeholder"]
