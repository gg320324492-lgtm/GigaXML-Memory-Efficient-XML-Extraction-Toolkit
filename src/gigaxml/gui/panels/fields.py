"""The field configuration panel: a table of fields, and the project's verdict on it.

**There is no validation in this file, and that is deliberate.** The brief makes a second
implementation of the path, type and name rules a rejection, and the surest way to comply
is to have nothing to comply with: every keystroke builds a config mapping and hands it to
:func:`gigaxml.config.parse_config`, and whatever that function says is what the user is
shown. The type dropdown is filled from :class:`gigaxml.fields.FieldType`, so a type the
library does not know cannot be offered.

**Where the rules live.** ``gigaxml.gui.field_rows`` assembles and submits; this file only
moves data between widgets and that module. Anything that looks like a rule appearing here
-- a regular expression over a path, a set of type names, a check for repeated field names
-- would be the thing the brief forbids.

**Editing is live.** Every change re-validates, because a panel that only complains when
you press a button teaches the user to press the button. The message shown is the library's
own sentence, unedited, including its own name for the table.
"""

from __future__ import annotations

import json
import pathlib
from pathlib import Path

import yaml
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.cli_process import CliProcess, RunResult
from gigaxml.gui.field_rows import (
    FIELD_TYPE_NAMES,
    FieldRow,
    ValidationResult,
    build_config_dict,
    rows_from_config,
    validate,
)
from gigaxml.gui.inspect_report import Candidate
from gigaxml.gui.sampling import (
    discard_run_directory,
    generate_config_args,
    make_run_directory,
)
from gigaxml.gui.saved_configs import ConfigLibrary, SavedConfig

#: How often the UI thread drains what the reader thread collected. Matches the other
#: panels; the reasoning is in :mod:`gigaxml.gui.panels.execution`.
PUMP_INTERVAL_MS = 50

#: The columns, in order.
HEADERS = ["field name", "path", "type", "required"]

#: Where the row a drag started from is remembered. Not read from the view during a drop,
#: because the view's current row can change while the drag is in flight.
_COLUMN_NAME = 0
_COLUMN_PATH = 1
_COLUMN_TYPE = 2
_COLUMN_REQUIRED = 3


