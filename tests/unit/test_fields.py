"""Unit tests: field paths, text normalisation and type conversion.

These are the pieces that decide what a value *is*, so they are tested away from
any file: a wrong answer here is a wrong answer in every extracted row.
"""

from __future__ import annotations

import datetime
import decimal

import pytest
from lxml import etree

from gigaxml.errors import FieldPathError, FieldTypeError
from gigaxml.fields import FieldType, coerce_value, normalize_text, parse_field_path

URI = "urn:example:shop"


# --- the five supported path forms ------------------------------------------


def test_attribute_of_the_record_element() -> None:
    spec = parse_field_path("@id")
    assert spec.segments == ()
    assert spec.attribute == "id"
    assert spec.targets_self is True


def test_text_of_the_record_element() -> None:
    spec = parse_field_path(".")
    assert spec.segments == ()
    assert spec.attribute is None
    assert spec.targets_self is True


def test_child_element_text() -> None:
    spec = parse_field_path("Name")
    assert spec.segments == ("Name",)
    assert spec.attribute is None
    assert spec.targets_self is False


def test_step_down_through_children() -> None:
    spec = parse_field_path("Manufacturer/Name")
    assert spec.segments == ("Manufacturer", "Name")
    assert spec.attribute is None


def test_attribute_of_a_child_element() -> None:
    spec = parse_field_path("Price/@currency")
    assert spec.segments == ("Price",)
    assert spec.attribute == "currency"


def test_surrounding_whitespace_is_ignored() -> None:
    assert parse_field_path("  @id  ").attribute == "id"
    assert parse_field_path("\tManufacturer/Name\n").segments == ("Manufacturer", "Name")


# --- refused forms ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/Name", "//Name", "/catalog/products/product"])
def test_absolute_paths_are_rejected(path: str) -> None:
    """Field paths are relative to the record; only the record path is absolute."""
    with pytest.raises(FieldPathError, match="must be relative"):
        parse_field_path(path)


@pytest.mark.parametrize("path", ["", "   ", "\t"])
def test_empty_paths_are_rejected(path: str) -> None:
    with pytest.raises(FieldPathError, match="is empty"):
        parse_field_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../Name",
        "Manufacturer/../Name",
        "*",
        "Manufacturer/*",
        "Name[1]",
        "Name[@id='1']",
        "contains(Name)",
        "Name/text()",
    ],
)
def test_xpath_constructs_are_rejected_not_ignored(path: str) -> None:
    """Phase 2 is not a partial XPath; unsupported syntax must fail loudly.

    Silently ignoring ``[1]`` or ``*`` would produce a *plausible* value from the
    wrong element, which is the failure mode this whole project is built to avoid.
    """
    with pytest.raises(FieldPathError, match="does not support"):
        parse_field_path(path)


def test_a_dot_inside_a_name_is_not_a_parent_step() -> None:
    """``.`` is a legal XML name character, so only a whole ``..`` segment is refused.

    A substring scan for ``..`` would reject the perfectly valid element name
    ``x..y``; the check is therefore per segment, not per character run.
    """
    assert parse_field_path("a.b").segments == ("a.b",)
    assert parse_field_path("x..y").segments == ("x..y",)
    assert parse_field_path("a.b/c.d").segments == ("a.b", "c.d")


def test_a_lone_at_sign_is_rejected() -> None:
    with pytest.raises(FieldPathError, match="empty attribute name"):
        parse_field_path("@")


def test_empty_segments_are_rejected() -> None:
    for path in ("Name/", "Manufacturer//Name", "/Name/"):
        with pytest.raises(FieldPathError):
            parse_field_path(path)


def test_an_attribute_may_only_be_read_from_the_last_segment() -> None:
    with pytest.raises(FieldPathError, match="last segment"):
        parse_field_path("@id/Name")


# --- namespace resolution per segment ---------------------------------------


def test_prefixed_segment_resolves_through_the_map() -> None:
    spec = parse_field_path("s:Manufacturer/s:Name", {"s": URI})
    assert spec.segments == (f"{{{URI}}}Manufacturer", f"{{{URI}}}Name")


