"""⑩ error presentation, through the window.

The three failures the spec names are each produced for real here -- a record whose text
will not convert, a target file held open by another program, and a config that will not
load -- because the point of the phase is that the panel recognises what actually happens,
not what a stub says happens.
"""

from __future__ import annotations

import ctypes
import pathlib
import sys
from collections.abc import Callable
from ctypes import wintypes

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_FREE_TARGET,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
    advice_for,
)
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels.errors import ErrorPanel
from gigaxml.gui.run_report import RunFailure, failure_from_stderr

DOCUMENT = """<catalog><products>
<product id="1"><name>Alpha Lamp</name></product>
</products></catalog>
"""

TYPE_ERROR_CONFIG = """record: /catalog/products/product
fields:
  name:
    path: name
    type: int
"""

GOOD_CONFIG = """record: /catalog/products/product
fields:
  name:
    path: name
"""

BROKEN_CONFIG = """record: /catalog/products/product
fields:
  name: {path: name
"""

NAMESPACED_DOCUMENT = (
    '<catalog xmlns:sh="urn:example:shop"><sh:products>'
    '<sh:product id="1"><sh:name>Alpha</sh:name></sh:product>'
    "</sh:products></catalog>\n"
)

#: A field path naming a prefix the map does not declare. The loader raises this one **in
#: the GUI's own process**, before any child is started, so it is the case with no report.
MISSING_PREFIX_CONFIG = """record: /catalog/products/product
namespaces:
  sh: urn:example:shop
fields:
  name:
    path: "zz:missing"
"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture
def window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    del app
    main = MainWindow(state_dir=tmp_path / "state")
    # **Close it on the way out.** Without this the window outlives its test:
    # closeEvent never fires, the panels' shutdown() never runs, and their pumps
    # stay in Qt's timer list -- which is what fires into a dead object during the
    # next test's waitUntil. See the core dump in 8A-32.
    yield main
    main.close()


def wait_until(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    qtbot.waitUntil(predicate, timeout=timeout_ms)
    return predicate()


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


def run(window: MainWindow, qtbot: QtBot, config: pathlib.Path, output: pathlib.Path) -> None:
    """Start a run and wait for the window to have reacted to it."""
    panel = window.execution_panel()
    panel._source.setText(str(config.parent / "doc.xml"))
    panel._config.setText(str(config))
    panel._output.setText(str(output))
    panel.start()
    wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=5_000)
    # One more turn for the pump to drain the result and the window to read the report.
    qtbot.wait(200)


@pytest.fixture
def document(tmp_path: pathlib.Path) -> pathlib.Path:
    return write(tmp_path / "doc.xml", DOCUMENT)


# --- the three failures -------------------------------------------------------


def test_a_field_type_failure_suggests_quarantine(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The record is bad, the run is not -- so the advice is the setting that says so."""
    del document
    run(window, qtbot, write(tmp_path / "typeerr.yaml", TYPE_ERROR_CONFIG), tmp_path / "out.csv")

    panel = window.error_panel()
    failure = panel.failure()
    assert failure is not None
    assert failure.error_type == "FieldTypeError", "the CLI's own classification"
    assert failure.had_report is True
    assert panel.advice() is not None
    assert panel.advice().kind == KIND_QUARANTINE
    assert "quarantine" in panel.action_text().lower()


