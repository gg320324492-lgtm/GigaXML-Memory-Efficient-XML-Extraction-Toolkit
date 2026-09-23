"""The preview panel, and the example values the structure panel borrows from it.

**Two claims are worth more than the rest here.**

``test_the_preview_shows_exactly_the_rows_it_asked_for`` is Gate 4: set N, get N rows. It
is checked against the file the CLI wrote as well as against the grid, so a panel that
filled its table from somewhere else would not pass.

``test_the_example_values_come_through_the_previews_mechanism`` is Gate 6, and it does not
assert that some text appeared. It patches the sampling function the preview uses and
checks the structure panel called *that* one -- because "the values look right" and "there
is one implementation" are different claims, and only the second is what the brief asks
for.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import Callable

import psutil
import pytest

pytest.importorskip("PySide6", reason="the desktop application is an optional extra")

from PySide6.QtCore import QElapsedTimer
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui import sampling
from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.panels import structure as structure_module
from gigaxml.gui.panels.preview import PreviewPanel

REPO = pathlib.Path(__file__).resolve().parent.parent.parent

RECORD = "/catalog/products/product"

#: Names and quantities that cannot be guessed: a hard-coded example value, or one read
#: from the wrong record, will not match these.
CATALOGUE = (
    "<catalog><products>"
    "<product id='1'><name>Alpha Lamp</name><qty>5</qty></product>"
    "<product id='2'><name>Beta Suite</name><qty>7</qty></product>"
    "<product id='3'><name>Gamma Desk</name><qty>9</qty></product>"
    "</products></catalog>"
)

CONFIG = {
    "record": RECORD,
    "fields": {"id": {"path": "@id"}, "name": {"path": "name"}},
}


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


def document(
    tmp_path: pathlib.Path, text: str = CATALOGUE, name: str = "catalogue.xml"
) -> pathlib.Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def wait_until(qtbot: QtBot, predicate: Callable[[], bool], timeout_ms: int = 60_000) -> bool:
    elapsed = QElapsedTimer()
    elapsed.start()
    while elapsed.elapsed() < timeout_ms:
        qtbot.wait(25)
        if predicate():
            return True
    return predicate()


def assert_the_chain_succeeded(structure: object) -> None:
    """The example chain is not allowed to have failed.

    Every candidate these tests select is **top-level**, which is what
    ``inspect --generate-config`` accepts, so a failure is the chain breaking rather than
    the document being unusual.

    **The ``or structure.example_failure()`` in the ``wait_until`` is not an excuse.** It
    is there so a failure ends the wait instead of hanging until the timeout -- that is
    about *waiting*, not about *tolerating*. Without this check a failing chain left every
    test green and every assertion below it comparing against a pane that had never been
    filled in, which is how the coverage of the whole chain could disappear without a
    single test going red.

    The nested-candidate case is the exception, and it is not this helper:
    ``test_a_chain_from_a_candidate_you_have_moved_past_does_not_report`` selects one on
    purpose, and says so.

    ``example_failure`` returns a ``str``, and its "there was no failure" value is the
    **empty string** -- not ``None``. An ``is None`` check here would fail on every run,
    which is how this was found.
    """
    failure = structure.example_failure()  # type: ignore[attr-defined]
    assert failure == "", f"the example chain failed: {failure}"


@pytest.fixture(scope="module")
def big_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A document big enough that a child reading it is still alive a moment later.

    Built once for the module: the two tests that need it are about a child that is still
    running, and a document small enough to finish instantly would let both of them pass
    without the behaviour they are checking.
    """
    path = tmp_path_factory.mktemp("big") / "big.xml"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<catalog><products>")
        padding = "x" * 400
        number = 0
        while handle.tell() < 40 * 1024 * 1024:
            number += 1
            handle.write(
                f'<product id="{number}"><name>{padding}</name>'
                f"<tags><tag>t{number}</tag><tag>u{number}</tag></tags></product>"
            )
        handle.write("</products></catalog>")
    return path


def run_preview(panel: PreviewPanel, qtbot: QtBot, limit: int) -> None:
    panel.set_limit(limit)
    panel.preview()
    assert wait_until(qtbot, lambda: panel.run_result() is not None), "sample never finished"
    assert wait_until(qtbot, lambda: not panel._pump.isActive()), "the panel never drained"


