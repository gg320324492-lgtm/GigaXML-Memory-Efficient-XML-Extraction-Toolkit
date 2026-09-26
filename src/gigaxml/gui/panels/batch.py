"""The batch panel: a list of files, and one child process at a time.

**The constraint this panel exists to honour.** "Batch" must not mean "one process doing
many files". The CLI's memory guarantees are per-run: a run holds one document's records
and releases them as it writes. Feeding it several documents would either hold them all at
once -- the thing this project exists to avoid -- or need a second extraction loop inside
the interface, which would be a second implementation of the thing being tested.

So the loop below is deliberately shaped like this: take one job, build one argument list,
start one `CliProcess`, and when it exits take the next. `_start_next` is the only place a
process is created, it creates exactly one, and it is called once per job. That is what
"each file gets its own process" means in code, and `test_gui_batch.py` checks it against
this file rather than taking anybody's word for it.

**The queue itself is in `batch_queue.py` and knows nothing about processes.** This panel
is the half that spawns.
"""

from __future__ import annotations

import pathlib

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.batch_queue import BatchQueue
from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.i18n import tr
from gigaxml.gui.settings import FORMATS

__all__ = ["PUMP_INTERVAL_MS", "BatchPanel"]

#: How often the panel looks at whether the child has finished. The reader thread only sets
#: a flag -- it may not touch a widget -- so something on the UI thread has to notice.
PUMP_INTERVAL_MS = 50

#: The table's columns. Translated where they are displayed, so the constant stays the
#: identity the tests and this file know.
_COLUMNS = ("Source", "State", "Detail")

#: The file extension each output format implies, so ``-o`` and ``--format`` agree. The CLI
#: is explicit about the pair, and a mismatch fails the run for a reason a user cannot act
#: on from the screen.
#:
#: **Derived from :data:`gigaxml.gui.settings.FORMATS`, and looked up with ``get`` rather
#: than ``[]``.** Both are load-bearing. The list used to be a literal here *and* another
#: literal in ``addItems`` *and* a third in ``settings.FORMATS``, which meant "add a format"
#: was a three-place edit and forgetting one produced a ``KeyError`` on a user's machine
#: rather than a test failure. The ``get`` is the second half: a stored preference from a
#: newer version of the settings file must not be able to crash this panel, and the value
#: that reaches ``build_args`` is a widget's selection, not a validated setting.
_SUFFIXES = {fmt: f".{fmt}" for fmt in FORMATS}


