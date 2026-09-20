"""Four document shapes that had no fixture and no test: CDATA, comments, PIs, mixed content.

These are about the **value and candidate** side, not the parser. ``iterparse`` emits no
events for comments or processing instructions (measured in Phase 1, and asserted in
``tests/integration/test_element_lifecycle.py``), so the parser never sees them; what
was untested was whether everything built on top behaves.

**Two tests here are expected to fail.** A comment or processing instruction *before*
the root element makes ``_release`` call ``del elem.getparent()[0]`` on an element with
no parent, which raises ``TypeError``. That is a defect in product code, not a gap in
the tests, and this phase is not allowed to change product behaviour -- so it is pinned
with ``xfail(strict=True)``: the marker documents it, and the day somebody fixes it
these tests start passing and pytest fails loudly, which is the signal to remove the
marker. See the report for the full characterisation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.cli import main
from gigaxml.config import parse_config
from gigaxml.fields import extract_record
from gigaxml.inspect import inspect_document
from gigaxml.parser.streaming import StreamingRecordReader

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def rows_of(name: str, record: str, fields: dict[str, dict[str, str]]) -> list[dict[str, object]]:
    config = parse_config({"record": record, "fields": fields})
    return [
        extract_record(element, config.fields).values
        for element in StreamingRecordReader(FIXTURES / name, config.record_path)
    ]


def candidate_paths(name: str) -> list[str]:
    return [candidate.path for candidate in inspect_document(FIXTURES / name).candidates]


# --- CDATA ------------------------------------------------------------------


def test_cdata_content_is_the_text_not_the_marker() -> None:
    """``<![CDATA[x]]>`` holds text; the extracted value must be ``x``.

    Getting ``"<![CDATA[x]]>"`` back would be the kind of plausible-looking wrong answer
    that a downstream consumer cannot detect, so it is worth pinning.
    """
    rows = rows_of("cdata.xml", "/root/item", {"id": {"path": "id"}, "body": {"path": "body"}})

    assert rows == [
        {"id": "1", "body": '<b>bold</b> & "quoted"'},
        {"id": "2", "body": "plain text"},
        {"id": "3", "body": "a & b tail"},
    ]
    for row in rows:
        assert "<![CDATA[" not in str(row["body"])
        assert "]]>" not in str(row["body"])


def test_cdata_does_not_disturb_the_candidates() -> None:
    assert candidate_paths("cdata.xml") == ["/root/item"]


def test_cdata_survives_the_whole_cli_path(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps({"record": "/root/item", "fields": {"body": {"path": "body"}}}), encoding="utf-8"
    )
    output = tmp_path / "out.csv"

    assert main(["extract", str(FIXTURES / "cdata.xml"), "-c", str(config), "-o", str(output)]) == 0

    text = output.read_text(encoding="utf-8")
    assert "<![CDATA[" not in text
    assert '"quoted"' in text


# --- comments ---------------------------------------------------------------


def test_comments_inside_the_tree_produce_no_paths(tmp_path: Path) -> None:
    """A comment is not an element and must not become a candidate or a field.

    The document is built here rather than read from ``with_comments.xml`` because that
    fixture deliberately carries a **leading** comment as well, and a leading comment
    trips the defect pinned by the xfail below. Testing the inner shape on its own keeps
    this test about comments rather than about that defect.
    """
    source = tmp_path / "comments.xml"
    source.write_text(
        "<root><!-- between the root and the records -->"
        "<item><a>1</a><!-- inside a record --></item>"
        "<item><a>2</a></item>"
        "<!-- after the records --></root>",
        encoding="utf-8",
    )

    paths = [candidate.path for candidate in inspect_document(source).candidates]

    assert paths == ["/root/item"]
    assert not any("comment" in path for path in paths)


def test_comments_inside_the_tree_do_not_disturb_values(tmp_path: Path) -> None:
    source = tmp_path / "comments.xml"
    source.write_text(
        "<root><item><a>1</a><!-- inside --></item><!-- between --><item><a>2</a></item></root>",
        encoding="utf-8",
    )

    config = parse_config({"record": "/root/item", "fields": {"a": {"path": "a"}}})
    rows = [
        extract_record(element, config.fields).values
        for element in StreamingRecordReader(source, config.record_path)
    ]

    assert rows == [{"a": "1"}, {"a": "2"}]


@pytest.mark.xfail(strict=True, reason="a leading comment crashes _release; see the report")
def test_a_comment_before_the_root_is_handled() -> None:
    """Known defect, pinned rather than fixed.

    ``_release`` loops ``while elem.getprevious() is not None: del elem.getparent()[0]``.
    The root element has no parent, so the loop is normally never entered -- but a
    comment before the root *is* the root's previous sibling, so it is entered and the
    deletion raises ``TypeError``. Any document with a licence header hits this.
    """
    assert candidate_paths("with_comments.xml") == ["/root/item"]


@pytest.mark.xfail(strict=True, reason="a leading PI crashes _release; see the report")
def test_a_pi_before_the_root_is_handled() -> None:
    """The same defect, reached through a processing instruction."""
    assert candidate_paths("with_pi.xml") == ["/root/item"]


def test_comments_and_pis_after_the_root_are_fine(tmp_path: Path) -> None:
    """The shape that works, so the xfails above are about position and nothing else."""
    document = (
        "<root><item><a>1</a></item><item><a>2</a></item></root>"
        "<!-- a trailing comment -->"
        "<?trailing pi?>"
    )
    source = tmp_path / "trailing.xml"
    source.write_text(document, encoding="utf-8")

    config = parse_config({"record": "/root/item", "fields": {"a": {"path": "a"}}})
    rows = [
        extract_record(element, config.fields).values
        for element in StreamingRecordReader(source, config.record_path)
    ]

    assert rows == [{"a": "1"}, {"a": "2"}]


def test_comments_and_pis_inside_the_root_are_fine(tmp_path: Path) -> None:
    document = (
        "<root><!-- leading inside --><?pi inside?>"
        "<item><a>1</a></item><!-- between --><item><?pi between?><a>2</a></item>"
        "</root>"
    )
    source = tmp_path / "inside.xml"
    source.write_text(document, encoding="utf-8")

    config = parse_config({"record": "/root/item", "fields": {"a": {"path": "a"}}})
    rows = [
        extract_record(element, config.fields).values
        for element in StreamingRecordReader(source, config.record_path)
    ]

    assert rows == [{"a": "1"}, {"a": "2"}]


# --- processing instructions ------------------------------------------------


def test_a_pi_inside_a_record_does_not_become_a_field(tmp_path: Path) -> None:
    source = tmp_path / "pi.xml"
    source.write_text(
        "<root><item><a>1</a><?target data?></item><item><a>2</a></item></root>",
        encoding="utf-8",
    )

    config = parse_config({"record": "/root/item", "fields": {"a": {"path": "a"}}})
    rows = [
        extract_record(element, config.fields).values
        for element in StreamingRecordReader(source, config.record_path)
    ]

    assert rows == [{"a": "1"}, {"a": "2"}]


# --- mixed content ----------------------------------------------------------


def test_mixed_content_candidates_are_sensible() -> None:
    """``body`` has children, so it is a candidate in its own right as well as the record.

    Both are offered, and ``item`` comes first: it repeats as often and has more
    structure. The point of pinning this is that mixed content does not confuse the
    scoring into offering something odd, like the ``<b>`` inside ``body``.
    """
    assert candidate_paths("mixed_content.xml") == ["/root/item", "/root/item/body"]


def test_mixed_content_text_joins_text_and_child_text() -> None:
    """``Hello <b>world</b> again`` reads as ``Hello world again``.

    The documented rule is ``" ".join("".join(elem.itertext()).split())``: text from
    children is included, runs of whitespace collapse, and adjacent runs are separated.
    """
    rows = rows_of(
        "mixed_content.xml",
        "/root/item",
        {"id": {"path": "id"}, "body": {"path": "body"}, "bold": {"path": "body/b"}},
    )

    assert rows == [
        {"id": "1", "body": "Hello world again", "bold": "world"},
        {"id": "2", "body": "Only text", "bold": None},
        {"id": "3", "body": "all inside elements", "bold": "all"},
    ]


def test_mixed_content_does_not_leak_between_records(tmp_path: Path) -> None:
    """A field taken from a child element must not pick up the next record's text."""
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps(
            {"record": "/root/item", "fields": {"id": {"path": "id"}, "bold": {"path": "body/b"}}}
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out.csv"

    assert (
        main(["extract", str(FIXTURES / "mixed_content.xml"), "-c", str(config), "-o", str(output)])
        == 0
    )

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines == ["id,bold", "1,world", "2,", "3,all"]