def select_path(panel, path: str) -> None:  # noqa: ANN001 - a StructurePanel
    """Select a candidate by its path rather than by row number.

    Row numbers move: the table sorts itself as soon as it is filled, so "row 0" means
    "whichever candidate sorts first", which is not a statement about the panel.
    """
    table = panel._candidates
    for row in range(table.rowCount()):
        if table.item(row, 0).text() == path:
            panel.select_candidate(row)
            return
    raise AssertionError(f"{path} is not in the candidate list")


# --- the tab and the accessor -------------------------------------------------


def test_the_window_exposes_the_preview_panel(window: MainWindow) -> None:
    assert isinstance(window.preview_panel(), PreviewPanel)
    labels = [window.tabs().tabText(index) for index in range(window.tabs().count())]
    assert "Preview" in labels


# --- Gate 4: N rows -----------------------------------------------------------


def test_the_preview_shows_exactly_the_rows_it_asked_for(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Set N, get N rows -- and the same N the CLI wrote to its file."""
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 2)

    sampled = panel.sampled()
    assert sampled is not None
    assert sampled.requested == 2
    assert panel.table_row_count() == 2
    assert sampled.row_count == 2, "the file holds a different number of rows than asked"
    assert panel.headers() == ("id", "name")


def test_a_different_limit_gives_a_different_number_of_rows(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 1)
    assert panel.table_row_count() == 1

    run_preview(panel, qtbot, 3)
    assert panel.table_row_count() == 3


def test_a_limit_larger_than_the_document_says_the_document_ran_out(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 50)

    sampled = panel.sampled()
    assert sampled is not None
    assert panel.table_row_count() == 3
    assert sampled.short_of_request is True
    assert "ran out" in panel.status_text()


def test_the_values_shown_are_the_documents_values(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 2)

    assert [panel.cell(row, 0) for row in range(2)] == ["1", "2"]
    assert [panel.cell(row, 1) for row in range(2)] == ["Alpha Lamp", "Beta Suite"]


# --- Gate 5: the rejected tab -------------------------------------------------


def test_the_rejected_tab_is_there_and_says_when_there_is_nothing(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Always present, and explicit: a tab that vanishes cannot be told from a missing
    feature."""
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 2)

    assert panel.rejected_tab_label() == "Rejected (0)"
    assert panel.rejected_row_count() == 0
    assert "No records were rejected" in panel.rejected_note_text()


