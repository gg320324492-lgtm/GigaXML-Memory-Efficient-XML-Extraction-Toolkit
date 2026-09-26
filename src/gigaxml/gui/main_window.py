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

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QMainWindow,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gigaxml.checkpoint import CheckpointError
from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_CHECKPOINT,
    KIND_FREE_TARGET,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
)
from gigaxml.gui.i18n import set_language, tr
from gigaxml.gui.panels.batch import BatchPanel
from gigaxml.gui.panels.document import DocumentPanel
from gigaxml.gui.panels.errors import ErrorPanel
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.panels.fields import FieldConfigPanel
from gigaxml.gui.panels.preview import PreviewPanel
from gigaxml.gui.panels.results import ResultPanel
from gigaxml.gui.panels.settings import SettingsPanel
from gigaxml.gui.panels.structure import StructurePanel
from gigaxml.gui.recent_files import RecentFiles, default_state_dir
from gigaxml.gui.run_report import (
    failure_from_stderr,
    left_behind,
    read_failure,
    read_outcome,
)
from gigaxml.gui.saved_configs import ConfigLibrary
from gigaxml.gui.settings import Settings, SettingsStore

#: The file the recent-documents list lives in, under the state directory.
RECENT_FILES_NAME = "recent_files.json"
SETTINGS_FILE_NAME = "settings.json"


