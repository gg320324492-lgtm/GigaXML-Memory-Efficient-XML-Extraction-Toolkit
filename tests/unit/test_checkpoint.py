"""Unit tests: the checkpoint manifest and the two identities behind it.

The identities are the whole safety story for ``--resume``: they are what stops a run
being continued against a source or a config it was not made from. Most of these
tests are about what must *not* be treated as a change.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.checkpoint import (
    CHECKPOINT_FILENAME,
    CHECKPOINT_VERSION,
    Checkpoint,
    PartRecord,
    config_identity,
    part_name,
    read_checkpoint,
    source_identity,
    validate_resume,
    write_checkpoint,
)
from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import CheckpointError

DOC = '<?xml version="1.0"?><root><item><id>1</id></item><item><id>2</id></item></root>'
FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}


def config(**overrides: object) -> ExtractionConfig:
    payload: dict[str, object] = {"record": "/root/item", "fields": FIELDS}
    payload.update(overrides)
    return parse_config(payload)


def sample_checkpoint(**overrides: object) -> Checkpoint:
    base: dict[str, object] = {
        "source": {"path": "x.xml", "size": 1, "sha256": "ab"},
        "config": "hash",
        "records_consumed": 5,
        "rejected": 1,
        "parts": (PartRecord("part-00000.csv", 4),),
        "complete": False,
    }
    base.update(overrides)
    return Checkpoint(**base)  # type: ignore[arg-type]


# --- part naming ------------------------------------------------------------


@pytest.mark.parametrize(
    "index,expected",
    [
        (0, "part-00000.csv"),
        (1, "part-00001.csv"),
        (12345, "part-12345.csv"),
        (99999, "part-99999.csv"),
    ],
)
def test_part_names_are_zero_padded_so_sorting_matches_order(index: int, expected: str) -> None:
    assert part_name(index, "csv") == expected


def test_part_names_sort_in_run_order() -> None:
    names = [part_name(index, "parquet") for index in (0, 1, 2, 10, 100, 1000)]
    assert sorted(names) == names


# --- source identity --------------------------------------------------------


def test_source_identity_records_path_size_and_hash(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")

    identity = source_identity(source)

    assert identity["path"] == str(source)
    assert identity["size"] == source.stat().st_size
    assert len(str(identity["sha256"])) == 64


def test_source_identity_changes_with_content(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")
    before = source_identity(source)

    source.write_text(DOC.replace("<id>2</id>", "<id>3</id>"), encoding="utf-8")
    after = source_identity(source)

    assert before["sha256"] != after["sha256"]


def test_source_identity_changes_when_the_length_changes(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")
    before = source_identity(source)

    source.write_text(DOC + "<!-- padding -->", encoding="utf-8")
    after = source_identity(source)

    assert before["size"] != after["size"]
    assert before["sha256"] != after["sha256"]


def test_a_missing_source_is_a_checkpoint_error(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError, match="cannot identify the source"):
        source_identity(tmp_path / "nope.xml")


# --- config identity --------------------------------------------------------


def test_config_identity_is_stable_for_the_same_config() -> None:
    assert config_identity(config()) == config_identity(config())


def test_config_identity_ignores_key_order() -> None:
    """Two spellings of the same config describe the same run."""
    first = parse_config(
        {"record": "/root/item", "fields": {"a": {"path": "a"}, "b": {"path": "b"}}}
    )
    second = parse_config(
        {"fields": {"a": {"path": "a"}, "b": {"path": "b"}}, "record": "/root/item"}
    )

    assert config_identity(first) == config_identity(second)


def test_config_identity_changes_with_a_field_type() -> None:
    plain = parse_config({"record": "/root/item", "fields": {"a": {"path": "a"}}})
    typed = parse_config({"record": "/root/item", "fields": {"a": {"path": "a", "type": "int"}}})

    assert config_identity(plain) != config_identity(typed)


@pytest.mark.parametrize(
    "change",
    [
        {"record": "/root/other"},
        {"namespaces": {"p": "urn:x"}},
        {"on_error": "quarantine"},
    ],
)
def test_config_identity_changes_with_each_meaningful_key(change: dict[str, object]) -> None:
    assert config_identity(config()) != config_identity(config(**change))


def test_config_identity_changes_with_field_order() -> None:
    """Field order decides the CSV column order, so it is part of the identity."""
    first = parse_config(
        {"record": "/root/item", "fields": {"a": {"path": "a"}, "b": {"path": "b"}}}
    )
    second = parse_config(
        {"record": "/root/item", "fields": {"b": {"path": "b"}, "a": {"path": "a"}}}
    )

    assert config_identity(first) != config_identity(second)


# --- reading and writing ----------------------------------------------------


def test_a_manifest_round_trips(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    checkpoint = sample_checkpoint()

    write_checkpoint(path, checkpoint)

    assert read_checkpoint(path) == checkpoint


def test_writing_a_manifest_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    write_checkpoint(path, sample_checkpoint())

    assert path.is_file()
    assert not path.with_name(path.name + ".tmp").exists()


def test_writing_a_manifest_over_an_existing_one_replaces_it(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    write_checkpoint(path, sample_checkpoint(records_consumed=5))
    write_checkpoint(path, sample_checkpoint(records_consumed=50))

    assert read_checkpoint(path).records_consumed == 50


def test_a_missing_manifest_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(CheckpointError, match="there is nothing to resume"):
        read_checkpoint(tmp_path / CHECKPOINT_FILENAME)


def test_a_truncated_manifest_is_a_clear_error(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text('{"version": 1, "source":', encoding="utf-8")

    with pytest.raises(CheckpointError, match="could not be read"):
        read_checkpoint(path)


def test_a_manifest_that_is_not_an_object_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(CheckpointError, match="must contain a JSON object"):
        read_checkpoint(path)


def test_a_manifest_from_a_newer_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    payload = sample_checkpoint().to_dict()
    payload["version"] = CHECKPOINT_VERSION + 1
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="format version"):
        read_checkpoint(path)


def test_a_manifest_missing_a_key_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    payload = sample_checkpoint().to_dict()
    del payload["records_consumed"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="missing"):
        read_checkpoint(path)


def test_a_manifest_with_a_malformed_source_block_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    payload = sample_checkpoint().to_dict()
    payload["source"] = {"path": "x", "size": 1}
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="malformed 'source'"):
        read_checkpoint(path)


def test_a_manifest_with_a_malformed_part_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / CHECKPOINT_FILENAME
    payload = sample_checkpoint().to_dict()
    payload["parts"] = [{"name": "part-00000.csv"}]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CheckpointError, match="malformed part"):
        read_checkpoint(path)


def test_rows_adds_up_the_parts() -> None:
    checkpoint = sample_checkpoint(
        parts=(PartRecord("a", 3), PartRecord("b", 4), PartRecord("c", 5))
    )
    assert checkpoint.rows == 12


# --- validation -------------------------------------------------------------


def test_a_matching_source_and_config_validates(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")
    cfg = config()
    checkpoint = sample_checkpoint(source=source_identity(source), config=config_identity(cfg))

    validate_resume(checkpoint, source, cfg)


def test_a_changed_source_is_refused_with_both_values(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")
    cfg = config()
    checkpoint = sample_checkpoint(source=source_identity(source), config=config_identity(cfg))
    source.write_text(DOC.replace("<id>2</id>", "<id>9</id>"), encoding="utf-8")

    with pytest.raises(CheckpointError) as info:
        validate_resume(checkpoint, source, cfg)

    message = str(info.value)
    assert "cannot resume" in message
    assert "sha256" in message
    assert "Nothing was written" in message
    assert checkpoint.source["sha256"] in message


def test_a_changed_config_is_refused_with_both_values(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(DOC, encoding="utf-8")
    cfg = config()
    checkpoint = sample_checkpoint(source=source_identity(source), config=config_identity(cfg))

    other = parse_config({"record": "/root/item", "fields": {"id": {"path": "id"}}})

    with pytest.raises(CheckpointError) as info:
        validate_resume(checkpoint, source, other)

    message = str(info.value)
    assert "config:" in message
    assert config_identity(cfg) in message
    assert config_identity(other) in message


def test_a_moved_source_of_the_same_content_is_refused_on_the_path(tmp_path: Path) -> None:
    first = tmp_path / "one.xml"
    second = tmp_path / "two.xml"
    first.write_text(DOC, encoding="utf-8")
    second.write_text(DOC, encoding="utf-8")
    cfg = config()
    checkpoint = sample_checkpoint(source=source_identity(first), config=config_identity(cfg))

    with pytest.raises(CheckpointError, match="source path"):
        validate_resume(checkpoint, second, cfg)


def test_the_config_file_is_not_what_is_hashed(tmp_path: Path) -> None:
    """A comment change must not look like an extraction change.

    The identity is built from the parsed config, so two YAML files that differ only
    in a comment produce the same hash -- which is the point: hashing the file would
    refuse a resume that is perfectly safe.
    """
    import yaml

    from gigaxml.config import load_config

    first = tmp_path / "a.yaml"
    second = tmp_path / "b.yaml"
    body = {"record": "/root/item", "fields": FIELDS}
    first.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    second.write_text(
        "# a comment somebody added\n" + yaml.safe_dump(body, sort_keys=False), encoding="utf-8"
    )

    assert first.read_bytes() != second.read_bytes(), "the files really do differ"
    assert config_identity(load_config(first)) == config_identity(load_config(second))
