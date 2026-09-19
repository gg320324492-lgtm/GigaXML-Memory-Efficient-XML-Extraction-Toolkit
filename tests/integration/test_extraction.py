"""Integration tests: config + reader + extractor against the two fixtures.

The two fixtures carry **the same document** twice: once with a default namespace
and once with a prefix. Because lxml matches on qualified names, the same config
must extract identical values from both -- that is asserted directly, and it is
the cheapest proof that matching is by URI rather than by prefix spelling.
"""

from __future__ import annotations

import datetime
import decimal
from pathlib import Path
from typing import Any

import pytest

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import FieldTypeError, MissingRequiredFieldError
from gigaxml.fields import ExtractionResult, extract_record
from gigaxml.parser.streaming import StreamingRecordReader

URI = "urn:example:shop"
DEFAULT_NS_FIXTURE = "extract_default_ns.xml"
PREFIXED_NS_FIXTURE = "extract_prefixed_ns.xml"

FIELDS: dict[str, dict[str, Any]] = {
    "product_id": {"path": "@id"},
    "active": {"path": "@active", "type": "bool"},
    "name": {"path": "name"},
    "price": {"path": "price", "type": "decimal", "required": True},
    "currency": {"path": "price/@currency"},
    "stock": {"path": "stock", "type": "int"},
    "released": {"path": "released", "type": "date"},
    "manufacturer": {"path": "manufacturer/name"},
    "country": {"path": "manufacturer/country"},
    "first_tag": {"path": "tags/tag"},
    "warranty": {"path": "warranty"},
}

EXPECTED_PRODUCT_1: dict[str, object] = {
    "product_id": "1",
    "active": True,
    "name": "Aurora Desk Lamp",
    "price": decimal.Decimal("49.90"),
    "currency": "USD",
    "stock": 12,
    "released": datetime.date(2023, 11, 14),
    "manufacturer": "Northwind Works",
    "country": "SE",
    "first_tag": "desk",
    "warranty": None,
}

EXPECTED_PRODUCT_2: dict[str, object] = {
    "product_id": "2",
    "active": False,
    "name": "Pulse Audio Suite",
    "price": decimal.Decimal("129.00"),
    "currency": "EUR",
    "stock": 0,
    "released": datetime.date(2024, 2, 29),
    "manufacturer": "Kestrel Labs",
    "country": "DE",
    "first_tag": "audio",
    "warranty": None,
}


def build_config(
    namespaces: dict[str, str],
    *,
    record: str = "/catalog/products/product",
    fields: dict[str, dict[str, Any]] | None = None,
) -> ExtractionConfig:
    return parse_config({"record": record, "namespaces": namespaces, "fields": fields or FIELDS})


def extract_all(fixture: Path, config: ExtractionConfig) -> list[ExtractionResult]:
    reader = StreamingRecordReader(fixture, config.record_path, config.namespaces)
    return [extract_record(record, config.fields) for record in reader]


# --- the two fixtures, value by value ---------------------------------------


def test_default_namespace_fixture_extracts_every_value(fixtures_dir: Path) -> None:
    config = build_config({"": URI})
    results = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)

    assert len(results) == 2
    assert results[0].values == EXPECTED_PRODUCT_1
    assert results[1].values == EXPECTED_PRODUCT_2


def test_prefixed_namespace_fixture_extracts_every_value(fixtures_dir: Path) -> None:
    fields = {
        name: {**definition, "path": _prefix_path(str(definition["path"]))}
        for name, definition in FIELDS.items()
    }
    config = build_config({"s": URI}, record="/s:catalog/s:products/s:product", fields=fields)
    results = extract_all(fixtures_dir / PREFIXED_NS_FIXTURE, config)

    assert len(results) == 2
    assert results[0].values == EXPECTED_PRODUCT_1
    assert results[1].values == EXPECTED_PRODUCT_2


def test_the_same_config_extracts_the_same_values_from_both_fixtures(
    fixtures_dir: Path,
) -> None:
    """One config, two documents that declare the namespace differently.

    ``<catalog xmlns="urn:...">`` and ``<s:catalog xmlns:s="urn:...">`` produce
    identical qualified names, so nothing in the config needs to change.
    """
    config = build_config({"": URI})
    from_default = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)
    from_prefixed = extract_all(fixtures_dir / PREFIXED_NS_FIXTURE, config)

    assert [r.values for r in from_default] == [r.values for r in from_prefixed]
    assert from_prefixed[0].values == EXPECTED_PRODUCT_1


