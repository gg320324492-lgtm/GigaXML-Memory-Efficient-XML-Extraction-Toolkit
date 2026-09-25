"""Saving, loading and remembering configs -- the parts a display cannot check.

**Why these are unit tests.** `saved_configs` does not import PySide6, and the cases worth
testing are the ones a window would not help with: a name that would escape the directory,
a file that was deleted from under the list, two stores sharing a directory.
"""

from __future__ import annotations

import pathlib

import pytest

from gigaxml.gui.saved_configs import ConfigLibrary

CONFIG = "source: input.xml\non_error: abort\n"


def test_nothing_is_written_by_merely_constructing(tmp_path: pathlib.Path) -> None:
    ConfigLibrary(tmp_path)

    assert not (tmp_path / "configs").exists()


def test_a_saved_config_reads_back_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """The claim worth testing: what comes back is what went in, through a second store."""
    ConfigLibrary(tmp_path).save("catalogue", CONFIG)

    assert ConfigLibrary(tmp_path).load("catalogue").read() == CONFIG


def test_saving_again_replaces_rather_than_appends(tmp_path: pathlib.Path) -> None:
    library = ConfigLibrary(tmp_path)
    library.save("catalogue", CONFIG)

    library.save("catalogue", "source: other.xml\n")

    assert library.load("catalogue").read() == "source: other.xml\n"
    assert library.names() == ("catalogue",)


def test_names_are_sorted_so_a_menu_does_not_reorder_itself(tmp_path: pathlib.Path) -> None:
    library = ConfigLibrary(tmp_path)
    for name in ("zebra", "alpha", "middle"):
        library.save(name, CONFIG)

    assert library.names() == ("alpha", "middle", "zebra")


@pytest.mark.parametrize(
    "bad",
    ["", "   ", ".", "..", "a/b", "a\\b", "with:colon", "star*", "pipe|"],
)
def test_a_name_that_would_escape_the_directory_is_refused(tmp_path: pathlib.Path, bad: str) -> None:
    """A name with a separator in it is a path, and accepting it writes outside the folder.

    Refused here rather than at the filesystem so the caller can say why, and so nothing is
    created before the refusal.
    """
    library = ConfigLibrary(tmp_path)

    with pytest.raises(ValueError):
        library.save(bad, CONFIG)


@pytest.mark.parametrize("good", ["catalogue", "my project", "配置", "v1.2", "a_b-c"])
def test_a_name_that_is_a_name_is_accepted(tmp_path: pathlib.Path, good: str) -> None:
    """Spaces, dots, dashes and non-ASCII are names. Only separators and control characters are not."""
    library = ConfigLibrary(tmp_path)

    library.save(good, CONFIG)

    assert library.names() == (good.strip(),)


def test_removing_takes_it_out_of_the_list(tmp_path: pathlib.Path) -> None:
    library = ConfigLibrary(tmp_path)
    library.save("catalogue", CONFIG)

    library.remove("catalogue")

    assert library.names() == ()


def test_removing_something_absent_is_not_an_error(tmp_path: pathlib.Path) -> None:
    """A caller that removes twice, or removes what a colleague already removed, is not broken."""
    ConfigLibrary(tmp_path).remove("never-existed")


def test_opening_notes_the_path_most_recent_first(tmp_path: pathlib.Path) -> None:
    library = ConfigLibrary(tmp_path)
    first = tmp_path / "one.yaml"
    second = tmp_path / "two.yaml"

    library.note_opened(first)
    library.note_opened(second)

    assert library.recent() == (second, first)


def test_opening_the_same_config_twice_does_not_duplicate_it(tmp_path: pathlib.Path) -> None:
    library = ConfigLibrary(tmp_path)
    path = tmp_path / "one.yaml"

    library.note_opened(path)
    library.note_opened(path)

    assert library.recent() == (path,)


def test_a_recent_config_that_vanished_is_marked_not_dropped(tmp_path: pathlib.Path) -> None:
    """Dropping it would leave the list looking tidy and the user unable to tell where it went."""
    library = ConfigLibrary(tmp_path)
    gone = tmp_path / "gone.yaml"
    library.note_opened(gone)

    entries = library.recent_entries()

    assert [entry.path for entry in entries] == [gone]
    assert entries[0].exists is False


def test_the_recent_configs_list_is_not_the_recent_documents_list(tmp_path: pathlib.Path) -> None:
    """Clearing one must not clear the other; they answer different questions."""
    library = ConfigLibrary(tmp_path)
    library.note_opened(tmp_path / "a.yaml")

    library.forget_recent()

    assert library.recent() == ()
    assert not (tmp_path / "recent.json").exists()


def test_two_libraries_over_one_directory_see_the_same_files(tmp_path: pathlib.Path) -> None:
    ConfigLibrary(tmp_path).save("catalogue", CONFIG)

    assert ConfigLibrary(tmp_path).names() == ("catalogue",)
