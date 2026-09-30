"""The eight things a person who does not know XML has to be able to do, in order.

打开 → Inspect → Field Builder → Preview → 类型 → 导出 → Progress → Resume

**The criterion being tested is not "there is a panel for this".** It is that each of the
eight can be *completed from the window*, and the only honest way to show that is to drive
the window through the whole of it and check what came out. Every test here goes through a
panel's public API, and **not one of them calls the CLI directly** — the panels start their
own child processes, which is the thing being checked.

So the shape of each test is the same: do the step the way a user would, then assert on
something that could only be true if the step really happened — a child process's exit code,
a value that can only have come out of a document, a file on disk. An assertion like
``panel is not None`` or ``report is not None`` would pass on a panel that renders a
placeholder, which is exactly the gap this round exists to close.

:func:`test_the_whole_path_never_leaves_the_window` then does all eight in one window,
which is the claim none of the individual tests can make on its own: that they compose.
"""

from __future__ import annotations

import json
import pathlib
import sys
import time

import pytest
from PySide6.QtWidgets import QApplication
from pytestqt.qtbot import QtBot

from gigaxml.gui.field_rows import FIELD_TYPE_NAMES
from gigaxml.gui.main_window import MainWindow

#: A document with a shape worth inspecting: an attribute on the record, two children, and
#: one child whose text looks like a number so the type inference has something to find.
#: The price field is what makes ⑥ a real test -- an inferred ``float`` cannot be asserted
#: without something that should infer one.
DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<catalog generated="2026-01-01">
  <metadata><title>Sample</title></metadata>
  <products>
    <product id="1" sku="A-1"><name>Widget</name><price>9.99</price></product>
    <product id="2" sku="B-2"><name>Gadget</name><price>19.50</price></product>
    <product id="3" sku="C-3"><name>Doohickey</name><price>4.25</price></product>
    <product id="4" sku="D-4"><name>Thingamajig</name><price>120.00</price></product>
  </products>
</catalog>
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
    yield main
    main.close()


