"""The preview panel: what the first N records actually look like.

**Why a child process and a temporary file.** ``sample`` has no stdout mode -- ``-o -`` is
refused outright -- so a preview is: run the CLI with ``-o <somewhere>``, then read that
file back. The alternative, reading the document here, is the one thing this application
exists not to do.

**The rejected tab is fed by the child too.** ``sample`` writes ``rejected.jsonl`` beside
its output when the config says ``on_error: quarantine``, and names it in the summary. The
panel reads that file; it does not decide what counts as a rejection, and it does not
infer rejections from a count.

**The tab is always there, and says which case it is.** A tab that vanishes when there is
nothing to show leaves the user unable to tell "nothing was rejected" from "this build has
no such feature". Its label carries the count, and its body says so in words.

**The sampling mechanism is shared.** The structure panel asks for example values through
the same :mod:`gigaxml.gui.sampling` functions, so the two are one implementation rather
than two that agree today.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.sampling import (
    SampledTable,
    discard_run_directory,
    make_run_directory,
    read_rejected,
    sample_args,
    table_from,
)

#: How often the UI thread drains what the reader thread collected. Matches the other
#: panels; the reasoning is in :mod:`gigaxml.gui.panels.execution`.
PUMP_INTERVAL_MS = 50

#: The rejected file's columns, in the order ``sample`` writes them.
REJECTED_HEADERS = ["index", "record_path", "error", "field", "raw", "message"]


class PreviewPanel(QWidget):
    """Sample the current config and show the rows it produces."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: Path | None = None
        self._config: dict[str, object] | None = None
        self._process: CliProcess | None = None
        self._run_result: RunResult | None = None
        self._finished = False
        #: Which run the answer in hand belongs to. Pressing Preview again replaces the run
        #: in flight, and the answer that arrives for the old one must not be applied to
        #: the new one's settings.
        self._generation = 0
        self._finished_generation = 0
        self._table: SampledTable | None = None
        self._run_directory: Path | None = None
        self._output: Path | None = None

        self._build()
        # Set the initial state here rather than relying on a first notification: the
        # field panel announces its (empty, and therefore unacceptable) table during its
        # own construction, which happens before the window has connected anything, so
        # that announcement is never heard.
        self._refresh_readiness()

        self._pump = QTimer(self)
        self._pump.setInterval(PUMP_INTERVAL_MS)
        self._pump.timeout.connect(self._drain)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self._run = QPushButton("Preview", self)
        self._run.clicked.connect(self.preview)
        self._cancel = QPushButton("Cancel", self)
        self._cancel.clicked.connect(self.cancel)
        self._cancel.setEnabled(False)
        controls.addWidget(self._run)
        controls.addWidget(self._cancel)
        controls.addWidget(QLabel("rows", self))
        self._limit = QSpinBox(self)
        self._limit.setRange(1, 1_000_000)
        self._limit.setValue(5)
        self._limit.setToolTip("How many records to sample, passed to `sample -n`.")
        controls.addWidget(self._limit)
        self._status = QLabel("nothing sampled yet", self)
        self._status.setObjectName("preview_status")
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        controls.addWidget(self._status, 1)
        layout.addLayout(controls)

        self._tabs = QTabWidget(self)
        self._tabs.setObjectName("preview_tabs")

        rows_page = QWidget(self)
        rows_layout = QVBoxLayout(rows_page)
        self._rows = QTableWidget(0, 0, self)
        self._rows.setObjectName("preview_table")
        self._rows.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rows.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        rows_layout.addWidget(self._rows)
        self._tabs.addTab(rows_page, "Sample")

        rejected_page = QWidget(self)
        rejected_layout = QVBoxLayout(rejected_page)
        self._rejected_note = QLabel("", self)
        self._rejected_note.setObjectName("rejected_note")
        self._rejected_note.setWordWrap(True)
        rejected_layout.addWidget(self._rejected_note)
        self._rejected = QTableWidget(0, len(REJECTED_HEADERS), self)
        self._rejected.setObjectName("rejected_table")
        self._rejected.setHorizontalHeaderLabels(REJECTED_HEADERS)
        self._rejected.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._rejected.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        rejected_layout.addWidget(self._rejected)
        self._tabs.addTab(rejected_page, "Rejected (0)")
        layout.addWidget(self._tabs, 1)

        self._set_rejected_label(0)

    # -- what is being sampled --------------------------------------------

    def set_source(self, path: Path | str | None) -> None:
        self._source = Path(path) if path is not None else None
        self._refresh_readiness()

    def source(self) -> Path | None:
        return self._source

    def set_config(self, payload: dict[str, object] | None) -> None:
        """The config to sample, as the mapping the CLI would read."""
        self._config = dict(payload) if payload else None
        self._refresh_readiness()

    def config(self) -> dict[str, object] | None:
        return self._config

    def set_limit(self, limit: int) -> None:
        self._limit.setValue(limit)

    def limit(self) -> int:
        return self._limit.value()

    def _refresh_readiness(self) -> None:
        ready = self._source is not None and self._config is not None
        self._run.setEnabled(ready)
        if ready:
            # Only replaces a "waiting for" message, so a finished run's summary is not
            # wiped out by an unrelated refresh.
            if self._status.text().startswith("waiting for"):
                self._status.setText("ready to preview")
            return
        missing = []
        if self._source is None:
            missing.append("a document")
        if self._config is None:
            missing.append("a config the CLI accepts")
        self._status.setText("waiting for " + " and ".join(missing))

    # -- running -----------------------------------------------------------

    def preview(self) -> None:
        """Start ``sample``. Returns as soon as the child is started.

        Pressing Preview while one is running means "sample with the settings as they are
        now", so the run in flight is stopped and replaced rather than ignored. Ignoring
        reads as the button being broken: the user changes the limit, presses it, and
        nothing happens. Same shape as the field panel's "From candidate", and the same two
        parts -- the generation decides which answer is the right one, the cancellation
        decides that the other one stops running.
        """
        if self._source is None or self._config is None:
            self._refresh_readiness()
            return
        self._cancel_run()
        self._generation += 1
        generation = self._generation

        self._run_result = None
        self._finished = False
        self._table = None
        self._clear_tables()
        self._status.setText("sampling…")
        self._run.setEnabled(False)
        self._cancel.setEnabled(True)

        # One directory at a time. A session that previews fifty times should leave one
        # directory behind, not fifty, and the one it leaves is the one whose files the
        # rejected tab is reading.
        discard_run_directory(self._run_directory)
        self._run_directory = make_run_directory()
        config_path = self._run_directory / "config.yaml"
        _write_config(config_path, self._config)
        # The suffix is not decoration: `sample` infers the format from it and refuses to
        # guess, so this has to be one it knows.
        self._output = self._run_directory / "sample.csv"

        process = CliProcess(
            sample_args(self._source, config_path, self._limit.value(), self._output),
            # reader thread: record only
            on_finished=lambda run, gen=generation: self._note_finished(run, gen),
        )
        self._process = process
        process.start()
        self._pump.start()

    def _cancel_run(self) -> None:
        """Stop the run in flight, if there is one.

        ``CliProcess.kill`` waits for the reader thread, so by the time this returns the
        previous run's callback has already been delivered -- which is what lets the caller
        start the next one without the two answers racing for the same slot.
        """
        process = self._process
        if process is None:
            return
        process.kill()
        self._process = None
        self._pump.stop()

    def _note_finished(self, run: RunResult, generation: int) -> None:
        """Reader thread. Records the outcome; touches no widget."""
        self._run_result = run
        self._finished_generation = generation
        self._finished = True

    def shutdown(self) -> None:
        """Give up what the window cannot clean up when it closes.

        **Qt does not deliver ``closeEvent`` to a child widget.** Closing the window hides
        and destroys the panels rather than closing them, so without this a panel that is
        only hidden keeps its child process running, keeps its pump firing at a window
        nobody is looking at, and keeps the directory that child wrote in. Measured: a test
        session leaves ``gigaxml-preview-*`` and ``gigaxml-examples-*`` directories behind,
        each holding a finished run's ``run-report.json``, because nothing ever discarded
        the last one -- and the reader threads of those abandoned children are still alive
        while the next test runs.
        """
        self._cancel_run()
        discard_run_directory(self._run_directory)
        self._run_directory = None

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Also correct when the panel itself is closed, as a test may do."""
        self.shutdown()
        super().closeEvent(event)

    def cancel(self) -> None:
        if self._process is None:
            return
        self._cancel.setEnabled(False)
        self._status.setText("cancelling…")
        self._process.kill()

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    def pid(self) -> int | None:
        return None if self._process is None else self._process.pid

    def run_result(self) -> RunResult | None:
        return self._run_result

    def sampled(self) -> SampledTable | None:
        return self._table

    def run_directory(self) -> Path | None:
        return self._run_directory

    # -- the UI-thread pump ------------------------------------------------

    def _drain(self) -> None:
        if not self._finished:
            return
        self._finished = False
        if self._finished_generation != self._generation:
            # Recorded for a run the user has replaced since. ``kill`` guarantees the
            # callback has been delivered before the next run starts, so this is about the
            # gap between the answer arriving and the UI thread getting a turn -- the user
            # can press Preview again in that window.
            return
        self._pump.stop()
        self._finish()

    def _finish(self) -> None:
        run = self._run_result
        self._refresh_readiness()
        self._cancel.setEnabled(False)
        if run is None:
            return
        if run.killed:
            self._status.setText("cancelled")
            return
        if self._output is None:
            return
        sampled = table_from(run.summary, self._output)
        self._table = sampled
        self._fill(sampled)
        if not run.ok:
            first = next((line for line in run.warnings if line.strip()), None)
            self._status.setText(first or f"sample failed with exit code {run.exit_code}")
            return
        self._status.setText(self._describe(sampled))

    def _describe(self, sampled: SampledTable) -> str:
        parts = [f"{sampled.row_count} row(s) shown"]
        if sampled.written != sampled.row_count:
            parts.append(f"the CLI wrote {sampled.written}")
        if sampled.rejected:
            parts.append(f"{sampled.rejected} rejected")
        if sampled.short_of_request:
            parts.append("the document ran out before the limit")
        return "  ·  ".join(parts)

    # -- filling the tables ------------------------------------------------

    def _clear_tables(self) -> None:
        self._rows.setRowCount(0)
        self._rows.setColumnCount(0)
        self._rejected.setRowCount(0)
        self._rejected_note.setText("")
        self._set_rejected_label(0)

    def _fill(self, sampled: SampledTable) -> None:
        self._rows.setColumnCount(len(sampled.headers))
        self._rows.setHorizontalHeaderLabels(list(sampled.headers))
        self._rows.setRowCount(len(sampled.rows))
        for row, values in enumerate(sampled.rows):
            for column, value in enumerate(values):
                self._rows.setItem(row, column, QTableWidgetItem(value))

        entries = read_rejected(sampled.rejected_path)
        self._fill_rejected(entries)

    def _fill_rejected(self, entries: tuple[dict[str, str], ...]) -> None:
        self._set_rejected_label(len(entries))
        self._rejected.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            for column, header in enumerate(REJECTED_HEADERS):
                self._rejected.setItem(row, column, QTableWidgetItem(entry.get(header, "")))
        if entries:
            self._rejected_note.setText("")
            return
        # Said in words, because an empty grid does not distinguish "nothing was rejected"
        # from "the rejections were not collected".
        self._rejected_note.setText(
            "No records were rejected in this sample. Rejections are only collected when "
            "the config sets on_error: quarantine, and only if some record fails to "
            "extract."
        )

    def _set_rejected_label(self, count: int) -> None:
        self._tabs.setTabText(1, f"Rejected ({count})")

    # -- what the widgets are showing --------------------------------------

    def status_text(self) -> str:
        return self._status.text()

    def headers(self) -> tuple[str, ...]:
        return tuple(
            self._rows.horizontalHeaderItem(column).text()
            for column in range(self._rows.columnCount())
        )

    def table_row_count(self) -> int:
        return self._rows.rowCount()

    def cell(self, row: int, column: int) -> str:
        item = self._rows.item(row, column)
        return item.text() if item is not None else ""

    def rejected_row_count(self) -> int:
        return self._rejected.rowCount()

    def rejected_tab_label(self) -> str:
        return self._tabs.tabText(1)

    def rejected_note_text(self) -> str:
        return self._rejected_note.text()

    def rejected_cell(self, row: int, column: int) -> str:
        item = self._rejected.item(row, column)
        return item.text() if item is not None else ""


def _write_config(path: Path, payload: dict[str, object]) -> None:
    """Write the config the child will read. YAML, which is what ``sample -c`` expects."""
    import yaml

    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
