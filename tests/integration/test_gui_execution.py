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
    main = MainWindow(state_dir=tmp_path / "state")
    # **Close it on the way out.** Without this the window outlives its test:
    # closeEvent never fires, the panels' shutdown() never runs, and their pumps
    # stay in Qt's timer list -- which is what fires into a dead object during the
    # next test's waitUntil. See the core dump in 8A-32.
    yield main
    main.close()


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


# --- 8A-9: the direction that was recorded backwards ---------------------------
#
# The bug was filed as "the dropdown silently overrides the config's on_error", and that has
# been handled for some time -- the tests above cover it. The live problem is the reverse
# and it was invisible from the code, because the two cases look identical until you drive
# the panel: a config *silently replaces the user's own choice*, and the one widget that
# could have said so has nothing left to compare.


def test_a_config_opened_after_the_user_chose_says_it_replaced_their_choice(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The measured case: choose ``quarantine``, then open a config that says ``abort``.

    Before the fix this produced ``notice == ""`` and a run that quarantined nothing --
    the panel followed the file, as it should, and the user's preference was gone without a
    word. The two *current* values agree at that point, which is exactly why comparing them
    could never catch it.
    """
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()

    panel.set_on_error("quarantine")
    assert panel.user_on_error() == "quarantine"

    panel.set_config(config)  # this config asks for abort

    assert panel.current_on_error() == "abort", "the panel should follow the config"
    assert panel.is_override_noted(), "the user's replaced choice was dropped without a word"
    notice = panel.on_error_notice_text()
    assert "abort" in notice, notice
    assert "quarantine" in notice, notice
    # The wording has to say *which* way round it happened. "the run will use abort" is
    # true, and useless: the dropdown already says abort.
    assert "your choice" in notice.lower(), notice


def test_the_remembered_choice_survives_a_config_that_agrees(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """Following the file must not overwrite the memory of what the user wanted.

    Otherwise a second config opened later would compare against the file's value instead of
    the user's, and the notice would be wrong rather than absent.
    """
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()

    panel.set_on_error("quarantine")
    panel.set_config(config)  # agrees with the user

    assert panel.current_on_error() == "quarantine"
    assert panel.user_on_error() == "quarantine", "the config overwrote the user's choice"
    assert not panel.is_override_noted(), "agreement should stay quiet"


def test_setting_the_policy_through_the_advice_counts_as_the_user_choosing(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """``set_on_error`` is also how the error panel's advice arrives, so it must count.

    Otherwise pressing "set on_error to quarantine" would leave the panel believing the user
    had never expressed a preference, and a later config would silently take it away with
    no notice -- the same 8A-9 failure reached by a different route.
    """
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()
    panel.set_config(config)

    panel.set_on_error("quarantine")  # as the error panel's button does

    assert panel.user_on_error() == "quarantine"
    assert panel.is_override_noted()
    assert "quarantine" in panel.on_error_notice_text()


def test_an_unreadable_config_leaves_the_choice_alone(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """A config that cannot be read must not cost the user their preference, or crash.

    ``_configs_on_error`` swallows the error on purpose -- the run reports it properly -- so
    this is the path where "the file said nothing" and "the file said abort" look alike.
    """
    panel = window.execution_panel()
    panel.set_on_error("quarantine")

    panel.set_config(tmp_path / "missing.yaml")

    assert panel.user_on_error() == "quarantine"
    assert panel.current_on_error() == "quarantine", "an unreadable file changed the policy"
    assert not panel.is_override_noted()


# --- 8A-9 residual: a notice that has stopped being true -------------------------
#
# The first round of 8A-9 was tested only on the side where the notice should *appear*. The
# defect that survived it is entirely on the other side: a notice that outlives the fact it
# was reporting. It had two causes, and only one of them was a missing call.
#
# The Qt trap: ``currentIndexChanged`` fires only when the index *changes*, so setting the
# dropdown to the value it already holds emits nothing, and the notice hangs off that signal.
# Measured at the time: 0 firings, and the notice unchanged. The second cause was in the
# wording, which named a config whether or not one was open.


def test_the_notice_goes_away_when_the_user_agrees_with_the_config(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The case the first round never tested: all three values agree, and it must be silent.

    Setting the dropdown back to what the config asked for does not move the combo -- it is
    already there -- so the signal never fires and the notice kept claiming the user's
    choice had been replaced, naming ``quarantine`` while the user was looking at ``abort``.
    """
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()
    panel.set_on_error("quarantine")
    panel.set_config(config)
    assert panel.is_override_noted(), "precondition: the notice is showing"

    panel.set_on_error("abort")  # the user agrees with the config

    assert panel.current_on_error() == "abort"
    assert panel.user_on_error() == "abort"
    assert not panel.is_override_noted(), "the notice outlived the disagreement"
    assert panel.on_error_notice_text() == "", panel.on_error_notice_text()


def test_clearing_the_config_stops_claiming_a_config_says_anything(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """No config open means nothing may be blamed on a config.

    Measured before the fix: the config field cleared, and the panel still said "this config
    sets on_error to abort" -- about a file that was no longer there. A user reading that
    goes looking for a file to go and fix.
    """
    config = write(tmp_path / "plain.yaml", PLAIN)
    panel = window.execution_panel()
    panel.set_on_error("quarantine")
    panel.set_config(config)
    assert panel.is_override_noted(), "precondition: the notice is showing"

    panel.set_config("")

    notice = panel.on_error_notice_text()
    assert "this config" not in notice, f"the panel still blames a config that is gone: {notice}"
    # What it may still say is the effect and the earlier choice, which remain true.
    assert "abort" in notice and "quarantine" in notice, notice


def test_a_value_pushed_in_without_a_config_is_reported_without_blaming_a_file(
    window: MainWindow,
) -> None:
    """The settings panel pushing a value is a real change, and must be reported.

    But with no config open there is nothing to point at, so the notice states the effect and
    names the earlier choice without saying where the current value came from -- because the
    panel does not know.
    """
    panel = window.execution_panel()
    panel.set_on_error("quarantine")
    assert not panel.is_override_noted(), "precondition: nothing to report yet"

    panel.apply_defaults(output_format="csv", on_error="abort", batch_size=1000)

    notice = panel.on_error_notice_text()
    assert panel.is_override_noted(), "a pushed-in value went unreported"
    assert "config" not in notice, f"blames a config that was never opened: {notice}"
    assert "abort" in notice and "quarantine" in notice, notice


def test_a_pushed_in_value_is_not_remembered_as_the_user_having_chosen_it(
    window: MainWindow,
) -> None:
    """A stored preference is the program handing a value back, not a choice.

    If it were recorded as the user's own, a later config would report replacing a choice
    they never made -- the 8A-9 failure reached by a different route, and invisible from
    whichever test looked at the dropdown alone.
    """
    panel = window.execution_panel()
    panel.set_on_error("quarantine")

    panel.apply_defaults(output_format="csv", on_error="abort", batch_size=1000)

    assert panel.user_on_error() == "quarantine", (
        "a pushed-in preference was recorded as the user's own choice"
    )


def test_a_value_pushed_in_for_a_user_who_never_chose_says_nothing(
    window: MainWindow,
) -> None:
    """There is no earlier choice to report, so there is nothing to say.

    A panel that invented one would put "not your earlier choice of ..." in front of a user
    who has no earlier choice -- the same failure as a stale notice, in a fresh window.
    """
    panel = window.execution_panel()

    panel.apply_defaults(output_format="csv", on_error="abort", batch_size=1000)

    assert panel.user_on_error() is None
    assert not panel.is_override_noted()
    assert panel.on_error_notice_text() == ""


def test_the_notice_is_re_derived_even_when_the_value_does_not_change(
    window: MainWindow, tmp_path: pathlib.Path
) -> None:
    """The general case behind all of the above: same value, different facts.

    Every way this panel moves the dropdown goes through one helper, because the signal only
    fires on a change and the notice depends on more than the dropdown. Re-deriving from a
    state the panel can actually be in -- the value steady, the config gone -- is the only
    thing that catches it.
    """
    config = write(tmp_path / "quarantine.yaml", QUARANTINING)
    panel = window.execution_panel()
    panel.set_config(config)
    assert not panel.is_override_noted(), "precondition: agreeing is quiet"

    # The dropdown does not move here, so nothing about the widgets changes either.
    panel.set_config(config)

    assert panel.current_on_error() == "quarantine"
    assert not panel.is_override_noted()