def test_the_quarantine_advice_actually_sets_it(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A button that only re-words the message is decoration. This one arrives somewhere."""
    del document
    run(window, qtbot, write(tmp_path / "typeerr.yaml", TYPE_ERROR_CONFIG), tmp_path / "out.csv")
    execution = window.execution_panel()
    assert execution._on_error.currentText() == "abort"

    window.error_panel().action_requested.emit(KIND_QUARANTINE)

    assert execution._on_error.currentText() == "quarantine"


def test_a_locked_target_suggests_freeing_it(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """A writer failure: the records were fine, the finished file could not be put in place."""
    del document
    target = write(tmp_path / "locked.csv", "")
    holder = _hold_exclusively(target)
    try:
        run(window, qtbot, write(tmp_path / "good.yaml", GOOD_CONFIG), target)
    finally:
        holder.close()

    panel = window.error_panel()
    failure = panel.failure()
    assert failure is not None
    assert failure.error_type == "WriterError", "the CLI's own classification"
    assert panel.advice() is not None
    assert panel.advice().kind == KIND_FREE_TARGET
    assert failure.partial_path is not None, "the complete output is in the .tmp file"


def test_freeing_the_target_copies_where_the_output_is(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """The advice is "close the program holding it" -- so the button has to say which file."""
    del document
    target = write(tmp_path / "locked.csv", "")
    holder = _hold_exclusively(target)
    try:
        run(window, qtbot, write(tmp_path / "good.yaml", GOOD_CONFIG), target)
    finally:
        holder.close()
    QApplication.clipboard().setText("")

    window.error_panel().action_requested.emit(KIND_FREE_TARGET)

    assert str(target) in QApplication.clipboard().text()


def test_a_config_that_will_not_load_is_still_shown(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The case with no report at all.**

    The CLI never ran -- the config failed to load first -- so there is no report to read
    ``error.type`` from. It is shown anyway, with the message and the raw output, rather
    than swallowed for being unclassified. The kind comes from the exception the loader
    raised in this process, which is the only place it exists.
    """
    del document
    run(window, qtbot, write(tmp_path / "broken.yaml", BROKEN_CONFIG), tmp_path / "out.csv")

    panel = window.error_panel()
    failure = panel.failure()
    assert failure is not None
    assert failure.had_report is False, "nothing was written down"
    assert failure.error_type == "ConfigError", "carried from the exception, not guessed"
    assert failure.message, "the project's own message, shown rather than reworded"
    assert panel.advice() is not None
    assert panel.advice().kind == KIND_CHECK_CONFIG
    assert panel.stderr_text().strip(), "the message is also in the raw output"


def test_a_kind_nobody_expected_is_still_shown(
    window: MainWindow, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The fallback, at the panel rather than at the advice.**

    A failure whose kind this build has never heard of still gets its message, its raw
    output, and a button -- the one thing that must not happen is a blank panel. Reached
    here by handing the panel a failure directly, since no current error produces an
    unknown type.
    """
    del document, tmp_path
    from gigaxml.gui.run_report import RunFailure

    panel = window.error_panel()
    panel.show_failure(
        RunFailure(
            error_type="SomeErrorFromAFutureVersion",
            message="something this build has never seen",
            output_complete=False,
            partial_path=None,
            had_report=True,
        ),
        ["error: something this build has never seen"],
    )

    assert panel.message_text() == "something this build has never seen"
    assert panel.stderr_text().strip()
    assert panel.action_text(), "a button with no text is worse than none"
    assert panel.advice() is not None and panel.advice().kind == KIND_CHECK_CONFIG


def test_going_to_the_config_switches_tab(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    del document
    run(window, qtbot, write(tmp_path / "broken.yaml", BROKEN_CONFIG), tmp_path / "out.csv")
    tabs = window.tabs()
    tabs.setCurrentIndex(0)

    window.error_panel().action_requested.emit(KIND_CHECK_CONFIG)

    assert tabs.tabText(tabs.currentIndex()) == "Fields"


def test_a_missing_namespace_prefix_does_not_escape_from_the_button(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**The one that used to crash.**

    ``load_config`` raises ``FieldPathError`` for a path naming a prefix the map does not
    declare, and that class is a *sibling* of ``ConfigError`` rather than a child -- so a
    handler written for ``ConfigError`` let it through, out of a button press and into the
    event loop. No report exists to read the kind from, which is why the panel has to pass
    the exception's class along with the message.
    """
    write(tmp_path / "doc.xml", NAMESPACED_DOCUMENT)
    config = write(tmp_path / "ns.yaml", MISSING_PREFIX_CONFIG)
    panel = window.execution_panel()
    panel._source.setText(str(tmp_path / "doc.xml"))
    panel._config.setText(str(config))
    panel._output.setText(str(tmp_path / "out.csv"))

    panel.start()  # would raise before the fix
    qtbot.wait(100)

    failure = window.error_panel().failure()
    assert failure is not None
    assert failure.error_type == "FieldPathError", "the kind survived the trip"
    assert failure.had_report is False
    assert window.error_panel().advice().kind == KIND_NAMESPACES


def test_the_namespace_advice_goes_to_the_namespace_panel(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The spec's first example, and the reason it needed a kind of its own: a prefix
    problem is fixed by looking at what the document declares, not at the field list."""
    write(tmp_path / "doc.xml", NAMESPACED_DOCUMENT)
    panel = window.execution_panel()
    panel._source.setText(str(tmp_path / "doc.xml"))
    panel._config.setText(str(write(tmp_path / "ns.yaml", MISSING_PREFIX_CONFIG)))
    panel._output.setText(str(tmp_path / "out.csv"))
    panel.start()
    qtbot.wait(100)
    tabs = window.tabs()
    tabs.setCurrentIndex(0)

    window.error_panel().action_requested.emit(KIND_NAMESPACES)

    assert tabs.tabText(tabs.currentIndex()) == "Structure"


def test_the_namespace_advice_says_when_there_is_nothing_to_look_at_yet(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**Landing somewhere and landing at nothing look the same on screen.**

    The namespace panel only has the document's prefixes once it has been analysed, and the
    advice says that is where they are. With no analysis yet the panel is empty -- and an
    unanalysed document is indistinguishable from a document that declares no namespaces,
    so the panel cannot explain itself. The window has to.
    """
    write(tmp_path / "doc.xml", NAMESPACED_DOCUMENT)
    panel = window.execution_panel()
    panel._source.setText(str(tmp_path / "doc.xml"))
    panel._config.setText(str(write(tmp_path / "ns.yaml", MISSING_PREFIX_CONFIG)))
    panel._output.setText(str(tmp_path / "out.csv"))
    panel.start()
    qtbot.wait(100)
    assert window.structure_panel().report() is None, "nothing has been analysed yet"

    window.error_panel().action_requested.emit(KIND_NAMESPACES)

    assert window.tabs().tabText(window.tabs().currentIndex()) == "Structure"
    message = window.statusBar().currentMessage()
    assert "Analyse" in message, f"the empty panel was not explained: {message!r}"


def test_the_namespace_advice_stops_saying_it_once_there_is_something_to_see(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The other half: once analysed, there is nothing to warn about."""
    source = write(tmp_path / "doc.xml", DOCUMENT)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)
    window.error_panel().show_failure(
        RunFailure(
            error_type="FieldPathError",
            message="field path segment 'zz:x' uses namespace prefix 'zz'",
            output_complete=False,
            partial_path=None,
            had_report=False,
        ),
        ["error: field path segment 'zz:x' uses namespace prefix 'zz'"],
    )

    window.error_panel().action_requested.emit(KIND_NAMESPACES)

    assert "Analyse" not in window.statusBar().currentMessage()


# --- the raw output -----------------------------------------------------------


def test_the_raw_output_is_there_and_expandable(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Kept in full and one press away. Showing it by default would bury the summary."""
    del document
    run(window, qtbot, write(tmp_path / "typeerr.yaml", TYPE_ERROR_CONFIG), tmp_path / "out.csv")

    panel = window.error_panel()
    assert not panel.is_stderr_expanded()
    assert panel.stderr_text().strip(), "the child's output was discarded"

    panel._toggle.setChecked(True)

    assert panel.is_stderr_expanded()
    assert panel._stderr.isVisibleTo(panel), "the toggle did not reveal anything"


# --- dispatch is on the type, not the wording ---------------------------------


def test_two_different_messages_of_the_same_type_get_the_same_advice(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**The behavioural half of "do not match the message".**

    Both runs fail with ``FieldTypeError``; the messages differ because the raw values do.
    If anything dispatched on the wording, these two would disagree -- and that is exactly
    the failure mode that looks fine until the CLI rewords a message.
    """
    write(tmp_path / "doc.xml", DOCUMENT)
    first = write(
        tmp_path / "one.yaml",
        "record: /catalog/products/product\nfields:\n  name:\n    path: name\n    type: int\n",
    )
    second = write(
        tmp_path / "two.yaml",
        "record: /catalog/products/product\nfields:\n  name:\n    path: name\n    type: float\n",
    )

    seen: list[tuple[str | None, str, str]] = []
    for index, config in enumerate((first, second)):
        run(window, qtbot, config, tmp_path / f"out{index}.csv")
        failure = window.error_panel().failure()
        assert failure is not None
        seen.append((failure.error_type, failure.message, window.error_panel().advice().kind))

    assert seen[0][0] == seen[1][0] == "FieldTypeError"
    assert seen[0][1] != seen[1][1], "the messages must differ for this to prove anything"
    assert seen[0][2] == seen[1][2] == KIND_QUARANTINE


# --- a message that is more than one line -------------------------------------


def test_a_multi_line_message_is_shown_whole(app: QApplication) -> None:
    """**The detail is the part that can be acted on, and it is not on the first line.**

    ``failure_from_stderr`` kept only the first non-empty line. For a one-line failure that
    is the whole message; for the CLI's own multi-line ones it is the sentence *before* the
    answer. A refused ``--resume`` writes "cannot resume: the run would not be the same run"
    and then indents the two values that differ underneath it -- so keeping the first line
    alone turned "here is what differs" into "cannot resume".

    The indentation is part of the message: it is what makes the two values read as a pair
    rather than as more prose.
    """
    del app
    panel = ErrorPanel()
    said = [
        "error: cannot resume: the run would not be the same run.",
        "  source content:",
        "    checkpoint: 12345 bytes, sha256 aaa",
        "    now:        12346 bytes, sha256 bbb",
        "  Nothing was written.",
    ]

    panel.show_failure(failure_from_stderr(said, 1), said)

    shown = panel.message_text()
    assert shown.splitlines() == [
        "cannot resume: the run would not be the same run.",
        "  source content:",
        "    checkpoint: 12345 bytes, sha256 aaa",
        "    now:        12346 bytes, sha256 bbb",
        "  Nothing was written.",
    ]
    assert "aaa" in shown and "bbb" in shown, "both values have to reach the screen"


def test_the_raw_stderr_is_kept_as_well_as_the_message(app: QApplication) -> None:
    """The message is the readable form and the raw output is the evidence. Showing one
    instead of the other would make the panel a summary of something it could have shown."""
    del app
    panel = ErrorPanel()
    said = ["error: something went wrong", "  with detail"]

    panel.show_failure(failure_from_stderr(said, 1), said)

    assert panel.message_text().startswith("something went wrong")
    assert "  with detail" in panel._stderr.toPlainText()


# --- the panel does not stretch the window ------------------------------------


def test_a_long_message_does_not_stretch_the_window(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**A Windows path has no spaces in it, so word wrap alone does not shorten a label.**

    A ``WriterError``'s message is two full paths and a WinError, and the label reporting
    its longest unbroken token as its minimum width pushed the window out to 4340 pixels.
    Found by photographing the panel: nothing in the code looks wrong and no test was
    failing, because a too-wide window is not a broken window.

    The threshold is loose on purpose -- what matters is that the message cannot set the
    window's width at all, not that the layout lands on a particular number.
    """
    del document
    target = write(tmp_path / "locked.csv", "")
    holder = _hold_exclusively(target)
    try:
        run(window, qtbot, write(tmp_path / "good.yaml", GOOD_CONFIG), target)
    finally:
        holder.close()

    failure = window.error_panel().failure()
    assert failure is not None and len(failure.message) > 200, "the message must be long"

    assert window.minimumSizeHint().width() < 1400, "the message set the window's width"
    assert window.execution_panel()._counts.minimumSizeHint().width() < 600


# --- the panel on its own -----------------------------------------------------


def test_the_panel_is_empty_until_something_fails(app: QApplication) -> None:
    del app
    panel = ErrorPanel()

    assert panel.failure() is None
    assert panel.headline_text() == ""
    assert not panel.isVisible()


def test_an_unknown_kind_still_gets_a_usable_button(app: QApplication) -> None:
    """Reachable only if the advice module grows a kind the window does not handle -- but a
    blank button would be worse than a vague one."""
    del app
    from gigaxml.gui.panels.errors import action_label_for

    assert action_label_for(advice_for("SomeErrorFromAFutureVersion"))


def _hold_exclusively(path: pathlib.Path):  # noqa: ANN202 - a Win32 handle
    """Open a file with no sharing, so the CLI cannot move onto it.

    Windows-only, and skipped elsewhere rather than faked: the failure this produces is a
    Windows one, and a test that pretended otherwise would be testing the mock.
    """
    if sys.platform != "win32":
        pytest.skip("exclusive file locking is a Windows behaviour")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        pytest.skip(f"could not take an exclusive handle: {ctypes.get_last_error()}")

    class Holder:
        def close(self) -> None:
            kernel32.CloseHandle(handle)

    return Holder()
