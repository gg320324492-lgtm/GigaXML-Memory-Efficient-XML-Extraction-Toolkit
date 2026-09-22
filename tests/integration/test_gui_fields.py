"""The field configuration panel, driven for real.

**The test the brief is really about** is
``test_an_invalid_path_is_rejected_with_the_librarys_own_sentence``: it asserts the panel's
message equals what ``gigaxml.config.parse_config`` raises for the same table. A
hand-written validator that happened to agree today would pass every other test in this
file and fail that one.

The panel is driven through its widgets where the widget is the thing under test -- typing
into a path box, choosing from the type dropdown, dragging a row -- because those are the
paths a user takes and the ones where a captured-at-the-wrong-time index hides.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Callable

import pytest

pytest.importorskip("PySide6", reason="the desktop application is an optional extra")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.config import load_config, parse_config
from gigaxml.errors import GigaXMLError
from gigaxml.gui.field_rows import FIELD_TYPE_NAMES, FieldRow
from gigaxml.gui.inspect_report import Candidate
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.fields import HEADERS, FieldConfigPanel

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "tests" / "fixtures"

RECORD = "/catalog/products/product"

CATALOGUE = (
    "<catalog><products>"
    + "".join(
        f'<product id="{n}" active="yes"><name>N{n}</name><price>{n}.50</price></product>'
        for n in range(1, 6)
    )
    + "</products></catalog>"
)


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture
def window(app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path) -> MainWindow:
    assert QApplication.instance() is app
    main = MainWindow(state_dir=tmp_path / "state")
    qtbot.addWidget(main)
    return main


def catalogue(tmp_path: pathlib.Path, text: str = CATALOGUE) -> pathlib.Path:
    path = tmp_path / "catalogue.xml"
    path.write_text(text, encoding="utf-8")
    return path


def a_candidate() -> Candidate:
    return Candidate(
        path=RECORD,
        score=1.0,
        count=5,
        shape_consistency=1.0,
        repeat_score=1.0,
        nested_inside=None,
        depth=3,
        child_tags=("name", "price"),
        attribute_names=("@id", "@active"),
        namespaces={"": "urn:example:shop"},
    )


def library_message(data: dict[str, object]) -> str:
    """What the project says about a mapping, with the label the panel uses."""
    with pytest.raises(GigaXMLError) as raised:
        parse_config(data, source="<the field table>")
    return str(raised.value)


# --- the tab and the accessor -------------------------------------------------


def test_the_window_exposes_the_field_panel(window: MainWindow) -> None:
    assert isinstance(window.field_panel(), FieldConfigPanel)
    labels = [window.tabs().tabText(index) for index in range(window.tabs().count())]
    assert "Fields" in labels


def test_the_table_has_the_four_columns_the_brief_asks_for(window: MainWindow) -> None:
    panel = window.field_panel()

    assert [
        panel._table.horizontalHeaderItem(column).text()
        for column in range(panel._table.columnCount())
    ] == HEADERS


def test_the_type_column_is_a_dropdown_offering_the_librarys_types(
    window: MainWindow,
) -> None:
    panel = window.field_panel()
    panel.set_rows([FieldRow("id", "@id")])

    dropdown = panel._table.cellWidget(0, 2)

    assert [dropdown.itemText(index) for index in range(dropdown.count())] == list(FIELD_TYPE_NAMES)


# --- editing ------------------------------------------------------------------


def test_typing_a_path_updates_the_row(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])

    panel._table.cellWidget(0, 1).setText("manufacturer/name")

    assert panel.rows()[0].path == "manufacturer/name"


def test_typing_a_name_updates_the_row(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])

    panel._table.cellWidget(0, 0).setText("identifier")

    assert panel.rows()[0].name == "identifier"


def test_choosing_a_type_updates_the_row(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("qty", "qty")])

    panel._table.cellWidget(0, 2).setCurrentText("decimal")

    assert panel.rows()[0].type_name == "decimal"


def test_ticking_required_updates_the_row(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("qty", "qty")])

    panel._table.cellWidget(0, 3).setChecked(True)

    assert panel.rows()[0].required is True


def test_a_row_can_be_added_and_deleted(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a")])

    panel.add_row()
    assert len(panel.rows()) == 2

    panel.delete_row(0)
    assert [row.name for row in panel.rows()] == [""]


def test_deleting_with_nothing_selected_does_nothing(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_rows([FieldRow("a", "@a")])
    panel._table.setCurrentCell(-1, -1)

    panel.delete_selected()

    assert len(panel.rows()) == 1


def test_rows_can_be_moved(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b"), FieldRow("c", "@c")])

    panel.move_row(0, 2)

    assert [row.name for row in panel.rows()] == ["b", "c", "a"]


def test_moving_clamps_to_the_ends(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b")])

    panel.move_row(1, 99)

    assert [row.name for row in panel.rows()] == ["a", "b"]


def test_a_row_can_be_reordered_by_dropping_it(window: MainWindow) -> None:
    """The drag path, with a real drop event aimed at the third row's position."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b"), FieldRow("c", "@c")])
    panel._table.setCurrentCell(0, 0)
    panel.resize(800, 600)
    panel.show()

    x = panel._table.columnViewportPosition(0) + 4
    y = panel._table.rowViewportPosition(2) + 4
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("ignored")])
    event = QDropEvent(
        QPointF(x, y),
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    panel.dropEvent(event)

    assert event.isAccepted()
    assert [row.name for row in panel.rows()] == ["b", "c", "a"]


def test_the_order_is_the_order_the_config_gets(window: MainWindow) -> None:
    """Reordering is not cosmetic: it is the order the fields come out in."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b")])

    panel.move_row(1, 0)

    assert list(panel.as_config_dict()["fields"]) == ["b", "a"]
    assert panel.config() is not None
    assert panel.config().field_names == ("b", "a")


# --- autocomplete -------------------------------------------------------------


def test_the_path_boxes_complete_against_the_path_table(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_rows([FieldRow("id", "@id")])

    panel.set_path_choices(("/catalog", "/catalog/products", "/catalog/products/product"))

    assert panel.path_choices() == (
        "/catalog",
        "/catalog/products",
        "/catalog/products/product",
    )
    assert panel.completer_count(0) == 3


def test_a_path_box_with_no_choices_has_no_completer(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_rows([FieldRow("id", "@id")])

    panel.set_path_choices(())

    assert panel.completer_count(0) == 0


def test_the_window_feeds_the_path_boxes_from_the_analysis(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The completion source is the path table, reached through the window."""
    from gigaxml.gui.panels.structure import StructurePanel

    source = catalogue(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert _wait(qtbot, lambda: structure.report() is not None)
    assert isinstance(structure, StructurePanel)

    panel = window.field_panel()

    assert panel.path_choices(), "the analysis produced no paths to complete against"
    assert any("/catalog/products/product" in choice for choice in panel.path_choices())
    assert panel.completer_count(0) == len(panel.path_choices())


# --- ★ live validation --------------------------------------------------------


def test_an_invalid_path_is_rejected_with_the_librarys_own_sentence(
    window: MainWindow,
) -> None:
    """**The brief's Gate 2.** The panel's message must be ``parse_config``'s message.

    A second implementation of the path rules -- however careful -- would produce its own
    wording here, and this assertion is what would catch it.
    """
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id"), FieldRow("oops", "a//b")])

    panel._table.cellWidget(1, 1).setText("a//b")

    result = panel.result()
    assert result is not None and result.ok is False
    assert panel.message_text() == library_message(
        {"record": RECORD, "fields": {"id": {"path": "@id"}, "oops": {"path": "a//b"}}}
    )
    assert panel.invalid_rows() == (1,)


def test_an_unknown_type_is_rejected_with_the_librarys_own_sentence(
    window: MainWindow,
) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("qty", "qty", "intt")])

    panel.validate_now()

    assert panel.message_text() == library_message(
        {"record": RECORD, "fields": {"qty": {"path": "qty", "type": "intt"}}}
    )


