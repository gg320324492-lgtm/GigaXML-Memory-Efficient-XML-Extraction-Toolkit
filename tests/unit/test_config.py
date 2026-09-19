"""Unit tests: YAML config parsing and its strict validation.

The strictness is the feature under test. Every case in the "unknown key" section
exists because the permissive alternative produces a *successful* run with wrong
contents -- a mistyped ``type:`` yields strings where numbers were meant, and a
mistyped ``requried:`` silently stops enforcing a field.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.config import ExtractionConfig, load_config, parse_config
from gigaxml.errors import ConfigError, FieldPathError, RecordPathError
from gigaxml.fields import FieldType

URI = "urn:example:catalog"

BRIEF_EXAMPLE: dict[str, object] = {
    "record": "/catalog/products/product",
    "namespaces": {"c": URI},
    "fields": {
        "product_id": {"path": "@id", "type": "string"},
        "name": {"path": "Name", "type": "string"},
        "currency": {"path": "Price/@currency", "type": "string"},
        "price": {"path": "Price", "type": "decimal", "required": True},
    },
}


def _minimal(**overrides: object) -> dict[str, object]:
    """The smallest valid config, with individual keys replaced for a test."""
    config: dict[str, object] = {
        "record": "/catalog/products/product",
        "fields": {"name": {"path": "Name"}},
    }
    config.update(overrides)
    return config


# --- the happy path ---------------------------------------------------------


def test_the_briefs_example_config_parses() -> None:
    config = parse_config(BRIEF_EXAMPLE)

    assert isinstance(config, ExtractionConfig)
    assert config.record_path == "/catalog/products/product"
    assert config.namespaces == {"c": URI}
    assert config.field_names == ("product_id", "name", "currency", "price")
    assert config.record_spec.anchored is True
    assert config.record_spec.chain == ("catalog", "products", "product")


def test_field_types_and_required_flags_are_carried_through() -> None:
    config = parse_config(BRIEF_EXAMPLE)
    by_name = {field.name: field for field in config.fields}

    assert by_name["product_id"].type is FieldType.STRING
    assert by_name["price"].type is FieldType.DECIMAL
    assert by_name["price"].required is True
    assert by_name["name"].required is False
    assert by_name["price"].raw_path == "Price"


def test_type_defaults_to_string_and_required_to_false() -> None:
    config = parse_config(_minimal())
    field = config.fields[0]
    assert field.type is FieldType.STRING
    assert field.required is False


def test_namespaces_are_optional() -> None:
    config = parse_config(_minimal())
    assert config.namespaces == {}


def test_field_order_follows_the_config() -> None:
    config = parse_config(_minimal(fields={"zeta": {"path": "Z"}, "alpha": {"path": "A"}}))
    assert config.field_names == ("zeta", "alpha")


def test_an_any_ancestor_record_path_is_not_anchored() -> None:
    config = parse_config(_minimal(record="//products/product"))
    assert config.record_spec.anchored is False
    assert config.record_spec.chain == ("products", "product")


def test_the_default_namespace_reaches_both_record_and_field_paths() -> None:
    config = parse_config(_minimal(record="/catalog/products/product", namespaces={"": URI}))
    assert config.record_spec.chain == (
        f"{{{URI}}}catalog",
        f"{{{URI}}}products",
        f"{{{URI}}}product",
    )
    assert config.fields[0].spec.segments == (f"{{{URI}}}Name",)


# --- unknown keys: the three shapes the gate asks for -----------------------


def test_unknown_top_level_key_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_minimal(records="/catalog/products/product"))

    message = str(info.value)
    assert "records" in message
    assert "top level" in message
    assert "allowed keys are" in message


def test_unknown_key_inside_a_field_is_rejected() -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_minimal(fields={"name": {"path": "Name", "requried": True}}))

    message = str(info.value)
    assert "requried" in message
    assert "field 'name'" in message


def test_an_unsupported_type_name_is_rejected() -> None:
    """``deciaml`` would otherwise fall back to ``string`` and emit text prices."""
    with pytest.raises(ConfigError) as info:
        parse_config(_minimal(fields={"price": {"path": "Price", "type": "deciaml"}}))

    message = str(info.value)
    assert "deciaml" in message
    assert "supported types are" in message
    assert "decimal" in message


def test_a_non_string_type_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported type"):
        parse_config(_minimal(fields={"price": {"path": "Price", "type": 7}}))


def test_the_unknown_key_message_lists_every_unknown_key_at_once() -> None:
    with pytest.raises(ConfigError) as info:
        parse_config(_minimal(bogus=1, also_bogus=2))

    message = str(info.value)
    assert "also_bogus" in message
    assert "bogus" in message


# --- other malformed shapes -------------------------------------------------


@pytest.mark.parametrize("data", [[], "record: x", 3, None])
def test_a_non_mapping_document_is_rejected(data: object) -> None:
    with pytest.raises(ConfigError, match="mapping at the top level"):
        parse_config(data)


@pytest.mark.parametrize("record", [None, "", "   ", 7, ["/a"]])
def test_a_missing_or_mistyped_record_is_rejected(record: object) -> None:
    config = _minimal()
    config["record"] = record
    with pytest.raises(ConfigError, match="'record'"):
        parse_config(config)


def test_a_missing_fields_block_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must define 'fields'"):
        parse_config({"record": "/catalog/products/product"})


def test_a_non_mapping_fields_block_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"'fields'.*must be a mapping"):
        parse_config(_minimal(fields=["Name"]))


def test_an_empty_fields_block_is_rejected() -> None:
    """An empty ``fields`` is a config that extracts nothing; that is a mistake."""
    with pytest.raises(ConfigError, match="is empty"):
        parse_config(_minimal(fields={}))


def test_a_field_that_is_not_a_mapping_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must be a mapping with at least a 'path'"):
        parse_config(_minimal(fields={"name": "Name"}))


@pytest.mark.parametrize("path", [None, "", "   ", 7])
def test_a_field_without_a_usable_path_is_rejected(path: object) -> None:
    with pytest.raises(ConfigError, match="must set 'path'"):
        parse_config(_minimal(fields={"name": {"path": path}}))


def test_a_field_missing_the_path_key_entirely_is_rejected() -> None:
    with pytest.raises(ConfigError, match="must set 'path'"):
        parse_config(_minimal(fields={"name": {"type": "string"}}))


@pytest.mark.parametrize("required", ["yes", 1, 0, None])
def test_a_non_boolean_required_is_rejected(required: object) -> None:
    """PyYAML resolves unquoted ``yes``/``on`` to booleans, so a *string* is the case to catch."""
    with pytest.raises(ConfigError, match="must set 'required' to true or false"):
        parse_config(_minimal(fields={"name": {"path": "Name", "required": required}}))


def test_a_non_mapping_namespaces_block_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"'namespaces'.*must be a mapping"):
        parse_config(_minimal(namespaces=["c", URI]))


@pytest.mark.parametrize("uri", ["", None, 7])
def test_a_namespace_without_a_usable_uri_is_rejected(uri: object) -> None:
    with pytest.raises(ConfigError, match="non-empty URI"):
        parse_config(_minimal(namespaces={"c": uri}))


def test_an_empty_field_name_is_rejected() -> None:
    with pytest.raises(ConfigError, match="field name"):
        parse_config(_minimal(fields={"": {"path": "Name"}}))


# --- errors from the path layers are propagated, not re-wrapped -------------


def test_a_relative_record_path_raises_record_path_error() -> None:
    """Propagated as-is: the message already names the path and explains anchoring."""
    with pytest.raises(RecordPathError, match="absolute element path"):
        parse_config(_minimal(record="catalog/products/product"))


def test_an_absolute_field_path_raises_field_path_error() -> None:
    with pytest.raises(FieldPathError, match="must be relative"):
        parse_config(_minimal(fields={"name": {"path": "/catalog/Name"}}))


def test_a_field_path_using_an_unsupported_construct_raises() -> None:
    with pytest.raises(FieldPathError, match="does not support"):
        parse_config(_minimal(fields={"name": {"path": "Name[1]"}}))


def test_a_field_path_with_an_unknown_prefix_raises() -> None:
    with pytest.raises(FieldPathError, match="not present in the namespace map"):
        parse_config(_minimal(fields={"name": {"path": "nope:Name"}}))


def test_every_config_error_is_catchable_through_the_shared_base() -> None:
    from gigaxml.errors import GigaXMLError

    with pytest.raises(GigaXMLError):
        parse_config(_minimal(bogus=1))


# --- loading from a file ----------------------------------------------------


def test_load_config_reads_a_yaml_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "\n".join(
            [
                "record: /catalog/products/product",
                "namespaces:",
                f'  c: "{URI}"',
                "fields:",
                "  product_id:",
                '    path: "@id"',
                "  price:",
                '    path: "Price"',
                "    type: decimal",
                "    required: true",
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(path)
    assert config.record_path == "/catalog/products/product"
    assert config.namespaces == {"c": URI}
    assert config.field_names == ("product_id", "price")
    assert config.fields[1].type is FieldType.DECIMAL
    assert config.fields[1].required is True


def test_load_config_accepts_a_utf8_byte_order_mark(tmp_path: Path) -> None:
    """A BOM must not turn the first key into ``\\ufeffrecord``.

    Decoding as plain ``utf-8`` leaves U+FEFF in the string, and the resulting
    "unknown key '\\ufeffrecord'" is baffling on a file that looks correct.
    """
    path = tmp_path / "bom.yaml"
    path.write_bytes(b"\xef\xbb\xbfrecord: /a\nfields:\n  x:\n    path: N\n")

    config = load_config(path)
    assert config.record_path == "/a"
    assert config.field_names == ("x",)


def test_load_config_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read config file"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_reports_invalid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "broken.yaml"
    path.write_text("record: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_load_config_reports_an_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="is empty"):
        load_config(path)


def test_load_config_names_the_offending_file(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("record: /a\nfields:\n  x:\n    path: N\n    bogus: 1\n", encoding="utf-8")
    with pytest.raises(ConfigError) as info:
        load_config(path)
    assert "config.yaml" in str(info.value)


def test_load_config_propagates_validation_errors(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("record: /a\nfields:\n  x:\n    path: N\n    type: nope\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unsupported type"):
        load_config(path)


def test_the_config_is_immutable() -> None:
    config = parse_config(_minimal())
    with pytest.raises(AttributeError):
        config.record_path = "/other"  # type: ignore[misc]