class MainWindow(QMainWindow):
    """The application window."""

    def __init__(self, parent: QWidget | None = None, *, state_dir: Path | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("GigaXML")
        self.resize(1100, 720)
        self.setAcceptDrops(True)

        self._state_dir = state_dir or default_state_dir()
        self._recent = RecentFiles(self._state_dir / RECENT_FILES_NAME)
        # ⑨ 与 ⑧ 的持久化落点,和「最近文件」同一个目录、同一个注入方式.
        self._settings_store = SettingsStore(self._state_dir / SETTINGS_FILE_NAME)
        self._config_library = ConfigLibrary(self._state_dir)
        # The language is read before anything is built, because every panel writes its
        # labels during construction and :mod:`gigaxml.gui.i18n` has no retranslate pass:
        # a window built first and translated after would come up in whichever language the
        # last launch used, with this launch's choice silently ignored until the next one.
        # Deliberately NOT applied again from `_apply_settings`: mid-session that would
        # mix two languages on screen, since only strings built from then on would follow.
        # The settings panel says the change takes effect after a restart.
        set_language(self._settings_store.read().language)

        self._tabs = QTabWidget(self)
        self._documents = DocumentPanel(self._recent, self)
        self._structure = StructurePanel(self)
        self._fields = FieldConfigPanel(self, library=self._config_library)
        self._preview = PreviewPanel(self)
        self._execution = ExecutionPanel(self)
        self._results = ResultPanel(self)
        self._errors = ErrorPanel(self)
        self._batch = BatchPanel(self)
        self._settings = SettingsPanel(self._settings_store, self)
        # The two outcome panels live with the run rather than in tabs of their own: they
        # are about the thing that just happened in this tab, and both are empty until
        # something happens. They are mutually exclusive, and the window is what keeps them
        # so -- a run either finished, was interrupted, or failed.
        self._execute_tab = QWidget(self)
        execute_layout = QVBoxLayout(self._execute_tab)
        execute_layout.addWidget(self._execution, 1)
        execute_layout.addWidget(self._results)
        execute_layout.addWidget(self._errors)
        for panel, title in (
            (self._documents, tr("Document")),
            (self._structure, tr("Structure")),
            (self._fields, tr("Fields")),
            (self._preview, tr("Preview")),
            (self._execute_tab, tr("Execute")),
            (self._batch, tr("Batch")),
            (self._settings, tr("Settings")),
        ):
            self._tabs.addTab(panel, title)
        self.setCentralWidget(self._tabs)

        self._connect_panels()
        # ⑨ 设置要「真的生效」:窗口一开就把存下来的偏好应用到该应用的地方.
        self._settings.settings_changed.connect(self._apply_settings)
        self._apply_settings(self._settings_store.read())
        self._build_menus()
        self.statusBar().showMessage(tr("ready"))

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
        self._results.path_copy_requested.connect(self._on_path_copy_requested)

    def _on_run_start_failed(self, message: str, error_type: str) -> None:
        """A run that never started, because the config would not load.

        There is no report, but there is still a kind: the loader raised in this process,
        so the exception's class is in hand even though nothing was written down. Passing
        it through is what lets a bad namespace prefix get its own advice rather than the
        general one -- and it is dispatch by type, which is the same rule the report path
        follows. The message is the project's own, which is why it is shown verbatim.
        """
        self._results.clear()
        self._errors.show_failure(
            failure_from_stderr([message], None, error_type=error_type), [message]
        )

    def _on_run_finished(self) -> None:
        """Say what happened: it finished, it was stopped, or it failed.

        **The child's own exit status is what decides, and it is asked first.** The report
        beside the output belongs to whichever run last reached its end, and a run that is
        stopped never overwrites it -- so for a stopped run the report is an *earlier* run's
        answer. Reading it would report a success, or a failure of the wrong kind, for a run
        that never got that far. Measured: a successful run to ``out.csv`` followed by a
        killed run to the same path leaves the first run's report saying ``rows: 2``.

        The report is then used for what it is good at -- the kind of a failure, and the
        numbers of a success -- and the disk for the one thing no report can say: that the
        run stopped partway and left work behind.
        """
        run = self._execution.run_result()
        if run is None:
            return
        output = self._execution.output_path()
        checkpointing = self._execution.is_checkpointing()
        left = left_behind(output, checkpointing=checkpointing)

        if run.killed:
            self._errors.clear()
            self._results.show_unfinished(left)
            return

        if run.ok:
            self._errors.clear()
            self._results.show_summary(read_outcome(output, checkpointing=checkpointing))
            return

        # It failed. **Whether this run said anything decides where to look next**, because
        # everything on disk can belong to an earlier run: a report is only overwritten by a
        # run that reaches the end, and a stopped checkpointed run leaves a manifest that
        # nothing removes.
        #
        # A run that failed on its own printed its error to stderr, so ``warnings`` -- stderr
        # without the progress events -- is non-empty and the report beside the output is
        # this run's. A run killed from outside says nothing at all, so a report that is
        # there belongs to an earlier one. Measured: a WriterError followed by a hard kill to
        # the same output was reported as the WriterError again, advice and all, with this
        # run's ``.tmp`` sitting there unmentioned.
        if run.warnings:
            failure = read_failure(output, checkpointing=checkpointing)
            if failure is None:
                failure = failure_from_stderr(
                    list(run.warnings), run.exit_code, error_type=self._unreported_kind()
                )
            self._results.clear()
            self._errors.show_failure(failure, list(run.stderr_lines))
            return

        if left is not None:
            # It said nothing and work is on disk: stopped from outside, which the child
            # cannot report on.
            self._errors.clear()
            self._results.show_unfinished(left)
            return

        self._results.clear()
        self._errors.show_failure(
            failure_from_stderr(list(run.stderr_lines), run.exit_code), list(run.stderr_lines)
        )

    def _unreported_kind(self) -> str | None:
        """What kind of failure a run that wrote no report is, when anything is known.

        **Not read from the message.** The one thing worse than not classifying is
        classifying by matching the wording: it breaks the first time a message is reworded
        and looks like it still works until then. What this uses is a fact about the
        *request* instead.

        A refused ``--resume`` is the case this exists for. ``validate_resume`` runs before
        the CLI has a report to write to, so that failure arrives with nothing on disk to
        read a type from -- but the panel knows it asked to resume, and a resumed run that
        dies before writing anything is one the checkpoint refused. A missing checkpoint
        raises the same error, which is why the advice covers both.
        """
        return CheckpointError.__name__ if self._execution.is_resuming() else None

    def _on_path_copy_requested(self, path: str) -> None:
        QApplication.clipboard().setText(path)
        self.statusBar().showMessage(tr("copied {}").format(path), 5000)

    def _on_error_action(self, kind: str) -> None:
        """Do the thing the advice suggested. The panel does not know what any of it means.

        Every branch ends somewhere the user can see: a setting changed, a path on the
        clipboard, or a different tab. An advice button that only re-worded the message
        would be decoration.
        """
        if kind == KIND_QUARANTINE:
            self._execution.set_on_error("quarantine")
            self._tabs.setCurrentWidget(self._execute_tab)
            self.statusBar().showMessage(tr("on_error set to quarantine"), 5000)
            return
        if kind == KIND_FREE_TARGET:
            failure = self._errors.failure()
            target = failure.partial_path if failure is not None else None
            if target is not None:
                QApplication.clipboard().setText(str(target))
                self.statusBar().showMessage(tr("copied {}").format(target), 5000)
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
                    tr("press Analyse on this tab to see what the document declares"), 8000
                )
            else:
                self.statusBar().showMessage(tr("the document's namespaces are listed here"), 5000)
            return
        if kind == KIND_CHECK_CONFIG:
            self._tabs.setCurrentWidget(self._fields)
            self.statusBar().showMessage(tr("the config is in the Fields tab"), 5000)
            return
        if kind == KIND_CHECKPOINT:
            # The checkpoint lives inside the output directory, so the directory is the
            # path worth having: it is what has to be pointed at the right source, or
            # emptied to start over. **Copied, not removed.** Starting over means throwing
            # away parts the user may have spent an hour on, and that is theirs to decide
            # with the path in hand rather than something this window does for them.
            directory = self._execution.output_path()
            QApplication.clipboard().setText(str(directory))
            self.statusBar().showMessage(tr("copied {}").format(directory), 5000)

    def _on_document_changed(self, path: str) -> None:
        self._structure.set_document(path)
        self._preview.set_source(path)
        # The field panel needs it too: the CLI cannot write a config for a candidate
        # without the document that candidate came from.
        self._fields.set_source(path)
        self.statusBar().showMessage(tr("opened {}").format(path), 5000)

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
        file_menu = self.menuBar().addMenu(tr("&File"))

        open_action = QAction(tr("&Open document…"), self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self._documents.choose_document)
        file_menu.addAction(open_action)

        save_action = QAction(tr("&Save config…"), self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self._fields.choose_save_path)
        file_menu.addAction(save_action)

        quit_action = QAction(tr("&Quit"), self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = self.menuBar().addMenu(tr("&Help"))
        about = QAction(tr("&About"), self)
        about.triggered.connect(self._show_about)
        help_menu.addAction(about)

    def _show_about(self) -> None:
        from gigaxml import __version__

        self.statusBar().showMessage(
            tr("GigaXML {} — the CLI does the work").format(__version__), 5000
        )

    # -- dropping on the window --------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt naming)
        """Hand the drag to the document panel, which decides what it will take."""
        self._documents.dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt naming)
        """As above. One implementation, reached from two widgets."""
        self._documents.dropEvent(event)

    # -- preferences -----------------------------------------------------

    def _apply_settings(self, settings: Settings) -> None:
        """Put the stored preferences where they are actually used.

        A preferences panel that writes a file and changes nothing is a panel that lies,
        so this is the part that makes ⑨ real: the combo boxes on the execution panel get
        the stored format and error policy, and the batch size is what the next run asks
        the CLI for.
        """
        self._execution.apply_defaults(
            output_format=settings.format,
            on_error=settings.on_error,
            batch_size=settings.batch_size,
        )
        self._apply_theme(settings.theme)

    @staticmethod
    def _apply_theme(theme: str) -> None:
        """Switch the palette, or ask the platform what the platform is doing.

        ``system`` is the default and means "leave it alone": a window that ignores the
        rest of the desktop is a window people turn off.
        """
        if theme == "system":
            return
        app = QApplication.instance()
        if app is None:
            return
        if not hasattr(app, "setStyle") or not hasattr(app, "styleHints"):
            return
        try:
            app.styleHints().setColorScheme(
                Qt.ColorScheme.Dark if theme == "dark" else Qt.ColorScheme.Light
            )
        except (AttributeError, RuntimeError):
            # An older Qt without ColorScheme, or one that will not let us switch. Either
            # way the answer is to carry on, not to stop the window opening.
            return

    def settings_store(self) -> SettingsStore:
        """Where the preferences live. Exposed so a test can point them somewhere else."""
        return self._settings_store

    def config_library(self) -> ConfigLibrary:
        """The saved-configuration library. Exposed for the same reason."""
        return self._config_library

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Let the panels give up what is not the user's to clean up.

        Qt does not deliver ``closeEvent`` to a child widget: closing the window hides and
        destroys the panels rather than closing them. Without this, a temporary config the
        execution panel wrote would outlive the window that needed it -- **and so would
        every panel's child process and its run directory.** Measured: only the execution
        panel used to be shut down here, so the preview and structure panels left their
        children running and their directories behind, one ``run-report.json`` per abandoned
        run, with the reader threads of those children still alive afterwards.
        """
        self._execution.shutdown()
        self._preview.shutdown()
        self._structure.shutdown()
        self._fields.shutdown()
        self._batch.shutdown()
        self._settings.shutdown()
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

    def result_panel(self) -> ResultPanel:
        return self._results

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(tr("{}: not built yet").format(title))


__all__ = ["RECENT_FILES_NAME", "MainWindow", "placeholder"]
