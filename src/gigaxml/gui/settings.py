"""The preferences the window remembers between runs.

**Why this is a separate module.** "The box says 500" and "the box says 500 because you set
it to 500 last week" are different claims, and only the second one is worth anything. The
second can only be tested by writing from one process and reading from another, so the
location is injected rather than reached for -- exactly the arrangement `recent_files`
already uses, and for the same reason.

**Nothing here may import PySide6.** Every rule in here -- an unknown theme, a batch size
that is not a number, a file written by an older version -- is testable without a display,
and a test that needs a display to check a JSON file is a test that will not be run.

**An unreadable setting falls back to its default rather than raising.** A preferences file
is a cache of choices, not a source of truth; the worst case is the user's old choice is
forgotten, and the alternative is a window that will not open because a JSON file went bad.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, replace
from typing import Any

__all__ = [
    "DEFAULTS",
    "FORMATS",
    "ON_ERROR",
    "THEMES",
    "Settings",
    "SettingsStore",
]

#: What the theme setting may be. ``system`` means "ask the platform", and is the default
#: because a window that ignores the rest of the desktop is a window people turn off.
THEMES = ("system", "light", "dark")

#: The output formats the CLI writes. Kept in step with the execution panel's combo box.
FORMATS = ("csv", "jsonl", "parquet")

#: What to do when one record cannot be extracted.
ON_ERROR = ("abort", "quarantine")

#: The value every setting has before the user touches anything. One mapping, so a caller
#: adding a setting has exactly one place to add its default and one place to document it.
DEFAULTS: dict[str, Any] = {
    "batch_size": 1000,
    "format": "csv",
    "on_error": "abort",
    "theme": "system",
}

#: Bounds for ``batch_size``. The lower bound is 1 because zero is not a batch size; the
#: upper bound matches what the CLI accepts, so a preference cannot describe a run the CLI
#: would refuse.
MIN_BATCH_SIZE = 1
MAX_BATCH_SIZE = 10_000_000


def _coerce(key: str, value: object) -> object:
    """Turn a stored value into one this module is willing to hand out.

    A stored file is not trusted: it may have been edited by hand, written by an older
    version, or truncated. Each setting is checked against what it is allowed to be, and
    anything that does not fit is replaced by the default rather than passed along to
    become somebody else's problem.
    """
    if key == "batch_size":
        if isinstance(value, bool):  # bool is an int subclass; True is not a batch size.
            return DEFAULTS[key]
        if isinstance(value, int) and MIN_BATCH_SIZE <= value <= MAX_BATCH_SIZE:
            return value
        return DEFAULTS[key]
    if key == "format":
        return value if value in FORMATS else DEFAULTS[key]
    if key == "on_error":
        return value if value in ON_ERROR else DEFAULTS[key]
    if key == "theme":
        return value if value in THEMES else DEFAULTS[key]
    return value


@dataclass(frozen=True)
class Settings:
    """One complete set of preferences.

    Frozen because the store hands these out to whoever asks, and a panel that mutated one
    in place would silently change what every other holder sees. Changing a preference is
    ``store.set(...)``, which writes and returns a new instance.
    """

    batch_size: int = DEFAULTS["batch_size"]
    format: str = DEFAULTS["format"]
    on_error: str = DEFAULTS["on_error"]
    theme: str = DEFAULTS["theme"]

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_size": self.batch_size,
            "format": self.format,
            "on_error": self.on_error,
            "theme": self.theme,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Settings:
        """Build from a stored mapping, coercing every field and ignoring unknown keys.

        Unknown keys are dropped rather than preserved: a setting this version does not
        know about is one it cannot honour, and carrying it forward would let a file claim
        a preference that nothing reads.
        """
        values = {key: _coerce(key, payload.get(key, DEFAULTS[key])) for key in DEFAULTS}
        return cls(**values)


class SettingsStore:
    """Reads and writes :class:`Settings` as JSON at a path the caller supplies.

    Args:
        store: the JSON file. Its directory is created on the first write; constructing a
            store writes nothing, so merely opening a window does not touch the disk.
    """

    def __init__(self, store: pathlib.Path) -> None:
        self._store = pathlib.Path(store)

    @property
    def store(self) -> pathlib.Path:
        """Where these preferences are kept. Exposed so a caller can tell the user."""
        return self._store

    def read(self) -> Settings:
        """The stored preferences, or the defaults if there is nothing usable there."""
        try:
            text = self._store.read_text(encoding="utf-8")
        except OSError:
            return Settings()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return Settings()
        if not isinstance(payload, dict):
            return Settings()
        return Settings.from_dict(payload)

    def write(self, settings: Settings) -> None:
        """Persist ``settings``.

        Written to a sibling temporary file and moved into place, so a crash halfway
        through leaves the previous preferences intact instead of a half-written file that
        reads back as corrupt. Same approach as `recent_files`, same reason.
        """
        self._store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store.with_name(self._store.name + ".tmp")
        temporary.write_text(json.dumps(settings.as_dict(), indent=2), encoding="utf-8")
        temporary.replace(self._store)

    def set(self, **changes: object) -> Settings:
        """Write ``changes`` on top of what is stored, and return the result.

        The write is a read-modify-write of the whole file rather than a patch, because
        the file is small and a partial update is a chance to lose a setting somebody else
        changed in between.
        """
        current = self.read()
        known = {key: value for key, value in changes.items() if key in DEFAULTS}
        updated = replace(current, **{k: _coerce(k, v) for k, v in known.items()})
        self.write(updated)
        return updated
