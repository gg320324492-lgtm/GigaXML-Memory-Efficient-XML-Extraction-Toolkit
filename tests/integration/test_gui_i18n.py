"""Phase 8C's three criteria: the interface translates, the data does not, and the choice sticks.

**Criterion 1 is the reason this file exists.** The trap this phase was written around is a
combo box whose label was translated while the code kept reading the label: a Chinese
interface would have handed the CLI a Chinese ``on_error`` value, and everything on screen
would have looked fine right up to the run refusing to start. So the core test here builds
a Chinese window, makes the user's choice through it the way the click would, runs a real
``extract``, and reads back what landed in the config file. The value in that file is the
whole point; everything else in this file is the rest of the promise.

**The tests pin languages explicitly** -- by writing the choice into the state directory
the window reads, which is the same path a real user's choice takes -- rather than relying
on the module-level default. A test that passes because of a default is a test about the
default; these are about what each language shows.
"""

from __future__ import annotations

import json
import pathlib
import sys
from collections.abc import Iterator

import pytest
from PySide6.QtWidgets import QApplication

from gigaxml.gui.main_window import MainWindow
from gigaxml.gui.settings import LANGUAGES, Settings, SettingsStore

#: Two records, enough for a real extract to have something to write and nothing to slow it.
TINY_DOCUMENT = """<?xml version="1.0"?>
<catalog><products>
<product id="1"><name>bracket</name></product>
<product id="2"><name>flange</name></product>
</products></catalog>
"""

#: A config whose policy is quarantine, so that choosing ``abort`` in the interface forces
#: the panel to write an effective config -- the exact write the trap would corrupt.
QUARANTINING = """record: /catalog/products/product
on_error: quarantine
fields:
  product_id:
    path: '@id'
  name:
    path: name
"""


@pytest.fixture(scope="module")
def app() -> QApplication:
    existing = QApplication.instance()
    if existing is not None:
        return existing  # type: ignore[return-value]
    return QApplication(sys.argv)


def window_in_language(
    app: QApplication, tmp_path: pathlib.Path, language: str
) -> Iterator[MainWindow]:
    """A window whose stored preference is ``language``, written the way the panel writes it."""
    del app
    store = SettingsStore(tmp_path / "state" / "settings.json")
    store.write(Settings(language=language))
    main = MainWindow(state_dir=tmp_path / "state")
    yield main
    main.close()


@pytest.fixture
def zh_window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    yield from window_in_language(app, tmp_path, "zh")


@pytest.fixture
def en_window(app: QApplication, tmp_path: pathlib.Path) -> MainWindow:
    yield from window_in_language(app, tmp_path, "en")


# -- criterion 2: the interface really does translate, both ways -----------------------


def test_the_chinese_interface_is_chinese_and_the_english_one_is_not(
    zh_window: MainWindow, en_window: MainWindow
) -> None:
    """Tab titles, a button and a status line, read off the widgets themselves."""
    zh_tabs = [zh_window.tabs().tabText(i) for i in range(zh_window.tabs().count())]
    en_tabs = [en_window.tabs().tabText(i) for i in range(en_window.tabs().count())]

    assert zh_tabs[:2] == ["文档", "结构"], zh_tabs
    assert en_tabs[:2] == ["Document", "Structure"], en_tabs

    assert zh_window.execution_panel()._start.text() == "运行"
    assert en_window.execution_panel()._start.text() == "Start"

    # The on-error combo shows the translated label -- and this is the label, not the
    # value; what the value is, is criterion 1's business two tests down.
    dropdown = zh_window.execution_panel()._on_error
    assert dropdown.currentText() == "中止", dropdown.currentText()
    assert en_window.execution_panel()._on_error.currentText() == "abort"


def test_the_language_combo_offers_both_and_shows_each_in_its_own_language(
    app: QApplication, tmp_path: pathlib.Path
) -> None:
    """The control a user who cannot read the current interface has to be able to read."""
    del app
    window = MainWindow(state_dir=tmp_path / "state")
    try:
        panel = window._settings
        items = [
            (panel._language.itemText(i), panel._language.itemData(i))
            for i in range(panel._language.count())
        ]
        assert ("English", "en") in items and ("中文", "zh") in items, items
        assert panel._language.currentData() == "en"
    finally:
        window.close()


