"""The history of what has been run, and the one button that continues a run.

**This panel displays; it does not record.** Every number in the table comes from a
``run-report.json`` the CLI wrote, read through :mod:`gigaxml.gui.job_history`. The panel
opens no document, starts no parser and reads no ``checkpoint.json`` — see the module
docstring there for why the report is the only source. What the window does with a row is
the rest of this file.

**"Not recorded" and "zero" are different cells.** The formatting helpers below return
:data:`None` for a field the report did not carry, and the cell says so. This is the same
rule :func:`gigaxml.run.peak_rss_mb` states for itself — a report that filled an unavailable
peak with ``0.0`` would produce a history row reading ``0.0 MiB``, which looks like a run
that allocated nothing and would pass a reader scanning for a regression. A row that says
"not recorded" is a row that has told the truth.

**A report that cannot be read gets a row.** :func:`gigaxml.gui.job_history.read_report`
returns an entry for a file it could not parse, with ``readable=False`` and a reason, and
this panel renders that row rather than dropping it. Dropping it would show an empty table
where a corrupt file is sitting on disk, and the user would conclude the run never happened.

**The Resume button does not resume.** It hands the row to the window, which fills the
execution panel in; starting the run is the user's separate, deliberate press of Start, and
the actual continuing is the CLI's ``--resume`` — the same flag the execution panel passes
when the user ticks Resume there. **A button labelled "resume" that silently starts
extracting is the wrong control**, and one that quietly ticked a checkbox would be
continuing somebody's earlier work for them, which ``test_gui_resume.py`` already pins down
as something this project refuses to do.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.i18n import tr
from gigaxml.gui.job_history import HistoryEntry, JobHistory
from gigaxml.gui.panels.results import count_of, format_elapsed

__all__ = ["HistoryPanel"]

#: The table's columns. Translated on the way to the header, so the constant stays the
#: identity the tests know.
_COLUMNS = ("When", "Source", "Records", "Time", "Peak", "Status")

#: The colour the results panel already uses for something the user has to look at, as a
#: hex string for a table cell and as a stylesheet for a label. A failure that looks like
#: ordinary status text is not a failure.
_WARNING_HEX = "#c0392b"
_WARNING_COLOUR = f"color: {_WARNING_HEX};"


def format_peak(mib: float | None) -> str:
    """A peak in MiB, or the words saying there is not one.

    **Never ``"0.0 MiB"`` for a missing measurement.** See the module docstring: a zero here
    is indistinguishable from a run that allocated nothing, and it is the number a reader
    scanning for a regression would find plausible.
    """
    return tr("not recorded") if mib is None else f"{mib:.1f} MiB"


def format_records(entry: HistoryEntry) -> str:
    """Rows written, or why there is no number.

    Three cases, three different sentences. A report that was not read has no rows and is
    not a run that wrote none; and a run that never reached the end of its document is
    neither of those — it committed parts and then stopped, and how many is in the
    manifest rather than anywhere this panel is allowed to read.
    """
    if not entry.has_report:
        return tr("unknown — the run wrote no report")
    if not entry.readable:
        return tr("unknown — the report could not be read")
    return count_of(entry.rows or 0, "row")


def format_when(entry: HistoryEntry) -> str:
    """When the run finished, or when it started, or that neither was recorded.

    A stopped run may have a ``started_at`` and no ``finished_at``, so the fallback is
    checked rather than assumed away — and a report with neither says so instead of showing
    a date that belongs to nothing.
    """
    stamp = entry.finished_at or entry.started_at
    return stamp.strftime("%Y-%m-%d %H:%M:%S") if stamp is not None else tr("not recorded")


def format_status(entry: HistoryEntry) -> str:
    """The one word a row's state is read from, for the status column.

    **Four states now, and the fourth was added because the CLI began writing it.** A run
    that was *stopped* used to leave no report at all, and the history inferred it from the
    checkpoint instead; since the CLI writes a report saying ``"interrupted"``, that row has
    a report of its own and a status of its own, and a panel that only knew ``ok`` and
    ``failed`` would have shown it as "unknown" — a stopped run labelled as an unrecognised
    one, which is worse than showing nothing because it looks like a conclusion.

    The two that cannot tell are still worded differently from the two that report a fact.
    A report that will not parse is a file to look at; a run that wrote no report was
    interrupted, and the tool has nothing to show for it because a process given no chance
    to return never reached the line that would have written one — see
    :func:`gigaxml.gui.job_history.interrupted_run` for the measurement, and note that a
    run stopped by a *signal* now does write a report, while one killed without one still
    does not.
    """
    if not entry.has_report:
        return tr("stopped — no report")
    if not entry.readable:
        return tr("report unreadable")
    if entry.status == "ok":
        return tr("finished")
    if entry.status == "interrupted":
        return tr("interrupted")
    if entry.status == "failed":
        return tr("failed")
    return tr("unknown")


def is_warning_text(text: str) -> bool:
    """Whether a status is one the user has to look at.

    Derived from the text rather than from a parallel flag, so the two cannot disagree: the
    cell and the colour come from the same :func:`format_status` call.
    """
    return text in (
        tr("failed"),
        tr("interrupted"),
        tr("report unreadable"),
        tr("stopped — no report"),
        tr("unknown"),
    )


class HistoryPanel(QWidget):
    """Past runs, read from their reports, and a way back into one of them."""

    #: Emitted with the chosen :class:`HistoryEntry`. The window owns what happens next --
    #: a panel that filled another panel's controls itself would know about tabs.
    resume_requested = Signal(object)

    def __init__(self, history: JobHistory, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._history = history
        self._entries: list[HistoryEntry] = []

        layout = QVBoxLayout(self)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_selection)
        layout.addWidget(self._table, 1)

        self._detail = QLabel(self)
        self._detail.setWordWrap(True)
        self._detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._detail)

        buttons = QHBoxLayout()
        self._resume = QPushButton(tr("Resume this job"), self)
        self._resume.setToolTip(
            tr(
                "Fill the execution panel with this run so it can be continued. Nothing is "
                "extracted until you press Start there."
            )
        )
        self._resume.clicked.connect(self._on_resume)
        buttons.addWidget(self._resume)
        self._forget = QPushButton(tr("Forget this directory"), self)
        self._forget.setToolTip(
            tr("Stop listing this directory. The reports on disk are left alone.")
        )
        self._forget.clicked.connect(self._on_forget)
        buttons.addWidget(self._forget)
        self._refresh = QPushButton(tr("Refresh"), self)
        self._refresh.setToolTip(tr("Read the reports again, including ones written since."))
        self._refresh.clicked.connect(self.refresh)
        buttons.addWidget(self._refresh)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._headline = QLabel(self)
        layout.addWidget(self._headline)

        self.refresh()

    # -- reading -----------------------------------------------------------

    def refresh(self) -> None:
        """Re-read every remembered report.

        Called on construction and by the Refresh button. Reading the reports rather than
        trusting a cache is what makes the panel honest after a run the window did not start
        — a job run from the command line into a remembered directory appears the next time
        the list is opened, because its report is the record and not this window's memory.
        """
        self._entries = self._history.entries()
        self._table.setRowCount(len(self._entries))
        warn = QBrush(QColor(_WARNING_HEX))
        for row, entry in enumerate(self._entries):
            status = format_status(entry)
            cells = (
                format_when(entry),
                str(entry.source) if entry.source is not None else tr("not recorded"),
                format_records(entry),
                format_elapsed(entry.elapsed_seconds) or tr("not recorded"),
                format_peak(entry.peak_rss_mb),
                status,
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if is_warning_text(status):
                    # Applied to every cell in the row rather than to the status cell alone,
                    # because a red word at the edge of a wide table is a red word nobody
                    # reads -- and the whole row is what the user is deciding about.
                    item.setForeground(warn)
                self._table.setItem(row, column, item)
            if is_warning_text(status):
                self._table.item(row, 0).setToolTip(
                    entry.unreadable_reason or entry.error_type or status
                )
        self._table.setHorizontalHeaderLabels([tr(column) for column in _COLUMNS])
        self._on_selection()

    def entries(self) -> tuple[HistoryEntry, ...]:
        """The rows currently listed, newest first."""
        return tuple(self._entries)

    def selected(self) -> HistoryEntry | None:
        """The selected row's entry, or ``None``."""
        rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
        if not rows:
            return None
        index = rows[0].row()
        return self._entries[index] if 0 <= index < len(self._entries) else None

    def row_count(self) -> int:
        return self._table.rowCount()

    def cell(self, row: int, column: int) -> str:
        item = self._table.item(row, column)
        return item.text() if item is not None else ""

    def detail_text(self) -> str:
        return self._detail.text()

    def headline_text(self) -> str:
        return self._headline.text()

    def is_resume_enabled(self) -> bool:
        """Whether the row chosen is one the CLI would accept ``--resume`` for.

        **``isEnabled``, not ``isVisible``**, so it can be read without a window on screen --
        the same rule ``ExecutionPanel.is_resume_offered`` follows.
        """
        return self._resume.isEnabled()

    def resume_button_text(self) -> str:
        return self._resume.text()

    def forget_button_text(self) -> str:
        return self._forget.text()

    # -- internals ---------------------------------------------------------

    def _on_selection(self) -> None:
        entry = self.selected()
        self._detail.setText("" if entry is None else _detail_lines(entry))
        self._resume.setEnabled(entry is not None and entry.can_resume)
        self._forget.setEnabled(entry is not None)
        if entry is None:
            self._headline.setText(tr("no runs recorded yet"))
            self._headline.setStyleSheet("")
        elif not entry.readable:
            self._headline.setText(_unreadable_headline(entry))
            self._headline.setStyleSheet(_WARNING_COLOUR)
        elif entry.can_resume:
            self._headline.setText(
                tr("This run did not finish. Continuing it re-reads the source from the beginning.")
            )
            self._headline.setStyleSheet("")
        else:
            self._headline.setText("")
            self._headline.setStyleSheet("")

    def _on_resume(self) -> None:
        entry = self.selected()
        if entry is not None and entry.can_resume:
            self.resume_requested.emit(entry)

    def _on_forget(self) -> None:
        entry = self.selected()
        if entry is None:
            return
        self._history.forget(entry.directory)
        self.refresh()


