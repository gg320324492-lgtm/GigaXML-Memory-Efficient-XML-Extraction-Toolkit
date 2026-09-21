"""The document panel, driven for real.

Two things here are worth more than the rest.

**The drop is a real drop.** ``setAcceptDrops(True)`` is a declaration; it opens nothing.
So a real ``QMimeData`` is built, a real ``QDropEvent`` is constructed around it, and the
window's handler is called directly -- ``QApplication.sendEvent`` does not deliver a drop
to a top-level window, which was measured before this file was written. The assertion is
that the panel is now showing *the document that was dropped*.

**The recent list is read by a second process.** A list written and read inside one
interpreter proves that a list exists, not that it was saved.

The state directory is always passed explicitly. The product default is the user's own
configuration directory, which is right for a product and wrong for a test: a test that
writes there is modifying the machine rather than testing anything.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6", reason="the desktop application is an optional extra")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QFileDialog
from pytestqt.qtbot import QtBot

from gigaxml.gui.main_window import MainWindow, placeholder
from gigaxml.gui.panels.document import DocumentPanel, first_local_file
from gigaxml.gui.panels.execution import ExecutionPanel
from gigaxml.gui.panels.structure import StructurePanel

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

CATALOG = "<catalog><products><product id='{n}'><name>N{n}</name></product></products></catalog>"


@pytest.fixture(scope="module")
def app() -> QApplication:
    """One QApplication for the module. Qt refuses to create a second one."""
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


def write_document(path: pathlib.Path, records: int = 1) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(CATALOG.format(n=n) for n in range(1, records + 1))
    path.write_text(body, encoding="utf-8")
    return path


#: Mime objects kept alive for the duration of the process.
#:
#: **``QDropEvent`` does not take ownership of the ``QMimeData`` it is given.** Building
#: the mime inside the helper below and returning only the event leaves the event holding
#: a pointer to freed memory, and the failure is an access violation rather than an
#: exception -- it takes the whole interpreter with it, so the test that crashed does not
#: even get to report. Holding a reference here is the fix.
_ALIVE: list[QMimeData] = []


def drop(target: pathlib.Path) -> QDropEvent:
    """A real drop event carrying one real local file."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])
    _ALIVE.append(mime)
    return QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def drag_enter(target: pathlib.Path) -> QDragEnterEvent:
    """The event Qt sends when a drag first comes over the widget."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])
    _ALIVE.append(mime)
    return QDragEnterEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


def drag_move(target: pathlib.Path) -> QDragMoveEvent:
    """The event Qt repeats while a drag stays over the widget."""
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(target))])
    _ALIVE.append(mime)
    return QDragMoveEvent(
        QPoint(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )


# --- the panel exists and is reachable ----------------------------------------


def test_the_window_exposes_the_document_panel(window: MainWindow) -> None:
    """Gate 10: one panel having an accessor and the other not makes the second look
    private, which is a claim about the design that is not true."""
    assert isinstance(window.document_panel(), DocumentPanel)
    assert isinstance(window.execution_panel(), ExecutionPanel)
    assert isinstance(window.structure_panel(), StructurePanel)


def test_the_window_offers_the_document_panel_as_a_tab(window: MainWindow) -> None:
    labels = [window.tabs().tabText(i) for i in range(window.tabs().count())]

    assert labels[0] == "Document"


# --- dropping -----------------------------------------------------------------


def test_dropping_a_document_opens_that_document(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The assertion that makes an empty implementation fail.

    A handler that accepted the drop and opened nothing would leave ``current_document``
    as ``None``, and a handler that read the mime and refused would leave
    ``isAccepted()`` false. Both halves are checked.
    """
    dropped = write_document(tmp_path / "dropped.xml", records=2)

    event = drop(dropped)
    window.dropEvent(event)

    assert event.isAccepted(), "the window refused a drop it should have taken"
    assert window.document_panel().current_document() == dropped


