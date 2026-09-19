"""Integration tests: the element lifecycle contract.

:class:`StreamingRecordReader` yields a record and then clears and unlinks it as
soon as the consumer asks for the next one. That is what keeps memory flat, and it
is also a trap: any code that keeps an ``Element`` -- or a list of them -- gets an
object that silently answers ``None`` and ``""`` afterwards instead of raising.

The Phase 1 docstring stated the contract but nothing pinned it. These tests do,
in both directions:

* the positive half proves a record is *complete* inside the iteration step, which
  is what makes extraction possible at all;
* the negative half proves the completeness is gone afterwards, so a future
  refactor cannot quietly "fix" the clearing away and leave a reader whose memory
  grows with the document.

Measured behaviour, asserted below: ``clear()`` removes children, attributes and
text but keeps the element's ``tag`` and its ``tail`` (``keep_tail=True``, so the
surviving tree stays well-formed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.fields import extract_record
from gigaxml.parser.streaming import StreamingRecordReader

URI = "urn:example:shop"
FIXTURE = "extract_default_ns.xml"
RECORD_PATH = "/catalog/products/product"

FIELDS: dict[str, dict[str, Any]] = {
    "product_id": {"path": "@id"},
    "name": {"path": "name"},
    "manufacturer": {"path": "manufacturer/name"},
}


def build_config() -> ExtractionConfig:
    return parse_config({"record": RECORD_PATH, "namespaces": {"": URI}, "fields": FIELDS})


def reader(fixtures_dir: Path) -> StreamingRecordReader:
    return StreamingRecordReader(fixtures_dir / FIXTURE, RECORD_PATH, {"": URI})


# --- the positive half: complete inside the step ----------------------------


def test_the_record_is_fully_intact_inside_the_iteration_step(fixtures_dir: Path) -> None:
    for record in reader(fixtures_dir):
        assert len(record) == 6
        assert set(record.attrib) == {"id", "active"}
        assert record.findall(f"{{{URI}}}name")


def test_extraction_within_the_step_sees_every_value(fixtures_dir: Path) -> None:
    config = build_config()
    rows = [extract_record(record, config.fields).values for record in reader(fixtures_dir)]

    assert rows == [
        {"product_id": "1", "name": "Aurora Desk Lamp", "manufacturer": "Northwind Works"},
        {"product_id": "2", "name": "Pulse Audio Suite", "manufacturer": "Kestrel Labs"},
    ]


# --- the negative half: gone after the step ---------------------------------


def test_the_element_is_cleared_once_the_consumer_advances(fixtures_dir: Path) -> None:
    stream = iter(reader(fixtures_dir))

    first = next(stream)
    assert len(first) == 6
    assert dict(first.attrib) == {"id": "1", "active": "yes"}

    next(stream)  # advance past the first record

    assert len(first) == 0
    assert dict(first.attrib) == {}
    assert first.text is None
    assert first.findall("name") == []


def test_the_last_record_is_cleared_too_when_the_stream_is_exhausted(
    fixtures_dir: Path,
) -> None:
    stream = iter(reader(fixtures_dir))
    first = next(stream)
    last = next(stream)
    assert len(last) == 6

    with pytest.raises(StopIteration):
        next(stream)

    assert len(last) == 0
    assert len(first) == 0


def test_list_of_the_reader_holds_only_cleared_elements(fixtures_dir: Path) -> None:
    """The brief's assertion, made precise.

    ``list(reader)`` is a list of *cleared* elements -- but only once the reader
    has been exhausted, because the clearing happens when the consumer asks for
    the next item. ``[len(e) for e in reader]`` would therefore still see 6,
    whereas ``els = list(reader)`` followed by ``len(els[0])`` sees 0.
    """
    elements = list(reader(fixtures_dir))

    assert len(elements) == 2
    assert [len(element) for element in elements] == [0, 0]
    assert [dict(element.attrib) for element in elements] == [{}, {}]
    assert [element.text for element in elements] == [None, None]


def test_clearing_keeps_the_tag_and_the_tail(fixtures_dir: Path) -> None:
    """``keep_tail=True`` is deliberate: dropping the tail would corrupt the tree."""
    elements = list(reader(fixtures_dir))

    assert [element.tag for element in elements] == [f"{{{URI}}}product"] * 2
    assert all(element.tail for element in elements), "the tail whitespace survives"


def test_a_cleared_element_answers_none_and_empty_instead_of_raising(
    fixtures_dir: Path,
) -> None:
    """The failure mode the contract exists to prevent: silent empty answers."""
    elements = list(reader(fixtures_dir))
    cleared = elements[0]

    assert cleared.get("id") is None
    assert cleared.find("name") is None
    assert "".join(cleared.itertext()) == ""


# --- why the contract matters -----------------------------------------------


def test_a_deferred_extractor_silently_loses_every_value(fixtures_dir: Path) -> None:
    """The reversed test: collect first, extract later, get nothing.

    This is what a plausible-looking refactor would do -- buffer the elements,
    then map the extractor over them. Nothing raises. The rows simply come back
    empty, which is exactly the class of bug (silent, plausible output) this
    project exists to avoid. If a future change makes this test pass, the reader
    has stopped releasing its elements.
    """
    config = build_config()
    elements = list(reader(fixtures_dir))

    rows = [extract_record(element, config.fields).values for element in elements]

    # Every field is gone, including the ones whose path is a single segment:
    # a cleared element has no children left to find, so the descent stops
    # immediately and each field is reported as missing.
    assert rows == [
        {"product_id": None, "name": None, "manufacturer": None},
        {"product_id": None, "name": None, "manufacturer": None},
    ]


def test_the_extractor_keeps_no_element_reference(fixtures_dir: Path) -> None:
    """Everything the extractor returns must be plain Python data."""
    config = build_config()
    results = [extract_record(record, config.fields) for record in reader(fixtures_dir)]

    def assert_no_elements(value: object) -> None:
        assert not isinstance(value, etree._Element)
        if isinstance(value, dict):
            for item in value.values():
                assert_no_elements(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                assert_no_elements(item)

    for result in results:
        assert_no_elements(result.values)
        assert_no_elements(result.multi_matches)

    # And the values are still correct, so nothing was read late.
    assert [result.values["product_id"] for result in results] == ["1", "2"]
