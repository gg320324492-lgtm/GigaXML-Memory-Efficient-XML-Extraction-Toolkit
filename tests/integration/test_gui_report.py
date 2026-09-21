"""Reading ``inspect --json``: the contract, without a window.

The report is what the analysis panel is built on, so the parsing has to be right before
any widget is involved. Everything here runs without PySide6.

Two of these pin cases the substep notes call out as self-checks, and one of them is
subtle enough to be worth naming: ``tests/fixtures/shadowed.xml`` is the document where a
prefix is declared two levels up and used once, which is **not** a rebinding. The fixture
name is about the shape of the document, not about the warning. The rebinding case is a
different document, written inline in the unit tests, and it is reproduced here.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from gigaxml.gui.inspect_report import (
    InspectReport,
    PathEntry,
    parse_report,
    paths_to_csv,
)
from tests._interpreter import gigaxml_script

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
FIXTURES = REPO / "tests/fixtures"

#: A prefix bound to one namespace at the top and rebound deeper down. This is the case
#: that produces a warning; see the note in the module docstring.
REBOUND = (
    '<root xmlns:p="urn:one"><p:a><p:b/></p:a><other xmlns:p="urn:two"><p:c/><p:c/></other></root>'
)


def inspect_json(source: pathlib.Path, *extra: str) -> dict:
    completed = subprocess.run(
        [str(gigaxml_script()), "inspect", str(source), "--json", *extra],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO),
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    return json.loads(completed.stdout)


# --- the shape of the payload -------------------------------------------------


def test_a_real_report_parses() -> None:
    report = parse_report(inspect_json(REPO / "data/s10.xml"))

    assert report is not None
    assert report.source.endswith("s10.xml")
    assert report.elements_seen > 0
    assert report.candidates, "the fixture has records"
    assert report.paths


def test_something_that_is_not_a_report_is_not_a_report() -> None:
    """The child may print something unexpected. That must not be fatal."""
    assert parse_report(None) is None
    assert parse_report({}) is None
    assert parse_report({"candidates": []}) is not None, "an empty document is still a report"
    assert parse_report([1, 2, 3]) is None
    assert parse_report("not a mapping") is None


def test_a_candidate_missing_fields_does_not_raise() -> None:
    """A future version may add or move keys. Older windows must degrade, not crash."""
    report = parse_report({"candidates": [{"path": "/a"}], "paths": [{}]})

    assert report is not None
    assert report.candidates[0].path == "/a"
    assert report.candidates[0].count == 0
    assert report.paths[0].path == ""


def test_the_nested_marker_comes_from_the_json_not_from_the_text() -> None:
    """The contract is `nested_inside`; the warning wording on stderr is not."""
    report = parse_report(inspect_json(REPO / "data/s10.xml"))
    assert report is not None

    nested = [candidate for candidate in report.candidates if candidate.is_nested]
    assert nested, "the fixture has structures inside its records"
    assert all(candidate.nested_inside for candidate in nested)

    top = [candidate for candidate in report.candidates if not candidate.is_nested]
    assert top, "and it has at least one top-level candidate"


def test_candidate_lookup_by_path() -> None:
    report = parse_report(inspect_json(REPO / "data/s10.xml"))
    assert report is not None

    first = report.candidates[0]
    assert report.candidate_for(first.path) is first
    assert report.candidate_for("/nothing/like/this") is None


# --- the warnings --------------------------------------------------------------


def test_the_shadowed_fixture_is_not_a_rebinding() -> None:
    """`shadowed.xml` is the negative case, despite the name.

    The prefix is declared two levels up and used once, which is a perfectly flat
    namespace. An existing unit test asserts the same thing; this records it where the
    window's own tests will look for it, so that a later reader does not reach for the
    fixture by its name and conclude the warning is broken.
    """
    report = parse_report(inspect_json(FIXTURES / "shadowed.xml"))

    assert report is not None
    assert report.shadowed_prefixes == ()
    assert not report.has_warnings


def test_a_rebound_prefix_is_reported_and_explained(tmp_path: pathlib.Path) -> None:
    document = tmp_path / "rebound.xml"
    document.write_text(REBOUND, encoding="utf-8")

    report = parse_report(inspect_json(document))

    assert report is not None
    assert report.shadowed_prefixes == ("p",)
    assert report.has_warnings
    lines = report.warnings()
    assert any("p" in line and "more than one thing" in line for line in lines)


def test_a_truncated_path_table_is_reported_with_its_numbers() -> None:
    report = parse_report(inspect_json(REPO / "data/s10.xml", "--max-paths", "5"))

    assert report is not None
    assert report.paths_truncated
    assert report.paths_limit == 5
    assert len(report.paths) == 5
    assert report.untracked_occurrences > 0

    line = next(line for line in report.warnings() if "stopped at" in line)
    assert "5" in line
    assert f"{report.untracked_occurrences:,}" in line


def test_no_warnings_on_a_clean_document() -> None:
    report = parse_report(inspect_json(REPO / "data/s10.xml"))

    assert report is not None
    assert not report.paths_truncated
    assert report.warnings() == []


# --- the export ----------------------------------------------------------------


def test_the_csv_export_has_a_header_and_one_row_per_path() -> None:
    report = parse_report(inspect_json(REPO / "data/s10.xml"))
    assert report is not None

    text = paths_to_csv(report)
    lines = text.strip().splitlines()

    assert lines[0] == "path,count,depth,has_children,shape_consistency,distinct_shapes"
    assert len(lines) == len(report.paths) + 1


def test_the_csv_export_escapes_awkward_paths() -> None:
    """A path can contain a comma or a quote. A naive join would produce a broken file."""
    report = InspectReport(
        source="x",
        input_mb=0.0,
        elements_seen=0,
        namespaces={},
        shadowed_prefixes=(),
        unmapped_namespaces=(),
        candidates=(),
        paths=(
            _entry("/root/a,b"),
            _entry('/root/he said "hi"'),
        ),
        paths_truncated=False,
        paths_limit=0,
        paths_tracked=0,
        untracked_occurrences=0,
        depth_seen=0,
        depth_limit=0,
        depth_truncated=False,
        values_truncated=False,
    )

    lines = paths_to_csv(report).strip().splitlines()

    assert len(lines) == 3, "three lines, not more: the commas are inside quotes"
    assert '"/root/a,b"' in lines[1]
    assert '"/root/he said ""hi"""' in lines[2]


def _entry(path: str) -> PathEntry:
    return PathEntry(
        path=path,
        count=1,
        depth=1,
        has_children=False,
        shape_consistency=1.0,
        distinct_shapes=1,
        dominant_shape_count=1,
    )


@pytest.mark.parametrize("name", ["shadowed.xml", "namespaced.xml", "with_comments.xml"])
def test_the_parser_survives_every_awkward_fixture(name: str) -> None:
    """These are the documents the CLI's own tests use for the hard cases."""
    source = FIXTURES / name
    if not source.is_file():
        pytest.skip(f"{source} is not present")

    report = parse_report(inspect_json(source))

    assert report is not None
    assert report.source.endswith(name)