class BatchPanel(QWidget):
    """Runs a list of documents, one child process each, in order.

    Args:
        state_dir: where a default output directory is derived from. Injected so a test
            does not write into the user's configuration directory.
    """

    #: Emitted with ``(finished, total)`` as the queue advances.
    progress_changed = Signal(int, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._queue = BatchQueue()
        self._process: CliProcess | None = None
        self._outcome: RunResult | None = None
        self._finished_flag = False

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self._config = QLineEdit(self)
        self._config.setPlaceholderText(tr("config file"))
        top.addWidget(QLabel(tr("Config"), self))
        top.addWidget(self._config, 2)
        self._format = QComboBox(self)
        # From ``FORMATS`` rather than a literal, so this combo and :data:`_SUFFIXES` cannot
        # drift apart. See the note there. Each item carries its value in its data, and the
        # reads below go through ``currentData``: this selection becomes a ``--format``
        # flag and half of an output file name, and the label is the part translation may
        # rewrite.
        for value in FORMATS:
            self._format.addItem(tr(value), value)
        top.addWidget(QLabel(tr("Format"), self))
        top.addWidget(self._format, 1)
        layout.addLayout(top)

        buttons = QHBoxLayout()
        self._add = QPushButton(tr("Add documents…"), self)
        self._add.clicked.connect(self._choose_documents)
        buttons.addWidget(self._add)
        self._output = QLineEdit(self)
        self._output.setPlaceholderText(tr("output directory"))
        buttons.addWidget(self._output, 2)
        self._start = QPushButton(tr("Start"), self)
        self._start.clicked.connect(self.start)
        buttons.addWidget(self._start)
        self._cancel = QPushButton(tr("Cancel"), self)
        self._cancel.clicked.connect(self.cancel)
        self._cancel.setEnabled(False)
        buttons.addWidget(self._cancel)
        layout.addLayout(buttons)

        self._table = QTableWidget(0, len(_COLUMNS), self)
        self._table.setHorizontalHeaderLabels([tr(column) for column in _COLUMNS])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._table, 1)

        self._summary = QLabel(tr("No documents queued."), self)
        layout.addWidget(self._summary)

        # The only timer this panel owns. It exists to look at a flag the reader thread
        # sets, because that thread may not touch a widget.
        self._pump = QTimer(self)
        self._pump.setInterval(PUMP_INTERVAL_MS)
        self._pump.timeout.connect(self._drain)

    # -- building the queue ------------------------------------------------

    def add_documents(self, sources: list[pathlib.Path | str]) -> None:
        """Queue documents. Each gets its own output directory, named after it.

        Separate directories rather than one shared: two runs writing the same directory
        would interleave their manifests and partial files, and the second would look like
        a resume of the first.
        """
        base = pathlib.Path(self._output.text().strip() or ".")
        for source in sources:
            path = pathlib.Path(source)
            self._queue.add(path, base / f"{path.stem}-out")
        self._refresh()

    def _choose_documents(self) -> None:
        chosen, _ = QFileDialog.getOpenFileNames(
            self, tr("Add documents"), "", "XML (*.xml *.xml.gz)"
        )
        if chosen:
            self.add_documents(list(chosen))

    def queue(self) -> BatchQueue:
        """The queue. Exposed so a test can drive the state machine without a dialog."""
        return self._queue

    # -- running -----------------------------------------------------------

    def build_args(self, job_source: pathlib.Path, job_output: pathlib.Path) -> list[str]:
        """The CLI arguments for one job. Public so the mapping is testable without a process.

        ``job_output`` is a **directory** this panel chose, not a file the user picked, and
        those are not interchangeable. ``-o`` names the file the rows go to, and neither the
        CLI nor the writers create the parent directory -- measured: pointing ``-o`` at a
        directory that does not exist fails with ``cannot open ... .tmp for writing`` before
        a single record is read. The execution panel gets away with passing a user's
        ``getSaveFileName`` choice straight through because the file dialog has already made
        the directory by then. Nothing makes this panel's directories, so this one does.
        """
        return [
            "extract",
            str(job_source),
            "-c",
            self._config.text().strip(),
            "-o",
            # The value, not the label -- twice over: once as the file's suffix, once as
            # the ``--format`` flag. Both reach the CLI and the disk, and neither may
            # follow the language of the interface.
            str(job_output / f"rows{_SUFFIXES.get(self._format.currentData(), '.csv')}"),
            "--format",
            self._format.currentData(),
            "--progress",
        ]

    def start(self) -> None:
        """Begin the queue, if it is not already running."""
        self._start.setEnabled(False)
        self._cancel.setEnabled(True)
        self._start_next()

    def _start_next(self) -> None:
        """Take the next job and start **one** child process for it.

        The single place in this panel that creates a process. One job in, one process out;
        when it exits, this is called again for the job after it. Nothing here hands the
        CLI more than one document.
        """
        job = self._queue.next_job()
        if job is None:
            self._pump.stop()
            self._start.setEnabled(True)
            self._cancel.setEnabled(False)
            self._process = None
            self._refresh()
            return
        # The one place a job's directory is made. ``add_documents`` only records the name;
        # nothing else creates it, and neither the CLI nor the writers will -- measured: a
        # missing parent directory fails the run at ``open(... .tmp)`` before a single record
        # is read, so a batch that skipped this would fail every job for a reason that has
        # nothing to do with the documents.
        job.output.mkdir(parents=True, exist_ok=True)
        self._outcome = None
        self._finished_flag = False
        self._refresh()
        process = CliProcess(
            self.build_args(job.source, job.output),
            on_finished=self._note_finished,  # reader thread: set a flag only
        )
        self._process = process
        process.start()
        self._pump.start()

    def _note_finished(self, run: RunResult) -> None:
        """Runs on the reader thread. Records the outcome and touches nothing else."""
        self._outcome = run
        self._finished_flag = True

    def _drain(self) -> None:
        """On the UI thread: if the child has finished, record it and move on."""
        if not self._finished_flag:
            return
        self._finished_flag = False
        job = self._queue.running
        outcome = self._outcome
        if job is not None:
            ok = outcome is not None and outcome.exit_code == 0
            detail = (
                ""
                if ok
                else (tr("exit {}").format(outcome.exit_code) if outcome else tr("no result"))
            )
            self._queue.note_finished(job, ok=ok, detail=detail)
        self._refresh()
        self._start_next()

    def cancel(self) -> None:
        """Stop what has not started, and stop waiting on what has.

        The running job's process is killed here rather than in the queue: the queue does
        not know there is one.
        """
        self._queue.stop_after_current()
        process = self._process
        if process is not None:
            process.kill()
        self._pump.stop()
        self._start.setEnabled(True)
        self._cancel.setEnabled(False)
        self._refresh()

    def is_running(self) -> bool:
        return self._queue.running is not None

    def refresh(self) -> None:
        """Redraw the table from the queue. Public so a test can assert on the state."""
        self._refresh()

    def _refresh(self) -> None:
        rows = self._queue.rows()
        self._table.setRowCount(len(rows))
        for index, (name, state, detail) in enumerate(rows):
            # The state is a stored value -- it also goes into the queue's JSON -- so the
            # translation happens here, at the moment it becomes a table cell, and never
            # in the queue itself.
            for column, text in enumerate((name, tr(state), detail)):
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self._table.setItem(index, column, item)
        counts = self._queue.counts()
        self._summary.setText(
            tr("{} done, {} failed, {} skipped, {} to go").format(
                counts["done"],
                counts["failed"],
                counts["skipped"],
                counts["waiting"] + counts["running"],
            )
        )
        self.progress_changed.emit(counts["done"] + counts["failed"] + counts["skipped"], len(rows))

    # -- the panel contract ------------------------------------------------

    def shutdown(self) -> None:
        """Idempotent, and it kills the child and stops the timer.

        Both, and in that order, because a timer left running fires into a panel nobody is
        looking at and a child left running outlives the window that started it -- which is
        the bug the twenty-eight round quality line was spent on.
        """
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        process = self._process
        self._process = None
        if process is not None:
            process.kill()
        self._pump.stop()
