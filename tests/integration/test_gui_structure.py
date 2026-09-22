"""The structure panel, driven against the real CLI.

The panel's whole job is to show what ``inspect`` said, so these tests run ``inspect``.
Nothing here fakes a subprocess: a panel tested against a fake would prove that it can
talk to a fake, which is not the part that breaks.

Two tests are worth more than the rest. ``test_cancelling_stops_the_child`` looks for the
PID in the operating system's process table rather than trusting ``result.killed``, and
``test_cancelling_the_analysis_leaves_the_execution_run_alone`` covers the mistake that
shared plumbing invites -- one panel's cancel reaching the other panel's child.
"""

from __future__ import annotations

import csv
import io
import pathlib
import sys
from collections.abc import Callable

import psutil
import pytest

pytest.importorskip("PySide6", reason="the desktop application is an optional extra")

from PySide6.QtCore import QElapsedTimer, QTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.inspect_report import InspectReport, paths_to_csv
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels import structure as structure_module

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "tests" / "fixtures"

#: The document from ``tests/unit/test_inspect.py``, which is the *real* rebinding case.
#: ``tests/fixtures/shadowed.xml`` is not: its prefix is declared once and used
#: throughout, so ``shadowed_prefixes`` is empty for it despite the name.
REBOUND = (
    '<root xmlns:p="urn:one"><p:a><p:b/></p:a><other xmlns:p="urn:two"><p:c/><p:c/></other></root>'
)

CONFIG = {"record": "/catalog/products/product", "fields": {"id": {"path": "@id"}}}


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


#: How big the shared document is. Chosen so that ``inspect`` cannot finish on its own
#: while a test is cancelling it: at the measured 14 MiB/s this is several seconds, and a
#: cancel that does nothing at all is then unmistakable. A smaller file made the
#: cancellation test pass without cancelling anything -- the child simply finished first,
#: and the reverse control is what found that out.
BIG_MIB = 100


def write_big(path: pathlib.Path, target_mib: int) -> pathlib.Path:
    """A document of roughly ``target_mib``, padded so it is not mostly names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    padding = "x" * 400
    limit = target_mib * 1024 * 1024
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<catalog><products>")
        number = 0
        while handle.tell() < limit:
            number += 1
            handle.write(
                f'<product id="{number}"><name>N{number}</name><note>{padding}</note></product>'
            )
        handle.write("</products></catalog>")
    return path


@pytest.fixture(scope="module")
def big_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Built once for the module: generating 100 MiB per test would dominate the suite."""
    return write_big(tmp_path_factory.mktemp("big") / "big.xml", BIG_MIB)


def many_paths(path: pathlib.Path, distinct: int) -> pathlib.Path:
    """A document with more distinct paths than the cap we are about to set."""
    path.parent.mkdir(parents=True, exist_ok=True)
    children = "".join(f"<level{n}><leaf>v</leaf></level{n}>" for n in range(distinct))
    path.write_text(f"<root>{children}</root>", encoding="utf-8")
    return path


