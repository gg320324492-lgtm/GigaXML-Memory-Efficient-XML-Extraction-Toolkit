"""What a run produced, or what it left behind if it never finished.

Two states, and they are the two the error panel does not cover: a run that succeeded and
a run that was interrupted. A failure has its own panel next door -- the three are mutually
exclusive, and the window is what keeps them so.

**The interrupted state is the one this exists for.** A killed run exits non-zero with
nothing on stdout, nothing on stderr, and no report, so there was nothing to show and the
window showed nothing: it looked like a run that finished. The half-written output is on
disk the whole time, and this is what says so.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.i18n import tr
from gigaxml.gui.run_report import LeftBehind, RunOutcome

__all__ = ["ResultPanel"]

#: The colour the field panel already uses for something the user has to look at. A warning
#: that looks like ordinary status text is not a warning.
_WARNING_COLOUR = "color: #c0392b;"


def _wrapping_label(parent: QWidget) -> QLabel:
    """A label that wraps and lets the layout decide its width.

    The paths shown here are full Windows paths with no spaces in them, and a wrapping label
    still reports its longest unbroken token as its minimum width -- the mistake that once
    stretched the window to 4340 pixels.
    """
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(1)
    return label


def format_elapsed(seconds: float | None) -> str | None:
    """Seconds as something worth reading, or ``None`` when there is nothing to say."""
    if seconds is None:
        return None
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    return f"{seconds:.1f} s"


def count_of(number: int, noun: str) -> str:
    """``1 row`` but ``2 rows``, in the interface's language.

    The template is looked up whole -- ``{} row`` against ``{} rows`` -- because Chinese
    has no plural and the English plural is an ``s``: one lookup per language instead of
    a rule that has to know both grammars.
    """
    key = f"{{}} {noun}s" if number != 1 else f"{{}} {noun}"
    return tr(key).format(f"{number:,}")


class ResultPanel(QWidget):
    """Shown when a run finishes or is interrupted; empty the rest of the time."""

    #: Emitted with a path when the user presses the button that copies one. The window owns
    #: the clipboard, the same way it does for the error panel's advice.
    path_copy_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._outcome: RunOutcome | None = None
        self._left: LeftBehind | None = None
        self._unfinished = False

        layout = QVBoxLayout(self)

        self._headline = _wrapping_label(self)
        font = self._headline.font()
        font.setBold(True)
        self._headline.setFont(font)
        layout.addWidget(self._headline)

        self._detail = _wrapping_label(self)
        layout.addWidget(self._detail)

        self._path = _wrapping_label(self)
        layout.addWidget(self._path)

        buttons = QHBoxLayout()
        self._copy = QPushButton(self)
        self._copy.clicked.connect(self._on_copy)
        buttons.addWidget(self._copy)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.clear()

    # -- the two states ----------------------------------------------------

    def show_summary(self, outcome: RunOutcome | None) -> None:
        """A run that finished. ``outcome`` is ``None`` when there is no report to read.

        Still shown, because the exit code said it succeeded -- an absent report is a
        missing summary, not a missing run, and saying nothing would be the same silence
        this panel exists to remove.
        """
        self._outcome = outcome
        self._left = None
        self._unfinished = False
        if outcome is None:
            self._headline.setText(tr("finished"))
            self._detail.setText(tr("the tool wrote no summary for this run"))
            self._path.setText("")
            self._copy.setVisible(False)
        else:
            self._headline.setText(tr("finished — {}").format(count_of(outcome.rows, "row")))
            self._detail.setText("\n".join(self._summary_lines(outcome)))
            self._path.setText(str(outcome.output) if outcome.output else "")
            self._copy.setText(tr("Copy the output path"))
            self._copy.setVisible(outcome.output is not None)
        self._headline.setStyleSheet("")
        self.setVisible(True)

    def show_unfinished(self, left: LeftBehind | None) -> None:
        """A run that was stopped. ``left`` is what it left, if it left anything.

        The size is reported and the row count is not, except where a manifest recorded it:
        a half-written file's length says how far it got, and any count read out of it would
        be a guess dressed as a fact. A checkpointed run's manifest is a different matter --
        it *is* the record of how far the run got, written as each part was committed.
        """
        self._outcome = None
        self._left = left
        self._unfinished = True
        self._headline.setText(tr("this run did not finish"))
        self._headline.setStyleSheet(_WARNING_COLOUR)
        if left is None:
            self._detail.setText(
                tr("It was stopped before it wrote anything, so there is no output to look at.")
            )
            self._path.setText("")
            self._copy.setVisible(False)
        elif left.is_directory:
            self._detail.setText(_parts_detail(left))
            self._path.setText(str(left.path))
            self._copy.setText(tr("Copy the path of the parts directory"))
            self._copy.setVisible(True)
        else:
            self._detail.setText(_unfinished_detail(left.path))
            self._path.setText(str(left.path))
            self._copy.setText(tr("Copy the path of the partial output"))
            self._copy.setVisible(True)
        self.setVisible(True)

    def clear(self) -> None:
        """Back to nothing to say. Called when a new run starts or another panel speaks."""
        self._outcome = None
        self._left = None
        self._unfinished = False
        self._headline.setText("")
        self._headline.setStyleSheet("")
        self._detail.setText("")
        self._path.setText("")
        self._copy.setText("")
        self.setVisible(False)

    # -- reading back ------------------------------------------------------

    def outcome(self) -> RunOutcome | None:
        return self._outcome

    def left(self) -> LeftBehind | None:
        return self._left

    def is_unfinished(self) -> bool:
        return self._unfinished

    def headline_text(self) -> str:
        return self._headline.text()

    def detail_text(self) -> str:
        return self._detail.text()

    def path_text(self) -> str:
        return self._path.text()

    def copy_text(self) -> str:
        return self._copy.text()

    def is_warning(self) -> bool:
        """Whether the headline is wearing the warning colour. The visible form of
        "prominent", which is otherwise only a claim about the design."""
        return self._headline.styleSheet() == _WARNING_COLOUR

    # -- internals ---------------------------------------------------------

    def _summary_lines(self, outcome: RunOutcome) -> list[str]:
        lines: list[str] = []
        if outcome.rejected:
            # First, because a run that skipped records still reports success and this
            # count is the only thing that says otherwise. Phrased as a label rather than
            # a sentence so the count needs no verb: a template with "was"/"were" in it
            # would have to know both languages' agreement rules for no gain.
            where = (
                f" — {tr('see {}').format(outcome.rejected_path)}" if outcome.rejected_path else ""
            )
            lines.append(tr("rejected: {}").format(count_of(outcome.rejected, "record")) + where)
        else:
            lines.append(tr("nothing was rejected"))
        elapsed = format_elapsed(outcome.elapsed_seconds)
        if elapsed is not None:
            lines.append(tr("took {}").format(elapsed))
        if outcome.format:
            lines.append(tr("written as {}").format(outcome.format))
        return lines

    def _on_copy(self) -> None:
        target = (
            (self._left.path if self._left is not None else None)
            if self._unfinished
            else (self._outcome.output if self._outcome is not None else None)
        )
        if target is not None:
            self.path_copy_requested.emit(str(target))


def _parts_detail(left: LeftBehind) -> str:
    """What to say about a checkpointed run that stopped.

    The counts come from the manifest, which is written as each part is committed -- so they
    are what the tool recorded, not a count taken from a file that is by definition not
    finished. Saying "some parts" when the manifest says how many would be throwing away the
    one thing that makes a stopped checkpointed run recoverable.
    """
    if left.recorded_parts is None:
        return tr(
            "The parts it finished are in this directory. It is not the whole run: the "
            "manifest does not say the source was consumed."
        )
    rows = (
        ""
        if left.recorded_rows is None
        else tr(", {} rows in them").format(f"{left.recorded_rows:,}")
    )
    return tr(
        "The {} it finished are in this directory{}. It is not the whole run: the "
        "manifest says the source was not fully consumed, so what is here is where it "
        "got to."
    ).format(count_of(left.recorded_parts, "part"), rows)


def _unfinished_detail(partial: Path) -> str:
    """What to say about a half-written output.

    **The size is reported and the row count is not.** A partial file's length says how far
    the run got; any number of rows read out of it would be a guess dressed as a fact, and
    the file is by definition not a complete one. Zero bytes is its own sentence rather than
    "0 bytes of it", because a run stopped before the writer's first flush is a different
    thing from one stopped in the middle of writing.
    """
    size = _size_of(partial)
    if size == 0:
        return tr(
            "The output file was started, but the run was stopped before anything was "
            "written to it. The target still holds whatever it held before."
        )
    return tr(
        "Part of the output is on disk, {} of it. It is not the finished file: the run "
        "never got to the point of putting it in place, so the target still holds "
        "whatever it held before. Nothing else on the disk was changed."
    ).format(_human_size(size))


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _human_size(size: int) -> str:
    """A byte count in the unit that reads best."""
    if size < 1024:
        return tr("{} bytes").format(size)
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / (1024 * 1024):.1f} MB"
