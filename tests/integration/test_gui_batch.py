"""The batch panel's hard constraint: batch must not mean one process doing many files.

Three things are checked here, all of them about scheduling rather than extraction: each
job in the queue gets its own ``CliProcess`` and no argument list ever names more than one
document, ``shutdown()`` is idempotent, and ``shutdown()`` collects both the child and the
timer.
"""

from __future__ import annotations

import pathlib
from typing import ClassVar

import pytest

pytest.importorskip("PySide6")

from gigaxml.gui.cli_process import RunResult
from gigaxml.gui.panels.batch import BatchPanel
from gigaxml.gui.settings import FORMATS


class _FakeProcess:
    """A stand-in for ``CliProcess``: counts what it was built with, and reports success.

    Reporting success immediately is the point. The real class calls back from a reader
    thread, and what this test is after is scheduling -- when the next job gets its
    process -- not extraction.
    """

    built: ClassVar[list[list[str]]] = []

    def __init__(self, args: list[str], **kwargs: object) -> None:
        self.args = args
        self.killed = False
        self._on_finished = kwargs.get("on_finished")
        type(self).built.append(list(args))

    def start(self) -> None:
        if self._on_finished is not None:
            self._on_finished(RunResult(exit_code=0))

    def kill(self) -> None:
        self.killed = True


@pytest.fixture(autouse=True)
def _reset() -> None:
    _FakeProcess.built = []


def _panel(monkeypatch: pytest.MonkeyPatch, qtbot) -> BatchPanel:  # noqa: ANN001
    panel = BatchPanel()
    qtbot.addWidget(panel)
    monkeypatch.setattr("gigaxml.gui.panels.batch.CliProcess", _FakeProcess)
    return panel


def test_each_job_gets_its_own_process(monkeypatch: pytest.MonkeyPatch, qtbot) -> None:  # noqa: ANN001
    """Three documents, three processes.

    A queue that handed the CLI all three at once would hold three documents' records in
    memory at the same time, which is the thing this project exists not to do.
    """
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    panel.add_documents(["/tmp/a.xml", "/tmp/b.xml", "/tmp/c.xml"])

    for _ in range(3):
        panel._start_next()
        panel._drain()

    assert len(_FakeProcess.built) == 3


def test_no_invocation_names_two_documents(monkeypatch: pytest.MonkeyPatch, qtbot) -> None:  # noqa: ANN001
    """Stronger still: no argument list ever carries more than one source document."""
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    panel.add_documents(["/tmp/a.xml", "/tmp/b.xml"])

    panel._start_next()
    panel._drain()
    panel._start_next()

    assert len(_FakeProcess.built) == 2
    for args in _FakeProcess.built:
        assert len([item for item in args if item.endswith(".xml")]) == 1, args


def test_shutdown_is_idempotent(monkeypatch: pytest.MonkeyPatch, qtbot) -> None:  # noqa: ANN001
    """The panel contract the twenty-eight round quality line was spent on."""
    panel = _panel(monkeypatch, qtbot)
    panel.shutdown()
    assert panel._shut_down is True
    # A second call must not raise, and must not touch what the first one freed.
    panel.shutdown()
    assert panel._shut_down is True


def test_shutdown_kills_the_child(monkeypatch: pytest.MonkeyPatch, qtbot) -> None:  # noqa: ANN001
    """A child left running outlives the window that started it."""
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    panel.add_documents(["/tmp/a.xml"])
    panel._start_next()
    child = panel._process
    assert child is not None

    panel.shutdown()
    assert child.killed is True
    assert panel._process is None


def test_shutdown_stops_the_timer(monkeypatch: pytest.MonkeyPatch, qtbot) -> None:  # noqa: ANN001
    """A timer left running fires into a panel nobody is looking at."""
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    panel.add_documents(["/tmp/a.xml"])
    panel._start_next()
    assert panel._pump.isActive() is True

    panel.shutdown()
    assert panel._pump.isActive() is False


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_the_output_suffix_matches_the_chosen_format(
    monkeypatch: pytest.MonkeyPatch,
    qtbot,  # noqa: ANN001 -- pytest-qt's fixture
    fmt: str,
) -> None:
    """`-o` and `--format` have to agree, for every format the combo offers.

    **This is the same root cause as the bug this panel shipped with.** A run failed because
    ``-o`` named a path the writer could not use, and a run will fail the same way if ``-o``
    says ``rows.csv`` while ``--format`` says ``jsonl`` -- the CLI is explicit about the pair
    and says nothing a user could act on. The extension used to be a bare ``[]`` lookup into a
    hand-written dict, which turned "somebody added a format and forgot this file" into a
    ``KeyError`` on a user's machine instead of a test failure.
    """
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    panel._format.setCurrentText(fmt)

    args = panel.build_args(pathlib.Path("/tmp/a.xml"), pathlib.Path("/tmp/out/a-out"))

    output_flag = args[args.index("-o") + 1]
    format_flag = args[args.index("--format") + 1]
    assert format_flag == fmt
    assert output_flag.endswith(f"rows.{fmt}"), (
        f"format {fmt!r} writes to {output_flag!r}, which does not match"
    )


def test_every_format_in_the_settings_has_a_suffix() -> None:
    """The combo, the suffix table and the settings file cannot disagree.

    One test for the coupling rather than three: what matters is that adding a format to
    ``FORMATS`` reaches both the combo and ``_SUFFIXES`` without a second edit anywhere.
    """
    from gigaxml.gui.panels.batch import _SUFFIXES

    assert set(_SUFFIXES) == set(FORMATS), (
        f"the suffix table has {sorted(_SUFFIXES)} but the formats are {sorted(FORMATS)}"
    )
    for fmt in FORMATS:
        assert _SUFFIXES[fmt] == f".{fmt}"


def test_an_unknown_format_does_not_crash_build_args(
    monkeypatch: pytest.MonkeyPatch,
    qtbot,  # noqa: ANN001
) -> None:
    """A format with no suffix mapping falls back rather than raising ``KeyError``.

    Reachable from a preferences file written by a newer version of the application, so the
    value that lands here is not necessarily one this build knows about. Failing the run
    with a traceback would be the wrong answer; producing a ``.csv`` name is a defined one.
    """
    panel = _panel(monkeypatch, qtbot)
    panel._output.setText("/tmp/out")
    # A combo whose selection is not a known format -- the state after such a file is read.
    panel._format.addItem("parquetish")
    panel._format.setCurrentText("parquetish")

    args = panel.build_args(pathlib.Path("/tmp/a.xml"), pathlib.Path("/tmp/out/a-out"))
    assert args[args.index("-o") + 1].endswith("rows.csv")