def test_rejected_records_appear_in_their_tab(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    source = document(
        tmp_path,
        "<catalog><products>"
        "<product id='1'><qty>5</qty></product>"
        "<product id='2'><qty>not a number</qty></product>"
        "</products></catalog>",
        name="mixed.xml",
    )
    panel = window.preview_panel()
    panel.set_source(source)
    panel.set_config(
        {
            "record": RECORD,
            "on_error": "quarantine",
            "fields": {"id": {"path": "@id"}, "qty": {"path": "qty", "type": "int"}},
        }
    )

    run_preview(panel, qtbot, 5)

    assert panel.rejected_tab_label() == "Rejected (1)"
    assert panel.rejected_row_count() == 1
    assert panel.rejected_cell(0, 3) == "qty", "the field that failed"
    assert panel.rejected_cell(0, 4) == "not a number", "the value that failed"
    assert panel.rejected_note_text() == ""
    assert panel.table_row_count() == 1, "only the good record is in the sample"


def test_the_rejections_come_from_the_file_the_child_wrote(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Not inferred from the count: the path is the one the summary named."""
    source = document(
        tmp_path,
        "<catalog><products><product id='1'><qty>x</qty></product></products></catalog>",
        name="one_bad.xml",
    )
    panel = window.preview_panel()
    panel.set_source(source)
    panel.set_config(
        {
            "record": RECORD,
            "on_error": "quarantine",
            "fields": {"qty": {"path": "qty", "type": "int"}},
        }
    )

    run_preview(panel, qtbot, 3)

    sampled = panel.sampled()
    assert sampled is not None and sampled.rejected_path is not None
    assert sampled.rejected_path.is_file()
    assert sampling.read_rejected(sampled.rejected_path), "the file the summary named is empty"


def test_abort_never_produces_rejections(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The default policy stops at the first bad record rather than collecting it."""
    source = document(
        tmp_path,
        "<catalog><products><product id='1'><qty>x</qty></product></products></catalog>",
        name="aborts.xml",
    )
    panel = window.preview_panel()
    panel.set_source(source)
    panel.set_config(
        {
            "record": RECORD,
            "fields": {"qty": {"path": "qty", "type": "int"}},
        }
    )

    run_preview(panel, qtbot, 3)

    assert panel.rejected_row_count() == 0
    assert panel.rejected_tab_label() == "Rejected (0)"


# --- when it cannot run -------------------------------------------------------


def test_the_button_is_off_until_there_is_a_document_and_a_config(window: MainWindow) -> None:
    panel = window.preview_panel()

    assert panel._run.isEnabled() is False
    assert "document" in panel.status_text()


def test_the_button_turns_on_once_both_are_present(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    panel = window.preview_panel()

    panel.set_source(document(tmp_path))
    assert panel._run.isEnabled() is False
    assert "config" in panel.status_text()

    panel.set_config(CONFIG)
    assert panel._run.isEnabled() is True


def test_pressing_preview_with_nothing_set_says_what_is_missing(window: MainWindow) -> None:
    panel = window.preview_panel()

    panel.preview()

    assert not panel.is_running()
    assert "document" in panel.status_text()


# --- cancelling ---------------------------------------------------------------


def test_a_preview_can_be_cancelled(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path
) -> None:
    panel = window.preview_panel()
    panel.set_source(big_document)
    panel.set_config(CONFIG)
    panel.set_limit(200_000)

    panel.preview()
    assert wait_until(qtbot, lambda: panel.is_running(), timeout_ms=10_000)
    pid = panel.pid()
    assert pid is not None

    panel.cancel()

    assert psutil.pid_exists(pid) is False, f"PID {pid} outlived the cancel"
    assert wait_until(qtbot, lambda: not panel.is_running(), timeout_ms=20_000)


# --- Gate 6: the structure panel's example values -----------------------------


def test_the_example_values_are_the_documents_values(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """Real values, not a placeholder: these strings are in the document and nowhere else."""
    source = document(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)

    structure.select_candidate(0)
    assert wait_until(
        qtbot, lambda: structure.example_values() is not None or structure.example_failure()
    ), "no example values arrived"

    values = structure.example_values()
    assert structure.example_failure() == ""
    assert values is not None
    assert "Alpha Lamp" in values.values(), values
    assert "Beta Suite" not in values.values(), "the first record, not the second"

    # And the same value has to reach the screen. Asserting only on `example_values()`
    # would leave the rendering free to show placeholders while the data behind it was
    # right -- which is the shape of the failure the brief asks to guard against.
    assert "Alpha Lamp" in structure.detail_text()


def test_the_example_values_come_through_the_previews_mechanism(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Gate 6, stated as an identity rather than as a resemblance.**

    The brief allows the structure panel to show example values only because the preview
    now exists to produce them. So the claim to check is not "the values look right" but
    "there is one implementation": this patches the function the preview reads its table
    with, and asserts the structure panel went through it.
    """
    calls: list[object] = []
    original = structure_module.table_from

    def counting(summary, output):  # noqa: ANN001, ANN202
        calls.append(output)
        return original(summary, output)

    monkeypatch.setattr(structure_module, "table_from", counting)

    source = document(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)

    structure.select_candidate(0)
    assert wait_until(
        qtbot, lambda: structure.example_values() is not None or structure.example_failure()
    )
    assert_the_chain_succeeded(structure)

    assert calls, "the example values did not come through the sampling mechanism"
    assert structure.example_values() is not None


def test_the_side_panel_says_it_is_still_sampling(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The three states are distinguishable: pending, arrived, failed."""
    source = document(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)

    structure.select_candidate(0)
    pending_text = structure.detail_text()
    assert "example values" in pending_text

    assert wait_until(
        qtbot, lambda: structure.example_values() is not None or structure.example_failure()
    )
    assert_the_chain_succeeded(structure)
    assert "example values" in structure.detail_text()


def test_a_document_with_no_records_says_so_rather_than_showing_nothing(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**This document has no candidates at all, so there is no chain to wait for.**

    There used to be a block here that selected candidate 0 *if there was one* and waited
    for example values or a failure. With nothing selected it never ran, so the test
    asserted nothing and could not fail -- one more place where the chain's fate was
    unobserved.

    What can be asserted is that the panel does not invent values for records it does not
    have. Whether it should say something more in the empty case is a question about the
    panel rather than about the chain, and it is not answered here.
    """
    source = document(tmp_path, "<catalog><products/></catalog>", name="empty.xml")
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)

    report = structure.report()
    assert report is not None
    assert not report.candidates, "the fixture is meant to have nothing to sample"

    assert structure.example_values() is None
    assert structure.example_failure() == ""


def test_selecting_again_does_not_start_a_second_sample(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The table emits a selection change on every rebuild; each one must not spawn two
    more children."""
    source = document(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)

    structure.select_candidate(0)
    assert wait_until(qtbot, lambda: structure.example_values() is not None)
    first = structure.example_directory()

    structure.select_candidate(0)

    assert structure.example_directory() == first, "a second sampling chain was started"


def test_the_example_directory_is_kept_where_it_can_be_looked_at(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """The files are the evidence, so the panel says where they are."""
    source = document(tmp_path)
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)
    structure.select_candidate(0)
    assert wait_until(qtbot, lambda: structure.example_values() is not None)

    directory = structure.example_directory()

    assert directory is not None and directory.is_dir()
    assert (directory / "sample.csv").is_file()


# --- not leaving a trail ------------------------------------------------------


def test_the_whole_chain_works_on_a_namespaced_document(window: MainWindow, qtbot: QtBot) -> None:
    """**The realistic case, and the one that would notice a dropped namespace map.**

    Every element in this fixture is in a default namespace, so a field path only resolves
    if the document's namespace map reaches the config and from there the CLI. The
    hand-written documents used elsewhere in this file have no namespaces at all, and
    would pass just as happily with the map discarded somewhere along the way -- which is
    why the assertion at the end is that rows came out, not that the chain ran.
    """
    source = REPO / "tests" / "fixtures" / "extract_default_ns.xml"
    window.document_panel().open_document(source)

    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)
    report = structure.report()
    assert report is not None
    assert report.namespaces == {"": "urn:example:shop"}, "the fixture changed"

    select_path(structure, "/catalog/products/product")
    assert wait_until(
        qtbot, lambda: structure.example_values() is not None or structure.example_failure()
    ), "no example values arrived"
    assert_the_chain_succeeded(structure)

    fields = window.field_panel()
    fields.regenerate_from_candidate()
    # Waiting is new in 8A-7-FIX: the button now asks the CLI, so it is a child process
    # rather than a loop over the candidate already in hand.
    assert wait_until(qtbot, lambda: not fields.is_regenerating()), "the CLI never answered"
    assert fields.result() is not None and fields.result().ok, fields.message_text()
    assert fields.as_config_dict()["namespaces"] == report.namespaces, (
        "the map did not reach the config the panel would save"
    )

    preview = window.preview_panel()
    assert preview.config() is not None
    run_preview(preview, qtbot, 3)

    assert preview.table_row_count() > 0, (
        "no rows came out -- with a namespace on every element, that is what a dropped "
        "namespace map looks like"
    )
    name_column = preview.headers().index("name")
    names = [preview.cell(row, name_column) for row in range(preview.table_row_count())]
    assert names == ["Aurora Desk Lamp", "Pulse Audio Suite"], names


def select_report_index(panel, index: int) -> None:  # noqa: ANN001 - a StructurePanel
    """Select the row showing the report's candidate number ``index``.

    The view row and the report index are different numbers once the table has sorted
    itself, so this goes through the stored index rather than assuming they match.
    """
    table = panel._candidates
    for row in range(table.rowCount()):
        if table.item(row, 0).data(structure_module._REPORT_INDEX_ROLE) == index:
            panel.select_candidate(row)
            return
    raise AssertionError(f"candidate {index} is not in the table")


def test_a_chain_from_a_candidate_you_have_moved_past_does_not_report(
    window: MainWindow, qtbot: QtBot
) -> None:
    """**Clicking down the candidate list starts a chain per click.**

    The ones already passed are still running. Without a generation check the first to
    report back is shown against whichever row is selected by then -- so a failure
    belonging to one candidate appears beside another candidate's fields. That is what
    happened here before this test existed: the nested candidate's chain fails, because
    ``inspect --generate-config`` refuses a candidate that sits inside another, and its
    message was being displayed for the candidate the user had moved on to.
    """
    source = REPO / "tests" / "fixtures" / "extract_default_ns.xml"
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)
    report = structure.report()
    assert report is not None

    nested = next(index for index, item in enumerate(report.candidates) if item.is_nested)
    top = next(index for index, item in enumerate(report.candidates) if not item.is_nested)

    # **The one place a failure is expected, and the reason it is allowed.** A nested
    # candidate is one that sits inside another, and ``inspect --generate-config`` refuses
    # it -- so the chain started for ``nested`` is *supposed* to fail. What is being
    # checked is not that it succeeded but that its failure never reaches the screen: the
    # user has moved to ``top`` by the time it reports. This is why the assertion below is
    # ``example_failure() == ""`` rather than ``assert_the_chain_succeeded``.
    select_report_index(structure, nested)
    select_report_index(structure, top)
    assert structure._example_for == top, "the second selection did not take over"

    assert wait_until(
        qtbot,
        lambda: structure.example_values() is not None or structure.example_failure(),
    )
    # The chain that was abandoned must not have written anything.
    assert structure._example_for == top
    assert structure.example_failure() == "", "an abandoned chain reported"
    assert structure.example_values() is not None, "the row on screen has no values"


def test_moving_on_stops_the_chain_you_left_behind(
    window: MainWindow, qtbot: QtBot, big_document: pathlib.Path
) -> None:
    """A chain is two children, and a user clicking down the list starts one per click.

    Leaving the abandoned ones running would mean twenty candidates in a row leaves forty
    processes reading the same document. **The big document is what makes this a test**:
    with a small one the first child would have finished before the second selection, and
    the assertion would hold whether or not anything was stopped.
    """
    window.document_panel().open_document(big_document)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None, timeout_ms=180_000)
    report = structure.report()
    assert report is not None and len(report.candidates) >= 2, "need two to move between"

    structure.select_candidate(0)
    abandoned = structure._example_process
    assert abandoned is not None, "no chain started"
    assert abandoned.is_running, "the child finished too fast for this test to mean anything"

    structure.select_candidate(1)

    assert structure._example_process is not abandoned
    assert not abandoned.is_running, "the abandoned chain was left running"


def test_pressing_preview_again_samples_with_the_settings_as_they_are_now(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """**The second press must win.**

    Pressing Preview while one was running used to do nothing at all, which reads as the
    button being broken: the user changes the limit, presses it, and the table goes on
    showing the previous run. Same shape as the field panel's "From candidate", and fixed
    the same way -- the run in flight is stopped and replaced.

    Both presses happen before the event loop is given a turn, so the first run cannot have
    answered in between. That is what makes this deterministic rather than a race the test
    might win for the wrong reason.
    """
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    panel.set_limit(1)
    panel.preview()
    first = panel._process
    assert first is not None and first.is_running, "the first run never started"

    panel.set_limit(3)
    panel.preview()
    assert panel._process is not first, "the second press was ignored"

    assert wait_until(qtbot, lambda: panel.run_result() is not None), "no run finished"
    assert wait_until(qtbot, lambda: not panel._pump.isActive()), "the panel never drained"

    assert panel.table_row_count() == 3, "the replaced run's rows survived"
    assert not first.is_running, "the replaced run was left running"


def test_the_preview_says_where_its_files_are(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 2)

    directory = panel.run_directory()

    assert directory is not None and directory.is_dir()
    assert (directory / "sample.csv").is_file()
    assert (directory / "config.yaml").is_file()


def test_previewing_twice_leaves_one_directory_behind(
    window: MainWindow, qtbot: QtBot, tmp_path: pathlib.Path
) -> None:
    """A session that previews fifty times should not leave fifty directories in the
    temporary folder -- and the one it keeps is the one the rejected tab reads."""
    panel = window.preview_panel()
    panel.set_source(document(tmp_path))
    panel.set_config(CONFIG)

    run_preview(panel, qtbot, 1)
    first = panel.run_directory()
    run_preview(panel, qtbot, 1)
    second = panel.run_directory()

    assert first is not None and second is not None
    assert first != second
    assert not first.exists(), "the previous run's directory was left behind"
    assert second.is_dir()


def test_moving_between_candidates_leaves_one_directory_behind(
    window: MainWindow, qtbot: QtBot
) -> None:
    """The fixture, not the hand-written document: it has four candidates, and moving
    between two of them is what this test is about."""
    source = REPO / "tests" / "fixtures" / "extract_default_ns.xml"
    window.document_panel().open_document(source)
    structure = window.structure_panel()
    structure.analyze()
    assert wait_until(qtbot, lambda: structure.report() is not None)
    report = structure.report()
    assert report is not None and len(report.candidates) >= 2, "need two to move between"

    structure.select_candidate(0)
    assert wait_until(qtbot, lambda: structure.example_values() is not None)
    first = structure.example_directory()
    first_index = structure.selected_candidate_index()

    structure.select_candidate(1)
    assert wait_until(
        qtbot,
        lambda: (
            # Compared against the first index rather than against 1: the view row and the
            # report index are different numbers once the table has sorted itself.
            structure.selected_candidate_index() != first_index
            and (structure.example_values() is not None or structure.example_failure())
        ),
    )
    assert_the_chain_succeeded(structure)
    second = structure.example_directory()

    assert first is not None and second is not None
    assert first != second
    assert not first.exists(), "the previous candidate's directory was left behind"
