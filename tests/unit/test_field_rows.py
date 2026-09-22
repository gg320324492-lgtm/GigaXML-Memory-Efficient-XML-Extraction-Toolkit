"""The field table's state, and the project's verdict on it.

**The test that matters is the equivalence one.** Every other test here says the panel
rejects what it should; those could all pass with a hand-written validator that happens to
agree today. ``test_the_message_is_the_librarys_own`` compares the text the panel would
show against the text ``parse_config`` raises, so a paraphrased or reimplemented rule
cannot pass it.

**Nothing here asserts the library's wording.** The messages are compared to themselves,
never to a string typed into this file -- a test that pinned "field path has an empty
segment" would break the first time somebody improved the sentence, and would be testing
the wrong thing in the meantime.
"""

from __future__ import annotations

import pytest

from gigaxml.config import parse_config
from gigaxml.errors import GigaXMLError
from gigaxml.fields import FieldType
from gigaxml.gui.field_rows import (
    DEFAULT_TYPE_NAME,
    FIELD_TYPE_NAMES,
    FieldRow,
    build_config_dict,
    validate,
)

RECORD = "/catalog/products/product"

#: The label the module uses when the caller does not name the table. Taken from the
#: module rather than typed, so the comparison below is against the sentence a user would
#: actually see.
SOURCE = "<the field table>"


def library_message(data: dict[str, object]) -> str:
    """What ``parse_config`` says about a mapping, as text, for the same source label."""
    with pytest.raises(GigaXMLError) as raised:
        parse_config(data, source=SOURCE)
    return str(raised.value)


# --- the type list ------------------------------------------------------------


def test_the_dropdown_offers_exactly_the_types_the_library_knows() -> None:
    """Derived from the enum, so a type the library does not know cannot be offered."""
    assert tuple(member.value for member in FieldType) == FIELD_TYPE_NAMES
    assert set(FIELD_TYPE_NAMES) == {"string", "int", "float", "decimal", "bool", "date"}


def test_the_default_type_is_the_one_the_library_assumes() -> None:
    assert DEFAULT_TYPE_NAME == "string"
    assert FieldRow("a", "a").type_name == DEFAULT_TYPE_NAME


# --- assembling the mapping ---------------------------------------------------


def test_the_mapping_keeps_the_order_of_the_rows() -> None:
    rows = [FieldRow("z", "z"), FieldRow("a", "a"), FieldRow("m", "m")]

    data = build_config_dict(rows, record_path=RECORD)

    assert list(data["fields"]) == ["z", "a", "m"]


def test_a_row_is_written_with_its_type_spelled_out() -> None:
    """The interface shows a type for every row, so the config carries one."""
    data = build_config_dict([FieldRow("a", "@a", "int", True)], record_path=RECORD)

    assert data["fields"]["a"] == {"path": "@a", "type": "int", "required": True}


def test_absent_pieces_are_left_out_of_the_mapping() -> None:
    """No namespaces block when there are none, and no on_error unless one is set."""
    data = build_config_dict([FieldRow("a", "@a")], record_path=RECORD)

    assert "namespaces" not in data
    assert "on_error" not in data


def test_namespaces_and_on_error_are_carried_when_given() -> None:
    data = build_config_dict(
        [FieldRow("a", "@a")],
        record_path=RECORD,
        namespaces={"p": "urn:one"},
        on_error="quarantine",
    )

    assert data["namespaces"] == {"p": "urn:one"}
    assert data["on_error"] == "quarantine"


# --- what is accepted ---------------------------------------------------------


def test_a_good_table_is_accepted() -> None:
    result = validate(
        [FieldRow("id", "@id"), FieldRow("name", "name", "string", True)],
        record_path=RECORD,
    )

    assert result.ok is True
    assert result.row_index is None
    assert result.message == ""
    assert result.config is not None
    assert result.config.field_names == ("id", "name")


def test_acceptance_matches_what_the_library_would_accept() -> None:
    rows = [FieldRow("id", "@id"), FieldRow("qty", "qty", "int")]
    result = validate(rows, record_path=RECORD)

    direct = parse_config(build_config_dict(rows, record_path=RECORD))

    assert result.ok is True
    assert result.config is not None
    assert result.config.field_names == direct.field_names


def test_an_empty_table_is_refused_by_the_library_not_by_us() -> None:
    result = validate([], record_path=RECORD)

    assert result.ok is False
    assert result.message == library_message({"record": RECORD, "fields": {}})


# --- ★ the message is the library's own ---------------------------------------


def test_the_message_is_the_librarys_own_for_a_bad_path() -> None:
    """The equivalence the brief is really asking for: no paraphrase, no second rule."""
    rows = [FieldRow("id", "@id"), FieldRow("oops", "a//b")]

    result = validate(rows, record_path=RECORD)

    expected = library_message(
        {"record": RECORD, "fields": {"id": {"path": "@id"}, "oops": {"path": "a//b"}}}
    )
    assert result.ok is False
    assert result.message == expected
    assert result.row_index == 1