def test_the_prefixed_spelling_agrees_with_the_default_namespace_spelling(
    fixtures_dir: Path,
) -> None:
    """``s:name`` and a bare ``name`` resolve to the same tag, so results match."""
    bare = extract_all(fixtures_dir / PREFIXED_NS_FIXTURE, build_config({"": URI}))
    prefixed_fields = {
        name: {**definition, "path": _prefix_path(str(definition["path"]))}
        for name, definition in FIELDS.items()
    }
    prefixed = extract_all(
        fixtures_dir / PREFIXED_NS_FIXTURE,
        build_config(
            {"s": URI},
            record="/s:catalog/s:products/s:product",
            fields=prefixed_fields,
        ),
    )

    assert [r.values for r in bare] == [r.values for r in prefixed]


@pytest.mark.parametrize(
    "field,expected,expected_type",
    [
        ("product_id", "1", str),
        ("active", True, bool),
        ("name", "Aurora Desk Lamp", str),
        ("price", decimal.Decimal("49.90"), decimal.Decimal),
        ("currency", "USD", str),
        ("stock", 12, int),
        ("released", datetime.date(2023, 11, 14), datetime.date),
        ("manufacturer", "Northwind Works", str),
        ("country", "SE", str),
        ("first_tag", "desk", str),
        ("warranty", None, type(None)),
    ],
)
def test_each_field_is_asserted_with_its_value_and_its_python_type(
    fixtures_dir: Path, field: str, expected: object, expected_type: type
) -> None:
    """Per-value assertions, including the exact Python type of each one."""
    config = build_config({"": URI})
    first = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)[0]

    value = first.values[field]
    assert value == expected
    assert type(value) is expected_type


# --- the five path forms ----------------------------------------------------


def test_all_five_path_forms_extract_from_one_record(fixtures_dir: Path) -> None:
    """``@attr`` / ``.`` / ``child`` / ``a/b`` / ``child/@attr`` in one pass."""
    config = build_config(
        {"": URI},
        fields={
            "attribute": {"path": "@id"},
            "self_text": {"path": "."},
            "child": {"path": "name"},
            "descend": {"path": "manufacturer/name"},
            "child_attribute": {"path": "price/@currency"},
        },
    )
    result = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)[0]

    assert result.values["attribute"] == "1"
    assert result.values["child"] == "Aurora Desk Lamp"
    assert result.values["descend"] == "Northwind Works"
    assert result.values["child_attribute"] == "USD"
    # "." is the record element's own itertext(), whitespace-normalised. The
    # fixture is pretty-printed, so the newlines *between* child elements are
    # real text nodes and each collapses to one space -- which is why this reads
    # as a list of values. Adjacent elements with no whitespace between them
    # would instead concatenate with nothing in between (see
    # tests/unit/test_fields.py::test_adjacent_nested_elements_do_not_gain_a_space).
    assert result.values["self_text"] == (
        "Aurora Desk Lamp 49.90 12 2023-11-14 Northwind Works SE desk led"
    )


def test_the_record_element_attribute_is_not_expanded_into_the_default_namespace(
    fixtures_dir: Path,
) -> None:
    """A default namespace must not turn ``@id`` into ``{uri}id``, which would miss."""
    config = build_config({"": URI})
    result = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)[0]
    assert result.values["product_id"] == "1"


# --- multi-match behaviour --------------------------------------------------


def test_a_multi_match_takes_the_first_and_reports_the_count(fixtures_dir: Path) -> None:
    """``tags/tag`` matches two children in product 1 and one in product 2."""
    config = build_config({"": URI})
    results = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)

    assert results[0].values["first_tag"] == "desk"
    assert results[0].multi_matches == {"first_tag": 1}

    assert results[1].values["first_tag"] == "audio"
    assert results[1].multi_matches == {}


def test_an_unambiguous_record_reports_no_multi_matches(fixtures_dir: Path) -> None:
    config = build_config({"": URI})
    result = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)[1]
    assert result.multi_matches == {}


