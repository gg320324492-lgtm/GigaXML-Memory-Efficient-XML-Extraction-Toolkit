"""A schema is part of what a run *is*, and the identity says so.

An XSD decides how every field is typed, so two runs against two different schemas
produce two different files. Before the schema joined the run identity, editing an XSD
in place left that identity unmoved: ``--resume`` compared it, found it unchanged,
proceeded, and appended rows of a different shape to parts written under the old
schema -- with nothing anywhere reporting a problem.

**Keyed on content, not on the path.** The same declarations in a different directory
type the fields identically and describe the same run; only the bytes matter. Asserted
both ways: changing the contents must change the identity, and moving an unchanged file
must not.

The other half of the suite is the configs that name *no* schema -- the overwhelming
majority, and the ones that must not have moved at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.checkpoint import (
    Checkpoint,
    config_identity,
    schema_identity,
    source_identity,
    validate_resume,
)
from gigaxml.config import parse_config
from gigaxml.errors import CheckpointError

_XSD_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
)


def schema_declaring(field_type: str) -> str:
    """A one-element schema. ``field_type`` is what makes two of them differ."""
    return f'{_XSD_HEAD}<xs:element name="name" type="xs:{field_type}"/></xs:schema>'


def write_schema(directory: Path, name: str, field_type: str) -> Path:
    path = directory / name
    path.write_text(schema_declaring(field_type), encoding="utf-8")
    return path


def config_naming(schema: Path) -> object:
    return parse_config(
        {
            "record": "/catalog/products/product",
            "fields": {"name": {"path": "name"}},
            "schema": str(schema),
        }
    )


# --- criterion B: a config with no schema must not have moved -----------------
#
# Ranked first. If this is wrong, every checkpoint written before the schema joined the
# identity refuses to resume -- a much larger breakage than the defect being fixed.


def test_a_config_naming_no_schema_hashes_exactly_as_it_did_before() -> None:
    """The pinned digests of three schema-less configs, from before this change.

    Literal values rather than a round trip, because the claim is specifically that the
    numbers did not move. A test that recomputed them would pass for any implementation,
    including one that had just invalidated every existing checkpoint.
    """
    cases = [
        (
            {"record": "/a/b", "fields": {"x": {"path": "y"}}},
            "0f6b54ade0394f69b67f0b4780d85eb2e37b87a82e1e21c5821fa82bb74b21cf",
        ),
        (
            {"record": "/a/b", "namespaces": {"c": "urn:x"}, "fields": {"x": {"path": "y"}}},
            "7d00bde752ed886d474841e755546697bdc8a7b9623137ba57adc5bbdea2a066",
        ),
        (
            {"record": "/a/b", "on_error": "quarantine", "fields": {"x": {"path": "y"}}},
            "8256b70b3433e1b5f5ef986413beed9d4180d8fed27c5ad1ffd461373fcb5f12",
        ),
    ]
    for raw, digest in cases:
        assert config_identity(parse_config(raw)) == digest, raw


# --- criterion A: the schema's content is part of the identity -----------------


def test_two_schemas_of_different_content_give_different_identities(tmp_path: Path) -> None:
    """The defect, stated directly: same path, different bytes, different run.

    The identity is taken while each version is on disk. Reading both after the second
    write would compare an identity against itself, so the file's own history is what
    makes the two values mean different things.
    """
    schema = write_schema(tmp_path, "catalog.xsd", "string")
    as_string = config_identity(config_naming(schema))

    schema.write_text(schema_declaring("decimal"), encoding="utf-8")
    as_decimal = config_identity(config_naming(schema))

    assert as_string != as_decimal


def test_the_same_schema_under_another_name_is_the_same_run(tmp_path: Path) -> None:
    """Content-keyed, not path-keyed -- the property that keeps the identity about semantics.

    Moving a schema does not change a single field's type, so it must not change the
    identity. Including the path would make "moved the file" indistinguishable from
    "edited the file", which is the same mistake as leaving the schema out entirely,
    only smaller.
    """
    first = write_schema(tmp_path, "catalog.xsd", "decimal")
    second = tmp_path / "elsewhere" / "catalog.xsd"
    second.parent.mkdir()
    second.write_bytes(first.read_bytes())

    assert config_identity(config_naming(first)) == config_identity(config_naming(second))


def test_naming_a_schema_changes_the_identity_at_all(tmp_path: Path) -> None:
    """A config with a schema is not the same run as one without it."""
    schema = write_schema(tmp_path, "catalog.xsd", "string")
    without = parse_config({"record": "/a/b", "fields": {"x": {"path": "y"}}})

    assert config_identity(config_naming(schema)) != config_identity(without)


def test_a_schema_that_cannot_be_read_is_not_silently_treated_as_absent(
    tmp_path: Path,
) -> None:
    """An unreadable schema hashes differently from no schema -- and from another one.

    Falling back to "no schema" would make a config pointing at a file that is gone
    compare equal to a config that never named one, and a resume would treat them as the
    same run. The failure is recorded as its own value instead, so the identity still
    tells these cases apart.

    Nothing here raises: an identity is computed on paths that may be about to fail for
    a different reason, and the schema's own error is raised where the schema is used.
    """
    missing = parse_config(
        {"record": "/a/b", "fields": {"x": {"path": "y"}}, "schema": str(tmp_path / "gone.xsd")}
    )
    also_missing = parse_config(
        {"record": "/a/b", "fields": {"x": {"path": "y"}}, "schema": str(tmp_path / "other.xsd")}
    )
    without = parse_config({"record": "/a/b", "fields": {"x": {"path": "y"}}})

    assert config_identity(missing) != config_identity(without)
    assert config_identity(missing) != config_identity(also_missing)


def test_schema_identity_is_the_file_content_hash(tmp_path: Path) -> None:
    """``schema_identity`` is the digest, and the same one ``source_identity`` computes.

    The two must not be separate notions of "the same file": a resume checks one and a
    report records the other, and agreement between them should be structural rather
    than a coincidence that holds today.
    """
    schema = write_schema(tmp_path, "catalog.xsd", "string")

    assert schema_identity(schema) == source_identity(schema)["sha256"]


# --- criterion E: a refusal has to say that it was the schema ------------------


def test_the_resume_refusal_names_the_schema_when_the_config_hash_moved(
    tmp_path: Path,
) -> None:
    """An edited schema is the least obvious cause of a config mismatch.

    The config file is unchanged, the command line is unchanged, and the only edit was
    to a document the config points at. Without naming it, the message sends a reader to
    diff two identical YAML files.

    The message says what the schema is *now* and admits it cannot show the previous
    value -- the manifest stores one config digest and not the schema's, so the old
    fingerprint is genuinely not recoverable from the checkpoint.
    """
    source = tmp_path / "source.xml"
    source.write_text("<catalog/>", encoding="utf-8")
    schema = write_schema(tmp_path, "catalog.xsd", "string")

    checkpoint = Checkpoint(
        source=source_identity(source),
        config=config_identity(config_naming(schema)),
        records_consumed=1,
        rejected=0,
        parts=(),
        complete=False,
    )

    schema.write_text(schema_declaring("decimal"), encoding="utf-8")

    with pytest.raises(CheckpointError) as refused:
        validate_resume(checkpoint, source, config_naming(schema))

    message = str(refused.value)
    assert "schema" in message
    # The path appears through repr(), so it carries this platform's separators escaped;
    # matching the repr is what a reader of the message actually sees.
    assert repr(str(schema)) in message
    assert schema_identity(schema) in message


def test_the_refusal_says_nothing_about_a_schema_when_there_is_none(tmp_path: Path) -> None:
    """A config with no schema gets the plain message, not a sentence about schemas.

    The diagnostic is added only when it could be the cause. Attaching it to every
    mismatch would train a reader to skip it.
    """
    source = tmp_path / "source.xml"
    source.write_text("<catalog/>", encoding="utf-8")
    checkpoint = Checkpoint(
        source=source_identity(source),
        config="0" * 64,
        records_consumed=1,
        rejected=0,
        parts=(),
        complete=False,
    )
    config = parse_config({"record": "/a/b", "fields": {"x": {"path": "y"}}})

    with pytest.raises(CheckpointError) as refused:
        validate_resume(checkpoint, source, config)

    assert "schema" not in str(refused.value)