def wait_until(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    elapsed = QElapsedTimer()
    elapsed.start()
    while elapsed.elapsed() < timeout_ms:
        qtbot.wait(25)
        if predicate():
            return True
    return predicate()


def analyse(window: MainWindow, qtbot: QtBot, source: pathlib.Path) -> None:
    panel = window.structure_panel()
    panel.set_document(source)
    panel.analyze()
    assert wait_until(qtbot, lambda: panel.run_result() is not None), "inspect never finished"
    assert wait_until(qtbot, lambda: not panel._pump.isActive()), "the panel never drained"


def select_path(panel: object, path: str) -> None:
    """Select a candidate by its path rather than by row number.

    Row numbers move: the table sorts itself as soon as it is filled. A test that says
    "row 0" is really saying "whichever candidate sorts first", which is not a statement
    about the panel.
    """
    table = panel._candidates  # type: ignore[attr-defined]
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == path:
            panel.select_candidate(row)  # type: ignore[attr-defined]
            return
    raise AssertionError(f"{path} is not in the candidate list")


# --- the tab and the arguments ------------------------------------------------


def test_the_window_offers_the_structure_panel_as_a_tab(window: MainWindow) -> None:
    labels = [window.tabs().tabText(i) for i in range(window.tabs().count())]

    assert "Structure" in labels


def test_the_arguments_are_built_without_starting_anything(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.structure_panel()
    panel.set_document(tmp_path / "doc.xml")

    assert panel.build_args() == ["inspect", str(tmp_path / "doc.xml"), "--json"]

    panel.set_max_paths(10)
    assert panel.build_args()[-2:] == ["--max-paths", "10"]


# --- the candidate list -------------------------------------------------------


def test_analysing_a_document_fills_the_candidate_list(window: MainWindow, qtbot: QtBot) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")

    panel = window.structure_panel()
    report = panel.report()
    assert report is not None
    assert report.elements_seen > 0
    assert panel._candidates.rowCount() == len(report.candidates)
    assert panel._paths.rowCount() == len(report.paths)
    assert panel._candidates.item(0, 0).text().startswith("/")


def test_a_nested_candidate_is_marked_as_nested(window: MainWindow, qtbot: QtBot) -> None:
    """Read from ``nested_inside``, which exists because the path text is ambiguous."""
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")

    panel = window.structure_panel()
    markers = [panel._candidates.item(row, 4).text() for row in range(panel._candidates.rowCount())]

    assert any(marker.startswith("[inside ") for marker in markers), (
        f"no candidate was marked as nested; the column held {markers}"
    )


def test_selecting_a_row_after_sorting_shows_that_candidate(
    window: MainWindow, qtbot: QtBot
) -> None:
    """The row number is not the report index once the view has been sorted.

    Treating them as the same shows a different candidate than the one clicked, which
    looks like a working panel until somebody sorts a column.
    """
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()

    panel._candidates.sortItems(0)  # by path, ascending
    panel.select_candidate(0)

    clicked_path = panel._candidates.item(0, 0).text()
    assert panel.detail_text().splitlines()[0] == clicked_path
    index = panel.selected_candidate_index()
    assert index is not None
    assert panel.report().candidates[index].path == clicked_path


def test_sorting_keeps_the_right_candidate_after_a_second_sort(
    window: MainWindow, qtbot: QtBot
) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()

    panel._candidates.sortItems(1)  # by score
    panel.select_candidate(1)

    clicked_path = panel._candidates.item(1, 0).text()
    assert panel.detail_text().splitlines()[0] == clicked_path


def test_a_numeric_column_sorts_as_a_number_not_as_text(window: MainWindow, qtbot: QtBot) -> None:
    """Text sorting puts 10 before 9, which looks deliberate and is wrong."""
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()

    panel._paths.sortItems(1)  # count, ascending
    keys = []
    for row in range(panel._paths.rowCount()):
        item = panel._paths.item(row, 1)
        keys.append(int(item.text().replace(",", "")))

    assert keys == sorted(keys), f"count column is not in numeric order: {keys}"


def test_the_search_box_hides_rows_that_do_not_match(window: MainWindow, qtbot: QtBot) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    total = panel._paths.rowCount()

    panel.set_filter("manufacturer")

    visible = panel.visible_path_rows()
    assert 0 < visible < total, f"filter left {visible} of {total} rows visible"


def test_the_search_box_can_be_cleared(window: MainWindow, qtbot: QtBot) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    total = panel._paths.rowCount()

    panel.set_filter("manufacturer")
    panel.set_filter("")

    assert panel.visible_path_rows() == total


# --- the side panel -----------------------------------------------------------


def test_the_side_panel_shows_fields_and_namespaces(window: MainWindow, qtbot: QtBot) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()

    # The top candidate by score, found by path: the row it sits in depends on the sort.
    select_path(panel, "/catalog/products/product")
    detail = panel.detail_text()

    assert "child elements" in detail
    assert "attributes" in detail
    assert "@id" in detail
    assert "namespaces" in detail
    assert "urn:example:shop" in detail


def test_the_side_panel_does_not_pretend_to_have_example_values(
    window: MainWindow, qtbot: QtBot
) -> None:
    """**Rewritten in 8A-7, and the reason matters.**

    This used to assert the section was *absent*: ``inspect --json`` carries no sample
    values, and inventing one -- or reading the document in this process to get one --
    would both have been worse than the gap. 8A-7 gave the panel a real source, so the
    section is now present and the assertion has to be about something else: that it holds
    nothing until a sample has actually arrived.

    Checked synchronously, with no event loop turn in between, so "has the sample landed
    yet" cannot make it flaky.
    """
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()

    select_path(panel, "/catalog/products/product")
    detail = panel.detail_text()

    assert "example values" in detail
    assert "sampling…" in detail, "the panel claimed values before it had any"
    assert " = " not in detail, "a value appeared before anything was sampled"


# --- the namespace panel and the warnings -------------------------------------


def test_the_namespace_panel_lists_the_prefixes(window: MainWindow, qtbot: QtBot) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")

    text = window.structure_panel()._namespaces.toPlainText()

    assert "<default>" in text
    assert "urn:example:shop" in text


def test_a_rebound_prefix_is_warned_about(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The real rebinding document. ``shadowed.xml`` would pass and prove nothing."""
    source = tmp_path / "rebound.xml"
    source.write_text(REBOUND, encoding="utf-8")

    analyse(window, qtbot, source)

    report = window.structure_panel().report()
    assert report is not None
    assert report.shadowed_prefixes == ("p",), "the fixture no longer rebinds p"
    assert "p" in window.structure_panel().warnings_text()
    assert "more than one thing" in window.structure_panel().warnings_text()


def test_the_flat_document_the_brief_warns_about_produces_no_warning(
    window: MainWindow, qtbot: QtBot
) -> None:
    """``tests/fixtures/shadowed.xml`` declares its prefix once. It is not a rebinding,
    and a panel that warned about it would be warning about the file's name."""
    analyse(window, qtbot, FIXTURES / "shadowed.xml")

    report = window.structure_panel().report()
    assert report is not None
    assert report.shadowed_prefixes == ()
    assert "more than one thing" not in window.structure_panel().warnings_text()


def test_hitting_the_path_cap_is_said_out_loud(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    source = many_paths(tmp_path / "wide.xml", distinct=20)
    panel = window.structure_panel()
    panel.set_document(source)
    panel.set_max_paths(10)
    panel.analyze()
    assert wait_until(qtbot, lambda: panel.run_result() is not None)
    assert wait_until(qtbot, lambda: not panel._pump.isActive())

    report = panel.report()
    assert report is not None and report.paths_truncated, "the cap was not reached"
    warnings = panel.warnings_text()
    assert "stopped at" in warnings, warnings
    assert warnings.index("stopped at") < len(warnings), "truncation must lead the list"


def test_every_warning_the_report_has_is_shown(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Showing three of five is worse than showing none: the reader believes it is done."""
    source = many_paths(tmp_path / "wide.xml", distinct=20)
    panel = window.structure_panel()
    panel.set_document(source)
    panel.set_max_paths(10)
    panel.analyze()
    assert wait_until(qtbot, lambda: panel.run_result() is not None)
    assert wait_until(qtbot, lambda: not panel._pump.isActive())

    report = panel.report()
    assert report is not None
    shown = panel.warnings_text()
    for line in report.warnings():
        assert line in shown, f"this warning was dropped: {line}"


# --- the path table export ----------------------------------------------------


def test_the_path_table_exports_as_csv(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    target = tmp_path / "paths.csv"

    panel.export_csv(target)

    rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8"))))
    assert rows[0][0] == "path"
    assert len(rows) == panel._paths.rowCount() + 1


def test_the_export_is_the_foundations_csv_not_a_second_one(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Gate 6's escaping lives in ``inspect_report.paths_to_csv``, which has its own test.

    What is checked here is that the panel hands that function the report rather than
    joining columns itself -- a second implementation would be the one with the bug.

    **The escaping cannot be reached through a document.** XML element names may not
    contain a comma or a quote, so no real path in any real document has one; the
    foundation test constructs the entries directly. See
    ``tests/unit/test_inspect_report_csv.py``.
    """
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    target = tmp_path / "paths.csv"

    panel.export_csv(target)

    assert target.read_text(encoding="utf-8") == paths_to_csv(panel.report())


def test_the_exported_table_has_one_column_count(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    target = tmp_path / "paths.csv"

    panel.export_csv(target)

    rows = list(csv.reader(io.StringIO(target.read_text(encoding="utf-8"))))
    widths = {len(row) for row in rows}
    assert len(widths) == 1, f"the exported rows disagree on their column count: {widths}"


# --- cancelling ---------------------------------------------------------------


def test_cancelling_stops_the_child(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path
) -> None:
    """**The PID, checked the instant ``cancel()`` returns.**

    ``CliProcess.kill`` calls ``process.wait()``, so when ``cancel()`` comes back the child
    is already gone -- there is no window in which it might still be dying. An
    implementation that set its own "cancelled" flag and returned would leave the child in
    the process table right here, which is the failure this catches.

    ``result.killed`` alone would not: that flag is set by the same code that is supposed
    to do the killing, so it agrees with itself.
    """
    panel = window.structure_panel()
    panel.set_document(big_document)
    panel.analyze()
    assert wait_until(qtbot, lambda: panel.is_running(), timeout_ms=10_000)
    pid = panel.pid()
    assert pid is not None
    assert panel.is_running(), "the child finished before it could be cancelled"

    panel.cancel()

    assert psutil.pid_exists(pid) is False, (
        f"cancel() returned while PID {pid} was still in the process table -- "
        f"the child was not killed, or kill() did not wait for it"
    )

    assert wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=20_000)
    result = panel.run_result()
    assert result is not None and result.killed


def test_cancelling_the_analysis_leaves_the_execution_run_alone(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """Two panels, two processes. Shared plumbing is where this coupling would creep in."""
    config = tmp_path / "config.json"
    config.write_text(
        '{"record": "/catalog/products/product", "fields": {"id": {"path": "@id"}}}',
        encoding="utf-8",
    )

    execution = window.execution_panel()
    execution._source.setText(str(big_document))
    execution._config.setText(str(config))
    execution._output.setText(str(tmp_path / "out.csv"))
    execution.start()
    assert wait_until(qtbot, lambda: execution.is_running(), timeout_ms=10_000)

    structure = window.structure_panel()
    structure.set_document(big_document)
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.pid() is not None, timeout_ms=10_000)
    structure_pid = structure.pid()
    assert structure_pid is not None

    structure.cancel()

    assert psutil.pid_exists(structure_pid) is False
    assert execution.is_running(), "cancelling the analysis killed the extraction too"

    execution.cancel()
    assert wait_until(qtbot, lambda: not execution.is_running(), timeout_ms=20_000)


# --- the interface does not freeze --------------------------------------------


def test_the_interface_keeps_breathing_while_analysing(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path
) -> None:
    """Measured against an idle baseline on this machine, not against a theoretical rate."""
    idle_beats = _count_beats(qtbot, 1000)

    panel = window.structure_panel()
    panel.set_document(big_document)
    beats = _count_beats(qtbot, 1500, start=panel.analyze)
    panel.cancel()
    assert wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=20_000)

    assert idle_beats > 0, "the idle baseline produced no beats at all"
    assert beats >= idle_beats / 2, (
        f"the window beat {beats} times while analysing against {idle_beats} idle -- "
        f"it was starved, so something is blocking the UI thread"
    )


def _count_beats(qtbot: QtBot, milliseconds: int, start: Callable[[], None] | None = None) -> int:
    beats = 0

    def beat() -> None:
        nonlocal beats
        beats += 1

    timer = QTimer()
    timer.setInterval(50)
    timer.timeout.connect(beat)
    timer.start()
    if start is not None:
        start()
    qtbot.wait(milliseconds)
    timer.stop()
    return beats


# --- when it goes wrong -------------------------------------------------------


def test_analysing_nothing_is_refused_rather_than_crashing(window: MainWindow) -> None:
    panel = window.structure_panel()

    panel.analyze()

    assert not panel.is_running()
    assert "open a document" in panel._status.text()


def test_a_document_that_cannot_be_read_says_so(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    broken = tmp_path / "broken.xml"
    broken.write_text("<catalog><products>", encoding="utf-8")
    panel = window.structure_panel()
    panel.set_document(broken)

    panel.analyze()
    assert wait_until(qtbot, lambda: panel.run_result() is not None)
    # Waiting for the result is not enough: the reader thread sets it, and the panel
    # updates the label later, on the timer. Asserting between the two would be a race.
    assert wait_until(qtbot, lambda: not panel._pump.isActive())

    assert panel.report() is None
    assert panel._status.text().strip() != "analysing…"
    assert panel._status.text().strip() != ""


def test_nothing_to_export_leaves_the_button_off(window: MainWindow) -> None:
    """Disabled until there is a report, so the button cannot write an empty file."""
    assert window.structure_panel()._export.isEnabled() is False


# --- branches that a document cannot reach ------------------------------------


def test_the_arguments_cannot_be_built_without_a_document(window: MainWindow) -> None:
    with pytest.raises(ValueError, match="no document"):
        window.structure_panel().build_args()


def test_analysing_twice_does_not_start_a_second_child(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path
) -> None:
    panel = window.structure_panel()
    panel.set_document(big_document)
    panel.analyze()
    assert wait_until(qtbot, lambda: panel.is_running(), timeout_ms=10_000)
    first_pid = panel.pid()

    panel.analyze()  # a second press while the first is going

    assert panel.pid() == first_pid, "the second press started another process"
    panel.cancel()
    assert wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=20_000)


def test_cancelling_before_anything_started_does_nothing(window: MainWindow) -> None:
    panel = window.structure_panel()

    panel.cancel()

    assert not panel.is_running()


def test_finishing_without_a_result_does_not_crash(window: MainWindow) -> None:
    """Defensive: the pump can only fire after a result is recorded, but a panel that
    raised here would take the window down with it."""
    panel = window.structure_panel()

    panel._finish()

    assert not panel.is_running()


def test_selecting_nothing_leaves_the_detail_pane_alone(window: MainWindow, qtbot: QtBot) -> None:
    """**Waiting first is new in 8A-7.** The pane now updates a second time, when the
    example values arrive, so capturing the text before that would compare a pane mid
    sentence against one that had finished."""
    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    select_path(panel, "/catalog/products/product")
    assert wait_until(
        qtbot, lambda: panel.example_values() is not None or panel.example_failure()
    ), "the example values never arrived"
    before = panel.detail_text()

    panel._candidates.clearSelection()
    panel._candidates.setCurrentCell(-1, -1)
    panel._show_selected_candidate()

    assert panel.detail_text() == before
    assert panel.selected_candidate_index() is None


def test_selecting_before_a_report_does_nothing(window: MainWindow) -> None:
    """``analyze`` clears the tables before it has a report, and clearing them can change
    the selection -- so this runs in the ordinary course of pressing Analyse."""
    panel = window.structure_panel()

    panel._show_selected_candidate()

    assert panel.detail_text() == ""
    assert panel.selected_candidate_index() is None


def test_a_row_with_no_stored_index_is_ignored(window: MainWindow, qtbot: QtBot) -> None:
    """A row that the panel did not fill cannot be resolved to a candidate, and guessing
    from the row number is the mistake the stored index exists to avoid.

    Analysed first, so the panel has a report and the check being exercised is the one
    about the missing index rather than the one about the missing report.
    """
    from PySide6.QtWidgets import QTableWidgetItem

    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    panel._candidates.setRowCount(1)
    panel._candidates.setItem(0, 0, QTableWidgetItem("planted"))
    panel._candidates.setCurrentCell(0, 0)

    panel._show_selected_candidate()

    assert panel.selected_candidate_index() is None
    assert panel.detail_text() == ""


def test_exporting_without_a_report_writes_nothing(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.structure_panel()
    target = tmp_path / "paths.csv"

    panel.export_csv(target)

    assert panel.csv_text() == ""
    assert target.read_text(encoding="utf-8") == ""


def test_the_export_dialog_writes_where_it_says(
    window: MainWindow, qtbot: QtBot, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from PySide6.QtWidgets import QFileDialog

    analyse(window, qtbot, FIXTURES / "extract_default_ns.xml")
    panel = window.structure_panel()
    target = tmp_path / "exported.csv"
    monkeypatch.setattr(
        QFileDialog, "getSaveFileName", staticmethod(lambda *_a, **_k: (str(target), ""))
    )

    panel.choose_export_path()

    assert target.is_file()
    assert target.read_text(encoding="utf-8").startswith("path,")


def test_the_namespace_panel_lists_unprefixed_namespaces(window: MainWindow) -> None:
    """Reached with a constructed report: an unprefixed namespace is not something a
    small hand-written document reliably produces."""
    report = _report_with(unmapped_namespaces=("urn:nowhere",))
    panel = window.structure_panel()

    panel._fill_namespaces(report)

    text = panel._namespaces.toPlainText()
    assert "no prefix" in text
    assert "urn:nowhere" in text


def test_a_candidate_without_namespaces_says_so() -> None:
    """The side pane distinguishes "no namespaces" from "the list is empty because
    something went wrong"."""
    from gigaxml.gui.inspect_report import Candidate

    candidate = Candidate(
        path="/a",
        score=1.0,
        count=2,
        shape_consistency=1.0,
        repeat_score=1.0,
        nested_inside=None,
        depth=1,
    )

    text = structure_module._describe_candidate(candidate)

    assert "(none)" in text
    assert "child elements" not in text, "nothing to list, so no heading for it"


def test_a_candidate_with_unprefixed_namespaces_and_evidence_shows_both() -> None:
    from gigaxml.gui.inspect_report import Candidate

    candidate = Candidate(
        path="/a",
        score=1.0,
        count=2,
        shape_consistency=1.0,
        repeat_score=1.0,
        nested_inside="/parent",
        depth=2,
        namespaces={"p": "urn:one"},
        missing_namespaces=("urn:two",),
        evidence="two occurrences",
    )

    text = structure_module._describe_candidate(candidate)

    assert "no prefix" in text
    assert "urn:two" in text
    assert "two occurrences" in text
    assert "/parent" in text


def test_a_numeric_cell_falls_back_to_text_comparison() -> None:
    """The numeric item may be compared with a plain one, and must not recurse.

    **This is why the fallback does not call ``super().__lt__``.** PySide6 dispatches that
    back into this override, which recurses until the stack runs out -- a ``RecursionError``
    raised inside Qt's sort rather than at the line that caused it. Comparing the display
    text is what the base class would have done, done explicitly, and the assertion below
    pins that: text order puts "10" before "9".
    """
    from PySide6.QtWidgets import QTableWidgetItem

    numeric = structure_module._NumericItem("10", 10)
    plain = QTableWidgetItem("9")

    assert (numeric < plain) is True
    assert (numeric < structure_module._NumericItem("9", 9)) is False, "numbers, not text"


def _report_with(**overrides: object) -> InspectReport:
    """A minimal report, so a branch can be exercised without a document for it."""
    fields: dict[str, object] = {
        "source": "constructed",
        "input_mb": 0.0,
        "elements_seen": 0,
        "namespaces": {},
        "shadowed_prefixes": (),
        "unmapped_namespaces": (),
        "candidates": (),
        "paths": (),
        "paths_truncated": False,
        "paths_limit": 10_000,
        "paths_tracked": 0,
        "untracked_occurrences": 0,
        "depth_seen": 1,
        "depth_limit": 32,
        "depth_truncated": False,
        "values_truncated": False,
    }
    fields.update(overrides)
    return InspectReport(**fields)  # type: ignore[arg-type]
