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

from PySide6.QtCore import Qt, QTimer
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

#: How often the UI thread drains what the reader thread collected. Matches the execution
#: panel; the reasoning is there.
PUMP_INTERVAL_MS = 50

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source: Path | None = None
        self._process: CliProcess | None = None
        self._run_result: RunResult | None = None
        self._finished = False
        self._report: InspectReport | None = None

        self._build()

        self._pump = QTimer(self)
        self._pump.setInterval(PUMP_INTERVAL_MS)
        self._pump.timeout.connect(self._drain)

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
            lines.append("this document declares no namespaces")
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
        self._detail.setPlainText(_describe_candidate(report.candidates[stored]))

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


def _describe_candidate(candidate: Candidate) -> str:
    """The side panel's text for one candidate.

    Everything here comes from the JSON. **Example values are not included, because
    ``inspect --json`` does not carry them** -- ``value_sampling`` reports only whether
    sampling was truncated. Showing a made-up example, or one obtained by reading the
    document in the window's process, would both be worse than saying nothing.
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
    if candidate.evidence:
        lines += ["", "why it was proposed", f"  {candidate.evidence}"]
    return "\n".join(lines)
