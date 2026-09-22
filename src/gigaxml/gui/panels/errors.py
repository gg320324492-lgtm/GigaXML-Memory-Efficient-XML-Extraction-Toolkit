"""What went wrong, what to do about it, and the raw output underneath.

A panel rather than a modal dialog. The advice is a thing to *act* on -- change a setting,
copy a path, go and look at the config -- and a modal would have to be dismissed before any
of that could happen. It also means this is testable without a modal event loop.

The raw output is one press away rather than always on screen: it is what someone reaches
for when the summary is not enough, and it is long enough that showing it by default would
bury the summary.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_FREE_TARGET,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
    Advice,
)
from gigaxml.gui.run_report import RunFailure

__all__ = ["ErrorPanel", "action_label_for"]

#: What the action button says, per advice kind. The wording is the whole point of the
#: button: "Learn more" would be a button that does nothing useful.
_ACTION_LABELS: dict[str, str] = {
    KIND_QUARANTINE: "Set on_error to quarantine",
    KIND_FREE_TARGET: "Copy the path of the complete output",
    KIND_NAMESPACES: "Go to the namespace panel",
    KIND_CHECK_CONFIG: "Go to the field panel",
}

#: Shown for an advice kind this build does not know. Reachable only if the advice module
#: grows a kind and the window does not, but a missing label would be a blank button.
_FALLBACK_LABEL = "Show the details"


def _wrapping_label(parent: QWidget) -> QLabel:
    """A label that wraps, and that lets the layout decide how wide it is.

    **``setWordWrap`` is not enough on its own.** A wrapping label still reports its longest
    unbroken run of text as its minimum width, and the messages here are full of Windows
    paths, which have no spaces in them -- so one ``WriterError`` pushed the window out to
    4340 pixels. ``Ignored`` says "the width is whatever the layout gives me", which is
    what wrapping is supposed to mean in the first place.

    Found by photographing the panel rather than by reading it: nothing about the code
    looks wrong, and no test was failing.
    """
    label = QLabel(parent)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
    label.setMinimumWidth(1)
    return label


def action_label_for(advice: Advice) -> str:
    """The button's text for this advice."""
    return _ACTION_LABELS.get(advice.kind, _FALLBACK_LABEL)


class ErrorPanel(QWidget):
    """Shown when a run fails; empty the rest of the time."""

    #: Emitted with the advice kind when the action button is pressed. The window decides
    #: what each kind does -- the panel does not know about tabs or the clipboard.
    action_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._failure: RunFailure | None = None
        self._advice: Advice | None = None

        layout = QVBoxLayout(self)

        self._headline = _wrapping_label(self)
        font = self._headline.font()
        font.setBold(True)
        self._headline.setFont(font)
        layout.addWidget(self._headline)

        self._detail = _wrapping_label(self)
        layout.addWidget(self._detail)

        # The type and the CLI's own message. The type is here because it is what someone
        # searching the project or the docs would look for; the message because rewording
        # it here would only make it less precise.
        self._kind_line = _wrapping_label(self)
        layout.addWidget(self._kind_line)

        self._message = _wrapping_label(self)
        layout.addWidget(self._message)

        buttons = QHBoxLayout()
        self._action = QPushButton(self)
        self._action.clicked.connect(self._on_action)
        buttons.addWidget(self._action)

        self._toggle = QToolButton(self)
        self._toggle.setText("Show the raw output")
        self._toggle.setCheckable(True)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.toggled.connect(self._on_toggled)
        buttons.addWidget(self._toggle)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._stderr = QPlainTextEdit(self)
        self._stderr.setReadOnly(True)
        self._stderr.setVisible(False)
        layout.addWidget(self._stderr)

        self.clear()

    # -- showing -----------------------------------------------------------

    def show_failure(self, failure: RunFailure, stderr_lines: list[str]) -> None:
        """Fill the panel in. ``stderr_lines`` is everything the child wrote to stderr."""
        self._failure = failure
        self._advice = advice_for_failure(failure)
        self._headline.setText(self._advice.headline)
        self._detail.setText(self._advice.detail)
        self._kind_line.setText(
            "the tool did not say what kind of failure this was"
            if failure.error_type is None
            else f"the tool called this: {failure.error_type}"
        )
        self._message.setText(failure.message)
        self._stderr.setPlainText("\n".join(stderr_lines))
        self._action.setText(action_label_for(self._advice))
        self._toggle.setVisible(bool(stderr_lines))
        self.setVisible(True)

    def clear(self) -> None:
        """Back to nothing to say. Called when a new run starts."""
        self._failure = None
        self._advice = None
        self._headline.setText("")
        self._detail.setText("")
        self._kind_line.setText("")
        self._message.setText("")
        self._stderr.setPlainText("")
        self._action.setText("")
        self._toggle.setChecked(False)
        self.setVisible(False)

    # -- reading back, for tests and for the window ------------------------

    def failure(self) -> RunFailure | None:
        return self._failure

    def advice(self) -> Advice | None:
        return self._advice

    def headline_text(self) -> str:
        return self._headline.text()

    def detail_text(self) -> str:
        return self._detail.text()

    def kind_text(self) -> str:
        return self._kind_line.text()

    def message_text(self) -> str:
        return self._message.text()

    def action_text(self) -> str:
        return self._action.text()

    def stderr_text(self) -> str:
        return self._stderr.toPlainText()

    def is_stderr_expanded(self) -> bool:
        """Whether the raw output is showing. The toggle's state, not ``isVisible`` --
        a widget inside a window that was never shown is invisible either way."""
        return self._toggle.isChecked()

    # -- internals ---------------------------------------------------------

    def _on_action(self) -> None:
        if self._advice is not None:
            self.action_requested.emit(self._advice.kind)

    def _on_toggled(self, checked: bool) -> None:
        self._stderr.setVisible(checked)
        self._toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)
        self._toggle.setText("Hide the raw output" if checked else "Show the raw output")


def advice_for_failure(failure: RunFailure) -> Advice:
    """The advice for a failure, from its type. Thin, and here so the panel has one import."""
    from gigaxml.gui.error_advice import advice_for

    return advice_for(failure.error_type)