def test_default_namespace_applies_to_bare_element_names() -> None:
    spec = parse_field_path("Manufacturer/Name", {"": URI})
    assert spec.segments == (f"{{{URI}}}Manufacturer", f"{{{URI}}}Name")


def test_bare_element_name_without_a_default_namespace_stays_bare() -> None:
    assert parse_field_path("Name").segments == ("Name",)


def test_unknown_prefix_is_rejected() -> None:
    with pytest.raises(FieldPathError, match="not present in the namespace map"):
        parse_field_path("nope:Name", {"s": URI})


def test_a_prefix_mapped_to_an_empty_uri_is_rejected() -> None:
    with pytest.raises(FieldPathError, match="empty URI"):
        parse_field_path("s:Name", {"s": ""})


def test_default_namespace_does_not_apply_to_attributes() -> None:
    """An unprefixed XML attribute is in *no* namespace.

    This is the subtle one: applying the default namespace to ``@id`` would look
    up ``{urn:example:shop}id``, which does not exist, so every attribute field
    would silently read ``None`` instead of failing.
    """
    spec = parse_field_path("@id", {"": URI})
    assert spec.attribute == "id"

    child = parse_field_path("Price/@currency", {"": URI})
    assert child.segments == (f"{{{URI}}}Price",)
    assert child.attribute == "currency"


def test_a_prefixed_attribute_resolves_through_the_map() -> None:
    spec = parse_field_path("@s:id", {"s": URI})
    assert spec.attribute == f"{{{URI}}}id"


def test_a_path_may_mix_prefixed_and_bare_segments() -> None:
    spec = parse_field_path("s:Manufacturer/Name", {"s": URI, "": "urn:example:other"})
    assert spec.segments == (f"{{{URI}}}Manufacturer", "{urn:example:other}Name")


def test_the_spec_is_immutable() -> None:
    spec = parse_field_path("Name")
    with pytest.raises(AttributeError):
        spec.segments = ()  # type: ignore[misc]


# --- text normalisation -----------------------------------------------------


def test_indented_text_collapses_to_a_single_line() -> None:
    """``itertext()`` includes the indentation around a nested value.

    Phase 1 lost time to exactly this: an int field written on its own line would
    arrive as ``"\\n      12\\n    "`` and fail to convert.
    """
    elem = etree.fromstring(b"<price>\n      49.90\n    </price>")
    assert normalize_text(elem) == "49.90"


def test_internal_whitespace_runs_collapse_to_one_space() -> None:
    elem = etree.fromstring(b"<name>Aurora    Desk\n\tLamp</name>")
    assert normalize_text(elem) == "Aurora Desk Lamp"


def test_nested_markup_is_flattened_without_inventing_spaces() -> None:
    elem = etree.fromstring(b"<name>Aurora <b>Desk</b> Lamp</name>")
    assert normalize_text(elem) == "Aurora Desk Lamp"


def test_adjacent_nested_elements_do_not_gain_a_space() -> None:
    """Element boundaries are not whitespace; adding one would be inventing data."""
    elem = etree.fromstring(b"<a>x<b>y</b>z</a>")
    assert normalize_text(elem) == "xyz"


def test_an_empty_element_normalises_to_the_empty_string() -> None:
    assert normalize_text(etree.fromstring(b"<name/>")) == ""
    assert normalize_text(etree.fromstring(b"<name>   </name>")) == ""


# --- type conversion --------------------------------------------------------


def test_string_is_returned_unchanged() -> None:
    assert coerce_value("49.90", FieldType.STRING, "f") == "49.90"
    assert coerce_value("", FieldType.STRING, "f") == ""


@pytest.mark.parametrize("text,expected", [("12", 12), ("0", 0), ("-7", -7), ("+7", 7)])
def test_int_accepts_whole_numbers(text: str, expected: int) -> None:
    value = coerce_value(text, FieldType.INT, "stock")
    assert value == expected
    assert isinstance(value, int)


@pytest.mark.parametrize("text", ["", "1.5", "1_000", "abc", " 12 ", "1e3", "0x10"])
def test_int_rejects_everything_that_is_not_a_whole_number(text: str) -> None:
    """``int("1_000")`` is ``1000`` in Python, so a bare ``int()`` would accept it."""
    with pytest.raises(FieldTypeError):
        coerce_value(text, FieldType.INT, "stock")