# -- criterion 1: translated display, untranslated value, proven by a real run ----------


def test_a_chinese_interface_still_hands_the_cli_english_values(
    app: QApplication, tmp_path: pathlib.Path, zh_window: MainWindow
) -> None:
    """Choose 中止 in a Chinese window, run extract, read the config file it produced.

    The config on disk says ``quarantine``; the user's choice in the interface is
    ``abort``; the panel must therefore write an effective config, and the value it
    writes for ``on_error`` is the thing this test exists to see. Everything about this
    run goes through the panel's own code paths: the choice is made the way the dropdown
    makes it, the arguments are ``build_args``, and the extraction is a real child
    process that would refuse a Chinese value.
    """
    del app
    panel = zh_window.execution_panel()
    source = tmp_path / "tiny.xml"
    source.write_text(TINY_DOCUMENT, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(QUARANTINING, encoding="utf-8")
    output = tmp_path / "out.csv"

    panel._source.setText(str(source))
    panel._output.setText(str(output))
    panel.set_config(str(config))
    assert panel.current_on_error() == "quarantine", "the panel starts by agreeing with the file"

    panel.set_on_error("abort")
    assert panel._on_error.currentText() == "中止", "the label the user chose is Chinese"

    args = panel.build_args()
    effective = pathlib.Path(args[args.index("-c") + 1])
    assert effective != config, "the choice differs from the file, so a rewritten config was needed"
    written = json.loads(effective.read_text(encoding="utf-8"))
    assert written["on_error"] == "abort", written
    assert "中止" not in json.dumps(written, ensure_ascii=False), written

    from gigaxml.gui.cli_process import run_to_completion

    run = run_to_completion(args)
    assert run.exit_code == 0, f"the CLI refused what the Chinese window wrote: {run.warnings}"
    rows = output.read_text(encoding="utf-8").strip().splitlines()
    assert len(rows) == 3, rows  # header plus the two records


# -- criterion 3: the choice outlives the process --------------------------------------


def test_the_language_choice_survives_into_a_fresh_process(tmp_path: pathlib.Path) -> None:
    """One store writes the way the settings panel does; a second store reads it back."""
    writer = SettingsStore(tmp_path / "settings.json")
    writer.set(language="zh")
    reader = SettingsStore(tmp_path / "settings.json")
    assert reader.read().language == "zh"


def test_an_unknown_stored_language_falls_back_to_the_default(
    app: QApplication, tmp_path: pathlib.Path
) -> None:
    """A hand-edited or newer-build settings file must not stop the window opening."""
    del app
    store_path = tmp_path / "state" / "settings.json"
    store = SettingsStore(store_path)
    store.write(Settings(language="zh"))
    payload = json.loads(store_path.read_text(encoding="utf-8"))
    payload["language"] = "fr"
    store_path.write_text(json.dumps(payload), encoding="utf-8")

    assert store.read().language == "en", "the store coerces, before a window is even built"

    window = MainWindow(state_dir=tmp_path / "state")
    try:
        assert window.tabs().tabText(0) == "Document", "and the window comes up in English"
    finally:
        window.close()


# -- the mechanism's own fallback rule -------------------------------------------------


def test_an_untranslated_string_comes_back_as_its_english_self() -> None:
    """A key with no entry displays English -- never blank, never the key as a symbol."""
    from gigaxml.gui import i18n

    i18n.set_language("zh")
    try:
        assert i18n.tr("no such string anywhere") == "no such string anywhere"
    finally:
        i18n.set_language("en")
    assert i18n.tr("中止") == "中止", "in English the table is bypassed entirely"


def test_an_unknown_language_behaves_as_english() -> None:
    from gigaxml.gui import i18n

    i18n.set_language("fr")
    assert i18n.current_language() == i18n.FALLBACK_LANGUAGE
    assert i18n.tr("Start") == "Start"


def test_the_settings_whitelist_and_the_translator_agree_on_what_exists() -> None:
    """Every language the settings file may hold is one the window can actually apply."""
    from gigaxml.gui import i18n

    for language in LANGUAGES:
        i18n.set_language(language)
        assert i18n.current_language() == language, language
    i18n.set_language("en")
