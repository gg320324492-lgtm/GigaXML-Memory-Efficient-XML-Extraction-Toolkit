"""The execution panel: run an extraction and show what it is doing.

**The one rule that shapes this file.** ``CliProcess`` calls back on its reader thread.
Qt widgets belong to the thread that created them, so touching one from that callback is
undefined behaviour -- and the failure mode is not a crash but a window that never
updates again, which is the exact thing this application exists to avoid. So the
callbacks do nothing but append to a list and set a flag, and a timer on the UI thread
drains them. The same applies to quitting: calling ``app.quit()`` from the reader thread
leaves the window on screen forever.

**Where the total comes from.** ``inspect --json`` reports an exact record count per
candidate. When there is one, the bar has a denominator. When there is not -- the user
typed a record path by hand, or the probe has not finished -- the panel shows counts and
no percentage. A guessed denominator produces a bar that fills at the wrong speed, which
is worse than a bar that only counts.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gigaxml.checkpoint import CHECKPOINT_FILENAME, Checkpoint, CheckpointError, read_checkpoint
from gigaxml.config import ConfigError, load_config, parse_config
from gigaxml.errors import GigaXMLError
from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.progress import Progress, format_eta, fraction_done

#: How often the UI thread drains what the reader thread collected. 50 ms is under the
#: threshold where a person notices a stall, and the work it does is a handful of widget
#: updates. The brief measured an idle Qt timer firing at 78% of its nominal rate at this
#: interval, so nothing here assumes it fires on schedule.
PUMP_INTERVAL_MS = 50

#: The name every temporary config this panel writes begins with. See
#: :func:`_discard_effective_config` for why it is a named constant rather than a literal
#: in one place.
_EFFECTIVE_PREFIX = "gigaxml-gui-"


def _discard_effective_config(path: Path | None) -> bool:
    """Remove a temporary config this panel wrote, and only one of those.

    **The guard is the point.** This runs once per run started and once more when the
    window closes, unattended; a removal that deletes whatever path it is handed is one
    bad argument away from deleting something that was never ours. So it refuses anything
    whose name does not begin with :data:`_EFFECTIVE_PREFIX`, and anything that is not a
    file.

    This is the second place in ``gigaxml.gui`` that guards a removal this way -- the
    sampling module has the first, for its run directories. Two is not yet worth a shared
    helper; a third would be.
    """
    if path is None:
        return False
    target = Path(path)
    if not target.name.startswith(_EFFECTIVE_PREFIX):
        return False
    if not target.is_file():
        return False
    target.unlink(missing_ok=True)
    return True


class ExecutionPanel(QWidget):
    """Choose an output, run the extraction, watch it, stop it."""

    #: Emitted once when a run ends, whatever the outcome. The window listens so it can
    #: look at the run report and show what went wrong; this panel does not read the report
    #: itself, because saying what a failure means is not this panel's job.
    finished = Signal()

    #: Emitted when a run could not be started at all, with the reason and its kind.
    #:
    #: This is the config that will not load. There is no child process and no run report,
    #: so it never reaches :attr:`finished` -- and it is still a failure the user has to be
    #: told about, with the same treatment as any other.
    #:
    #: The second argument is the exception's class name, and it is what the window
    #: dispatches on. It is available here and nowhere else: there is no report to read it
    #: from, so without passing it along the advice would have to guess from the wording.
    start_failed = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._process: CliProcess | None = None
        self._received: list[Progress] = []
        self._run_result: RunResult | None = None
        self._finished = False
        self._total: int | None = None
        self._probe: CliProcess | None = None
        #: The temporary config the last ``effective_config()`` wrote, if it wrote one.
        self._effective_config: Path | None = None

        self._build()

        self._pump = QTimer(self)
        self._pump.setInterval(PUMP_INTERVAL_MS)
        self._pump.timeout.connect(self._drain)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        source_box = QGroupBox("Source", self)
        source_form = QFormLayout(source_box)
        self._source = QLineEdit(self)
        self._source.setPlaceholderText("an .xml or .xml.gz file")
        browse = QPushButton("Browse…", self)
        browse.clicked.connect(self.choose_source)
        source_row = QHBoxLayout()
        source_row.addWidget(self._source)
        source_row.addWidget(browse)
        source_form.addRow("Input file", source_row)

        self._config = QLineEdit(self)
        self._config.setPlaceholderText("a YAML or JSON config")
        config_browse = QPushButton("Browse…", self)
        config_browse.clicked.connect(self._choose_config)
        config_row = QHBoxLayout()
        config_row.addWidget(self._config)
        config_row.addWidget(config_browse)
        source_form.addRow("Config", config_row)
        # Typing a path and leaving the field is the other way a config is chosen, and it has
        # to sync the policy too. ``editingFinished`` rather than ``textChanged``: the latter
        # fires per keystroke, so every prefix of the path would be a load attempt.
        self._config.editingFinished.connect(self._sync_on_error_with_config)

        self._record_path = QLineEdit(self)
        self._record_path.setPlaceholderText("/catalog/products/product")
        self._record_path.setToolTip(
            "Used only to find the total for the progress bar, by asking `inspect`. "
            "The extraction itself takes the record path from the config."
        )
        source_form.addRow("Record path (for the total)", self._record_path)
        layout.addWidget(source_box)

        output_box = QGroupBox("Output", self)
        output_form = QFormLayout(output_box)
        self._output = QLineEdit(self)
        self._output.setPlaceholderText("where to write the rows")
        output_browse = QPushButton("Browse…", self)
        output_browse.clicked.connect(self._choose_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self._output)
        output_row.addWidget(output_browse)
        output_form.addRow("Output", output_row)

        self._format = QComboBox(self)
        self._format.addItems(["csv", "jsonl", "parquet"])
        output_form.addRow("Format", self._format)

        self._on_error = QComboBox(self)
        self._on_error.addItems(["abort", "quarantine"])
        self._on_error.setToolTip(
            "What to do when one record cannot be extracted. Opening a config sets this to "
            "what that config says; changing it afterwards overrides the config, and the "
            "line underneath says so."
        )
        self._on_error.currentIndexChanged.connect(self._note_the_override)
        output_form.addRow("On error", self._on_error)

        self._on_error_notice = QLabel("", self)
        self._on_error_notice.setWordWrap(True)
        self._on_error_notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._on_error_notice.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._on_error_notice.setMinimumWidth(1)
        # Hidden from the start, not merely empty: a widget nobody has hidden is "visible",
        # so an empty one would read as a notice that says nothing.
        self._on_error_notice.setVisible(False)
        output_form.addRow(self._on_error_notice)

        self._checkpoint = QSpinBox(self)
        self._checkpoint.setRange(0, 100_000_000)
        self._checkpoint.setSpecialValueText("off")
        self._checkpoint.setValue(0)
        self._checkpoint.setToolTip(
            "Commit the output in parts of this many records, so an interrupted run can "
            "be continued. Off writes a single file."
        )
        output_form.addRow("Checkpoint every", self._checkpoint)

        self._resume = QCheckBox("Resume the run already in that directory", self)
        self._resume.setToolTip(
            "Continue the run a checkpoint was made from. The tool refuses if the source "
            "or the config has changed since, and says exactly what differs. Never turned "
            "on for you: resuming is a decision, not a default."
        )
        self._resume.setEnabled(False)
        output_form.addRow(self._resume)

        self._resume_notice = QLabel("", self)
        self._resume_notice.setWordWrap(True)
        self._resume_notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._resume_notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._resume_notice.setMinimumWidth(1)
        self._resume_notice.setVisible(False)
        output_form.addRow(self._resume_notice)
        layout.addWidget(output_box)

        # The two things that decide whether there is anything to resume: which directory,
        # and whether checkpointing is on at all.
        self._output.editingFinished.connect(self._note_resumable)
        self._checkpoint.valueChanged.connect(self._note_resumable)

        progress_box = QGroupBox("Progress", self)
        progress_layout = QVBoxLayout(progress_box)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        progress_layout.addWidget(self._bar)
        self._counts = QLabel("not started", self)
        self._counts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        # A failure puts the CLI's own message here, and those are full of Windows paths --
        # no spaces, so wrapping alone does not help and the label's width hint grew until
        # the window was 4340 pixels wide. Ignored means the layout decides the width.
        self._counts.setWordWrap(True)
        self._counts.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._counts.setMinimumWidth(1)
        progress_layout.addWidget(self._counts)
        layout.addWidget(progress_box)

        buttons = QHBoxLayout()
        self._start = QPushButton("Start", self)
        self._start.clicked.connect(self.start)
        self._cancel = QPushButton("Cancel", self)
        self._cancel.clicked.connect(self.cancel)
        self._cancel.setEnabled(False)
        buttons.addStretch(1)
        buttons.addWidget(self._start)
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)
        layout.addStretch(1)

    def effective_config(self) -> Path:
        """The config to run with, with the panel's ``on_error`` choice applied.

        ``on_error`` is a config key rather than a command-line flag, so honouring the
        dropdown means producing a config. That is done by reading the user's file into a
        mapping, changing the one key, and handing it to the project's own
        :func:`~gigaxml.config.parse_config` -- which is the same validation the CLI runs,
        so a config this panel accepts is a config the CLI accepts.

        When the choice already matches the file, the file is passed through untouched:
        an override that rewrites the user's config for no reason is a way to lose the
        comments and the formatting they wrote it with.

        Raises:
            ConfigError: the config is missing or invalid. The caller shows the message,
                which comes from the project's loader and says what and where.
        """
        original = self._config.text().strip()
        if not original:
            raise ConfigError("no config chosen")
        path = Path(original)
        wanted = self._on_error.currentText()

        loaded = load_config(path)
        if loaded.on_error.value == wanted:
            return path

        raw = _read_mapping(path)
        raw["on_error"] = wanted
        # Validated by the project, not by this panel. If the override produced something
        # the CLI would reject, that is a bug here and it should fail here.
        parse_config(raw, source=str(path))

        # One at a time. Each call replaces the last, and the panel removes the last when
        # it closes -- so a session that starts a hundred runs leaves nothing behind.
        _discard_effective_config(self._effective_config)
        handle, name = tempfile.mkstemp(prefix=_EFFECTIVE_PREFIX, suffix=".json", text=True)
        with open(handle, "w", encoding="utf-8") as stream:  # noqa: PTH123
            json.dump(raw, stream, indent=2)
        self._effective_config = Path(name)
        return self._effective_config

    def shutdown(self) -> None:
        """Give up whatever is not the user's to clean up.

        Called by the window when it closes, because Qt does not deliver ``closeEvent`` to
        a child widget -- closing the window hides and destroys the panels, it does not
        close them -- and a temporary config left behind is exactly the sort of thing that
        accumulates silently.

        **And the child and the pump, which this used to leave running.** The other two
        panels were given the same treatment in the round that found this, and this one was
        missed -- so a window closed mid-run left its child reading pipes, its pump firing
        at a window nobody was looking at, and the child's two reader threads alive. Those
        are the threads the CI segfault's traceback shows still in their loops while the
        main thread collects garbage.
        """
        # Idempotent: the window closes this panel and a test may also close the
        # panel itself, and both routes land here. Without this, anything that
        # destroys the C++ objects (deleteLater, say) runs twice, and the second
        # pass touches what the first one freed.
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        process = self._process
        self._process = None
        if process is not None:
            process.kill()
        # **The probe too.** This panel starts a second child -- `inspect <source> --json` --
        # to count the records for the progress bar, and it was the one thing shutdown()
        # did not touch. A window closed while it was in flight left that child reading
        # pipes, and its on_finished callback reaches for widgets that are on their way out.
        probe = self._probe
        self._probe = None
        if probe is not None:
            probe.kill()
        self._pump.stop()
        _discard_effective_config(self._effective_config)
        self._effective_config = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Also correct when the panel itself is closed, as a test may do."""
        self.shutdown()
        super().closeEvent(event)

    # -- file pickers ------------------------------------------------------

    def choose_source(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a document", "", "XML documents (*.xml *.xml.gz);;All files (*)"
        )
        if chosen:
            self._source.setText(chosen)
            self._probe_total()

    def _choose_config(self) -> None:
        chosen, _ = QFileDialog.getOpenFileName(
            self, "Choose a config", "", "Configs (*.yaml *.yml *.json);;All files (*)"
        )
        if chosen:
            self.set_config(chosen)

    def _choose_output(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Where to write", "", "CSV (*.csv);;JSON Lines (*.jsonl);;Parquet (*.parquet)"
        )
        if chosen:
            self._output.setText(chosen)

    # -- the total ---------------------------------------------------------

    def _probe_total(self) -> None:
        """Ask ``inspect`` for the exact record count, in the background.

        Nothing here guesses. If the probe cannot run, or does not find the path the
        user named, the total stays unknown and the bar shows counts only.
        """
        source = self._source.text().strip()
        record_path = self._record_path.text().strip()
        if not source or not record_path:
            return
        self._total = None

        def finished(run: RunResult) -> None:
            # Reader thread. Parse here -- it is pure -- but touch no widgets.
            if run.summary is None:
                return
            for candidate in run.summary.get("candidates", []):
                if isinstance(candidate, dict) and candidate.get("path") == record_path:
                    count = candidate.get("count")
                    if isinstance(count, int):
                        self._total = count
                    return

        probe = CliProcess(
            ["inspect", source, "--json"],
            on_finished=finished,
        )
        self._probe = probe
        probe.start()

    def total(self) -> int | None:
        """The denominator for the bar, or ``None`` when it is not known."""
        return self._total

    # -- running -----------------------------------------------------------

    def build_args(self) -> list[str]:
        """The CLI arguments this panel's controls describe.

        Public and separate from :meth:`start` so that the argument mapping can be
        tested without starting a process.
        """
        args = [
            "extract",
            self._source.text().strip(),
            "-c",
            str(self.effective_config()),
            "-o",
            self._output.text().strip(),
            "--format",
            self._format.currentText(),
            "--progress",
        ]
        every = self._checkpoint.value()
        if every > 0:
            args += ["--checkpoint-every", str(every)]
        # Only when the user ticked it. A resumed run and a fresh one are different runs,
        # and quietly continuing somebody's earlier work is not something to do for them.
        if every > 0 and self._resume.isChecked():
            args.append("--resume")
        return args

    def start(self) -> None:
        """Launch the extraction. Returns as soon as the child is started."""
        if self._process is not None and self._process.is_running:
            return
        if not self._source.text().strip() or not self._output.text().strip():
            self._counts.setText("choose an input and an output first")
            return
        try:
            args = self.build_args()
        except GigaXMLError as exc:
            # **``GigaXMLError``, not ``ConfigError``.** The loader raises more than one
            # kind and they do not share that parent: a field path with a namespace prefix
            # the map does not declare raises ``FieldPathError``, which is a sibling of
            # ``ConfigError`` rather than a child of it. Catching only ``ConfigError`` let
            # that one escape from a button press into the event loop.
            #
            # The project's own loader produced this, so it already says what is wrong and
            # where. Rewording it here would only make it less precise.
            self._counts.setText(f"config error: {exc}")
            # And said again where the user can act on it. The panel that owns the run is
            # not the place to explain a config -- there is no run to explain.
            self.start_failed.emit(str(exc), type(exc).__name__)
            return

        self._received = []
        self._run_result = None
        self._finished = False
        self._bar.setValue(0)
        self._bar.setRange(0, 100)
        self._counts.setText("starting…")
        self._start.setEnabled(False)
        self._cancel.setEnabled(True)

        process = CliProcess(
            args,
            on_progress=self._received.append,  # reader thread: append only
            on_finished=self._note_finished,  # reader thread: set a flag only
        )
        self._process = process
        process.start()
        self._pump.start()

    def _note_finished(self, run: RunResult) -> None:
        """Runs on the reader thread. Records the outcome and nothing else.

        Not even ``app.quit()``. Doing that from here leaves the window on screen with
        no event loop to close it -- a hang that looks like the application ignoring you.
        """
        self._run_result = run
        self._finished = True

    def cancel(self) -> None:
        """Stop the child and wait for it to actually be gone."""
        if self._process is None:
            return
        self._cancel.setEnabled(False)
        self._counts.setText("cancelling…")
        self._process.kill()

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    def run_result(self) -> RunResult | None:
        return self._run_result

    # -- reading back, for the window --------------------------------------

    def output_path(self) -> Path:
        """Where the output goes, as typed. The run report lands beside it."""
        return Path(self._output.text().strip())

    def is_checkpointing(self) -> bool:
        """Whether this run writes parts into a directory instead of one file.

        The report's location depends on it: ``--output`` names the parts directory in that
        mode, so the summary goes inside rather than beside.
        """
        return self._checkpoint.value() > 0

    # -- resuming ----------------------------------------------------------

    def set_resume(self, resume: bool) -> None:
        """Tick or untick Resume, as if the user had."""
        self._resume.setChecked(resume)

    def is_resuming(self) -> bool:
        return self._resume.isChecked()

    def resume_notice_text(self) -> str:
        return self._resume_notice.text()

    def is_resume_offered(self) -> bool:
        """Whether the panel is telling the user there is something to resume.

        ``isHidden`` rather than ``isVisible``: the panel has to be readable without a
        window on screen.
        """
        return not self._resume_notice.isHidden()

    def unfinished_run_here(self) -> Checkpoint | None:
        """The manifest of an unfinished run in this output directory, if there is one.

        Read with the project's own reader rather than by parsing JSON here: the manifest
        has a format version and a set of required keys, and a second reader would be a
        second answer to what a valid one is.
        """
        if not self.is_checkpointing():
            return None
        directory = self._output.text().strip()
        if not directory:
            return None
        try:
            checkpoint = read_checkpoint(Path(directory) / CHECKPOINT_FILENAME)
        except CheckpointError:
            # Missing, unreadable, or a format this build does not know. None of those is
            # something to offer a resume from, and none of them is worth complaining about
            # here -- pressing Start says what is wrong with the directory.
            return None
        return None if checkpoint.complete else checkpoint

    def _note_resumable(self) -> None:
        """Say there is an unfinished run here, and how far it got.

        The counts come from the manifest, which is written as each part is committed --
        they are what the tool recorded, not a guess about a directory that is by
        definition not a finished run.
        """
        self._resume.setEnabled(self.is_checkpointing())
        if not self.is_checkpointing():
            self._resume.setChecked(False)
        unfinished = self.unfinished_run_here()
        if unfinished is None:
            self._resume_notice.setText("")
            self._resume_notice.setVisible(False)
            return
        parts = len(unfinished.parts)
        noun = "part" if parts == 1 else "parts"
        self._resume_notice.setText(
            f"There is an unfinished run in this directory: {parts:,} {noun}, "
            f"{unfinished.rows:,} rows, {unfinished.records_consumed:,} records consumed. "
            "Tick Resume to continue it, or choose another directory."
        )
        self._resume_notice.setVisible(True)

    def set_on_error(self, policy: str) -> None:
        """Choose the on-error policy, as if the user had picked it.

        Here so the error panel's advice can be acted on rather than only read: pressing
        "set on_error to quarantine" has to arrive at the same state as choosing it from
        the list. A policy this panel does not offer is ignored rather than added -- the
        list is the set of things the CLI accepts here, and inventing an entry would put
        something in front of the user that the run would then reject.
        """
        index = self._on_error.findText(policy)
        if index >= 0:
            self._on_error.setCurrentIndex(index)

    # -- following the config's policy -------------------------------------

    def set_config(self, path: str | Path) -> None:
        """Choose a config file, and follow the ``on_error`` it asks for.

        **The panel starts out agreeing with the file.** A dropdown reading ``abort`` while
        the file says ``quarantine`` is the panel quietly rewriting the user's config: the
        run does what the dropdown says, and nothing tells them it did. Opening the file
        sets the dropdown to what the file says, so a disagreement can only come from
        someone changing the dropdown afterwards -- which is deliberate, and which the line
        under it then states with both values.
        """
        self._config.setText(str(path))
        self._sync_on_error_with_config()

    def _sync_on_error_with_config(self) -> None:
        """Take the policy from the config, if there is one to take."""
        asked = self._configs_on_error()
        if asked is not None:
            self.set_on_error(asked)
        self._note_the_override()

    def _configs_on_error(self) -> str | None:
        """The ``on_error`` the chosen config asks for, or ``None`` if it cannot be read.

        **Nothing is raised and nothing is said.** :meth:`start` already reports an
        unreadable config with the loader's own message, and a second complaint from a
        widget that is only trying to be helpful would be noise on top of it.
        """
        path = self._config.text().strip()
        if not path:
            return None
        try:
            return load_config(Path(path)).on_error.value
        except (GigaXMLError, OSError):
            return None

    def _note_the_override(self) -> None:
        """State the disagreement, naming both values, or say nothing at all.

        Silent when they agree, which is the ordinary case and the one that has to stay
        quiet: a notice on every run is a notice nobody reads.
        """
        wanted = self._on_error.currentText()
        asked = self._configs_on_error()
        if asked is None or asked == wanted:
            self._on_error_notice.setText("")
            self._on_error_notice.setVisible(False)
            return
        self._on_error_notice.setText(f"the run will use {wanted}, overriding the config's {asked}")
        self._on_error_notice.setVisible(True)

    def on_error_notice_text(self) -> str:
        return self._on_error_notice.text()

    def is_override_noted(self) -> bool:
        """Whether the notice is showing. ``isHidden`` rather than ``isVisible``: the panel
        has to be readable without a window on screen."""
        return not self._on_error_notice.isHidden()

    # -- the UI-thread pump ------------------------------------------------

    def _drain(self) -> None:
        """Move what the reader thread collected onto the widgets. UI thread only."""
        if self._received:
            latest = self._received[-1]
            # Cleared, not trimmed: `del self._received[:-1]` keeps the last element,
            # which leaves one stale entry behind for the next pass to read again.
            self._received.clear()
            self._show(latest)
        if self._finished:
            self._pump.stop()
            self._finish()
            # After _finish, so whoever listens can read the widgets this panel just
            # settled -- and always, not only on failure: the listener decides whether
            # there is anything to say, and a panel that only spoke on failure would have
            # to duplicate that decision.
            self.finished.emit()

    def _show(self, progress: Progress) -> None:
        fraction = fraction_done(progress.records, self._total)
        if fraction is None:
            # No denominator, so no bar. A bar that fills at a guessed speed is a lie
            # told with a widget, and the counts beside it are honest on their own.
            self._bar.setRange(0, 0)
        else:
            self._bar.setRange(0, 100)
            self._bar.setValue(int(fraction * 100))

        parts = [f"{progress.records:,} records"]
        if self._total is not None:
            parts[0] += f" of {self._total:,}"
        parts.append(f"{progress.rows:,} rows")
        if progress.rejected:
            parts.append(f"{progress.rejected:,} rejected")
        parts.append(f"{progress.elapsed_seconds:.1f}s")
        if progress.part is not None:
            parts.append(f"part {progress.part}")
        eta = format_eta(progress.elapsed_seconds, progress.records, self._total)
        if eta:
            parts.append(eta)
        self._counts.setText("  ·  ".join(parts))

    def _finish(self) -> None:
        run = self._run_result
        self._start.setEnabled(True)
        self._cancel.setEnabled(False)
        if run is None:
            return
        if run.killed:
            self._counts.setText("cancelled")
            return
        if run.ok:
            rows = (run.summary or {}).get("rows")
            self._bar.setRange(0, 100)
            self._bar.setValue(100)
            self._counts.setText(
                f"finished — {rows:,} rows" if isinstance(rows, int) else "finished"
            )
            return
        # The CLI's own messages are written to be read, so the first one is what the
        # user sees; the rest are kept for the detail view that comes with the error
        # panel, and are never discarded.
        first = next((line for line in run.warnings if line.strip()), None)
        self._counts.setText(first or f"failed with exit code {run.exit_code}")


def read_report_rows(report_path: Path) -> int | None:
    """Rows from a ``run-report.json``. Used to check the bar against the record.

    Kept here rather than inline so the comparison the brief asks for -- the number the
    progress bar ended on versus the number in the report -- can be made in a test
    without a window.
    """
    if not report_path.is_file():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    rows = payload.get("rows")
    return rows if isinstance(rows, int) else None


def _read_mapping(path: Path) -> dict[str, object]:
    """The config file as a plain mapping, whatever syntax it is written in.

    Both spellings the CLI accepts are handled, because the file dialog offers both and a
    user who saved JSON should not be told their config is unreadable.
    """
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        import yaml

        payload = yaml.safe_load(text)
    if not isinstance(payload, dict):
        raise ConfigError(f"{path} does not contain a config mapping")
    return payload
