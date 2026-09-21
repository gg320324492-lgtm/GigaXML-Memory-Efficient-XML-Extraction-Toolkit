"""The recent-documents store.

The point of this file is the second half: a list that is in memory is not a list that
survives a restart, and the only way to tell the two apart is to write in one process and
read in another. Everything before that is bookkeeping.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

from gigaxml.gui.recent_files import RecentFiles, default_state_dir

REPO = pathlib.Path(__file__).resolve().parent.parent.parent


def _document(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    path = tmp_path / name
    path.write_text("<catalog/>", encoding="utf-8")
    return path


# --- the store itself ---------------------------------------------------------


def test_a_store_that_was_never_written_reads_as_empty(tmp_path: pathlib.Path) -> None:
    store = RecentFiles(tmp_path / "recent.json")

    assert store.paths() == ()
    assert store.entries() == ()


def test_building_the_store_does_not_write_anything(tmp_path: pathlib.Path) -> None:
    """Merely opening the window must not touch the user's configuration directory."""
    target = tmp_path / "recent.json"

    RecentFiles(target)

    assert not target.exists()


def test_the_most_recent_document_is_first(tmp_path: pathlib.Path) -> None:
    store = RecentFiles(tmp_path / "recent.json")
    first = _document(tmp_path, "first.xml")
    second = _document(tmp_path, "second.xml")

    store.add(first)
    store.add(second)

    assert store.paths() == (second, first)


def test_adding_a_document_twice_moves_it_up_rather_than_repeating_it(
    tmp_path: pathlib.Path,
) -> None:
    store = RecentFiles(tmp_path / "recent.json")
    first = _document(tmp_path, "first.xml")
    second = _document(tmp_path, "second.xml")
    store.add(first)
    store.add(second)

    store.add(first)

    assert store.paths() == (first, second)


def test_the_list_is_trimmed_to_the_limit(tmp_path: pathlib.Path) -> None:
    store = RecentFiles(tmp_path / "recent.json", limit=2)
    for index in range(4):
        store.add(_document(tmp_path, f"doc{index}.xml"))

    assert [path.name for path in store.paths()] == ["doc3.xml", "doc2.xml"]


# --- the entry that is not there ----------------------------------------------


def test_a_missing_document_is_kept_and_marked_not_removed(tmp_path: pathlib.Path) -> None:
    """Deleting it would look tidy and would lose the user's entry without telling them.

    A document on a drive that is currently unplugged is missing today and present
    tomorrow, so "missing" is a state to show, not a reason to forget.
    """
    store = RecentFiles(tmp_path / "recent.json")
    present = _document(tmp_path, "present.xml")
    absent = tmp_path / "absent.xml"
    store.add(present)
    store.add(absent)

    entries = store.entries()

    assert [entry.path for entry in entries] == [absent, present]
    assert [entry.exists for entry in entries] == [False, True]
    assert store.paths() == (absent, present), "still remembered, still in order"


def test_the_user_can_remove_an_entry_themselves(tmp_path: pathlib.Path) -> None:
    store = RecentFiles(tmp_path / "recent.json")
    keep = _document(tmp_path, "keep.xml")
    drop = _document(tmp_path, "drop.xml")
    store.add(keep)
    store.add(drop)

    store.remove(drop)

    assert store.paths() == (keep,)


def test_clearing_empties_the_store(tmp_path: pathlib.Path) -> None:
    store = RecentFiles(tmp_path / "recent.json")
    store.add(_document(tmp_path, "one.xml"))

    store.clear()

    assert store.paths() == ()


# --- a store that went bad ----------------------------------------------------


def test_a_corrupt_store_reads_as_empty_rather_than_raising(tmp_path: pathlib.Path) -> None:
    """A window that will not open because a cache file went bad is the worse outcome."""
    target = tmp_path / "recent.json"
    target.write_text("{ this is not json", encoding="utf-8")

    assert RecentFiles(target).paths() == ()


def test_a_store_of_the_wrong_shape_reads_as_empty(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "recent.json"
    target.write_text(json.dumps(["not", "a", "mapping"]), encoding="utf-8")

    assert RecentFiles(target).paths() == ()


def test_a_recent_key_that_is_not_a_list_reads_as_empty(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "recent.json"
    target.write_text(json.dumps({"recent": "not a list"}), encoding="utf-8")

    assert RecentFiles(target).paths() == ()


def test_non_string_entries_are_ignored(tmp_path: pathlib.Path) -> None:
    target = tmp_path / "recent.json"
    target.write_text(json.dumps({"recent": ["/a.xml", 7, None, "/b.xml"]}), encoding="utf-8")

    assert [path.name for path in RecentFiles(target).paths()] == ["a.xml", "b.xml"]


# --- across a restart ---------------------------------------------------------


def test_the_list_is_still_there_in_a_second_process(tmp_path: pathlib.Path) -> None:
    """**The test that matters.** Writing and reading in one process proves nothing.

    Two independent interpreters, the first exiting before the second starts. If the list
    only ever lived in memory, this is the test that would find out.
    """
    store = tmp_path / "recent.json"
    document = _document(tmp_path, "kept.xml")

    writer = textwrap.dedent(
        f"""
        import pathlib
        from gigaxml.gui.recent_files import RecentFiles
        RecentFiles(pathlib.Path({str(store)!r})).add(pathlib.Path({str(document)!r}))
        """
    )
    written = subprocess.run(
        [sys.executable, "-c", writer], capture_output=True, text=True, check=False, cwd=REPO
    )
    assert written.returncode == 0, written.stderr
    assert store.is_file(), "the first process left nothing on disk"

    reader = textwrap.dedent(
        f"""
        import pathlib
        from gigaxml.gui.recent_files import RecentFiles
        entries = RecentFiles(pathlib.Path({str(store)!r})).entries()
        for entry in entries:
            print(f"{{entry.path}}|{{entry.exists}}")
        """
    )
    read = subprocess.run(
        [sys.executable, "-c", reader], capture_output=True, text=True, check=False, cwd=REPO
    )
    assert read.returncode == 0, read.stderr

    assert read.stdout.strip() == f"{document}|True"


# --- where it lives -----------------------------------------------------------


def test_the_state_directory_follows_the_environment(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """How a test, or a throwaway run, stays out of the real user's home directory."""
    monkeypatch.setenv("GIGAXML_GUI_STATE_DIR", str(tmp_path))

    assert default_state_dir() == tmp_path


def test_the_default_state_directory_is_under_the_users_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The product default. A per-machine location is right for the product and wrong
    for a test, which is why the environment variable above exists."""
    monkeypatch.delenv("GIGAXML_GUI_STATE_DIR", raising=False)

    location = default_state_dir()

    assert location.is_absolute()
    assert location.name == "gigaxml"


def test_the_posix_state_directory_follows_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Checked by pretending to be Linux, because that branch cannot be reached here and
    a branch nobody runs is a branch nobody has checked."""
    monkeypatch.delenv("GIGAXML_GUI_STATE_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert default_state_dir() == tmp_path / "gigaxml"


def test_the_posix_state_directory_falls_back_to_dot_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GIGAXML_GUI_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(sys, "platform", "linux")

    assert default_state_dir() == pathlib.Path.home() / ".config" / "gigaxml"