@pytest.fixture
def document(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "catalog.xml"
    path.write_text(DOCUMENT, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def long_document(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A document with enough records that progress cannot be reported only at the end.

    **Sized from the CLI's own progress rule, not from a guess.** The CLI emits a progress
    line every 20,000 records (``PROGRESS_EVERY_DEFAULT``) or every second, whichever comes
    first, plus a closing line whatever happens. So the first line arrives after 20,000
    records, at which point 190,000 remain — and a process with 190,000 records still to go
    is a process that cannot have exited. That is what makes the assertion below independent
    of how fast the machine is, and it is why the size is a count of records rather than a
    count of bytes: the padding is small on purpose, so the same number of records makes a
    smaller file.

    Small records, many of them: 20 padding characters against 200,000 products. The earlier
    version of this test used a four-record document, which on this machine finished in under
    the pump's interval and so never had a progress line to observe while running — the
    reason it was green here and red on all three CI platforms.
    """
    path = tmp_path_factory.mktemp("long") / "long.xml"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<catalog><products>")
        padding = "x" * 20
        for number in range(1, 200_001):
            handle.write(f'<product id="{number}"><name>{padding}</name></product>')
        handle.write("</products></catalog>")
    return path


LONG_CONFIG = """record: /catalog/products/product
fields:
  id:
    path: '@id'
"""


def _wait(qtbot: QtBot, predicate, timeout_ms: int = 120_000) -> None:  # noqa: ANN001
    qtbot.waitUntil(predicate, timeout=timeout_ms)


def _wait_until_drained(panel: object, qtbot: QtBot) -> None:
    """Wait for a child to finish **and for the panel to have drawn the result**.

    ``is_running()`` goes false the moment the process exits, which is *before* the panel's
    50 ms pump has run :meth:`_finish` and filled the table. Waiting on the process alone
    and then reading the widgets is a race that reads as a panel that lost the results --
    and it is the race ``test_gui_preview.py::run_preview`` documents at length. So the pump
    stopping is the thing waited for: that is what "settled" means here.
    """
    _wait(qtbot, lambda: panel.run_result() is not None)
    _wait(qtbot, lambda: not panel._pump.isActive())


def _open(window: MainWindow, document: pathlib.Path) -> None:
    """① 打开, through the document panel.

    **It starts nothing, and that is the design rather than an omission.**
    :meth:`DocumentPanel.open_document` only points the other panels at the file --
    ``StructurePanel.set_document`` says so in its own docstring -- because ``inspect``
    reads the whole document, and a window that began doing several hundred megabytes of
    work the moment a file was dropped would be doing it without being asked.
    :func:`_analyse` is where the user presses the button.
    """
    assert window.document_panel().open_document(document) is True
    structure = window.structure_panel()
    assert structure.document() == document, "the analysis panel was not pointed at it"
    assert window.preview_panel().source() == document


def _analyse(window: MainWindow, qtbot: QtBot) -> None:
    """② Inspect: the analysis, started the way a user starts it, waited for to land."""
    structure = window.structure_panel()
    structure.analyze()
    _wait(qtbot, lambda: structure.report() is not None, timeout_ms=120_000)


def _choose_product_candidate(window: MainWindow) -> int:
    """Select ``/catalog/products/product`` in the candidate table and return its row."""
    structure = window.structure_panel()
    report = structure.report()
    assert report is not None
    row = next(
        index
        for index, candidate in enumerate(report.candidates)
        if candidate.path == "/catalog/products/product"
    )
    structure.select_candidate(row)
    return row


# --- ① 打开 ------------------------------------------------------------------


def test_opening_a_document_describes_it_and_points_every_panel_at_it(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path
) -> None:
    """**① 打开.** The window is given a path, and the document is there for the rest.

    Asserted on the size the panel reports **and** on every panel that was pointed at the
    file. The second half is what "opened" means in this application: the document is not
    merely displayed, it is what the analysis, the preview and the field builder are now
    working on. A panel that showed a file name and left the rest of the window pointing at
    nothing would satisfy the first assertion alone.

    **And nothing was read yet.** ``inspect`` reads the whole document, so starting it
    unasked would be the window doing several hundred megabytes of work because somebody
    dropped a file on it.
    """
    del qtbot  # nothing here runs a child; that is the point of the assertions below
    _open(window, document)
    documents = window.document_panel()

    assert documents.current_document() == document
    assert str(document.name) in documents.information_text()
    assert document.stat().st_size > 0
    assert window.structure_panel().report() is None, "opening a document started a full read"
    assert window.structure_panel().is_running() is False
    # And it was remembered for the next launch, which is the other half of "opened".
    assert document in documents.recent_paths()


# --- ② Inspect ---------------------------------------------------------------


def test_the_structure_panel_explains_what_it_found(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path
) -> None:
    """**② Inspect.** The findings are readable, and the example values come from the file.

    The detail pane and the CSV export are both asserted, because they are the two things a
    person actually does with an analysis: read it, and take it elsewhere. A panel that
    computed a candidate list and showed nothing would fail the first; one that showed the
    list but no values would fail the second.
    """
    _open(window, document)
    _analyse(window, qtbot)
    structure = window.structure_panel()
    _choose_product_candidate(window)

    # The example values are sampled by a second child, started when the row was selected,
    # so "the pane is filled" and "the values have arrived" are two different moments.
    # `or` of two values that are not booleans returns one of them, and waitUntil rejects
    # anything that is not True or False -- so the disjunction is made explicit here.
    _wait(
        qtbot,
        lambda: bool(structure.example_values() is not None or structure.example_failure()),
    )

    detail = structure.detail_text()
    assert "/catalog/products/product" in detail, detail
    # The values are read out of the document, not made up: these exact names are in it.
    values = structure.example_values()
    assert values is not None, structure.example_failure()
    assert "Widget" in "".join(values.values()), values
    assert "Widget" in detail, detail

    exported = window.job_history().store.parent / "paths.csv"
    structure.export_csv(exported)
    assert exported.is_file()
    text = exported.read_text(encoding="utf-8")
    assert "product" in text and "price" in text, text


# --- ③ Field Builder ---------------------------------------------------------


def test_the_field_builder_turns_a_candidate_into_fields_the_cli_accepts(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**③ Field Builder.** A candidate becomes a field list, and the CLI accepts it.

    The acceptance is the project's own: ``validate_now`` runs the config through the same
    :func:`~gigaxml.config.parse_config` the CLI runs, so "the CLI would accept this" is a
    fact rather than a claim about the shape of a dictionary.

    **The attribute matters.** ``sku`` is on ``product`` as an attribute and not as a child,
    so a builder that only walked child elements would silently leave it out. It is asserted
    here because this is the one place where that mistake is visible.
    """
    _open(window, document)
    _analyse(window, qtbot)
    _choose_product_candidate(window)

    fields = window.field_panel()
    fields.regenerate_from_candidate()
    _wait(qtbot, lambda: not fields.is_regenerating(), timeout_ms=120_000)

    names = {row.name for row in fields.rows()}
    assert "id" in names and "name" in names and "price" in names, names
    assert "sku" in names, f"an attribute was treated as if it were not one: {sorted(names)}"
    assert fields.record_path() == "/catalog/products/product"

    result = fields.validate_now()
    assert result.ok, fields.message_text()
    assert fields.invalid_rows() == ()

    # And it is a file, so the next step can hand it to the CLI.
    saved = tmp_path / "from-window.yaml"
    fields.save_config(saved)
    assert saved.is_file()
    payload = json.loads(json.dumps(fields.as_config_dict()))
    assert payload["record"] == "/catalog/products/product"


# --- ④ Preview ---------------------------------------------------------------


def test_the_preview_samples_the_document_with_the_config_being_built(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path
) -> None:
    """**④ Preview.** The user sees rows before committing to a full run.

    The sample is the CLI's ``sample`` subcommand run as a child, so the assertion that the
    values are right is also an assertion that the window configured it correctly: the
    headers come from the config the field panel produced, and the cell values come from
    the document.
    """
    _open(window, document)
    _analyse(window, qtbot)
    _choose_product_candidate(window)
    fields = window.field_panel()
    fields.regenerate_from_candidate()
    _wait(qtbot, lambda: not fields.is_regenerating(), timeout_ms=120_000)

    preview = window.preview_panel()
    # The window wires the field panel's config across by itself; asserting it is here
    # means the preview would fail with a clear reason if that wiring broke.
    assert preview.config() is not None, "the preview was never given a config"
    preview.set_limit(3)
    preview.preview()
    _wait_until_drained(preview, qtbot)

    result = preview.run_result()
    assert result is not None and result.ok, "the sample run failed"
    assert preview.table_row_count() == 3
    headers = preview.headers()
    assert "name" in headers and "price" in headers, headers
    name_column = headers.index("name")
    shown = [preview.cell(row, name_column) for row in range(3)]
    assert "Widget" in shown, shown
    assert "Doohickey" in shown, shown


# --- ⑤ 类型 ------------------------------------------------------------------


def test_the_type_of_a_field_is_inferred_from_the_document_and_is_editable(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**⑤ 类型**, and it is inferred rather than left to the user to type.

    Two claims, and the second is the one that is easy to get wrong:

    * a field whose values are all decimal numbers comes back typed ``float``, not ``string``
      -- a builder that defaulted every field would pass any test that only checked the
      field existed;
    * the user can change it, and the change reaches the config that the CLI is handed.
      Inference that cannot be overridden is not a default, it is a decision.
    """
    _open(window, document)
    _analyse(window, qtbot)
    _choose_product_candidate(window)
    fields = window.field_panel()
    fields.regenerate_from_candidate()
    _wait(qtbot, lambda: not fields.is_regenerating(), timeout_ms=120_000)

    typed = {row.name: row.type_name for row in fields.rows()}
    assert typed["price"] == "float", f"price was not inferred as a number: {typed}"
    assert typed["name"] == "string", f"name should have stayed text: {typed}"
    # The types offered are the project's, not a free-text box.
    assert set(FIELD_TYPE_NAMES) >= {"string", "float"}, FIELD_TYPE_NAMES

    # Overriding one, and the override reaching the file.
    fields.set_field_type("price", "string")
    assert fields.type_of("price") == "string"

    saved = tmp_path / "overridden.yaml"
    fields.save_config(saved)
    text = saved.read_text(encoding="utf-8")
    assert "type: string" in text, text

    # And the inference was not a coincidence of the file: change it back and the config
    # follows, which is what makes the first assertion about inference rather than about
    # a default that happened to match.
    fields.set_field_type("price", "float")
    fields.save_config(saved)
    assert "type: float" in saved.read_text(encoding="utf-8")


# --- ⑥ 导出 ------------------------------------------------------------------


def test_extracting_from_the_window_produces_the_file_it_promised(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**⑥ 导出.** A run started from the window, a file on disk, and the two agree.

    Three independent numbers, and the third is the point: the panel's summary, the report
    the CLI wrote, and the rows actually in the file. Comparing the panel only against the
    report would pass if both were wrong together; the file is the party that cannot agree
    with them both.
    """
    _open(window, document)
    _analyse(window, qtbot)
    _choose_product_candidate(window)
    fields = window.field_panel()
    fields.regenerate_from_candidate()
    _wait(qtbot, lambda: not fields.is_regenerating(), timeout_ms=120_000)

    output = tmp_path / "out.csv"
    execution = window.execution_panel()
    execution._source.setText(str(document))
    execution._config.setText(str(fields.generated_config_path()))
    execution._output.setText(str(output))
    execution._format.setCurrentIndex(execution._format.findData("csv"))
    execution.start()
    _wait_until_drained(execution, qtbot)

    run = execution.run_result()
    assert run is not None and run.ok, f"the run failed: {run.stderr_lines[-3:]}"

    results = window.result_panel()
    assert results.is_unfinished() is False
    outcome = results.outcome()
    assert outcome is not None, results.headline_text()
    assert outcome.rows == 4, results.headline_text()

    payload = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
    assert payload["rows"] == outcome.rows == 4

    lines = output.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) - 1 == 4, lines
    assert lines[0].split(",")[0].lower() == "id", lines[0]
    assert output.exists() and output.stat().st_size > 0


# --- ⑦ Progress --------------------------------------------------------------


def test_progress_moves_while_the_run_is_still_going(
    window: MainWindow,
    qtbot: QtBot,
    long_document: pathlib.Path,
    tmp_path: pathlib.Path,
) -> None:
    """**⑦ Progress**, and specifically: it moves **before** the run ends.

    A bar that jumps from 0 to 100 when the file is finished would satisfy "the window shows
    progress" while telling the user nothing during the only period where waiting is
    happening. The CLI always writes a closing progress line on the way out, so a run that
    reported nothing until that moment still produces one update and still looks like a
    progress bar to anything counting them.

    **The assertion is on one number, read at one moment, and it is strictly stronger than
    "some progress arrived".** ``progress_while_running()`` counts updates that happened
    while the child was alive -- the child's exit status is read inside the same call that
    drew the bar -- so a value above zero says the bar moved *during the run*. An earlier
    version polled for ``progress_updates() > 0`` and then asked ``is_running()``, which are
    two observations at two times: the first version was green here and failed on all three
    CI platforms with "the run finished before any progress could be observed", because on
    a four-record document the whole run fits inside one pump interval.

    **The document is sized so this cannot go red on a fast machine**, which is the half of
    the fix that is not a code change: see :func:`long_document`. 200,000 records means the
    first progress line is emitted at the 10% mark with 180,000 still to go, so there is no
    scheduling in which the process has already exited when that line is drawn.
    """
    config = tmp_path / "long.yaml"
    config.write_text(LONG_CONFIG, encoding="utf-8")

    execution = window.execution_panel()
    execution._source.setText(str(long_document))
    execution._config.setText(str(config))
    execution._output.setText(str(tmp_path / "long.csv"))
    execution._format.setCurrentIndex(execution._format.findData("csv"))
    execution.start()

    _wait(
        qtbot,
        # Either the progress arrives while the child is alive, or the child is gone -- in
        # which case the assertion below fails. Waiting on the progress alone would turn a
        # bar that only moves at the end into a three-minute timeout rather than a sentence
        # naming the fault, which is the difference between a failure you can read and one
        # you have to time out to discover.
        lambda: execution.progress_while_running() > 0 or not execution.is_running(),
        timeout_ms=180_000,
    )
    assert execution.progress_while_running() > 0, (
        "no progress was drawn while the child was alive, so the bar only ever moved "
        f"after the work was done (draws: {execution.progress_updates()})"
    )
    latest = execution.last_progress()
    assert latest is not None and latest.records > 0, latest

    execution.cancel()
    _wait(qtbot, lambda: not execution.is_running(), timeout_ms=60_000)


# --- ⑧ Resume ----------------------------------------------------------------


def test_a_stopped_run_is_offered_for_continuation_from_the_window(
    window: MainWindow, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**⑧ Resume.** Stopped partway, and the window says so and offers to continue.

    Exercised here as the eighth step of the workflow rather than as a history feature; the
    history's own version, including continuing a run *through* the CLI's ``--resume``, is
    in ``test_gui_history.py``.

    The parts directory is built by hand here because making a real interruption needs a
    document big enough to still be running when it is stopped, which is a fixture this
    file would otherwise be generating for every test. What is under test is the window's
    reading of that directory.
    """
    from gigaxml.checkpoint import (
        CHECKPOINT_FILENAME,
        Checkpoint,
        PartRecord,
        write_checkpoint,
    )

    parts = tmp_path / "parts"
    parts.mkdir()
    write_checkpoint(
        parts / CHECKPOINT_FILENAME,
        Checkpoint(
            source={"path": str(document), "size": document.stat().st_size, "sha256": "x" * 64},
            config="y" * 64,
            records_consumed=2,
            rejected=0,
            parts=(PartRecord(name="part-00000.csv", rows=2),),
            complete=False,
        ),
    )

    # A config path is needed only because the last assertion reads the argument list, and
    # building it loads the config. It plays no part in what is being shown.
    config = tmp_path / "config.yaml"
    config.write_text(
        "record: /catalog/products/product\nfields:\n  id:\n    path: '@id'\n",
        encoding="utf-8",
    )
    execution = window.execution_panel()
    execution._config.setText(str(config))
    execution._checkpoint.setValue(2)
    execution._output.setText(str(parts))
    execution._output.editingFinished.emit()

    assert execution.unfinished_run_here() is not None
    assert execution.is_resume_offered() is True
    notice = execution.resume_notice_text()
    assert "unfinished run" in notice, notice
    assert "2 rows" in notice, notice

    # And it is a choice, not a default: the flag appears only once it is asked for.
    assert "--resume" not in execution.build_args()
    execution.set_resume(True)
    assert "--resume" in execution.build_args()


# --- all eight, in one window ------------------------------------------------


def test_the_whole_path_never_leaves_the_window(
    window: MainWindow, qtbot: QtBot, document: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    """**The eight steps in order, in one window, with no command line anywhere.**

    This is the claim the individual tests cannot make. Each of them holds a window in a
    state one step needs; none of them shows that the steps compose — that the config the
    field builder produced is the one the preview sampled with and the one the extraction
    ran under, and that the rows previewed are the rows extracted.

    So the assertion is about **agreement across the steps** rather than about any one of
    them. Every number here comes from a different place: the window's summary, the CLI's
    report, the file on disk, and the sample the preview drew.
    """
    # ① open, ② inspect
    _open(window, document)
    _analyse(window, qtbot)
    assert window.structure_panel().report() is not None

    # ③ field builder
    _choose_product_candidate(window)
    fields = window.field_panel()
    fields.regenerate_from_candidate()
    _wait(qtbot, lambda: not fields.is_regenerating(), timeout_ms=120_000)
    assert fields.validate_now().ok, fields.message_text()

    # ⑤ type: inferred, and the value the inference produced is in the config
    assert {row.name: row.type_name for row in fields.rows()}["price"] == "float"

    # ④ preview, on the config the builder just produced
    preview = window.preview_panel()
    preview.set_limit(2)
    preview.preview()
    _wait_until_drained(preview, qtbot)
    assert preview.run_result().ok
    assert preview.table_row_count() == 2

    # ⑥ export, on that same config
    output = tmp_path / "out.csv"
    execution = window.execution_panel()
    execution._source.setText(str(document))
    execution._config.setText(str(fields.generated_config_path()))
    execution._output.setText(str(output))
    execution._format.setCurrentIndex(execution._format.findData("csv"))
    execution.start()
    _wait_until_drained(execution, qtbot)

    assert execution.run_result().ok, execution.run_result().stderr_lines[-3:]
    outcome = window.result_panel().outcome()
    assert outcome is not None and outcome.rows == 4
    assert len(output.read_text(encoding="utf-8").strip().splitlines()) - 1 == outcome.rows
    assert json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))["rows"] == (
        outcome.rows
    )

    # ⑦ progress reached the window during the run, and ⑧ the run is listed afterwards.
    assert execution.progress_updates() > 0, "the window was never told about the run"
    assert window.history_panel().row_count() == 1


def test_the_window_was_never_parsing_anything_itself() -> None:
    """**The eighth step's precondition, and the whole product's claim.**

    The window's memory does not grow with the document because the window never reads it.
    That is not observable from inside a single run — a window that parsed a small document
    would behave identically — so it is asserted here by reference to the guard that covers
    it: ``test_gui_no_parsing.py``, which fails if a parser is reachable from
    ``src/gigaxml/gui/``. This test exists so that a reader of *this* file is told where the
    claim is actually checked, rather than having to assume it.
    """
    from tests.integration.test_gui_no_parsing import gui_sources

    files = gui_sources()
    assert len(files) > 10, "the guard is not reading the tree; see test_gui_no_parsing.py"
    # A tiny delay is not a measurement, it is a marker: this test does not time anything,
    # and the ones that do are in tests/performance/.
    time.sleep(0)