def test_validation_happens_without_pressing_anything(window: MainWindow) -> None:
    """A panel that only complains when asked teaches the user not to ask."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])
    assert panel.result() is not None and panel.result().ok

    panel._table.cellWidget(0, 1).setText("a//b")

    assert panel.result() is not None and panel.result().ok is False


def test_a_repaired_row_goes_back_to_green(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id"), FieldRow("name", "name")])

    panel._table.cellWidget(1, 1).setText("a//b")
    assert panel.invalid_rows() == (1,)

    panel._table.cellWidget(1, 1).setText("name")

    assert panel.invalid_rows() == ()
    assert panel.result() is not None and panel.result().ok


def test_an_empty_field_name_is_rejected(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)

    panel.set_rows([FieldRow("", "@id")])

    assert panel.result() is not None and panel.result().ok is False
    assert panel.invalid_rows() == (0,)


def test_two_rows_with_the_same_name_are_rejected(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)

    panel.set_rows([FieldRow("id", "@id"), FieldRow("id", "name")])

    assert panel.result() is not None and panel.result().ok is False
    assert panel.invalid_rows() == (1,)


def test_the_type_dropdown_cannot_offer_something_the_library_rejects(
    window: MainWindow,
) -> None:
    """Every type in the dropdown is one ``parse_config`` accepts, so choosing from it can
    never be the reason a table is refused."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a")])
    dropdown = panel._table.cellWidget(0, 2)

    for index in range(dropdown.count()):
        dropdown.setCurrentIndex(index)
        assert panel.result() is not None and panel.result().ok, panel.message_text()


# --- from a candidate ---------------------------------------------------------