def test_multi_match_counts_accumulate_across_steps(fixtures_dir: Path) -> None:
    """Two ambiguous steps on one path add up to the discarded-candidate total."""
    config = build_config(
        {"": URI},
        fields={"tags": {"path": "tags/tag"}, "manufacturer": {"path": "manufacturer/name"}},
    )
    results = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)
    # product 1: tags/tag matches 2 (1 discarded); manufacturer/name matches 1 (0).
    assert results[0].multi_matches == {"tags": 1}
    assert results[1].multi_matches == {}


# --- missing fields ---------------------------------------------------------


def test_a_missing_optional_field_is_none(fixtures_dir: Path) -> None:
    config = build_config({"": URI})
    for result in extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config):
        assert result.values["warranty"] is None


def test_a_missing_required_field_raises(fixtures_dir: Path) -> None:
    config = build_config(
        {"": URI},
        fields={"warranty": {"path": "warranty", "required": True}},
    )
    with pytest.raises(MissingRequiredFieldError) as info:
        extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)

    assert info.value.field == "warranty"
    assert "warranty" in str(info.value)


def test_a_present_but_empty_element_is_present_not_missing(tmp_path: Path) -> None:
    """An empty element exists, so it converts -- and fails loudly for ``int``."""
    document = tmp_path / "empty.xml"
    document.write_text(
        '<catalog><products><product id="1"><stock/></product></products></catalog>',
        encoding="utf-8",
    )
    config = build_config({}, fields={"stock": {"path": "stock", "type": "int"}})
    with pytest.raises(FieldTypeError) as info:
        extract_all(document, config)
    assert info.value.raw == ""


# --- type errors end to end -------------------------------------------------


def test_a_bad_value_raises_with_the_field_name_and_raw_value(tmp_path: Path) -> None:
    document = tmp_path / "bad.xml"
    document.write_text(
        '<catalog><products><product id="1"><stock>twelve</stock></product></products></catalog>',
        encoding="utf-8",
    )
    config = build_config({}, fields={"stock": {"path": "stock", "type": "int"}})

    with pytest.raises(FieldTypeError) as info:
        extract_all(document, config)

    assert info.value.field == "stock"
    assert info.value.raw == "twelve"


def test_an_indented_numeric_value_converts_despite_the_whitespace(tmp_path: Path) -> None:
    """The Phase 1 trap, pinned end to end: itertext() carries the indentation."""
    document = tmp_path / "indented.xml"
    document.write_text(
        '<catalog><products><product id="1">\n      <stock>\n        12\n      </stock>\n'
        "    </product></products></catalog>",
        encoding="utf-8",
    )
    config = build_config({}, fields={"stock": {"path": "stock", "type": "int"}})
    assert extract_all(document, config)[0].values["stock"] == 12


# --- whole-file behaviour ---------------------------------------------------


def test_every_record_is_extracted_exactly_once(fixtures_dir: Path) -> None:
    config = build_config({"": URI})
    results = extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config)
    assert [r.values["product_id"] for r in results] == ["1", "2"]


def test_the_extracted_values_contain_no_elements(fixtures_dir: Path) -> None:
    """Nothing that leaves the extractor may be an lxml element."""
    from lxml.etree import _Element

    config = build_config({"": URI})
    for result in extract_all(fixtures_dir / DEFAULT_NS_FIXTURE, config):
        for value in result.values.values():
            assert not isinstance(value, _Element)
        assert all(isinstance(count, int) for count in result.multi_matches.values())


def test_extraction_works_on_a_large_generated_document(s100_path: Path) -> None:
    """The extractor holds no per-record state, so it scales with the reader."""
    config = build_config(
        {},
        fields={"id": {"path": "@id"}, "name": {"path": "name"}},
    )
    reader = StreamingRecordReader(s100_path, config.record_path, config.namespaces)
    first = extract_record(next(iter(reader)), config.fields)

    assert first.values["id"] == "1"
    assert isinstance(first.values["name"], str)
    assert first.values["name"]


def _prefix_path(path: str) -> str:
    """Rewrite a bare field path into its ``s:``-prefixed spelling."""
    parts = path.split("/")
    return "/".join(part if part.startswith("@") or part == "." else f"s:{part}" for part in parts)