def test_the_message_is_the_librarys_own_for_a_bad_type() -> None:
    rows = [FieldRow("qty", "qty", "intt")]

    result = validate(rows, record_path=RECORD)

    expected = library_message(
        {"record": RECORD, "fields": {"qty": {"path": "qty", "type": "intt"}}}
    )
    assert result.message == expected
    assert result.row_index == 0


def test_the_message_is_the_librarys_own_for_an_empty_name() -> None:
    result = validate([FieldRow("", "@id")], record_path=RECORD)

    expected = library_message({"record": RECORD, "fields": {"": {"path": "@id"}}})
    assert result.message == expected
    assert result.row_index == 0


def test_the_message_is_the_librarys_own_for_a_bad_record_path() -> None:
    result = validate([FieldRow("id", "@id")], record_path="")

    expected = library_message({"record": "", "fields": {"id": {"path": "@id"}}})
    assert result.message == expected


def test_the_message_is_the_librarys_own_for_a_bad_on_error() -> None:
    result = validate([FieldRow("id", "@id")], record_path=RECORD, on_error="nope")

    expected = library_message(
        {"record": RECORD, "on_error": "nope", "fields": {"id": {"path": "@id"}}}
    )
    assert result.message == expected


# --- locating the row ---------------------------------------------------------


def test_a_bad_row_is_named_by_its_position() -> None:
    rows = [FieldRow("a", "a"), FieldRow("b", "b"), FieldRow("c", "c//d")]

    result = validate(rows, record_path=RECORD)

    assert result.row_index == 2


def test_a_one_row_table_whose_only_row_is_bad_blames_that_row() -> None:
    """An earlier version blamed nothing here.

    It assumed "every row failed, so the shared part must be wrong", which is true only
    when there is more than one row -- so a single wrong row was reported as no row at all.
    """
    result = validate([FieldRow("qty", "qty", "intt")], record_path=RECORD)

    assert result.row_index == 0


def test_a_fault_in_the_shared_part_is_not_blamed_on_a_row() -> None:
    """A bad record path fails every row; it is still not any row's fault."""
    result = validate([FieldRow("a", "a"), FieldRow("b", "b")], record_path="")

    assert result.row_index is None


def test_the_first_bad_row_is_the_one_named() -> None:
    rows = [FieldRow("a", "a//b"), FieldRow("b", "c//d")]

    result = validate(rows, record_path=RECORD)

    assert result.row_index == 0


# --- the one rule that is ours ------------------------------------------------


def test_a_repeated_field_name_is_refused() -> None:
    """``parse_config`` cannot see this: it takes a mapping, which cannot hold two entries
    under one name, so the rows collapse before the library is asked."""
    result = validate([FieldRow("id", "@id"), FieldRow("id", "name")], record_path=RECORD)

    assert result.ok is False
    assert result.row_index == 1, "the second row is the one that would be replaced"
    assert "id" in result.message


def test_the_repeated_name_message_is_ours_and_says_so_plainly() -> None:
    result = validate([FieldRow("x", "@x"), FieldRow("x", "@y")], record_path=RECORD)

    assert "cannot share a name" in result.message


def test_a_repeated_name_is_not_reported_by_the_library() -> None:
    """Which is why the check above exists: the mapping silently keeps the last one."""
    collapsed = build_config_dict(
        [FieldRow("id", "@id"), FieldRow("id", "name")], record_path=RECORD
    )

    assert list(collapsed["fields"]) == ["id"]
    assert parse_config(collapsed).field_names == ("id",), "the library accepted it"


# --- namespaces ---------------------------------------------------------------


def test_a_prefixed_path_is_accepted_when_the_prefix_is_declared() -> None:
    result = validate(
        [FieldRow("name", "p:name")],
        record_path="/p:catalog/p:item",
        namespaces={"p": "urn:one"},
    )

    assert result.ok is True


def test_a_prefixed_path_is_refused_when_the_prefix_is_not_declared() -> None:
    """Resolved by the library against the map, not by anything in ``gui``."""
    rows = [FieldRow("name", "p:name")]

    result = validate(rows, record_path=RECORD, namespaces={})

    expected = library_message({"record": RECORD, "fields": {"name": {"path": "p:name"}}})
    assert result.ok is False
    assert result.message == expected
    assert result.row_index == 0


@pytest.mark.parametrize("type_name", FIELD_TYPE_NAMES)
def test_every_type_the_dropdown_offers_is_accepted_by_the_library(type_name: str) -> None:
    """If the dropdown ever offered something the library did not know, this fails."""
    result = validate([FieldRow("a", "@a", type_name)], record_path=RECORD)

    assert result.ok is True, result.message
