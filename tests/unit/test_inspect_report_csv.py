"""The path table's CSV escaping.

**Why this is not an integration test.** Gate 6 asks that a path containing a comma or a
quote still produce the right number of columns. No such path can come out of a document:
XML element names may not contain either character, so `inspect` can never report one and
a test that wrote such a file would be testing a document that is not XML. The escaping is
therefore exercised where it lives -- on entries constructed by hand -- and the panel test
only checks that the panel calls this function rather than joining columns itself.

The failure being prevented is quiet: `",".join(row)` produces a file that opens, looks
like a table, and has a row with one column too many.
"""

from __future__ import annotations

import csv
import io

from gigaxml.gui.inspect_report import InspectReport, PathEntry, paths_to_csv

HEADER = ["path", "count", "depth", "has_children", "shape_consistency", "distinct_shapes"]


def _entry(path: str, count: int = 1) -> PathEntry:
    return PathEntry(
        path=path,
        count=count,
        depth=path.count("/"),
        has_children=False,
        shape_consistency=1.0,
        distinct_shapes=1,
        dominant_shape_count=count,
    )


def _report(*paths: str) -> InspectReport:
    return InspectReport(
        source="constructed",
        input_mb=0.0,
        elements_seen=len(paths),
        namespaces={},
        shadowed_prefixes=(),
        unmapped_namespaces=(),
        candidates=(),
        paths=tuple(_entry(path) for path in paths),
        paths_truncated=False,
        paths_limit=10_000,
        paths_tracked=len(paths),
        untracked_occurrences=0,
        depth_seen=1,
        depth_limit=32,
        depth_truncated=False,
        values_truncated=False,
    )


def _rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_the_header_is_the_first_row() -> None:
    assert _rows(paths_to_csv(_report("/a")))[0] == HEADER


def test_an_ordinary_table_round_trips() -> None:
    rows = _rows(paths_to_csv(_report("/a", "/a/b")))

    assert [row[0] for row in rows[1:]] == ["/a", "/a/b"]
    assert len(rows) == 3


def test_a_path_with_a_comma_keeps_its_column_count() -> None:
    rows = _rows(paths_to_csv(_report("/a,b", "/plain")))

    assert {len(row) for row in rows} == {len(HEADER)}
    assert rows[1][0] == "/a,b", "the comma has to survive, not be dropped"


def test_a_path_with_a_quote_keeps_its_column_count() -> None:
    rows = _rows(paths_to_csv(_report('/a"b', "/plain")))

    assert {len(row) for row in rows} == {len(HEADER)}
    assert rows[1][0] == '/a"b'


def test_a_path_with_a_comma_and_a_quote_together() -> None:
    awkward = '/a,b"c'

    rows = _rows(paths_to_csv(_report(awkward)))

    assert {len(row) for row in rows} == {len(HEADER)}
    assert rows[1][0] == awkward


def test_a_path_with_a_newline_does_not_become_two_rows() -> None:
    """A newline inside a field is the other way a table quietly gains a row."""
    rows = _rows(paths_to_csv(_report("/a\nb")))

    assert len(rows) == 2, "the embedded newline was written literally"
    assert rows[1][0] == "/a\nb"


def test_the_counts_are_written_as_numbers_not_as_quoted_text() -> None:
    rows = _rows(paths_to_csv(_report("/a")))

    assert rows[1][1] == "1"
    assert rows[1][2] == "1"
    assert rows[1][4] == "1.0"


def test_an_empty_table_is_a_header_and_nothing_else() -> None:
    assert _rows(paths_to_csv(_report())) == [HEADER]