@pytest.mark.parametrize(
    "text,expected", [("1.5", 1.5), ("-2", -2.0), (".5", 0.5), ("1e3", 1000.0)]
)
def test_float_accepts_decimal_literals(text: str, expected: float) -> None:
    value = coerce_value(text, FieldType.FLOAT, "ratio")
    assert value == expected
    assert isinstance(value, float)


@pytest.mark.parametrize("text", ["", "nan", "inf", "-Infinity", "1_000", "abc"])
def test_float_rejects_non_finite_and_odd_literals(text: str) -> None:
    """A non-finite value would poison every later aggregate, silently."""
    with pytest.raises(FieldTypeError):
        coerce_value(text, FieldType.FLOAT, "ratio")


def test_decimal_returns_a_decimal_and_keeps_the_trailing_zero() -> None:
    """The whole point of the type: ``49.90`` must not become the float 49.9."""
    value = coerce_value("49.90", FieldType.DECIMAL, "price")
    assert isinstance(value, decimal.Decimal)
    assert not isinstance(value, float)
    assert str(value) == "49.90"
    assert value == decimal.Decimal("49.90")


def test_decimal_addition_stays_exact_where_float_does_not() -> None:
    total = coerce_value("0.1", FieldType.DECIMAL, "p") + coerce_value(
        "0.2", FieldType.DECIMAL, "p"
    )
    assert total == decimal.Decimal("0.3")
    assert 0.1 + 0.2 != 0.3


@pytest.mark.parametrize("text", ["", "nan", "Infinity", "1_000", "abc"])
def test_decimal_rejects_what_float_rejects(text: str) -> None:
    with pytest.raises(FieldTypeError):
        coerce_value(text, FieldType.DECIMAL, "price")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("false", False),
        ("FALSE", False),
        ("0", False),
        ("no", False),
        ("No", False),
    ],
)
def test_bool_accepts_the_documented_spellings_case_insensitively(
    text: str, expected: bool
) -> None:
    assert coerce_value(text, FieldType.BOOL, "active") is expected


@pytest.mark.parametrize("text", ["", "maybe", "2", "y", "n", "on", "off"])
def test_bool_rejects_everything_else(text: str) -> None:
    with pytest.raises(FieldTypeError):
        coerce_value(text, FieldType.BOOL, "active")


def test_date_parses_iso_dates() -> None:
    value = coerce_value("2023-11-14", FieldType.DATE, "released")
    assert value == datetime.date(2023, 11, 14)
    assert isinstance(value, datetime.date)


def test_date_accepts_a_leap_day() -> None:
    assert coerce_value("2024-02-29", FieldType.DATE, "released") == datetime.date(2024, 2, 29)


@pytest.mark.parametrize("text", ["", "2023-13-01", "2023-02-30", "14/11/2023", "not a date"])
def test_date_rejects_non_iso_and_impossible_dates(text: str) -> None:
    with pytest.raises(FieldTypeError):
        coerce_value(text, FieldType.DATE, "released")


# --- the error carries what Phase 5 needs -----------------------------------


def test_type_error_carries_the_field_name_and_the_raw_value() -> None:
    with pytest.raises(FieldTypeError) as info:
        coerce_value("not-a-number", FieldType.INT, "stock")

    error = info.value
    assert error.field == "stock"
    assert error.raw == "not-a-number"
    assert "stock" in str(error)
    assert "not-a-number" in str(error)


def test_type_error_marks_an_empty_value_visibly() -> None:
    """An empty string would be invisible in a message; it is shown as <empty>."""
    with pytest.raises(FieldTypeError) as info:
        coerce_value("", FieldType.INT, "stock")

    assert info.value.raw == ""
    assert "<empty>" in str(info.value)


def test_every_error_is_catchable_through_the_shared_base() -> None:
    from gigaxml.errors import GigaXMLError

    with pytest.raises(GigaXMLError):
        coerce_value("x", FieldType.INT, "stock")
    with pytest.raises(GigaXMLError):
        parse_field_path("/absolute")
