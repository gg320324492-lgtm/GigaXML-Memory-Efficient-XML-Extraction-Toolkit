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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gigaxml.config import ConfigError, load_config, parse_config
from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.progress import Progress, format_eta, fraction_done

#: How often the UI thread drains what the reader thread collected. 50 ms is under the
#: threshold where a person notices a stall, and the work it does is a handful of widget
#: updates. The brief measured an idle Qt timer firing at 78% of its nominal rate at this
#: interval, so nothing here assumes it fires on schedule.
PUMP_INTERVAL_MS = 50


class ExecutionPanel(QWidget):
    """Choose an output, run the extraction, watch it, stop it."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._process: CliProcess | None = None
        self._received: list[Progress] = []
        self._run_result: RunResult | None = None
        self._finished = False
        self._total: int | None = None
        self._probe: CliProcess | None = None

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
        output_form.addRow("On error", self._on_error)

        self._checkpoint = QSpinBox(self)
        self._checkpoint.setRange(0, 100_000_000)
        self._checkpoint.setSpecialValueText("off")
        self._checkpoint.setValue(0)
        self._checkpoint.setToolTip(
            "Commit the output in parts of this many records, so an interrupted run can "
            "be continued. Off writes a single file."
        )
        output_form.addRow("Checkpoint every", self._checkpoint)
        layout.addWidget(output_box)

        progress_box = QGroupBox("Progress", self)
        progress_layout = QVBoxLayout(progress_box)
        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setValue(0)
        progress_layout.addWidget(self._bar)
        self._counts = QLabel("not started", self)
        self._counts.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
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

        handle, name = tempfile.mkstemp(prefix="gigaxml-gui-", suffix=".json", text=True)
        with open(handle, "w", encoding="utf-8") as stream:  # noqa: PTH123
            json.dump(raw, stream, indent=2)
        return Path(name)

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
            self._config.setText(chosen)

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
        except ConfigError as exc:
            # The project's own loader produced this, so it already says what is wrong
            # and where. Rewording it here would only make it less precise.
            self._counts.setText(f"config error: {exc}")
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
