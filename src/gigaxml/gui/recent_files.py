"""The recently opened documents, kept across runs.

**Why this is a separate module.** "It is in the list" and "it is in the list after a
restart" are different claims, and only the second one is worth anything. The second can
only be tested by writing from one process and reading from another, which needs a store
that takes its location as a parameter rather than reaching for a global. So the location
is injected, and the window is what supplies the default.

**A path that no longer exists is marked, never removed.** Dropping it would leave the
list looking tidy and the user unable to tell where their entry went -- and a document on
a disconnected drive is a missing path today and a present one tomorrow. Deleting and
marking are two different operations, and only the user gets to do the first one.

**Nothing here may import PySide6.** The interesting behaviour -- a corrupt store, a
store written by an older version, a path that vanished -- is testable without a display,
and a test that needs a display to check a JSON file is a test that will not be run.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import dataclass

__all__ = [
    "DEFAULT_LIMIT",
    "RecentEntry",
    "RecentFiles",
    "default_state_dir",
]

#: How many entries to keep. Long enough to cover a working session, short enough that
#: the menu stays scannable.
DEFAULT_LIMIT = 10

#: Where a store lives when the caller does not say. Set this to keep a test, or a
#: throwaway run, out of the real user's configuration directory -- a test that writes
#: into `%APPDATA%` is polluting the machine rather than testing anything.
STATE_DIR_ENV = "GIGAXML_GUI_STATE_DIR"


def default_state_dir() -> pathlib.Path:
    """The directory the window keeps its small state files in.

    The environment variable wins when it is set, which is how tests and CI stay out of
    the real user's home directory. Otherwise this is a per-user location, because a
    "recent files" list that is not per-user is not the feature anybody asked for.
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return pathlib.Path(override)
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = pathlib.Path(base) if base else pathlib.Path.home() / "AppData" / "Roaming"
        return root / "gigaxml"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    root = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return root / "gigaxml"


@dataclass(frozen=True)
class RecentEntry:
    """One remembered document, and whether it is still there.

    ``exists`` is a property of the moment the list was read, not of the entry. A caller
    that wants it fresh asks the store again.
    """

    path: pathlib.Path
    exists: bool

    @property
    def name(self) -> str:
        return self.path.name


class RecentFiles:
    """A most-recent-first list of document paths, persisted as JSON.

    Args:
        store: the JSON file. Its directory is created on the first write; nothing is
            written by construction, so merely building a window does not touch the disk.
        limit: how many entries to keep. Older ones fall off the end.
    """

    def __init__(self, store: pathlib.Path, *, limit: int = DEFAULT_LIMIT) -> None:
        self._store = pathlib.Path(store)
        self._limit = limit

    @property
    def store(self) -> pathlib.Path:
        """Where this list is kept. Exposed so a caller can tell the user."""
        return self._store

    # -- reading -----------------------------------------------------------

    def paths(self) -> tuple[pathlib.Path, ...]:
        """The remembered paths, most recent first, without touching the filesystem."""
        return tuple(self._read())

    def entries(self) -> tuple[RecentEntry, ...]:
        """The remembered paths, each marked with whether it is still there.

        **Missing paths are kept.** See the module docstring: removing them silently is
        how a user loses an entry and never learns why.
        """
        return tuple(RecentEntry(path=path, exists=path.is_file()) for path in self._read())

    def _read(self) -> list[pathlib.Path]:
        """The stored paths, or an empty list if the store is missing or unreadable.

        A corrupt store is not worth an exception: the worst case is a shorter list, and
        the alternative is a window that will not open because a cache file went bad.
        """
        try:
            text = self._store.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, dict):
            return []
        raw = payload.get("recent")
        if not isinstance(raw, list):
            return []
        return [pathlib.Path(item) for item in raw if isinstance(item, str) and item.strip()]

    # -- writing -----------------------------------------------------------

    def add(self, path: pathlib.Path | str) -> None:
        """Remember ``path``, most recent first, without duplicates."""
        candidate = pathlib.Path(path)
        remaining = [item for item in self._read() if item != candidate]
        self._write([candidate, *remaining])

    def remove(self, path: pathlib.Path | str) -> None:
        """Forget one path. The user asking for this is the only reason to drop an entry."""
        target = pathlib.Path(path)
        self._write([item for item in self._read() if item != target])

    def clear(self) -> None:
        self._write([])

    def _write(self, paths: list[pathlib.Path]) -> None:
        """Persist the list, trimmed to the limit.

        Written to a sibling temporary file and moved into place. A store half-written by
        a crash would read back as corrupt, which is recoverable, but it would also throw
        away the entries that were already there, which is not.
        """
        trimmed = paths[: self._limit]
        payload = {"recent": [str(item) for item in trimmed]}
        self._store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store.with_name(self._store.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self._store)