class FieldConfigPanel(QWidget):
    """Edit the fields a config will extract."""

    #: Emitted whenever the table changes, so the preview knows to re-sample.
    config_changed = Signal()

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        library: ConfigLibrary | None = None,
    ) -> None:
        """Build the table.

        Args:
            library: where saved configurations are remembered. Injected and optional, so
                a test may pass a temporary directory -- or nothing, and get a panel that
                still saves to a path but remembers nothing about it.
        """
        super().__init__(parent)
        self._library = library
        self._rows: list[FieldRow] = []
        self._namespaces: dict[str, str] = {}
        self._on_error: str | None = None
        self._result: ValidationResult | None = None
        self._path_choices: tuple[str, ...] = ()
        self._candidate: Candidate | None = None
        self._candidate_index: int | None = None
        self._source: Path | None = None
        #: The config request: a child process, its own directory, its own timer.
        self._regenerate_process: CliProcess | None = None
        self._regenerate_directory: Path | None = None
        self._generated_config: Path | None = None
        self._generate_result: RunResult | None = None
        self._generate_done = False
        #: Which request the child in flight belongs to, so an abandoned one cannot report
        #: into a table that has moved on.
        self._regenerate_generation = 0
        self._generate_generation = 0
        self._suspend = False

        self._build()

        self._regenerate_pump = QTimer(self)
        self._regenerate_pump.setInterval(PUMP_INTERVAL_MS)
        self._regenerate_pump.timeout.connect(self._drain_regenerate)
        self._refresh_regenerate()
        self.add_row()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        record_box = QGroupBox("Record", self)
        record_layout = QHBoxLayout(record_box)
        record_layout.addWidget(QLabel("record path", self))
        self._record = QLineEdit(self)
        self._record.setPlaceholderText("/catalog/products/product")
        self._record.textChanged.connect(self._on_edited)
        record_layout.addWidget(self._record, 1)
        self._regenerate = QPushButton("From candidate", self)
        self._regenerate.setToolTip(
            "Fill the record path and the field list from the candidate selected in the "
            "structure panel: its direct children and its attributes."
        )
        self._regenerate.clicked.connect(self.regenerate_from_candidate)
        record_layout.addWidget(self._regenerate)
        layout.addWidget(record_box)

        self._table = QTableWidget(0, len(HEADERS), self)
        self._table.setObjectName("field_table")
        self._table.setHorizontalHeaderLabels(HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        # Rows are dragged to reorder them. The move itself is done on the data and the
        # table rebuilt, because the cells hold widgets and Qt's own internal move carries
        # the items but leaves the widgets behind.
        self._table.setDragEnabled(True)
        self._table.setAcceptDrops(True)
        self._table.setDropIndicatorShown(True)
        self._table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._table.verticalHeader().setVisible(False)
        self._table.itemSelectionChanged.connect(self._on_edited)
        layout.addWidget(self._table, 1)

        buttons = QHBoxLayout()
        for label, slot in (
            ("Add", self.add_row),
            ("Delete", self.delete_selected),
            ("Move up", lambda: self.move_selected(-1)),
            ("Move down", lambda: self.move_selected(1)),
        ):
            button = QPushButton(label, self)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        buttons.addStretch(1)
        # No sample-size control here: the preview owns the limit, and a second spin box
        # for the same number is two places to keep in step.
        self._export = QPushButton("Save config…", self)
        self._export.clicked.connect(self.choose_save_path)
        buttons.addWidget(self._export)
        layout.addLayout(buttons)

        self._message = QLabel("", self)
        self._message.setObjectName("field_message")
        self._message.setWordWrap(True)
        self._message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._message)

    # -- the data ----------------------------------------------------------

    def rows(self) -> tuple[FieldRow, ...]:
        """The table as data. The single source of truth; the widgets are a rendering."""
        return tuple(self._rows)

    def set_rows(self, rows: list[FieldRow] | tuple[FieldRow, ...]) -> None:
        self._rows = list(rows)
        self._render()
        self._on_edited()

    def record_path(self) -> str:
        return self._record.text().strip()

    def set_record_path(self, path: str) -> None:
        self._record.setText(path)

    def set_namespaces(self, namespaces: dict[str, str]) -> None:
        """The namespace map field paths are resolved against.

        Taken from the analysed document rather than typed here: the map is a property of
        the document, and asking the user to retype it would be asking them to keep two
        things in step by hand.
        """
        self._namespaces = dict(namespaces)
        self._on_edited()

    def set_on_error(self, policy: str | None) -> None:
        self._on_error = policy
        self._on_edited()

    def set_path_choices(self, paths: tuple[str, ...]) -> None:
        """What the path boxes complete against -- the path table from the analysis."""
        self._path_choices = paths
        self._install_completers()

    def path_choices(self) -> tuple[str, ...]:
        return self._path_choices

    # -- editing -----------------------------------------------------------

    def add_row(self) -> None:
        self.set_rows([*self._rows, FieldRow(name="", path="")])
        self._table.setCurrentCell(len(self._rows) - 1, _COLUMN_NAME)

    def delete_selected(self) -> None:
        index = self._table.currentRow()
        if index >= 0:
            self.delete_row(index)

    def delete_row(self, index: int) -> None:
        if 0 <= index < len(self._rows):
            remaining = [row for position, row in enumerate(self._rows) if position != index]
            self.set_rows(remaining)

    def move_selected(self, offset: int) -> None:
        index = self._table.currentRow()
        if index >= 0:
            self.move_row(index, index + offset)

    def move_row(self, source: int, target: int) -> None:
        """Move a row, as a drag would. Out-of-range targets clamp to the ends."""
        if not 0 <= source < len(self._rows):
            return
        target = max(0, min(target, len(self._rows) - 1))
        if target == source:
            return
        rows = list(self._rows)
        row = rows.pop(source)
        rows.insert(target, row)
        self.set_rows(rows)
        self._table.setCurrentCell(target, _COLUMN_NAME)

    def regenerate_from_candidate(self) -> None:
        """Ask the CLI to write a config for the candidate, then show it as rows.

        **Through the CLI, not by rebuilding the list here.** ``inspect --generate-config``
        decides what a candidate's fields are -- including that its attributes count as
        fields -- and that decision belongs in one place. Assembling ``child_tags`` and
        ``attribute_names`` here would be a second implementation of it, and one that stops
        agreeing the first time the CLI's rule changes. The panel already has the machinery
        to run a child; this is the call the structure panel makes for its example values.

        Asynchronous, because it is a child process. :meth:`is_regenerating` says when it
        has landed.
        """
        source = self._source
        index = self._candidate_index
        if source is None or index is None:
            self._message.setText("select a candidate in the structure panel first")
            return
        # Clicking again means "the one selected now", so the request in flight is
        # abandoned rather than allowed to finish and win: leaving it would fill the table
        # with the fields of a candidate the user has moved off. Same shape as the
        # structure panel's example values, and the same two parts -- the generation check
        # decides which answer is correct, the cancellation decides that the other one
        # stops running.
        self._cancel_regenerate()
        self._regenerate_generation += 1
        generation = self._regenerate_generation
        discard_run_directory(self._regenerate_directory)
        self._regenerate_directory = make_run_directory("gigaxml-generate-")
        self._generated_config = self._regenerate_directory / "config.yaml"
        self._message.setText("asking the CLI for a config…")
        process = CliProcess(
            # 1-based, matching what `inspect` prints and what the candidate table shows.
            generate_config_args(source, self._generated_config, index + 1),
            on_finished=lambda run, gen=generation: self._note_generated_config(run, gen),
        )
        self._regenerate_process = process
        process.start()
        self._regenerate_pump.start()

    def shutdown(self) -> None:
        """Give up what the window cannot clean up when it closes.

        **This panel had no shutdown at all.** The window called execution, preview and
        structure on close and never called this one, so the regenerate pump kept firing at
        a window nobody was looking at and its child kept reading pipes. That is the shape
        the CI segfault's stack shows: a timer firing into something that is already gone.

        Idempotent for the same reason the other three are: the window closes this panel and
        a test may also close the panel itself, and both routes land here.
        """
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        self._cancel_regenerate()

    def _cancel_regenerate(self) -> None:
        """Stop the request in flight, if there is one, and forget it."""
        process = self._regenerate_process
        self._regenerate_process = None
        if process is not None:
            process.kill()
        # After kill(), not before. kill waits for the callback, and the callback is what
        # sets this flag -- so clearing it first would be undone by the very step it is
        # meant to forget, and the line would read as "forget it" while doing nothing.
        self._generate_done = False
        self._regenerate_pump.stop()

    def _note_generated_config(self, run: RunResult, generation: int) -> None:
        """Reader thread. Records the outcome and touches no widget.

        No filtering by generation here. It used to filter -- a superseded request's answer
        was dropped before it could reach the slot -- because ``CliProcess.kill`` returned
        without joining its reader thread when the child had already exited, so a request
        that finished on its own could report *after* the one that replaced it. That is
        fixed where it belongs, in ``kill``: it now always waits for the callback, so by the
        time the next request starts, the previous one has already reported. Two mechanisms
        for one rule would only leave the next person guessing which one to trust.
        """
        self._generate_result = run
        self._generate_generation = generation
        self._generate_done = True

    def _drain_regenerate(self) -> None:
        """UI thread. Reads the config the CLI wrote, once it has finished writing it."""
        if not self._generate_done:
            return
        self._generate_done = False
        if self._generate_generation != self._regenerate_generation:
            # Belt and braces, and a different job from the filter above: this catches the
            # case where the answer was recorded correctly and the user clicked again
            # before the UI thread got a turn, so the answer no longer matches the row.
            return
        run = self._generate_result
        self._regenerate_process = None
        self._regenerate_pump.stop()
        if run is None or not run.ok or self._generated_config is None:
            warnings = run.warnings if run is not None else []
            first = next((line for line in warnings if line.strip()), None)
            self._message.setText(first or "the CLI could not write a config for this candidate")
            return
        if not self._generated_config.is_file():
            self._message.setText("the CLI reported success but wrote no config")
            return
        payload = yaml.safe_load(self._generated_config.read_text(encoding="utf-8"))
        record, rows = rows_from_config(payload if isinstance(payload, dict) else {})
        if record:
            self._record.setText(record)
        self.set_rows(list(rows))

    def is_regenerating(self) -> bool:
        """Whether a request for a config is in flight."""
        return self._regenerate_process is not None

    def generated_config_path(self) -> Path | None:
        """Where the config the CLI wrote is, for a caller that wants to look at it."""
        return self._generated_config

    def set_source(self, path: Path | str | None) -> None:
        """The document the candidate was found in.

        The CLI needs it: a config for a candidate cannot be written without the document
        that candidate came from.
        """
        self._source = Path(path) if path is not None else None
        self._refresh_regenerate()

    def set_candidate(self, candidate: Candidate | None, index: int | None = None) -> None:
        """The candidate the "From candidate" button regenerates from.

        Both halves are needed: the candidate says what to build a config for, and ``index``
        is the number the CLI takes, which the panel cannot work out for itself -- the view
        row and the report index are different numbers once the table has sorted itself.

        Set by the window when the structure panel's selection changes, so the button is
        only live when there is something to build from.
        """
        self._candidate = candidate
        self._candidate_index = index
        self._refresh_regenerate()

    def candidate(self) -> Candidate | None:
        return self._candidate

    def candidate_index(self) -> int | None:
        return self._candidate_index

    def _refresh_regenerate(self) -> None:
        self._regenerate.setEnabled(
            self._source is not None
            and self._candidate is not None
            and self._candidate_index is not None
        )

    # -- validation --------------------------------------------------------

    def validate_now(self) -> ValidationResult:
        """Ask the project about the current table and show what it said.

        No ``source`` is passed, so the message carries the default label the module
        defines. Naming the table here as well would be a second place to keep in step
        with it, and the two would drift the moment one was reworded.
        """
        result = validate(
            self._rows,
            record_path=self.record_path(),
            namespaces=self._namespaces or None,
            on_error=self._on_error,
        )
        self._result = result
        self._show(result)
        return result

    def result(self) -> ValidationResult | None:
        return self._result

    def config(self):  # noqa: ANN201 - the type is ExtractionConfig, imported lazily above
        """The validated config, or ``None`` while the table is not acceptable."""
        return self._result.config if self._result is not None else None

    def message_text(self) -> str:
        return self._message.text()

    def invalid_rows(self) -> tuple[int, ...]:
        """Which rows to mark. Empty when the table is acceptable."""
        if self._result is None or self._result.ok or self._result.row_index is None:
            return ()
        return (self._result.row_index,)

    def _show(self, result: ValidationResult) -> None:
        if result.ok:
            self._message.setText(f"this is a config the CLI accepts — {len(self._rows)} fields")
            self._message.setStyleSheet("")
        else:
            # The library's own sentence, unedited. Rewording it here would be the first
            # step towards a second implementation of the rules behind it.
            self._message.setText(result.message)
            self._message.setStyleSheet("color: #c0392b;")
        self._export.setEnabled(result.ok)
        self._mark_rows(self.invalid_rows())

    def _mark_rows(self, indices: tuple[int, ...]) -> None:
        for row in range(self._table.rowCount()):
            bad = row in indices
            for column in range(self._table.columnCount()):
                widget = self._table.cellWidget(row, column)
                if widget is not None:
                    widget.setStyleSheet("background-color: #ffd6d6;" if bad else "")

    def _on_edited(self) -> None:
        """Re-validate after any change, unless we are the ones making the change."""
        if self._suspend:
            return
        self.validate_now()
        self.config_changed.emit()

    # -- rendering ---------------------------------------------------------

    def _render(self) -> None:
        """Rebuild the table from the data. The widgets never hold the truth."""
        self._suspend = True
        self._table.setRowCount(len(self._rows))
        for index, row in enumerate(self._rows):
            self._table.setCellWidget(index, _COLUMN_NAME, self._name_editor(row.name))
            self._table.setCellWidget(index, _COLUMN_PATH, self._path_editor(row.path))
            self._table.setCellWidget(index, _COLUMN_TYPE, self._type_editor(row.type_name))
            self._table.setCellWidget(index, _COLUMN_REQUIRED, self._required_editor(row.required))
        self._suspend = False

    def _name_editor(self, value: str) -> QLineEdit:
        editor = QLineEdit(value, self)
        editor.textChanged.connect(
            lambda text, widget=editor: self._set_name(self._row_of(widget), text)
        )
        return editor

    def _path_editor(self, value: str) -> QLineEdit:
        editor = QLineEdit(value, self)
        editor.setPlaceholderText("name  or  @id  or  manufacturer/name")
        editor.textChanged.connect(
            lambda text, widget=editor: self._set_path(self._row_of(widget), text)
        )
        return editor

    def _type_editor(self, value: str) -> QComboBox:
        editor = QComboBox(self)
        editor.addItems(list(FIELD_TYPE_NAMES))
        if value in FIELD_TYPE_NAMES:
            editor.setCurrentText(value)
        editor.currentTextChanged.connect(
            lambda text, widget=editor: self._set_type(self._row_of(widget), text)
        )
        return editor

    def _required_editor(self, value: bool) -> QCheckBox:
        editor = QCheckBox(self)
        editor.setChecked(value)
        editor.toggled.connect(
            lambda checked, widget=editor: self._set_required(self._row_of(widget), checked)
        )
        return editor

    def _row_of(self, widget: QWidget) -> int:
        """Which row a cell widget is in, asked of the table at call time.

        **At call time, not at connect time.** The editors are built before they are placed
        in the table, so asking when the signal is connected returns -1 for every one of
        them -- which made every edit silently do nothing while the table looked live. The
        widget is the identity that survives a rebuild; the index is looked up fresh.
        """
        for row in range(self._table.rowCount()):
            for column in range(self._table.columnCount()):
                if self._table.cellWidget(row, column) is widget:
                    return row
        return -1

    def _replace(self, index: int, **changes: object) -> None:
        if not 0 <= index < len(self._rows):
            return
        current = self._rows[index]
        self._rows[index] = FieldRow(
            name=changes.get("name", current.name),  # type: ignore[arg-type]
            path=changes.get("path", current.path),  # type: ignore[arg-type]
            type_name=changes.get("type_name", current.type_name),  # type: ignore[arg-type]
            required=bool(changes.get("required", current.required)),
        )

    def _set_name(self, index: int, text: str) -> None:
        self._replace(index, name=text)
        self._on_edited()

    def _set_path(self, index: int, text: str) -> None:
        self._replace(index, path=text)
        self._on_edited()

    def _set_type(self, index: int, text: str) -> None:
        self._replace(index, type_name=text)
        self._on_edited()

    def _set_required(self, index: int, checked: bool) -> None:
        self._replace(index, required=checked)
        self._on_edited()

    def _install_completers(self) -> None:
        for row in range(self._table.rowCount()):
            editor = self._table.cellWidget(row, _COLUMN_PATH)
            if isinstance(editor, QLineEdit) and self._path_choices:
                completer = QCompleter(list(self._path_choices), editor)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                editor.setCompleter(completer)

    def completer_count(self, row: int) -> int:
        """How many suggestions the path box in ``row`` has. For tests and for callers."""
        editor = self._table.cellWidget(row, _COLUMN_PATH)
        if isinstance(editor, QLineEdit) and editor.completer() is not None:
            return editor.completer().model().rowCount()
        return 0

    # -- saving ------------------------------------------------------------

    def as_config_dict(self) -> dict[str, object]:
        """The table as the mapping the CLI would read. For saving and for tests."""
        return build_config_dict(
            self._rows,
            record_path=self.record_path(),
            namespaces=self._namespaces or None,
            on_error=self._on_error,
        )

    def valid_config_dict(self) -> dict[str, object] | None:
        """The mapping, but only when the project has accepted it.

        The preview samples with this. Handing over a mapping the CLI would reject would
        make the preview fail with a message the user has already been shown, which reads
        as the preview being broken rather than the config being wrong.
        """
        return self.as_config_dict() if self._result is not None and self._result.ok else None

    # -- ⑧ saved configurations, the two halves the panel was missing ----------

    @property
    def library(self) -> ConfigLibrary | None:
        """Where saved configurations are remembered, or ``None`` when given none."""
        return self._library

    def load_config(self, name: str) -> SavedConfig:
        """Fetch the configuration saved under ``name``.

        Raises:
            RuntimeError: if this panel was given no library to look in.
        """
        if self._library is None:
            raise RuntimeError("this panel has no configuration library")
        return self._library.load(name)

    def recent_configs(self) -> tuple[pathlib.Path, ...]:
        """The recently saved configuration paths, most recent first."""
        if self._library is None:
            return ()
        return self._library.recent()

    def choose_save_path(self) -> None:
        chosen, _ = QFileDialog.getSaveFileName(
            self, "Save config", "", "YAML (*.yaml);;JSON (*.json);;All files (*)"
        )
        if chosen:
            self.save_config(Path(chosen))

    def save_config(self, path: Path | str) -> None:
        """Write the validated config. Only ever called when validation passed."""
        config = self.config()
        if config is None:
            return
        payload = self.as_config_dict()
        target = Path(path)
        if target.suffix.lower() == ".json":
            target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        else:
            target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
        # ⑧'s recent list: remember where this was saved. The library keeps the list.
        if self._library is not None:
            self._library.note_opened(target)

    # -- dragging rows -----------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt naming)
        """Only our own rows are accepted; a file dropped here is not a field."""
        if event.source() is self._table:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt naming)
        """Reorder on the data, then rebuild. Qt's own move would leave the widgets."""
        source = self._table.currentRow()
        target = self._table.indexAt(event.position().toPoint()).row()
        if target < 0:
            target = self._table.rowCount() - 1
        self.move_row(source, target)
        event.acceptProposedAction()
