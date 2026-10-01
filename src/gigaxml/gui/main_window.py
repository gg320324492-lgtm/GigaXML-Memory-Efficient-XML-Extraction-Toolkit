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
    QMessageBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gigaxml.checkpoint import CheckpointError
from gigaxml.errors import GigaXMLError
from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_CHECKPOINT,
    KIND_FREE_TARGET,
    KIND_INTERRUPTED,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
)
from gigaxml.gui.i18n import set_language, tr
from gigaxml.gui.job_history import HistoryEntry, JobHistory
from gigaxml.gui.panels.batch import BatchPanel
from gigaxml.gui.panels.document import DocumentPanel
from gigaxml.gui.panels.errors import ErrorPanel
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.panels.fields import FieldConfigPanel
from gigaxml.gui.panels.history import HistoryPanel
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
from gigaxml.gui.run_state import RunState
from gigaxml.gui.saved_configs import ConfigLibrary
from gigaxml.gui.settings import Settings, SettingsStore

#: The file the recent-documents list lives in, under the state directory.
RECENT_FILES_NAME = "recent_files.json"
SETTINGS_FILE_NAME = "settings.json"
#: The file the job history keeps its list of directories in. **It holds no run records** --
#: see :mod:`gigaxml.gui.job_history`; the runs themselves are the CLI's run reports, and
#: this file only remembers where to look for them and which config each was run with.
HISTORY_FILE_NAME = "job_history.json"


def about_text() -> str:
    """What the About box says.

    Built from the package's own metadata rather than typed out here. The version is the
    one the running code reports, and the homepage is the one declared in ``pyproject.toml``
    -- a second hand-written copy is a copy that drifts, which this project has paid for
    before (the installer script was the one version carrier that quietly fell behind).

    Nothing here is allowed to raise: an About box that fails to open is the exact defect
    it is meant to fix. A checkout with no installed metadata loses the homepage line and
    nothing else.

    **The homepage is not under a ``Homepage`` key.** ``pyproject.toml`` writes
    ``Homepage = "..."`` but the built metadata merges every such entry into ``Project-URL``
    as ``"Homepage, <url>"``. Reading ``.get("Homepage")`` looks right and always returns
    None; the first version of this function did exactly that.
    """
    from gigaxml import __version__

    lines = [tr("GigaXML {} — the CLI does the work").format(__version__)]

    home = _homepage()
    if home:
        lines.append("")
        lines.append(home)
    return "\n".join(lines)


