"""What the window can say about a document before reading it.

The estimate is arithmetic on a measured rate, so the arithmetic is pinned here with an
injected rate. **The rate itself is deliberately not asserted** -- it is a property of the
machine it was measured on, and a test that failed because somebody's laptop is slower
would be a test that punishes the wrong thing.
"""

from __future__ import annotations

import pathlib

from gigaxml.gui.document_info import (
    SLOW_ANALYSIS_SECONDS,
    describe,
    looks_like_document,
)

MIB = 1024 * 1024


def _document(tmp_path: pathlib.Path, name: str, mebibytes: float = 0.001) -> pathlib.Path:
    path = tmp_path / name
    path.write_bytes(b"<catalog/>" + b" " * max(0, int(mebibytes * MIB) - 10))
    return path


# --- describing a file --------------------------------------------------------


def test_a_present_document_is_described_with_its_size_and_time(tmp_path: pathlib.Path) -> None:
    path = _document(tmp_path, "doc.xml", mebibytes=0.5)

    info = describe(path)

    assert info.exists is True
    assert info.size_bytes == path.stat().st_size
    assert info.modified is not None
    assert info.size_mib > 0.4


def test_a_document_that_is_not_there_is_described_rather_than_raised(
    tmp_path: pathlib.Path,
) -> None:
    """A recent-files row for a document on an unplugged drive still has to render."""
    info = describe(tmp_path / "gone.xml")

    assert info.exists is False
    assert info.size_bytes == 0
    assert info.modified is None


def test_a_directory_is_not_a_document(tmp_path: pathlib.Path) -> None:
    folder = tmp_path / "things.xml"
    folder.mkdir()

    assert describe(folder).exists is False


# --- the estimate -------------------------------------------------------------


def test_the_estimate_is_arithmetic_on_the_rate_that_was_passed_in(
    tmp_path: pathlib.Path,
) -> None:
    """Pinned with a rate of our own so this cannot depend on how fast the machine is."""
    info = describe(_document(tmp_path, "doc.xml", mebibytes=100))

    assert info.estimated_seconds(rate=10.0) == info.size_mib / 10.0


def test_a_small_document_says_nothing_about_time(tmp_path: pathlib.Path) -> None:
    """A warning on every file is a warning nobody reads."""
    info = describe(_document(tmp_path, "small.xml", mebibytes=0.5))

    assert info.estimate_note(rate=14.0) == ""


def test_a_large_document_warns_before_the_wait_not_after(tmp_path: pathlib.Path) -> None:
    info = describe(_document(tmp_path, "large.xml", mebibytes=400))

    note = info.estimate_note(rate=14.0)

    assert note, "a 400 MiB document is not instant and the panel has to say so"
    assert "s" in note
    seconds = info.estimated_seconds(rate=14.0)
    assert seconds > SLOW_ANALYSIS_SECONDS
    assert f"{seconds:.0f}" in note, "the estimate has to be the one it computed"


def test_a_very_large_document_switches_to_minutes(tmp_path: pathlib.Path) -> None:
    info = describe(_document(tmp_path, "huge.xml", mebibytes=2048))

    note = info.estimate_note(rate=14.0)

    assert "min" in note


def test_a_document_that_is_not_there_gets_no_estimate(tmp_path: pathlib.Path) -> None:
    assert describe(tmp_path / "gone.xml").estimate_note() == ""


# --- what counts as a document ------------------------------------------------


def test_the_suffixes_the_dialog_offers_are_the_suffixes_a_drop_may_be(
    tmp_path: pathlib.Path,
) -> None:
    plain = _document(tmp_path, "catalog.xml")
    gzipped = _document(tmp_path, "catalog.xml.gz")

    assert looks_like_document(plain) is True
    assert looks_like_document(gzipped) is True


def test_the_suffix_check_is_case_insensitive(tmp_path: pathlib.Path) -> None:
    """A file called `CATALOG.XML` is a document on every platform that has such a name."""
    assert looks_like_document(_document(tmp_path, "CATALOG.XML")) is True


def test_other_files_are_not_documents(tmp_path: pathlib.Path) -> None:
    assert looks_like_document(_document(tmp_path, "notes.txt")) is False


def test_a_directory_named_like_a_document_is_still_not_a_document(
    tmp_path: pathlib.Path,
) -> None:
    """The suffix is necessary and not sufficient. A folder called `data.xml` is a folder."""
    folder = tmp_path / "data.xml"
    folder.mkdir()

    assert looks_like_document(folder) is False


def test_a_path_that_is_not_there_is_not_a_document(tmp_path: pathlib.Path) -> None:
    assert looks_like_document(tmp_path / "gone.xml") is False


def test_a_gzip_suffix_on_a_file_that_is_not_xml_is_not_a_document(
    tmp_path: pathlib.Path,
) -> None:
    assert looks_like_document(_document(tmp_path, "archive.tar.gz")) is False


def test_a_rate_of_zero_is_not_a_division(tmp_path: pathlib.Path) -> None:
    """A caller who has not measured their machine may pass anything; none of it may
    turn into a ZeroDivisionError in a window's information bar."""
    info = describe(_document(tmp_path, "doc.xml", mebibytes=100))

    assert info.estimated_seconds(rate=0.0) == 0.0
    assert info.estimated_seconds(rate=-1.0) == 0.0
    assert info.estimate_note(rate=0.0) == ""
