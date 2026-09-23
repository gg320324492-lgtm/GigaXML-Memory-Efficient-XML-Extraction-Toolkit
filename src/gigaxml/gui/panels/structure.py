"""The structure panel: run ``inspect``, then show what it found.

**Nothing here parses XML.** The window never sees the document -- that is the whole
point of the project, and a panel that read the file to build its tables would put a
multi-gigabyte document into the window's memory. Everything on screen comes from
``inspect --json``, read through :mod:`gigaxml.gui.inspect_report`, which is the layer
that knows the JSON is a contract and the warnings on stderr are not.

**The same threading rule as the execution panel.** ``CliProcess`` calls back on its
reader thread; Qt widgets belong to the thread that made them. So the callbacks record
and the timer drains. See :mod:`gigaxml.gui.panels.execution` for the full reasoning.

**This panel owns its own process.** It does not borrow the execution panel's, and the
two therefore cannot cancel each other. That is worth stating because the shared plumbing
is exactly where such a coupling would creep in, and cancelling one run while another is
still going is a thing a user will do.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.inspect_report import Candidate, InspectReport, parse_report, paths_to_csv
from gigaxml.gui.sampling import (
    SampledTable,
    discard_run_directory,
    generate_config_args,
    make_run_directory,
    sample_args,
    table_from,
)

#: How often the UI thread drains what the reader thread collected. Matches the execution
#: panel; the reasoning is there.
PUMP_INTERVAL_MS = 50

#: How many records to sample when showing a candidate's example values. One: the side
#: panel has a line per field, and the point is to show what a value *looks like* rather
#: than to describe the column.
EXAMPLE_ROWS = 1

#: Where the report's own index is kept on a candidate row.
#:
#: **Not the same as the row number.** Sorting is enabled, so the row the user clicked is
#: not the position of that candidate in the report. Reading the two as one shows the
#: wrong candidate's detail -- which looks like a working panel until somebody sorts.
_REPORT_INDEX_ROLE = Qt.ItemDataRole.UserRole


class _NumericItem(QTableWidgetItem):
    """A table cell that sorts as a number while displaying formatted text.

    ``QTableWidgetItem`` sorts on its display text, which puts 10 before 9 and 1.0 before
    0.9 -- a path table ordered that way is worse than an unsorted one, because it looks
    deliberate. The sort key is kept beside the text rather than replacing it, so the
    column still reads as ``1,164,800`` and not as ``1164800``.
    """

    def __init__(self, text: str, key: float) -> None:
        super().__init__(text)
        self._key = key

    def __lt__(self, other: QTableWidgetItem) -> bool:
        if isinstance(other, _NumericItem):
            return self._key < other._key
        # **Not `super().__lt__(other)`.** PySide6 dispatches that back into this override
        # and recurses until the stack runs out, which takes the window down rather than
        # raising where the comparison happened. Comparing the display text is what the
        # base class would have done, done explicitly.
        return self.text() < other.text()


CANDIDATE_HEADERS = ["path", "score", "count", "shape consistency", "nested inside"]
PATH_HEADERS = ["path", "count", "depth", "children", "shape consistency", "distinct shapes"]


class StructurePanel(QWidget):
    """Analyse a document in the background and present the result."""

    #: Emitted with the :class:`~gigaxml.gui.inspect_report.Candidate` the user selected,
    #: or ``None`` when the selection is cleared. The window passes it to the field panel.
    candidate_changed = Signal(object)

    #: Emitted when a new report has been parsed. The window passes its namespaces and its
    #: path list to the field panel, which is what completes the path boxes.
    report_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: Path | None = None
        self._process: CliProcess | None = None
        self._run_result: RunResult | None = None
        self._finished = False
        self._report: InspectReport | None = None

        # Example values. Sampling is a chain of two child processes -- write a config for
        # the candidate, then sample it -- so it keeps its own queue, its own process and
        # its own timer. Sharing the analysis pump would make "the analysis finished"
        # indistinguishable from "the values arrived", and the existing tests wait on
        # exactly that distinction.
        self._example_steps: list[list[str]] = []
        self._example_process: CliProcess | None = None
        self._example_step_done = False
        self._example_step_result: RunResult | None = None
        self._example_step_generation = 0
        #: Which selection the chain in flight belongs to. A user clicking down the
        #: candidate list starts a chain per click, and the ones they have moved past are
        #: still running; without this, the first one to report back would be shown
        #: against whichever row happens to be selected by then.
        self._example_generation = 0
        self._example_table: SampledTable | None = None
        self._example_failure = ""
        self._example_directory: Path | None = None
        self._example_output: Path | None = None
        self._example_for: int | None = None

        self._build()

        self._pump = QTimer(self)
        self._pump.setInterval(PUMP_INTERVAL_MS)
        self._pump.timeout.connect(self._drain)

        self._example_pump = QTimer(self)
        self._example_pump.setInterval(PUMP_INTERVAL_MS)
        self._example_pump.timeout.connect(self._drain_examples)

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        self._analyze = QPushButton("Analyse", self)
        self._analyze.clicked.connect(self.analyze)
        self._cancel = QPushButton("Cancel", self)
        self._cancel.clicked.connect(self.cancel)
        self._cancel.setEnabled(False)
        controls.addWidget(self._analyze)
        controls.addWidget(self._cancel)
        controls.addWidget(QLabel("max paths", self))
        self._max_paths = QSpinBox(self)
        self._max_paths.setRange(0, 100_000_000)
        self._max_paths.setSpecialValueText("default")
        self._max_paths.setValue(0)
        self._max_paths.setToolTip(
            "Stop tracking distinct paths after this many. Off uses inspect's own default. "
            "Hitting the cap is reported in the warnings, never silently."
        )
        controls.addWidget(self._max_paths)
        self._status = QLabel("no document analysed", self)
        self._status.setObjectName("structure_status")
        self._status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        controls.addWidget(self._status, 1)
        layout.addLayout(controls)

        # Every warning in one place. Showing three of the five would be worse than
        # showing none: the reader would believe the picture was complete.
        self._warnings = QLabel("", self)
        self._warnings.setObjectName("structure_warnings")
        self._warnings.setWordWrap(True)
        self._warnings.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._warnings)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("filter rows by path…")
        self._search.textChanged.connect(self._apply_filter)
        layout.addWidget(self._search)

        splitter = QSplitter(Qt.Orientation.Vertical, self)

        candidates_box = QGroupBox("Record candidates", self)
        candidates_layout = QVBoxLayout(candidates_box)
        self._candidates = QTableWidget(0, len(CANDIDATE_HEADERS), self)
        self._candidates.setObjectName("candidate_table")
        self._candidates.setHorizontalHeaderLabels(CANDIDATE_HEADERS)
        self._candidates.setSortingEnabled(True)
        self._candidates.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._candidates.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._candidates.itemSelectionChanged.connect(self._show_selected_candidate)
        self._candidates.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        candidates_layout.addWidget(self._candidates)
        splitter.addWidget(candidates_box)

        detail_box = QGroupBox("Selected candidate", self)
        detail_layout = QVBoxLayout(detail_box)
        self._detail = QTextBrowser(self)
        self._detail.setObjectName("candidate_detail")
        detail_layout.addWidget(self._detail)
        splitter.addWidget(detail_box)

        namespaces_box = QGroupBox("Namespaces", self)
        namespaces_layout = QVBoxLayout(namespaces_box)
        self._namespaces = QTextBrowser(self)
        self._namespaces.setObjectName("namespace_panel")
        namespaces_layout.addWidget(self._namespaces)
        splitter.addWidget(namespaces_box)

        paths_box = QGroupBox("All paths", self)
        paths_layout = QVBoxLayout(paths_box)
        self._paths = QTableWidget(0, len(PATH_HEADERS), self)
        self._paths.setObjectName("path_table")
        self._paths.setHorizontalHeaderLabels(PATH_HEADERS)
        self._paths.setSortingEnabled(True)
        self._paths.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._paths.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        paths_layout.addWidget(self._paths)

        export_row = QHBoxLayout()
        self._export = QPushButton("Export paths as CSV…", self)
        self._export.clicked.connect(self.choose_export_path)
        self._export.setEnabled(False)
        export_row.addStretch(1)
        export_row.addWidget(self._export)
        paths_layout.addLayout(export_row)
        splitter.addWidget(paths_box)

        layout.addWidget(splitter, 1)

    # -- what is being analysed -------------------------------------------

    def set_document(self, path: Path | str | None) -> None:
        """Point the panel at a document. Does not start anything."""
        self._source = Path(path) if path is not None else None
        self._status.setText(f"ready to analyse {self._source}" if self._source else "no document")
        self._analyze.setEnabled(self._source is not None)

    def document(self) -> Path | None:
        return self._source

    # -- running -----------------------------------------------------------

    def build_args(self) -> list[str]:
        """The CLI arguments this panel's controls describe.

        Public and separate from :meth:`analyze` for the same reason the execution panel's
        is: the mapping from controls to arguments is the part that can be wrong, and it
        can be checked without starting a process.
        """
        if self._source is None:
            raise ValueError("no document to analyse")
        args = ["inspect", str(self._source), "--json"]
        limit = self._max_paths.value()
        if limit > 0:
            args += ["--max-paths", str(limit)]
        return args

    def set_max_paths(self, limit: int) -> None:
        """Cap the walk. Zero means ``inspect``'s own default."""
        self._max_paths.setValue(limit)

    def analyze(self) -> None:
        """Start ``inspect --json``. Returns as soon as the child is started."""
        if self._source is None:
            self._status.setText("open a document first")
            return
        if self._process is not None and self._process.is_running:
            return

        self._run_result = None
        self._finished = False
        self._report = None
        self._clear_tables()
        self._status.setText("analysing…")
        self._analyze.setEnabled(False)
        self._cancel.setEnabled(True)

        process = CliProcess(
            self.build_args(),
            on_finished=self._note_finished,  # reader thread: record only
        )
        self._process = process
        process.start()
        self._pump.start()

    def _note_finished(self, run: RunResult) -> None:
        """Reader thread. Records the outcome; touches no widget."""
        self._run_result = run
        self._finished = True

    def cancel(self) -> None:
        """Stop the child and wait for it to actually be gone."""
        if self._process is None:
            return
        self._cancel.setEnabled(False)
        self._status.setText("cancelling…")
        self._process.kill()

    def shutdown(self) -> None:
        """Give up what the window cannot clean up when it closes.

        **Qt does not deliver ``closeEvent`` to a child widget.** Closing the window hides
        and destroys the panels rather than closing them, so without this a panel that is
        only hidden keeps **two** children running -- the analysis and the example chain --
        keeps both pumps firing at a window nobody is looking at, and never discards the
        example directory. Measured: a test session leaves ``gigaxml-examples-*`` directories
        behind, each holding a finished run's ``run-report.json``, and the reader threads of
        those abandoned children are still alive while the next test runs.
        """
        self._cancel_example_chain()
        discard_run_directory(self._example_directory)
        self._example_directory = None
        process = self._process
        self._process = None
        if process is not None:
            process.kill()
        self._pump.stop()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt naming)
        """Also correct when the panel itself is closed, as a test may do."""
        self.shutdown()
        super().closeEvent(event)

    def is_running(self) -> bool:
        return self._process is not None and self._process.is_running

    def pid(self) -> int | None:
        """The child's PID while it runs, so a caller can look for it from outside.

        ``result.killed`` is evidence from inside this process. A PID is evidence anyone
        can check, including for a grandchild that outlived its parent.
        """
        return None if self._process is None else self._process.pid

    def run_result(self) -> RunResult | None:
        return self._run_result

    def report(self) -> InspectReport | None:
        """The parsed report, or ``None`` until a run has succeeded."""
        return self._report

    # -- the UI-thread pump ------------------------------------------------

    def _drain(self) -> None:
        if self._finished:
            self._pump.stop()
            self._finish()

    def _finish(self) -> None:
        run = self._run_result
        self._analyze.setEnabled(self._source is not None)
        self._cancel.setEnabled(False)
        if run is None:
            return
        if run.killed:
            self._status.setText("cancelled")
            return
        report = parse_report(run.summary)
        if report is None:
            first = next((line for line in run.warnings if line.strip()), None)
            self._status.setText(first or f"inspect failed with exit code {run.exit_code}")
            return
        self._report = report
        self._fill(report)
        self.report_changed.emit()

    # -- filling the tables ------------------------------------------------

    def _clear_tables(self) -> None:
        self._candidates.setRowCount(0)
        self._paths.setRowCount(0)
        self._detail.setPlainText("")
        self._namespaces.setPlainText("")
        self._warnings.setText("")
        self._export.setEnabled(False)

    def _fill(self, report: InspectReport) -> None:
        self._fill_candidates(report)
        self._fill_paths(report)
        self._fill_namespaces(report)
        self._fill_warnings(report)
        self._export.setEnabled(bool(report.paths))
        self._status.setText(
            f"{report.elements_seen:,} elements · {len(report.candidates)} candidates · "
            f"{len(report.paths):,} paths"
        )

    def _fill_candidates(self, report: InspectReport) -> None:
        self._candidates.setSortingEnabled(False)
        self._candidates.setRowCount(len(report.candidates))
        for row, candidate in enumerate(report.candidates):
            # The nesting is read from the JSON rather than inferred from the path text:
            # that field exists precisely because the text is ambiguous.
            nested = candidate.nested_inside or ""
            path_item = QTableWidgetItem(candidate.path)
            path_item.setData(_REPORT_INDEX_ROLE, row)
            cells: list[QTableWidgetItem] = [
                path_item,
                _NumericItem(f"{candidate.score:.3f}", candidate.score),
                _NumericItem(f"{candidate.count:,}", candidate.count),
                _NumericItem(f"{candidate.shape_consistency:.1%}", candidate.shape_consistency),
                QTableWidgetItem(f"[inside {nested}]" if nested else ""),
            ]
            for column, item in enumerate(cells):
                self._candidates.setItem(row, column, item)
        self._candidates.setSortingEnabled(True)
        self._apply_filter()

    def _fill_paths(self, report: InspectReport) -> None:
        self._paths.setSortingEnabled(False)
        self._paths.setRowCount(len(report.paths))
        for row, entry in enumerate(report.paths):
            cells: list[QTableWidgetItem] = [
                QTableWidgetItem(entry.path),
                _NumericItem(f"{entry.count:,}", entry.count),
                _NumericItem(str(entry.depth), entry.depth),
                QTableWidgetItem("yes" if entry.has_children else "no"),
                _NumericItem(f"{entry.shape_consistency:.1%}", entry.shape_consistency),
                _NumericItem(str(entry.distinct_shapes), entry.distinct_shapes),
            ]
            for column, item in enumerate(cells):
                self._paths.setItem(row, column, item)
        self._paths.setSortingEnabled(True)
        self._apply_filter()

    def _fill_namespaces(self, report: InspectReport) -> None:
        lines: list[str] = []
        if report.namespaces:
            lines.append("prefix → URI")
            for prefix, uri in sorted(report.namespaces.items()):
                label = prefix if prefix else "<default>"
                lines.append(f"  {label} → {uri}")
        else:
            # **"Declares" was the wrong word, and this is a statement about the user's own
            # file.** ``namespaces`` is the map the *candidate records* resolve against,
            # not everything the document declares: a document can declare a prefix and
            # never use it, and this is then empty. Saying it declares none would be false
            # about their document -- and this panel is where the namespace advice sends
            # them, so being wrong here is worse than being vague.
            lines.append(
                "no candidate records were found, so there are no namespaces to show"
                if not report.candidates
                else "the records found use no namespace prefixes"
            )
        if report.shadowed_prefixes:
            lines.append("")
            lines.append("rebound prefixes (they mean more than one thing):")
            for prefix in report.shadowed_prefixes:
                lines.append(f"  {prefix}")
        if report.unmapped_namespaces:
            lines.append("")
            lines.append("namespaces used with no prefix:")
            for uri in report.unmapped_namespaces:
                lines.append(f"  {uri}")
        self._namespaces.setPlainText("\n".join(lines))

    def _fill_warnings(self, report: InspectReport) -> None:
        lines = report.warnings()
        if not lines:
            self._warnings.setText("")
            return
        # Truncation first and in its own words: it is the one that means "what you are
        # about to read is not the whole document", and a reader who skims must not miss
        # it among the rest.
        ordered = [line for line in lines if "stopped at" in line]
        ordered += [line for line in lines if line not in ordered]
        self._warnings.setText("\n".join(f"⚠ {line}" for line in ordered))

    # -- searching ---------------------------------------------------------

    def set_filter(self, text: str) -> None:
        """The filter the search box applies. Public so it can be driven in a test."""
        self._search.setText(text)

    def _apply_filter(self) -> None:
        needle = self._search.text().strip().lower()
        for table in (self._candidates, self._paths):
            for row in range(table.rowCount()):
                item = table.item(row, 0)
                text = item.text().lower() if item is not None else ""
                table.setRowHidden(row, bool(needle) and needle not in text)

    def visible_path_rows(self) -> int:
        """How many path rows the filter is currently showing."""
        return sum(1 for row in range(self._paths.rowCount()) if not self._paths.isRowHidden(row))

    # -- the side panel ----------------------------------------------------

    def select_candidate(self, row: int) -> None:
        """Select a candidate row, which is what fills the detail pane."""
        self._candidates.selectRow(row)

    def _show_selected_candidate(self) -> None:
        report = self._report
        if report is None:
            return
        row = self._candidates.currentRow()
        if row < 0:
            return
        item = self._candidates.item(row, 0)
        stored = item.data(_REPORT_INDEX_ROLE) if item is not None else None
        # The index from the row, not the row number: sorting reorders the view, and using
        # the row as an index would quietly show a different candidate than the one the
        # user clicked.
        if not isinstance(stored, int) or not 0 <= stored < len(report.candidates):
            return
        self._request_example_values(stored)
        # Rendered after the request, not before: the first paint has to show that values
        # are on their way, and painting first would show "(none)" for the moment before
        # the child starts.
        self._render_detail()
        self.candidate_changed.emit(report.candidates[stored])

    def _render_detail(self) -> None:
        """Repaint the side panel for whatever is selected right now."""
        report = self._report
        index = self.selected_candidate_index()
        if report is None or index is None or not 0 <= index < len(report.candidates):
            return
        self._detail.setPlainText(
            _describe_candidate(
                report.candidates[index],
                examples=self._example_table,
                pending=self._example_pending(),
                failure=self._example_failure,
            )
        )

    # -- example values ----------------------------------------------------

    def _example_pending(self) -> bool:
        """Whether a sampling chain is in flight for the selected candidate."""
        return bool(self._example_steps) or self._example_process is not None

    def _request_example_values(self, index: int) -> None:
        """Start the chain that puts real values beside the field list.

        **The same mechanism the preview uses**, which is what the brief asks for: this
        asks ``inspect`` to write a config for the candidate, then asks ``sample`` to
        produce one record from it. Nothing here reads the document -- the values come out
        of a child process writing a file, exactly as they do in the preview panel.

        Two steps rather than one because ``sample`` needs a config, and the config for a
        candidate is what ``inspect --generate-config`` exists to produce.
        """
        source = self._source
        if source is None:
            return
        already = index == self._example_for and (
            self._example_table is not None or self._example_pending()
        )
        if already:
            # Re-selecting the same row must not start a second chain: the table emits a
            # selection change on every rebuild, and each one would otherwise spawn two
            # more children. The values already in hand are left alone.
            return
        self._example_for = index
        self._example_table = None
        self._example_failure = ""
        # Whatever was in flight belongs to the previous selection, and its answer is no
        # longer wanted: stop it rather than let it report into a row it does not describe.
        self._cancel_example_chain()
        self._example_generation += 1
        # One directory at a time: clicking through fifty candidates should leave one
        # directory behind, not fifty.
        discard_run_directory(self._example_directory)
        self._example_directory = make_run_directory("gigaxml-examples-")
        config_path = self._example_directory / "config.yaml"
        self._example_output = self._example_directory / "sample.csv"
        # 1-based, matching what `inspect` prints and what the table shows.
        self._example_steps = [
            generate_config_args(source, config_path, index + 1),
            sample_args(source, config_path, EXAMPLE_ROWS, self._example_output),
        ]
        self._start_next_example_step(self._example_generation)

    def _cancel_example_chain(self) -> None:
        """Stop the chain in flight, if there is one, and forget it."""
        self._example_steps.clear()
        process = self._example_process
        self._example_process = None
        if process is not None:
            process.kill()
        # After kill(), not before. kill waits for the callback, and the callback is what
        # sets this flag -- so clearing it first would be undone by the very step it is
        # meant to forget, and the line would read as "forget it" while doing nothing.
        self._example_step_done = False
        self._example_pump.stop()

    def _start_next_example_step(self, generation: int) -> None:
        """Launch the next child in the chain, or finish if there is none left.

        ``generation`` is the selection this chain belongs to. A chain whose generation has
        been superseded stops here rather than starting another child.
        """
        if generation != self._example_generation:
            return
        if not self._example_steps:
            self._example_process = None
            self._example_pump.stop()
            self._render_detail()
            return
        args = self._example_steps.pop(0)
        process = CliProcess(
            args,
            on_finished=lambda run, gen=generation: self._note_example_step(run, gen),
        )
        self._example_process = process
        process.start()
        self._example_pump.start()

    def _note_example_step(self, run: RunResult, generation: int) -> None:
        """Reader thread. Records the step's outcome and touches no widget."""
        self._example_step_result = run
        self._example_step_generation = generation
        self._example_step_done = True

    def _drain_examples(self) -> None:
        """UI thread. Advances the chain when a step reports back."""
        if not self._example_step_done:
            return
        self._example_step_done = False
        if self._example_step_generation != self._example_generation:
            # The user has selected something else since this child started. Its answer
            # describes a row that is no longer the one on screen.
            return
        run = self._example_step_result
        if run is None:
            return
        if run.killed:
            self._example_failure = "sampling was cancelled"
            self._example_steps.clear()
            self._example_process = None
            self._example_pump.stop()
            self._render_detail()
            return
        if not run.ok:
            first = next((line for line in run.warnings if line.strip()), None)
            self._example_failure = first or f"sampling failed with exit code {run.exit_code}"
            self._example_steps.clear()
            self._example_process = None
            self._example_pump.stop()
            self._render_detail()
            return
        if self._example_steps:
            # The config was written; now sample it.
            self._start_next_example_step(self._example_generation)
            return
        if self._example_output is not None:
            self._example_table = table_from(run.summary, self._example_output)
        self._example_process = None
        self._example_pump.stop()
        self._render_detail()

    def example_values(self) -> dict[str, str] | None:
        """The first sampled record as field-name to value, or ``None``.

        Exposed so a test can assert the values are real rather than that some text
        appeared, which is the difference the brief cares about.
        """
        table = self._example_table
        if table is None or not table.rows:
            return None
        return dict(zip(table.headers, table.rows[0], strict=False))

    def example_failure(self) -> str:
        return self._example_failure

    def example_directory(self) -> Path | None:
        """Where the sample behind the example values was written.

        Exposed because those files are the evidence that the values came from a child
        process rather than from anything in this one.
        """
        return self._example_directory

    def selected_candidate_index(self) -> int | None:
        """The report index of the selected candidate, or ``None``.

        Exposed so a test can assert that clicking a row selects *that* candidate after
        the view has been sorted, which is the failure this indirection exists to stop.
        """
        row = self._candidates.currentRow()
        if row < 0:
            return None
        item = self._candidates.item(row, 0)
        stored = item.data(_REPORT_INDEX_ROLE) if item is not None else None
        return stored if isinstance(stored, int) else None

    def detail_text(self) -> str:
        return self._detail.toPlainText()

    def warnings_text(self) -> str:
        return self._warnings.text()

    # -- export ------------------------------------------------------------

    def csv_text(self) -> str:
        """The path table as CSV, using the escaping the foundation already tests."""
        if self._report is None:
            return ""
        return paths_to_csv(self._report)

    def choose_export_path(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Export paths", "", "CSV (*.csv);;All files (*)"
        )
        if chosen:
            self.export_csv(Path(chosen))

    def export_csv(self, path: Path | str) -> None:
        """Write the path table. Raises nothing for an empty report; writes the header."""
        Path(path).write_text(self.csv_text(), encoding="utf-8")