def _unreadable_headline(entry: HistoryEntry) -> str:
    """The one line above the table for a row with no usable report.

    **A stopped run gets the sentence about continuing, not just the one about being
    unreadable** — the checkpoint has said whether it can be continued, so there is now a
    button to press and the headline should say so rather than sending the user to a panel
    that would tell them the same thing in one more click.
    """
    if not entry.has_report:
        if entry.can_resume:
            return tr(
                "This run was stopped and wrote no report, so there is no count, no time and "
                "no peak to show. Its checkpoint says it did not finish, so it can be "
                "continued — the tool will check that the source and the config still match "
                "before it writes anything."
            )
        return tr(
            "This run was stopped, so it wrote no report: there is no count, no time and no "
            "peak to show. Its parts are in {}. Its checkpoint says the source was fully "
            "consumed, so there is nothing to continue."
        ).format(entry.directory)
    return entry.unreadable_reason or tr("report unreadable")


def _detail_lines(entry: HistoryEntry) -> str:
    """Everything the row does not have a column for, one label per line.

    **A row with no report still gets its checkpoint block.** There are no run facts to
    show -- no rows, no time, no peak -- but there are two things a user needs before
    pressing Resume: that it can be continued at all, and how big its parts were, since
    ``--resume`` cannot be passed without ``--checkpoint-every``. Both come from the
    checkpoint, and saying so on this row is what keeps it distinct from a row whose report
    said the same things.
    """
    lines: list[str] = []
    if not entry.readable:
        sentence = tr("This report could not be read, so nothing about the run can be shown: {}")
        if not entry.has_report:
            sentence = tr("This run was stopped and wrote no report, so nothing is known: {}")
        lines.append(sentence.format(entry.unreadable_reason or tr("the reason was not recorded")))
        lines.append(tr("Directory: {}").format(entry.directory))
    else:
        lines.append(tr("Output: {}").format(entry.output or tr("not recorded")))
        lines.append(tr("Format: {}").format(entry.output_format or tr("not recorded")))
        lines.append(tr("Record path: {}").format(entry.record_path or tr("not recorded")))
        if entry.rejected:
            # First among the numbers, for the reason the results panel gives: a run that
            # skipped records still reports success and this count is the only sign of it.
            lines.append(tr("Rejected: {}").format(count_of(entry.rejected, "record")))
        if entry.error_type:
            lines.append(tr("Failed with: {}").format(entry.error_type))

    if entry.checkpoint_directory is not None:
        lines.append(tr("Parts directory: {}").format(entry.checkpoint_directory))
        if entry.checkpoint_every is not None:
            lines.append(tr("Part size: {}").format(count_of(entry.checkpoint_every, "record")))
        if entry.complete is not None:
            lines.append(
                tr("The whole source was consumed: {}").format(
                    tr("yes") if entry.complete else tr("no")
                )
            )
            if entry.complete_source == "manifest":
                # Naming the file is the point. The same sentence in a row whose report said
                # so would be a different claim, and a user comparing the two rows has no
                # way to tell which is which.
                lines.append(tr("(read from the checkpoint — this run wrote no report)"))
        elif entry.complete_source is None:
            lines.append(tr("Nothing says whether the source was fully consumed."))
    if entry.config_hash:
        # The hash, not a path: that is what the report records, and it is what the CLI
        # compares against when it decides whether a resume is the same run. The path the
        # window remembers for it lives in the execution panel.
        lines.append(tr("Config fingerprint: {}").format(entry.config_hash[:16]))
    if entry.readable:
        lines.append(tr("Report: {}").format(entry.report_path))
    return "\n".join(lines)