def _homepage() -> str | None:
    """The project's homepage as the installed distribution declares it, if any.

    ``Project-URL`` may appear more than once (Homepage, Repository, ...), so this looks
    for the entry labelled ``Homepage`` rather than taking the first one. Anything the
    metadata layer does is caught: a missing or unreadable distribution is not a failure
    worth breaking the About box over.
    """
    try:
        from importlib.metadata import metadata

        entries = metadata("gigaxml").get_all("Project-URL") or []
    except Exception:
        return None
    for entry in entries:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage" and url.strip():
            return url.strip()
    return None


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
        # ⑫ The history is a pointer list, not a store of runs: it remembers which
        # directories have been run into, and every number it shows is read back out of the
        # report the CLI left there.
        self._job_history = JobHistory(self._state_dir / HISTORY_FILE_NAME)
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
        self._history = HistoryPanel(self._job_history, self)
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
            (self._history, tr("History")),
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
        self._history.resume_requested.connect(self._on_history_resume)

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

        **The child's own state is what decides, and it is asked first.** The report beside
        the output belongs to whichever run last reached its end, and a run that is stopped
        never overwrites it -- so for a stopped run the report is an *earlier* run's answer.
        Reading it would report a success, or a failure of the wrong kind, for a run that
        never got that far. Measured: a successful run to ``out.csv`` followed by a killed
        run to the same path leaves the first run's report saying ``rows: 2``.

        The report is then used for what it is good at -- the kind of a failure, and the
        numbers of a success -- and the disk for the one thing no report can say: that the
        run stopped partway and left work behind.

        ★ **Criterion E lands here, and it is the one branch that is new.** A run a signal
        ended used to arrive with ``killed=False``, ``ok=False`` and a non-empty stderr, so
        it fell through to the failure branch and was shown **in the error panel** -- the
        same red treatment as a config that will not load. That contradicted the CLI's own
        reasoning, which gives an interrupted run a separate exit code precisely because
        the two "want opposite reactions from a script": an error means do not run this
        again, an interrupted run means this was going fine, pick it up. It now goes where a
        stopped run goes, with the headline saying a signal ended it rather than the user
        stopping it, so the two are visibly different and neither is dressed as an error.
        """
        run = self._execution.run_result()
        if run is None:
            return
        state = self._execution.state
        output = self._execution.output_path()
        checkpointing = self._execution.is_checkpointing()
        self._remember_run(output, checkpointing=checkpointing)
        left = left_behind(output, checkpointing=checkpointing)

        if state is RunState.CANCELLED:
            self._errors.clear()
            self._results.show_unfinished(left)
            return

        if state is RunState.INTERRUPTED:
            # ★ Not a failure, so the error panel is cleared rather than filled. The
            # details are not thrown away: they are in the report this run did write,
            # which the history panel reads, and the results panel says what is on disk.
            self._errors.clear()
            self._results.show_unfinished(left, interrupted=True)
            return

        if state is RunState.FINISHED:
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
            # Whether this run wrote that report is asked rather than assumed, because a run
            # refused before it starts never gets to write one. ``validate_resume`` on a
            # changed source, and a second run over a directory that already holds a
            # checkpoint, both exit with words on stderr and nothing on disk -- and reading
            # the report there anyway explains this failure with an earlier run's.
            # Measured before this was asked: a checkpointed run that failed on a bad value,
            # then a resume against a source changed since, showed the **first** run's
            # ``FieldTypeError`` while the child had printed ``cannot resume`` with both
            # source hashes immediately before it.
            failure = None
            if self._execution.report_was_rewritten():
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

    def _remember_run(self, output: Path, *, checkpointing: bool) -> None:
        """Put this run's output directory into the history, and re-read the list.

        **Noted on every ending, not only on success.** A run that failed or was stopped is
        the one a user is most likely to come back to, and a history that listed successes
        only would be a list of the runs nobody needs to resume. The report beside the
        output is what says which of the two it was, and it is read from there on the next
        refresh rather than recorded here.

        The config path is remembered alongside, because ``--config`` is required and the
        report carries only a fingerprint — see
        :meth:`gigaxml.gui.job_history.JobHistory.note`.
        """
        try:
            config = Path(self._execution.effective_config())
        except GigaXMLError:
            # A config that will not load is one of the endings this method is called from,
            # and there is nothing to remember for it: no run happened, so there is no
            # report in that directory and the row would be empty.
            return
        self._job_history.note(output, checkpointing=checkpointing, config=config)
        self._history.refresh()

    def _on_history_resume(self, entry: HistoryEntry) -> None:
        """Carry a history row into the execution panel, and go there.

        **The run is not started.** The panel is filled, the tab is brought forward and the
        user presses Start. Continuing an earlier run is a decision, and ``test_gui_resume.py``
        already pins down that this project refuses to make it on their behalf — a button
        labelled "resume" that began extracting would be the same mistake wearing a
        different widget.

        The reason a row cannot be carried across is shown in the status line rather than
        swallowed, because the two reasons a user can act on — a config that is not
        remembered, and one that is no longer on disk — need different things from them.
        """
        problem = self._execution.apply_history_entry(
            entry, config=self._job_history.config_for(entry.directory)
        )
        if problem is not None:
            self._history.refresh()
            self._tabs.setCurrentWidget(self._history)
            self.statusBar().showMessage(problem, 10_000)
            return
        self._tabs.setCurrentWidget(self._execute_tab)
        self.statusBar().showMessage(
            tr("Press Start to continue the run. Nothing has been extracted yet."), 10_000
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
            return
        if kind == KIND_INTERRUPTED:
            # Go where the run can be picked up, and re-read first: the report that named
            # it may have been written by a terminal rather than by this window, and a
            # history that is a moment stale would not show the row the advice is about.
            # The button does not start anything -- the Resume button on that row fills the
            # execution panel, and the user still presses Start.
            self._history.refresh()
            self._tabs.setCurrentWidget(self._history)
            self.statusBar().showMessage(
                tr("this run is listed here; Resume fills the panel, Start does the work"),
                8000,
            )

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
        """Answer a question the user asked, somewhere they will actually look.

        This used to write a line to the status bar with a five second timeout. Every
        other status message in the window is a *transient* note about something that just
        happened and that the user can see for themselves -- a file opened, a path copied.
        A menu item called "About" is not that: the user asked for information and expects
        a panel to stay until they dismiss it. Five seconds in the corner reads as the menu
        item being broken, which is how it was reported.

        The text is built by ``about_text`` so that a test can assert what it says without
        opening a modal dialog and blocking there.
        """
        box = QMessageBox(self)
        box.setWindowTitle(tr("About GigaXML"))
        box.setText(about_text())
        box.setIcon(QMessageBox.Icon.Information)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.exec()

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

    def history_panel(self) -> HistoryPanel:
        return self._history

    def job_history(self) -> JobHistory:
        return self._job_history

    def tabs(self) -> QTabWidget:
        return self._tabs


def placeholder(title: str) -> QLabel:
    """A label for a tab that is not built yet, so the window says so rather than lying."""
    return QLabel(tr("{}: not built yet").format(title))


__all__ = ["RECENT_FILES_NAME", "MainWindow", "placeholder"]
