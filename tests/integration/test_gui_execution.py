"""The execution panel's own settings, and what it does when they disagree with the config.

**The panel used to rewrite the config silently.** A dropdown reading ``abort`` while the
chosen file said ``quarantine`` meant the run did what the dropdown said and nothing told
the user -- they had picked a config, and the run was not following it. Opening a config now
sets the dropdown to what the config says, so a disagreement can only come from someone
changing the dropdown afterwards, and that is stated rather than hidden.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from PySide6.QtWidgets import QApplication

from gigaxml.gui.main_window import MainWindow

QUARANTINING = """record: /catalog/products/product
on_error: quarantine
fields:
  id:
    path: '@id'
  qty:
    path: qty
    type: int
"""

#: No ``on_error`` at all, so the loader's default applies.
PLAIN = """record: /catalog/products/product
fields:
  id:
    path: '@id'
"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


@pytest.fixture
def window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    del app
    return MainWindow(state_dir=tmp_path / "state")


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- G1: the panel follows the config -----------------------------------------


def test_opening_a_config_takes_its_on_error(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """**The core of this round.** A config that asks for quarantine has to arrive with the
    dropdown saying quarantine, or the panel is overriding a file the user chose without
    saying so."""
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()
    assert panel._on_error.currentText() == "abort", "the default is what this starts from"

    panel.set_config(config)

    assert panel._on_error.currentText() == "quarantine"


def test_a_config_without_on_error_leaves_the_default(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The other half: a file that says nothing must not invent a policy."""
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()

    panel.set_config(config)

    assert panel._on_error.currentText() == "abort"


def test_opening_a_config_does_not_pass_the_policy_through_as_an_override(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """**Agreeing means the file is used untouched.** ``effective_config`` passes it through
    when the two agree, which is what keeps the user's comments and formatting -- so a
    panel that had to rewrite every config would be losing them for no reason."""
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()

    panel.set_config(config)

    assert panel.effective_config() == config, "the file was rewritten for no reason"


# --- G3: a disagreement is stated, with both values ---------------------------


def test_changing_the_dropdown_says_what_it_overrides(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Not prevented -- the user may want to try something for one run -- but said out loud,
    naming both values, because "the run will not do what your config says" is the whole
    point and one value alone does not say it."""
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()
    panel.set_config(config)

    panel.set_on_error("abort")

    assert panel.is_override_noted()
    notice = panel.on_error_notice_text()
    assert "abort" in notice and "quarantine" in notice, notice


def test_the_notice_names_the_other_direction_too(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Whichever way round it is, both values appear."""
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()
    panel.set_config(config)

    panel.set_on_error("quarantine")

    notice = panel.on_error_notice_text()
    assert "quarantine" in notice and "abort" in notice, notice


# --- G4: agreeing is quiet ----------------------------------------------------


def test_agreeing_says_nothing(window: MainWindow, tmp_path: pathlib.Path) -> None:
    """A notice on every run is a notice nobody reads."""
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()

    panel.set_config(config)

    assert not panel.is_override_noted()
    assert panel.on_error_notice_text() == ""


def test_going_back_to_the_configs_value_stops_saying_anything(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()
    panel.set_config(config)
    panel.set_on_error("abort")
    assert panel.is_override_noted()

    panel.set_on_error("quarantine")

    assert not panel.is_override_noted()
    assert panel.on_error_notice_text() == ""


def test_the_panel_says_nothing_before_a_config_is_chosen(window: MainWindow) -> None:
    """No config, nothing to disagree with."""
    panel = window.execution_panel()

    assert not panel.is_override_noted()
    assert panel.on_error_notice_text() == ""


# --- a config that cannot be read ---------------------------------------------


def test_an_unreadable_config_is_not_complained_about_twice(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """``start`` reports a config it cannot load, with the loader's own message. A widget
    that also complained would be noise on top of the real answer, and it would be guessing
    at what the config said."""
    broken = write(tmp_path / "broken.yaml", "record: [unclosed\n")
    panel = window.execution_panel()
    panel.set_config(broken)

    assert not panel.is_override_noted()
    assert panel.on_error_notice_text() == ""


def test_an_empty_path_says_nothing(window: MainWindow) -> None:
    panel = window.execution_panel()

    panel.set_config("")

    assert not panel.is_override_noted()
