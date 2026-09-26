"""The settings panel: construction, state flow, and that it actually writes.

**Why this is thin.** The rules live in `settings.py`, which needs no display and is tested
in full there. What is left to check here is that the widget is wired to that store -- a
control that shows a value but never writes it looks identical to one that works, until a
restart.
"""

from __future__ import annotations

import pathlib

import pytest
from PySide6.QtWidgets import QApplication

from gigaxml.gui.panels.settings import SettingsPanel
from gigaxml.gui.settings import SettingsStore


@pytest.fixture(scope="module")
def app() -> QApplication:
    """One QApplication for the module.

    Defined here rather than in a shared conftest, matching the other GUI test files: a
    module that needs a display says so in its own source.
    """
    existing = QApplication.instance()
    return existing if existing is not None else QApplication([])


@pytest.fixture
def store(tmp_path: pathlib.Path) -> SettingsStore:
    return SettingsStore(tmp_path / "settings.json")


def test_the_panel_opens_on_the_stored_values(app: QApplication, store: SettingsStore) -> None:
    del app  # the fixture boots QApplication; the test needs no handle on it
    store.set(batch_size=250, theme="dark")

    panel = SettingsPanel(store)

    assert panel.current().batch_size == 250
    assert panel.current().theme == "dark"


def test_editing_a_control_writes_immediately(app: QApplication, store: SettingsStore) -> None:
    """No Save button, so an edit that only lived in the widget would be lost on quit."""
    del app  # the fixture boots QApplication; the test needs no handle on it
    panel = SettingsPanel(store)

    panel._batch_size.setValue(777)

    assert SettingsStore(store.store).read().batch_size == 777


def test_editing_reaches_disk_not_just_the_widget(app: QApplication, store: SettingsStore) -> None:
    """Read back through a *second* store: the widget agreeing with itself proves nothing."""
    del app  # the fixture boots QApplication; the test needs no handle on it
    panel = SettingsPanel(store)

    panel._theme.setCurrentText("Dark")

    assert SettingsStore(store.store).read().theme == "dark"


def test_a_change_is_announced(app: QApplication, store: SettingsStore) -> None:
    """A window that wants to re-theme itself listens rather than polling the file."""
    del app  # the fixture boots QApplication; the test needs no handle on it
    panel = SettingsPanel(store)
    seen: list[object] = []
    panel.settings_changed.connect(seen.append)

    panel._on_error.setCurrentText("quarantine")

    assert len(seen) == 1
    assert seen[0].on_error == "quarantine"


def test_shutdown_is_idempotent(app: QApplication, store: SettingsStore) -> None:
    """The panel contract: called twice, it does nothing the second time."""
    del app  # the fixture boots QApplication; the test needs no handle on it
    panel = SettingsPanel(store)

    panel.shutdown()
    panel.shutdown()

    assert panel._shut_down is True


def test_the_panel_says_where_the_file_is(app: QApplication, store: SettingsStore) -> None:
    """A preference the user cannot find is a preference they cannot back up or delete."""
    del app  # the fixture boots QApplication; the test needs no handle on it
    panel = SettingsPanel(store)

    assert str(store.store) in panel._where.text()