def _describe_candidate(
    candidate: Candidate,
    *,
    examples: SampledTable | None = None,
    pending: bool = False,
    failure: str = "",
) -> str:
    """The side panel's text for one candidate.

    The fields, the namespaces and the evidence all come from the JSON. **The example
    values do not, because ``inspect --json`` does not carry any** -- ``value_sampling``
    reports only whether sampling was truncated. They come from sampling the candidate
    through the same mechanism the preview panel uses, which is a child process writing a
    file; nothing here reads the document.
    """
    lines = [
        candidate.path,
        "",
        f"score              {candidate.score:.4f}",
        f"occurrences        {candidate.count:,}",
        f"shape consistency  {candidate.shape_consistency:.1%}",
        f"repeat score       {candidate.repeat_score:.3f}",
        f"depth              {candidate.depth}",
        f"nested inside      {candidate.nested_inside or '(top level)'}",
    ]
    if candidate.child_tags:
        lines += ["", "child elements"]
        lines += [f"  {tag}" for tag in candidate.child_tags]
    if candidate.attribute_names:
        lines += ["", "attributes"]
        lines += [f"  {name}" for name in candidate.attribute_names]
    lines += ["", "namespaces"]
    if candidate.namespaces:
        for prefix, uri in sorted(candidate.namespaces.items()):
            lines.append(f"  {prefix if prefix else '<default>'} → {uri}")
    else:
        lines.append("  (none)")
    if candidate.missing_namespaces:
        lines += ["", "namespaces used with no prefix"]
        lines += [f"  {uri}" for uri in candidate.missing_namespaces]
    lines += _example_lines(examples, pending=pending, failure=failure)
    if candidate.evidence:
        lines += ["", "why it was proposed", f"  {candidate.evidence}"]
    return "\n".join(lines)


def _example_lines(
    examples: SampledTable | None,
    *,
    pending: bool,
    failure: str,
) -> list[str]:
    """The example-values section, which says which of the three states it is in.

    A section that simply vanishes while sampling would leave the reader unable to tell
    "no values exist" from "they are still coming".
    """
    if failure:
        return ["", "example values", f"  could not be sampled: {failure}"]
    if examples is None:
        return ["", "example values", "  sampling…" if pending else "  (none)"]
    if not examples.rows:
        return [
            "",
            "example values",
            "  the sample held no records, so there is nothing to show",
        ]
    lines = ["", f"example values (first {len(examples.rows)} of a sample)"]
    for header, value in zip(examples.headers, examples.rows[0], strict=False):
        lines.append(f"  {header} = {value}")
    if examples.rejected:
        lines.append(f"  ({examples.rejected} record(s) were rejected)")
    return lines
