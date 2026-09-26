"""The document panel: choose a document, remember it, describe it.

**The one thing this panel must not do is pretend.** A drop handler that sets
``setAcceptDrops(True)`` and never looks at ``QMimeData`` will pass any test that checks
the attribute, and will do nothing at all when a user drags a file onto it. So the drop
path reads ``mimeData().urls()``, and it goes through exactly the same method the file
dialog and the recent-files list go through -- there is one way to open a document, and
three ways to reach it. That is also why the reverse control is worth having: dropping
``report.xml`` from a different directory has to open *that* file, not a same-named one
already in the list.

**Refusals are visible.** Dropping a directory, a ``.txt``, or a path that is not there
leaves the current document alone and says why. Qt signals refusal through
``event.isAccepted()`` being false, and the panel says it in words as well, because a
silent refusal is indistinguishable from a hang.

**The recent list is written to disk by the store, not by this widget.** See
:mod:`gigaxml.gui.recent_files` for why the location is injected.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gigaxml.gui.document_info import DOCUMENT_SUFFIXES, describe, looks_like_document
from gigaxml.gui.i18n import tr
from gigaxml.gui.recent_files import RecentFiles

#: Where the path is kept on a recent-files row. Stored rather than parsed back out of the
#: label, because the label carries a marker for a missing file and would then have to be
#: un-marked to recover the path.
_PATH_ROLE = Qt.ItemDataRole.UserRole


def first_local_file(mime: object) -> Path | None:
    """The first local file in a drop, or ``None`` if there is not one.

    Separate from the widget so the rule -- which URLs count, and what happens to a
    non-local one -- can be exercised without constructing an event. A ``file://`` URL is
    the only form a desktop drag produces; anything else (text, an HTTP URL) is not a
    document and is refused by returning ``None``.
    """
    urls = getattr(mime, "urls", None)
    if urls is None:
        return None
    for url in urls():
        if url.isLocalFile():
            return Path(url.toLocalFile())
    return None


class DocumentPanel(QWidget):
    """Pick a document, keep a list of recent ones, show what is known about it."""

    #: Emitted with the path of the document that is now open. The structure panel
    #: listens, so opening a document from anywhere updates everything that shows it.
    document_changed = Signal(str)

    def __init__(self, recent: RecentFiles, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._recent = recent
        self._path: Path | None = None

        self.setAcceptDrops(True)
        self._build()
        self._show_info()
        self._refresh_recent()

    # -- construction ------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)

        open_row = QHBoxLayout()
        self._open = QPushButton(tr("Open document…"), self)
        self._open.clicked.connect(self.choose_document)
        open_row.addWidget(self._open)
        open_row.addStretch(1)
        layout.addLayout(open_row)

        # Filled by `_show_info`, which owns the wording for both the empty and the
        # populated case -- two places to write "no document open" is one place too many.
        self._info = QLabel("", self)
        self._info.setWordWrap(True)
        self._info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._info.setObjectName("document_info")
        layout.addWidget(self._info)

        recent_box = QGroupBox(tr("Recent documents"), self)
        recent_layout = QVBoxLayout(recent_box)
        self._list = QListWidget(self)
        self._list.setObjectName("recent_list")
        self._list.itemActivated.connect(self._open_recent_item)
        recent_layout.addWidget(self._list)

        buttons = QHBoxLayout()
        self._forget = QPushButton(tr("Remove from list"), self)
        self._forget.clicked.connect(self._remove_selected)
        self._clear = QPushButton(tr("Clear list"), self)
        self._clear.clicked.connect(self._clear_recent)
        buttons.addStretch(1)
        buttons.addWidget(self._forget)
        buttons.addWidget(self._clear)
        recent_layout.addLayout(buttons)

        self._recent_note = QLabel("", self)
        self._recent_note.setWordWrap(True)
        recent_layout.addWidget(self._recent_note)
        layout.addWidget(recent_box)
        layout.addStretch(1)

        self._drag_hint = QLabel(tr("or drag an .xml or .xml.gz file onto this window"), self)
        self._drag_hint.setObjectName("drag_hint")
        layout.addWidget(self._drag_hint)

    # -- opening -----------------------------------------------------------

    def choose_document(self) -> None:
        """The file dialog. Offers exactly the suffixes a drop is allowed to be."""
        chosen, _ = QFileDialog.getOpenFileName(
            self,
            tr("Open a document"),
            "",
            tr("XML documents (*.xml *.xml.gz);;All files (*)"),
        )
        if chosen:
            self.open_document(Path(chosen))

    def open_document(self, path: Path | str) -> bool:
        """Open ``path`` if it is a document. Returns whether it was opened.

        The single entry point for all three routes. A refusal changes nothing and says
        why; the current document stays open, because a failed drop that also closed what
        the user was looking at would be worse than doing nothing.
        """
        target = Path(path)
        if not looks_like_document(target):
            suffixes = f" {tr('or')} ".join(DOCUMENT_SUFFIXES)
            self._info.setText(
                tr("not a document: {} (expected an existing {} file)").format(target, suffixes)
            )
            return False

        self._path = target
        self._recent.add(target)
        self._refresh_recent()
        self._show_info()
        self.document_changed.emit(str(target))
        return True

    def current_document(self) -> Path | None:
        """The open document, or ``None``. What a drop is judged by."""
        return self._path

    def recent_paths(self) -> tuple[Path, ...]:
        """What the list is showing, in order. For the panel's own callers."""
        return self._recent.paths()

    def store_path(self) -> Path:
        """The file the list is kept in. Exposed so a caller can tell the user where."""
        return self._recent.store

    def forget(self, path: Path | str) -> None:
        """Drop one entry. The only operation that removes something the user added."""
        self._recent.remove(path)
        self._refresh_recent()

    def clear_recent(self) -> None:
        """Drop every entry."""
        self._recent.clear()
        self._refresh_recent()

    def refresh(self) -> None:
        """Re-read the store. What makes a document that vanished show as missing."""
        self._refresh_recent()

    # -- what the widgets are showing, for callers that need to check ---------

    def information_text(self) -> str:
        return self._info.text()

    def recent_list_text(self) -> str:
        return "\n".join(
            self._list.item(row).text()  # type: ignore[union-attr]
            for row in range(self._list.count())
        )

    def recent_note_text(self) -> str:
        return self._recent_note.text()

    # -- the recent list ---------------------------------------------------

    def _refresh_recent(self) -> None:
        """Rebuild the list from the store, marking entries whose file is gone."""
        self._list.clear()
        missing = 0
        for entry in self._recent.entries():
            item = QListWidgetItem(
                entry.name if entry.exists else tr("{}  (missing)").format(entry.name)
            )
            item.setData(_PATH_ROLE, str(entry.path))
            item.setToolTip(str(entry.path))
            if not entry.exists:
                missing += 1
                item.setForeground(Qt.GlobalColor.gray)
            self._list.addItem(item)
        if missing:
            # Said in words as well as in grey, because colour alone is not a message and
            # a user whose drive is unplugged deserves to be told what happened.
            self._recent_note.setText(
                tr(
                    "{} of these files are not there at the moment. They are kept so you "
                    "can see what you had; opening one will say so rather than fail silently."
                ).format(missing)
            )
        else:
            self._recent_note.setText("")

    def _selected_path(self) -> Path | None:
        item = self._list.currentItem()
        if item is None:
            return None
        stored = item.data(_PATH_ROLE)
        return Path(stored) if isinstance(stored, str) else None

    def _open_recent_item(self, item: QListWidgetItem) -> None:
        stored = item.data(_PATH_ROLE)
        if isinstance(stored, str):
            self.open_document(Path(stored))

    def _remove_selected(self) -> None:
        path = self._selected_path()
        if path is not None:
            self.forget(path)

    def _clear_recent(self) -> None:
        self.clear_recent()

    # -- the information bar ----------------------------------------------

    def _show_info(self) -> None:
        if self._path is None:
            self._info.setText(tr("no document open"))
            return
        info = describe(self._path)
        when = info.modified.strftime("%Y-%m-%d %H:%M:%S") if info.modified else tr("unknown")
        parts = [
            str(info.path),
            f"{info.size_mib:.2f} MiB ({info.size_bytes:,} bytes)",
            tr("modified {}").format(when),
        ]
        note = info.estimate_note()
        if note:
            parts.append(note)
        self._info.setText("\n".join(parts))

    # -- dropping ----------------------------------------------------------

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802 (Qt naming)
        """Accept the drag only if it carries something this panel would open.

        Saying yes here and no on the drop would make the cursor promise something the
        panel does not intend to do.
        """
        candidate = first_local_file(event.mimeData())
        if candidate is not None and looks_like_document(candidate):
            event.acceptProposedAction()
            return
        event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:  # noqa: N802 (Qt naming)
        """Keep the drag alive over the panel once it has been accepted."""
        candidate = first_local_file(event.mimeData())
        if candidate is not None and looks_like_document(candidate):
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802 (Qt naming)
        """Open the dropped file, and accept only if that worked.

        ``isAccepted()`` is the machine-readable half of this: a caller that never looks
        at the information bar still finds out whether the drop was taken. A refusal here
        is deliberate and is also said in words.
        """
        candidate = first_local_file(event.mimeData())
        if candidate is None:
            self._info.setText(tr("that drop carried no file"))
            event.ignore()
            return
        if not self.open_document(candidate):
            event.ignore()
            return
        event.acceptProposedAction()