def test_regenerating_from_a_candidate_fills_the_table(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_candidate(a_candidate())

    panel.regenerate_from_candidate()

    assert panel.record_path() == RECORD
    assert [row.path for row in panel.rows()] == ["name", "price", "@id", "@active"]
    assert [row.name for row in panel.rows()] == ["name", "price", "id", "active"]


def test_regenerating_without_a_candidate_says_so(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_candidate(None)

    panel.regenerate_from_candidate()

    assert "candidate" in panel.message_text()


def test_the_window_hands_the_candidate_over(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The selection in the structure panel is what the button regenerates from."""
    source = catalogue(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert _wait(qtbot, lambda: structure.report() is not None)

    structure.select_candidate(0)

    panel = window.field_panel()
    assert panel.candidate() is not None
    panel.regenerate_from_candidate()
    assert panel.result() is not None and panel.result().ok, panel.message_text()


# --- the config the panel produces --------------------------------------------


def test_the_saved_config_is_one_the_cli_loads(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """Written by the panel, read back by the project's own loader."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id"), FieldRow("qty", "qty", "int", True)])
    target = tmp_path / "config.yaml"

    panel.save_config(target)

    loaded = load_config(target)
    assert loaded.record_path == RECORD
    assert loaded.field_names == ("id", "qty")
    assert loaded.fields[1].type.value == "int"
    assert loaded.fields[1].required is True


def test_the_saved_json_config_is_one_the_cli_loads(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])
    target = tmp_path / "config.json"

    panel.save_config(target)

    assert json.loads(target.read_text(encoding="utf-8"))["record"] == RECORD


def test_saving_is_off_while_the_table_is_not_acceptable(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("oops", "a//b")])

    assert panel._export.isEnabled() is False


def test_saving_an_unacceptable_table_writes_nothing(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("oops", "a//b")])
    target = tmp_path / "config.yaml"

    panel.save_config(target)

    assert not target.exists()


# --- the window's wiring ------------------------------------------------------


def test_a_valid_table_reaches_the_preview(window: MainWindow) -> None:
    """The preview is handed a config only when the project has accepted it."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])

    assert window.preview_panel().config() is not None


def test_an_unacceptable_table_does_not_reach_the_preview(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("oops", "a//b")])

    assert window.preview_panel().config() is None


# --- paths the widgets reach but a typed value does not -----------------------


def test_the_save_dialog_writes_where_it_says(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from PySide6.QtWidgets import QFileDialog

    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])
    target = tmp_path / "saved.yaml"
    monkeypatch.setattr(
        QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *_a, **_k: (str(target), "")),
    )

    panel.choose_save_path()

    assert target.is_file()
    assert load_config(target).field_names == ("id",)


def test_cancelling_the_save_dialog_writes_nothing(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from PySide6.QtWidgets import QFileDialog

    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *_a, **_k: ("", "")))

    panel.choose_save_path()

    assert list(tmp_path.iterdir()) == []


def test_deleting_the_selected_row_deletes_it(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b")])
    panel._table.setCurrentCell(1, 0)

    panel.delete_selected()

    assert [row.name for row in panel.rows()] == ["a"]


def test_moving_the_selected_row_up_and_down(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b"), FieldRow("c", "@c")])
    panel._table.setCurrentCell(1, 0)

    panel.move_selected(-1)
    assert [row.name for row in panel.rows()] == ["b", "a", "c"]

    panel._table.setCurrentCell(0, 0)
    panel.move_selected(1)
    assert [row.name for row in panel.rows()] == ["a", "b", "c"]


def test_moving_with_nothing_selected_does_nothing(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a")])
    panel._table.setCurrentCell(-1, -1)

    panel.move_selected(1)

    assert [row.name for row in panel.rows()] == ["a"]


def test_moving_a_row_that_is_not_there_does_nothing(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a")])

    panel.move_row(9, 0)

    assert [row.name for row in panel.rows()] == ["a"]


def test_the_on_error_policy_reaches_the_config(window: MainWindow) -> None:
    """Set by the window from the execution panel's own choice, so the two agree."""
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("id", "@id")])

    panel.set_on_error("quarantine")

    assert panel.as_config_dict()["on_error"] == "quarantine"
    assert panel.config() is not None and panel.config().on_error.value == "quarantine"


def test_a_drag_from_somewhere_else_is_refused(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """A file dropped on the table is not a field."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QDragEnterEvent

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(tmp_path / "catalogue.xml"))])
    event = QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    window.field_panel().dragEnterEvent(event)

    assert event.isAccepted() is False


def test_a_drop_below_the_last_row_goes_to_the_end(window: MainWindow) -> None:
    panel = window.field_panel()
    panel.set_record_path(RECORD)
    panel.set_rows([FieldRow("a", "@a"), FieldRow("b", "@b")])
    panel._table.setCurrentCell(0, 0)
    panel.resize(800, 600)
    panel.show()
    # Far below the rows, where indexAt finds nothing.
    far = QPointF(4, panel._table.height() + 200)
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile("ignored")])
    event = QDropEvent(
        far,
        Qt.DropAction.MoveAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    panel.dropEvent(event)

    assert [row.name for row in panel.rows()] == ["b", "a"]


def _wait(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    from PySide6.QtCore import QElapsedTimer

    elapsed = QElapsedTimer()
    elapsed.start()
    while elapsed.elapsed() < timeout_ms:
        qtbot.wait(25)
        if predicate():
            return True
    return predicate()
