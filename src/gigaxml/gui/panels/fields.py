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

from pathlib import Path

from PySide6.QtCore import Qt, Signal
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

from gigaxml.gui.field_rows import (
    FIELD_TYPE_NAMES,
    FieldRow,
    ValidationResult,
    build_config_dict,
    validate,
)
from gigaxml.gui.inspect_report import Candidate

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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[FieldRow] = []
        self._namespaces: dict[str, str] = {}
        self._on_error: str | None = None
        self._result: ValidationResult | None = None
        self._path_choices: tuple[str, ...] = ()
        self._candidate: Candidate | None = None
        self._suspend = False

        self._build()
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
        """Fill the table from the candidate selected in the structure panel.

        The record path becomes the candidate's path, and one row is made for each of its
        direct children and each of its attributes -- which is what ``inspect
        --generate-config`` writes, built from the report already in hand rather than by
        starting another process.
        """
        candidate = self._candidate
        if candidate is None:
            self._message.setText("select a candidate in the structure panel first")
            return
        self._record.setText(candidate.path)
        rows = [FieldRow(name=tag, path=tag) for tag in candidate.child_tags]
        rows += [FieldRow(name=name.lstrip("@"), path=name) for name in candidate.attribute_names]
        self.set_rows(rows)

    def set_candidate(self, candidate: Candidate | None) -> None:
        """The candidate the "From candidate" button regenerates from.

        Set by the window when the structure panel's selection changes, so the button is
        only live when there is something to build from.
        """
        self._candidate = candidate
        self._regenerate.setEnabled(candidate is not None)

    def candidate(self) -> Candidate | None:
        return self._candidate

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
            import json

            target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            return
        import yaml

        target.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

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
