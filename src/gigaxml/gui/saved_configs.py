"""Named config files the user keeps, and the ones they opened most recently.

**Why this is a separate module.** Saving and loading are two claims -- "the file is on
disk" and "the file that comes back is the one that went in" -- and only the second one is
worth anything. The second can only be tested by writing through one store and reading
through another, which needs the location injected rather than reached for.

**Nothing here may import PySide6.** Every rule in here -- a name that is not a filename, a
file that vanished, a directory that is not writable -- is testable without a display.

**A saved config is text, not a parsed mapping.** The panel hands the store whatever YAML
it has, and takes back exactly those bytes. Parsing here would mean this module owned a
schema, and the schema belongs to the CLI's loader -- a second copy of it would be a second
answer to "is this config valid", and the two would drift.
"""

from __future__ import annotations

import pathlib
import re

from gigaxml.gui.recent_files import RecentFiles

__all__ = [
    "SAVED_DIR_NAME",
    "RECENT_CONFIGS_NAME",
    "SavedConfig",
    "ConfigLibrary",
]

#: Where saved configs live under the state directory.
SAVED_DIR_NAME = "configs"

#: Where the most-recently-opened list lives. A sibling of the recent *documents* list,
#: not the same file: the two lists answer different questions and a user who clears one
#: has not asked to clear the other.
RECENT_CONFIGS_NAME = "recent-configs.json"

#: What a name may contain. This is a filename, so it may not contain a path separator, and
#: it may not be `.` or `..`. Everything else is allowed -- including spaces and non-ASCII,
#: because a user naming a config after their project should not be told to rename it.
_UNSAFE = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")


class SavedConfig:
    """One saved config: its name and where it lives.

    ``path`` is exposed rather than kept private so a caller can show the user where their
    file went. A save that does not say where is a save the user cannot find later.
    """

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)

    @property
    def name(self) -> str:
        return self.path.stem

    def read(self) -> str:
        """The config text. Raises ``OSError`` if it is gone; the caller decides what to say."""
        return self.path.read_text(encoding="utf-8")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SavedConfig({self.path!r})"


class ConfigLibrary:
    """The configs a user has saved, plus the ones they opened most recently.

    Args:
        state_dir: where both live. Injected, not reached for -- see the module docstring.
    """

    def __init__(self, state_dir: pathlib.Path) -> None:
        self._dir = pathlib.Path(state_dir) / SAVED_DIR_NAME
        self._recent = RecentFiles(pathlib.Path(state_dir) / RECENT_CONFIGS_NAME)

    @property
    def directory(self) -> pathlib.Path:
        """Where saved configs are kept. Exposed so a caller can tell the user."""
        return self._dir

    # -- names -------------------------------------------------------------

    @staticmethod
    def is_usable_name(name: str) -> bool:
        """Whether ``name`` may be used as a config name.

        Rejected here rather than at the filesystem, so the caller can say why. A name with
        a separator in it is not a name, it is a path, and accepting it would let a save
        write outside the directory the user was shown.
        """
        candidate = name.strip()
        if not candidate or candidate in {".", ".."}:
            return False
        return _UNSAFE.search(candidate) is None

    # -- saving and loading ------------------------------------------------

    def save(self, name: str, text: str) -> SavedConfig:
        """Write ``text`` under ``name``, replacing any earlier file of that name.

        Raises ``ValueError`` for a name that cannot be one. The caller shows the message;
        this module does not know how to talk to a user.
        """
        if not self.is_usable_name(name):
            raise ValueError(f"{name!r} cannot be used as a config name")
        target = self._dir / f"{name.strip()}.yaml"
        self._dir.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        return SavedConfig(target)

    def load(self, name: str) -> SavedConfig:
        """The saved config called ``name``. The file need not exist yet; reading it will raise."""
        return SavedConfig(self._dir / f"{pathlib.Path(name).stem}.yaml")

    def names(self) -> tuple[str, ...]:
        """Every saved config, sorted, so a menu does not reorder itself between openings."""
        if not self._dir.is_dir():
            return ()
        return tuple(sorted(item.stem for item in self._dir.glob("*.yaml")))

    def remove(self, name: str) -> None:
        """Forget a saved config. The user asking is the only reason to delete a file."""
        target = self._dir / f"{pathlib.Path(name).stem}.yaml"
        try:
            target.unlink()
        except OSError:
            pass

    # -- recent ------------------------------------------------------------

    def note_opened(self, path: pathlib.Path | str) -> None:
        """Remember that the user opened ``path``, most recent first."""
        self._recent.add(path)

    def recent(self) -> tuple[pathlib.Path, ...]:
        """Recently opened configs, most recent first, without touching the filesystem."""
        return self._recent.paths()

    def recent_entries(self):
        """Recently opened configs, each marked with whether it is still there."""
        return self._recent.entries()

    def forget_recent(self) -> None:
        self._recent.clear()
