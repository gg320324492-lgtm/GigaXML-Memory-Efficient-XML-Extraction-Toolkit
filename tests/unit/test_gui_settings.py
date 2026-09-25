"""The preferences store: what survives a restart, and what does not get honoured.

**Why these are unit tests.** Nothing in `settings` imports PySide6, so none of this needs
a display -- and the interesting cases are the ones a display would not help with: a file
written by an older version, a value that is the wrong type, a crash halfway through a
write. A test that needs a window to check a JSON file is a test that will not be run.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from gigaxml.gui.settings import DEFAULTS, Settings, SettingsStore


def test_defaults_are_what_a_fresh_install_gets(tmp_path: pathlib.Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")

    assert store.read() == Settings()


def test_nothing_is_written_by_merely_constructing(tmp_path: pathlib.Path) -> None:
    """Opening a window must not touch the disk.

    Constructing a store is something that happens on every launch. If it wrote, every
    launch would create a file in the user's configuration directory, including the
    launches that never changed anything.
    """
    target = tmp_path / "settings.json"

    SettingsStore(target)

    assert not target.exists()


def test_a_setting_survives_a_restart(tmp_path: pathlib.Path) -> None:
    """The claim worth testing: a *second* store reads what the first one wrote."""
    target = tmp_path / "settings.json"
    SettingsStore(target).set(batch_size=250)

    assert SettingsStore(target).read().batch_size == 250


def test_only_the_named_settings_change(tmp_path: pathlib.Path) -> None:
    store = SettingsStore(tmp_path / "settings.json")
    store.set(batch_size=250, theme="dark")

    updated = store.set(on_error="quarantine")

    assert (updated.batch_size, updated.theme, updated.on_error) == (250, "dark", "quarantine")


def test_an_unknown_setting_is_refused_not_stored(tmp_path: pathlib.Path) -> None:
    """A preference this version cannot honour is not carried forward.

    Storing it would let the file claim a setting that nothing reads, which is how a user
    ends up believing they changed something.
    """
    target = tmp_path / "settings.json"
    SettingsStore(target).set(nonsense="whatever")

    payload = json.loads(target.read_text(encoding="utf-8"))

    assert "nonsense" not in payload
    assert set(payload) == set(DEFAULTS)


@pytest.mark.parametrize(
    ("key", "bad"),
    [
        ("batch_size", 0),
        ("batch_size", -5),
        ("batch_size", "lots"),
        ("batch_size", True),
        ("format", "xml"),
        ("on_error", "ignore"),
        ("theme", "solarized"),
    ],
)
def test_a_value_that_is_not_allowed_falls_back(tmp_path: pathlib.Path, key: str, bad: object) -> None:
    """Every field is checked, not just cast.

    A preferences file is editable by hand and written by older versions, so each value is
    compared against what it is allowed to be. Handing an unchecked value to the rest of
    the application would move the failure somewhere less obvious.
    """
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({key: bad}), encoding="utf-8")

    assert getattr(SettingsStore(target).read(), key) == DEFAULTS[key]


def test_a_corrupt_file_does_not_stop_the_window(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{ this is not json", encoding="utf-8")

    assert SettingsStore(target).read() == Settings()


def test_a_file_from_a_future_version_keeps_what_it_can(tmp_path: pathlib.Path) -> None:
    """Unknown keys are dropped, known ones are honoured."""
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps({"theme": "dark", "something_new": 1}),
        encoding="utf-8",
    )

    settings = SettingsStore(target).read()

    assert settings.theme == "dark"
    assert settings.batch_size == DEFAULTS["batch_size"]


def test_a_setting_is_not_edited_in_place(tmp_path: pathlib.Path) -> None:
    """The store hands out frozen values, so one holder cannot change what another sees."""
    store = SettingsStore(tmp_path / "settings.json")
    first = store.read()

    store.set(theme="dark")

    assert first.theme == DEFAULTS["theme"]