def test_a_dropped_document_wins_over_one_with_the_same_name(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The reverse control for "did it really read the mime".

    Two files with the same name in different directories. A panel that looked the name up
    in its recent list, or in a cache keyed by name, would open the first one. Only reading
    ``mimeData().urls()`` produces the second.
    """
    first = write_document(tmp_path / "one" / "same.xml", records=1)
    second = write_document(tmp_path / "two" / "same.xml", records=3)
    window.document_panel().open_document(first)

    event = drop(second)
    window.dropEvent(event)

    assert event.isAccepted()
    assert window.document_panel().current_document() == second
    assert window.document_panel().current_document() != first


def test_dropping_something_that_is_not_a_document_is_refused_out_loud(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Refusing is allowed. Refusing silently is not -- that reads as a hang."""
    opened = write_document(tmp_path / "opened.xml")
    window.document_panel().open_document(opened)
    notes = write_document(tmp_path / "notes.txt")

    event = drop(notes)
    window.dropEvent(event)

    assert event.isAccepted() is False, "a .txt is not a document and must not be taken"
    assert window.document_panel().current_document() == opened, "the open document stays"
    assert "not a document" in window.document_panel().information_text()


def test_dropping_a_path_that_is_not_there_is_refused(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    event = drop(tmp_path / "gone.xml")

    window.dropEvent(event)

    assert event.isAccepted() is False
    assert window.document_panel().current_document() is None


def test_dropping_a_directory_is_refused(window: MainWindow, tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "folder.xml"
    folder.mkdir()

    event = drop(folder)

    window.dropEvent(event)

    assert event.isAccepted() is False


def test_a_drop_with_no_file_in_it_is_refused(window: MainWindow) -> None:
    """Text dragged from an editor is not a document."""
    mime = QMimeData()
    mime.setText("just some words")
    event = QDropEvent(
        QPointF(5, 5),
        Qt.DropAction.CopyAction,
        mime,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )

    window.dropEvent(event)

    assert event.isAccepted() is False
    assert window.document_panel().current_document() is None


def test_the_panel_takes_a_drop_directly_too(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """A user aiming at the panel rather than the window edge gets the same behaviour."""
    dropped = write_document(tmp_path / "dropped.xml")
    panel = window.document_panel()

    event = drop(dropped)
    panel.dropEvent(event)

    assert event.isAccepted()
    assert panel.current_document() == dropped


def test_the_mime_helper_ignores_urls_that_are_not_local_files() -> None:
    """The rule, without an event: only a local file is a document."""
    mime = QMimeData()
    mime.setUrls([QUrl("https://example.invalid/catalog.xml")])

    assert first_local_file(mime) is None


# --- the recent list ----------------------------------------------------------


def test_an_opened_document_appears_in_the_recent_list(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    document = write_document(tmp_path / "doc.xml")

    window.document_panel().open_document(document)

    assert window.document_panel().recent_paths() == (document,)


def test_a_document_that_vanished_is_still_listed_and_marked(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    document = write_document(tmp_path / "doc.xml")
    window.document_panel().open_document(document)
    document.unlink()

    window.document_panel().refresh()

    assert window.document_panel().recent_paths() == (document,), "kept, not deleted"
    assert "missing" in window.document_panel().recent_list_text()
    assert "not there" in window.document_panel().recent_note_text()


def test_the_list_survives_closing_and_reopening_the_window(
    app: QApplication, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Same process, second window: the store is re-read, not remembered."""
    assert QApplication.instance() is app
    state = tmp_path / "state"
    document = write_document(tmp_path / "doc.xml")

    first = MainWindow(state_dir=state)
    qtbot.addWidget(first)
    first.document_panel().open_document(document)
    first.close()

    second = MainWindow(state_dir=state)
    qtbot.addWidget(second)

    assert second.document_panel().recent_paths() == (document,)


def test_the_list_is_read_by_a_second_process(tmp_path: pathlib.Path) -> None:
    """**The one the auditor will run too.** A list only in memory dies with the process.

    The writer is a whole separate interpreter that constructs the window, opens a
    document and exits. The reader is another one that only asks what the list holds.
    """
    state = tmp_path / "state"
    document = write_document(tmp_path / "kept.xml")

    program = textwrap.dedent(
        """
        import pathlib, sys
        from PySide6.QtWidgets import QApplication
        from gigaxml.gui.main_window import MainWindow

        action, state, document = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
        app = QApplication.instance() or QApplication(sys.argv[:1])
        window = MainWindow(state_dir=state)
        if action == "write":
            window.document_panel().open_document(pathlib.Path(document))
        for path in window.document_panel().recent_paths():
            print(path)
        """
    )
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

    def run(action: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", program, action, str(state), str(document)],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO,
            env=environment,
        )

    written = run("write")
    assert written.returncode == 0, written.stderr
    assert str(document) in written.stdout

    read = run("read")
    assert read.returncode == 0, read.stderr

    assert read.stdout.strip() == str(document), "the second process saw nothing"


def test_the_store_is_a_file_the_user_could_find(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    document = write_document(tmp_path / "doc.xml")

    window.document_panel().open_document(document)

    store = window.document_panel().store_path()
    assert store.is_file()
    assert json.loads(store.read_text(encoding="utf-8"))["recent"] == [str(document)]


# --- the information bar ------------------------------------------------------


def test_the_information_bar_names_the_path_size_and_time(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    document = write_document(tmp_path / "doc.xml", records=50)

    window.document_panel().open_document(document)

    text = window.document_panel().information_text()
    assert str(document) in text
    assert "bytes" in text
    assert "modified" in text


def test_a_large_document_is_announced_before_it_is_read(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """400 MiB at the measured rate is about half a minute of nothing happening."""
    big = tmp_path / "big.xml"
    with big.open("wb") as handle:
        handle.truncate(400 * 1024 * 1024)

    window.document_panel().open_document(big)

    text = window.document_panel().information_text()
    assert "expect roughly" in text
    assert "reads the whole document" in text


def test_a_small_document_gets_no_time_warning(window: MainWindow, tmp_path: pathlib.Path) -> None:
    document = write_document(tmp_path / "doc.xml")

    window.document_panel().open_document(document)

    assert "expect roughly" not in window.document_panel().information_text()


# --- opening from the panel's own controls ------------------------------------


def test_the_open_method_refuses_what_the_drop_refuses(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """One rule, three routes. A dialog that could open a `.txt` would make the drop's
    refusal look arbitrary."""
    panel = window.document_panel()

    assert panel.open_document(tmp_path / "notes.txt") is False
    assert panel.current_document() is None


def test_forgetting_an_entry_leaves_the_others(window: MainWindow, tmp_path: pathlib.Path) -> None:
    first = write_document(tmp_path / "first.xml")
    second = write_document(tmp_path / "second.xml")
    panel = window.document_panel()
    panel.open_document(first)
    panel.open_document(second)

    panel.forget(second)

    assert panel.recent_paths() == (first,)


def test_clearing_the_list_empties_it(window: MainWindow, tmp_path: pathlib.Path) -> None:
    panel = window.document_panel()
    panel.open_document(write_document(tmp_path / "doc.xml"))

    panel.clear_recent()

    assert panel.recent_paths() == ()


def test_opening_a_document_tells_the_structure_panel(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Opening from anywhere has to update everything that shows the document."""
    document = write_document(tmp_path / "doc.xml")

    window.document_panel().open_document(document)

    assert window.structure_panel().document() == document


# --- the drag entering and moving over the widget -----------------------------


def test_a_drag_carrying_a_document_is_accepted(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """Saying yes here and no on the drop would make the cursor promise something the
    panel does not intend to do."""
    event = drag_enter(write_document(tmp_path / "doc.xml"))

    window.document_panel().dragEnterEvent(event)

    assert event.isAccepted()


def test_a_drag_carrying_something_else_is_refused(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    event = drag_enter(write_document(tmp_path / "notes.txt"))

    window.document_panel().dragEnterEvent(event)

    assert event.isAccepted() is False


def test_a_drag_that_stays_over_the_panel_keeps_being_accepted(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    event = drag_move(write_document(tmp_path / "doc.xml"))

    window.document_panel().dragMoveEvent(event)

    assert event.isAccepted()


def test_a_drag_that_stays_over_the_panel_is_refused_when_it_should_be(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    event = drag_move(tmp_path / "gone.xml")

    window.document_panel().dragMoveEvent(event)

    assert event.isAccepted() is False


def test_the_window_forwards_the_entering_drag(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """A user aiming at the window rather than the panel gets the same answer."""
    event = drag_enter(write_document(tmp_path / "doc.xml"))

    window.dragEnterEvent(event)

    assert event.isAccepted()


# --- the file dialog route ----------------------------------------------------


def test_the_dialog_opens_whatever_it_returns(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The dialog itself cannot be driven, but everything after it can."""
    document = write_document(tmp_path / "chosen.xml")
    monkeypatch.setattr(
        QFileDialog, "getOpenFileName", staticmethod(lambda *_a, **_k: (str(document), ""))
    )

    window.document_panel().choose_document()

    assert window.document_panel().current_document() == document


def test_the_dialog_offers_the_suffixes_a_drop_would_accept(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rule for both routes. A filter offering `.txt` would make the drop look
    arbitrary; a filter omitting `.xml.gz` would hide a format the CLI accepts."""
    seen: dict[str, str] = {}

    def fake_dialog(*args: object, **_kwargs: object) -> tuple[str, str]:
        seen["filter"] = str(args[3]) if len(args) > 3 else ""
        return ("", "")

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(fake_dialog))

    window.document_panel().choose_document()

    assert "*.xml" in seen["filter"]
    assert "*.xml.gz" in seen["filter"]
    assert "*.txt" not in seen["filter"]


def test_cancelling_the_dialog_changes_nothing(
    window: MainWindow, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    opened = write_document(tmp_path / "opened.xml")
    window.document_panel().open_document(opened)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *_a, **_k: ("", "")))

    window.document_panel().choose_document()

    assert window.document_panel().current_document() == opened


# --- the recent list's own controls ------------------------------------------


def test_activating_a_recent_entry_opens_it(window: MainWindow, tmp_path: pathlib.Path) -> None:
    first = write_document(tmp_path / "first.xml")
    second = write_document(tmp_path / "second.xml")
    panel = window.document_panel()
    panel.open_document(first)
    panel.open_document(second)

    older = panel._list.item(1)
    panel._open_recent_item(older)

    assert panel.current_document() == first


def test_removing_the_selected_entry_removes_it(window: MainWindow, tmp_path: pathlib.Path) -> None:
    first = write_document(tmp_path / "first.xml")
    second = write_document(tmp_path / "second.xml")
    panel = window.document_panel()
    panel.open_document(first)
    panel.open_document(second)
    panel._list.setCurrentRow(0)

    panel._remove_selected()

    assert panel.recent_paths() == (first,)


def test_removing_with_nothing_selected_does_nothing(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """A button that is pressed with no selection must not raise or clear the list."""
    panel = window.document_panel()
    panel.open_document(write_document(tmp_path / "doc.xml"))
    panel._list.setCurrentRow(-1)

    panel._remove_selected()

    assert len(panel.recent_paths()) == 1


def test_the_clear_button_empties_the_list(window: MainWindow, tmp_path: pathlib.Path) -> None:
    panel = window.document_panel()
    panel.open_document(write_document(tmp_path / "doc.xml"))

    panel._clear_recent()

    assert panel.recent_paths() == ()


# --- the window's own bits ----------------------------------------------------


def test_a_new_window_says_no_document_is_open(window: MainWindow) -> None:
    assert "no document open" in window.document_panel().information_text()


def test_the_placeholder_says_a_tab_is_not_built_yet() -> None:
    """Still used: the field, preview and execution areas arrive in later substeps, and a
    window that showed an empty tab would be claiming they are done."""
    label = placeholder("Fields")

    assert "not built yet" in label.text()
    assert "Fields" in label.text()


def test_about_names_the_version(window: MainWindow) -> None:
    from gigaxml import __version__

    window._show_about()

    assert __version__ in window.statusBar().currentMessage()


def test_a_mime_object_without_urls_is_not_a_file() -> None:
    """A drop from something that is not a desktop file manager."""

    class Bare:
        pass

    assert first_local_file(Bare()) is None
