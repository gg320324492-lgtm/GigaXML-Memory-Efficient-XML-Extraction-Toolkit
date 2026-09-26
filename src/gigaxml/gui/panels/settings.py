"""The settings panel: the four preferences, and the fact that they are remembered.

**What this panel is for.** The store in `settings.py` is what makes a preference survive a
restart; this is the surface that lets a user set one. It reads once when it is built and
writes on every change, so there is no Save button to forget to press -- a preference that
needs saving is a preference people lose.

**It owns no timers and no child processes.** Nothing here starts anything; it edits a JSON
file. `shutdown()` exists anyway, because a panel without one is the mistake the twenty-eight
round quality line was spent on, and "this one does not need it" is exactly the reasoning
that produced that bug.
"""

from __future__ import annotations

import pathlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.i18n import tr
from gigaxml.gui.settings import (
    FORMATS,
    LANGUAGES,
    MAX_BATCH_SIZE,
    MIN_BATCH_SIZE,
    ON_ERROR,
    THEMES,
    Settings,
    SettingsStore,
)

__all__ = ["SettingsPanel"]

#: What the theme combo shows for each stored value. The stored value is what the rest of
#: the application switches on, so the labels are a display concern and live here.
_THEME_LABELS = {
    "system": "Follow the system",
    "light": "Light",
    "dark": "Dark",
}

#: What the language combo shows, keyed by the stored value. Each name is written in the
#: language it selects -- the one place a user who cannot read the current interface can
#: still find their own -- so these are deliberately *not* run through ``tr``.
_LANGUAGE_LABELS = {"en": "English", "zh": "中文"}


class SettingsPanel(QWidget):
    """The preferences, editable in place.

    Args:
        store: where they are kept. Injected, so a test can point it at a temporary file
            and a real run points it at the user's state directory.
    """

    #: Emitted with the new :class:`Settings` after any change is written. A window that
    #: wants to re-theme itself listens here rather than polling the file.
    settings_changed = Signal(object)

    def __init__(self, store: SettingsStore, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._store = store
        self._loading = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        current = store.read()

        self._batch_size = QSpinBox(self)
        self._batch_size.setRange(MIN_BATCH_SIZE, MAX_BATCH_SIZE)
        self._batch_size.setValue(current.batch_size)
        self._batch_size.setToolTip(
            tr(
                "How many records the CLI holds before writing. Larger is faster and uses more "
                "memory; the tool warns above the point where that matters."
            )
        )
        form.addRow(tr("Default batch size"), self._batch_size)

        # Every combo below carries its value in the item's data and every read goes
        # through ``currentData``: these values are written into the settings file and
        # handed to the CLI, so they are data, while the labels are the part translation
        # may rewrite. Reading them back through the label -- the way the theme used to be
        # recovered by matching against ``_THEME_LABELS`` -- would go silently wrong the
        # moment a label was translated, which is the same trap the execution panel's
        # ``currentText`` reads were.
        self._format = QComboBox(self)
        for value in FORMATS:
            self._format.addItem(tr(value), value)
        self._format.setCurrentIndex(self._format.findData(current.format))
        form.addRow(tr("Default output format"), self._format)

        self._on_error = QComboBox(self)
        for value in ON_ERROR:
            self._on_error.addItem(tr(value), value)
        self._on_error.setCurrentIndex(self._on_error.findData(current.on_error))
        form.addRow(tr("Default on error"), self._on_error)

        self._theme = QComboBox(self)
        for value in THEMES:
            self._theme.addItem(tr(_THEME_LABELS[value]), value)
        self._theme.setCurrentIndex(self._theme.findData(current.theme))
        form.addRow(tr("Theme"), self._theme)

        self._language = QComboBox(self)
        for value in LANGUAGES:
            self._language.addItem(_LANGUAGE_LABELS[value], value)
        self._language.setCurrentIndex(self._language.findData(current.language))
        form.addRow(tr("Language"), self._language)
        # Said where the choice is made, because this panel writes on every change and a
        # user who sees the rest of the interface stay in the old language is otherwise
        # looking at something that reads as broken. The panels render their strings when
        # they are built, so the new language arrives with the next launch.
        self._language_note = QLabel(tr("Takes effect after restart"), self)
        self._language_note.setWordWrap(True)
        form.addRow("", self._language_note)

        layout.addLayout(form)

        self._where = QLabel(tr("Kept in {}").format(store.store), self)
        self._where.setWordWrap(True)
        self._where.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._where)
        layout.addStretch(1)

        self._batch_size.valueChanged.connect(self._save)
        self._format.currentIndexChanged.connect(self._save)
        self._on_error.currentIndexChanged.connect(self._save)
        self._theme.currentIndexChanged.connect(self._save)
        self._language.currentIndexChanged.connect(self._save)

    # -- reading -----------------------------------------------------------

    def current(self) -> Settings:
        """The settings as the controls currently describe them."""
        return self._store.read()

    # -- writing -----------------------------------------------------------

    def _save(self) -> None:
        """Write every control's value and tell anyone listening.

        Every control is written rather than the one that changed, because the store does a
        read-modify-write of the whole file and a caller that only sent the change would
        leave the file's other fields at whatever they were before this panel was built.

        The re-entrancy guard is not decoration: writing a value back into a control emits
        its change signal, and without it the first edit would recurse until the stack ran
        out.
        """
        if self._loading:
            return
        self._loading = True
        try:
            updated = self._store.set(
                batch_size=self._batch_size.value(),
                format=self._format.currentData(),
                language=self._language.currentData(),
                on_error=self._on_error.currentData(),
                theme=self._theme.currentData(),
            )
        finally:
            self._loading = False
        self.settings_changed.emit(updated)

    # -- the panel contract ------------------------------------------------

    def shutdown(self) -> None:
        """Idempotent, like every other panel.

        There is no timer to stop and no child to kill here. It exists because a panel
        without a shutdown is the bug this project spent twenty-eight rounds on, and
        "this one does not need it" is how that bug was reasoned into existence.
        """
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True

    def store_path(self) -> pathlib.Path:
        """Where these preferences live. Exposed so a test can assert on the file."""
        return self._store.store
