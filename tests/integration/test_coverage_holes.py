"""Integration tests: the places coverage said were untested, and one that lied.

★ **Two different jobs live in this file**, and the second is why the first exists.

The first is M13 criterion A: every uncovered line in ``checkpoint.py``, ``config.py``,
``writers.py`` and ``parser/streaming.py`` that a probe could actually **reach**. Each
was reached by running the code, not by reading it, and each is here as a named test so
the next person does not have to rediscover the construction.

The second is a **weakness in an existing test**, found by mutation B4 and measured
rather than guessed: see
:func:`test_a_boolean_count_is_refused_by_the_type_check_and_not_by_another`.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from gigaxml import StreamingRecordReader, create_writer, parse_config
from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    PartRecord,
    read_checkpoint,
    verify_parts,
    write_checkpoint,
)
from gigaxml.errors import CheckpointError, ConfigError, FieldPathError, WriterError
from gigaxml.fields import parse_field_path
from gigaxml.writers import WriterFormat, _json_default


def _config(one_field: bool = True) -> object:
    fields = {"v": {"path": "p"}} if one_field else {"v": {"path": "p"}, "w": {"path": "q"}}
    return parse_config({"record": "/r", "fields": fields})


# --- the weakness a mutation found, and the measurement behind it -------------------


def test_a_boolean_count_is_refused_by_the_type_check_and_not_by_another(
    tmp_path: pathlib.Path,
) -> None:
    """★ ★ M13 mutation B4. An existing test passes for the wrong reason, and this says why.

    ``tests/security/test_checkpoint_manifest.py`` parametrizes
    ``("records_consumed", True, "a boolean is not a count")`` and asserts the manifest is
    refused. **That test stays green even when the type check is replaced by
    ``isinstance``** -- measured, with the real numbers:

    ==========================  ==========================================
    ``rejected`` in the base    what refuses ``records_consumed: true``
    ==========================  ==========================================
    ``3`` (what that test uses) ``'rejected' is 3 but 'records_consumed' is only True``
    ``1``                       **accepted**
    ``0``                       **accepted**
    ==========================  ==========================================

    So the guarantee that test actually provides is "this manifest is refused", and
    *which* refusal fires is decided by the value of an unrelated field. With
    ``rejected: 0`` -- an ordinary manifest -- the boolean gets through, and 126
    hand-written tests do not notice; only the M12 property does.

    This test pins the **reason**, using a base manifest where nothing else can mask it,
    which is the difference between asserting the outcome and asserting the mechanism.
    """
    path = tmp_path / "boolean-count.json"
    path.write_text(
        json.dumps(
            {
                "version": CHECKPOINT_VERSION,
                "source": {"path": "source.xml", "sha256": "a" * 64, "size": 4096},
                "config": "c" * 64,
                "records_consumed": True,
                "rejected": 0,  # ★ nothing downstream can refuse this one for another reason
                "parts": [],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(CheckpointError) as refused:
        read_checkpoint(path)

    message = str(refused.value)
    assert "must be a JSON integer" in message, (
        f"the boolean was refused by something other than the type check, which means this "
        f"test would pass even if the type check were gone: {message}"
    )
    assert "cannot reject more records" not in message, (
        "a base manifest with rejected: 0 cannot trip that check, so reaching this line "
        "means the fixture changed: " + message
    )


def test_a_boolean_version_is_refused_by_the_version_check(tmp_path: pathlib.Path) -> None:
    """The same shape for ``version``, which has its own check and its own reason."""
    path = tmp_path / "boolean-version.json"
    path.write_text(
        json.dumps(
            {
                "version": True,
                "source": {"path": "source.xml", "sha256": "a" * 64, "size": 4096},
                "config": "c" * 64,
                "records_consumed": 0,
                "rejected": 0,
                "parts": [],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CheckpointError, match="declares format version"):
        read_checkpoint(path)


# --- an attribute in a middle segment: the right refusal, named ---------------------


@pytest.mark.parametrize("path_text", ["a/@b/c", "@a/b", "a/b/@c/d"])
def test_an_attribute_before_the_last_segment_is_refused_as_a_position_problem(
    path_text: str,
) -> None:
    """★ M13 mutation B3. A second check catches the same input with a worse message.

    Deleting the explicit "attribute may only be on its last segment" check does not make
    ``a/@b/c`` acceptable -- ``resolve_segments`` rejects ``@b`` as an invalid XML name --
    so every test asserting *that it is refused* stays green. What is lost is the
    diagnosis: the user is told ``segment is not a valid XML name: '@b'`` when the actual
    mistake is the attribute's position.

    This asserts the **message**, which is the thing the mutation removes, and it is the
    only kind of assertion that could tell the two apart.
    """
    with pytest.raises(FieldPathError) as refused:
        parse_field_path(path_text)
    assert "may only read an attribute from its last segment" in str(refused.value), (
        f"{path_text!r} was refused for the wrong reason, which would leave a user "
        f"chasing an invalid name instead of a misplaced attribute: {refused.value}"
    )


# --- checkpoint.py: verify_parts' three refusals -------------------------------------


def _checkpoint_with_part(name: str, rows: int) -> Checkpoint:
    return Checkpoint(
        source={"path": "source.xml", "sha256": "a" * 64, "size": 4096},
        config="c" * 64,
        records_consumed=rows,
        rejected=0,
        parts=(PartRecord(name=name, rows=rows),),
        complete=False,
        version=CHECKPOINT_VERSION,
    )


def test_verify_parts_reports_a_part_that_is_not_on_disk(tmp_path: pathlib.Path) -> None:
    """A missing part means a resume would skip records whose output is gone."""
    problems = verify_parts(_checkpoint_with_part("part-00000.csv", 3), tmp_path, "csv")
    assert problems == ["missing:  part-00000.csv"]


def test_verify_parts_reports_a_part_it_cannot_read(tmp_path: pathlib.Path) -> None:
    """A part that exists but cannot be counted is a different problem from a missing one.

    ★ Built with a **Parquet** part holding bytes that are not Parquet, because the branch
    is only reachable through a reader that actually fails -- and for CSV, counting rows
    out of any file on disk succeeds. The two construction attempts that did *not* work
    are worth recording: a directory at the part path is caught earlier by ``is_file()``
    and reported as **missing**, and a CSV full of binary noise parses as zero rows and is
    reported as **rows differ**. Only the Arrow metadata reader raises, which is why the
    code's own comment names it.
    """
    (tmp_path / "part-00000.parquet").write_bytes(b"PAR1 not actually parquet at all")
    checkpoint = _checkpoint_with_part("part-00000.parquet", 3)
    problems = verify_parts(checkpoint, tmp_path, "parquet")
    assert len(problems) == 1, problems
    assert problems[0].startswith("unreadable: part-00000.parquet ("), problems


def test_verify_parts_reports_a_part_whose_row_count_disagrees(tmp_path: pathlib.Path) -> None:
    """The manifest says 3 rows, the file has 2. Resuming on that skips or repeats one."""
    (tmp_path / "part-00000.csv").write_text("v\n1\n2\n", encoding="utf-8")
    problems = verify_parts(_checkpoint_with_part("part-00000.csv", 3), tmp_path, "csv")
    assert problems == ["rows differ: part-00000.csv (checkpoint 3, file 2)"]


def test_verify_parts_is_silent_when_everything_agrees(tmp_path: pathlib.Path) -> None:
    """The other half: an empty list means nothing is wrong, and that must be testable."""
    (tmp_path / "part-00000.csv").write_text("v\n1\n2\n", encoding="utf-8")
    assert verify_parts(_checkpoint_with_part("part-00000.csv", 2), tmp_path, "csv") == []


# --- checkpoint.py: the source block's type check -----------------------------------


def test_a_source_path_of_the_wrong_json_type_is_refused(tmp_path: pathlib.Path) -> None:
    """``source.path`` is the one field in that block checked by type rather than shape.

    The other two go through :func:`_integer` and :func:`_hex_digest`, which have their
    own tests; this line had none, and it is the one that would let a number into the
    "where the document was" slot that ``validate_resume`` compares.
    """
    path = tmp_path / "numeric-source-path.json"
    path.write_text(
        json.dumps(
            {
                "version": CHECKPOINT_VERSION,
                "source": {"path": 4096, "sha256": "a" * 64, "size": 4096},
                "config": "c" * 64,
                "records_consumed": 0,
                "rejected": 0,
                "parts": [],
                "complete": True,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(CheckpointError, match=r"\'source\.path\' must be a string"):
        read_checkpoint(path)


# --- checkpoint.py: the writer's own failure -----------------------------------------


def test_write_checkpoint_reports_a_target_it_cannot_replace(tmp_path: pathlib.Path) -> None:
    """The target is a directory, so the rename cannot land.

    ``write_checkpoint`` is the one place a checkpoint is produced, and its failure path
    had no test: a run that cannot record its progress gets a traceback instead of the
    one clear line every other failure in this module produces.
    """
    target = tmp_path / "run.ckpt.json"
    target.mkdir()
    with pytest.raises(CheckpointError, match="could not write the checkpoint"):
        write_checkpoint(target, _checkpoint_with_part("part-00000.csv", 0))


# --- config.py: a namespace prefix that is not a string -----------------------------


@pytest.mark.parametrize("prefix", [1, None, True, b"x", ("a",)])
def test_a_namespace_prefix_that_is_not_a_string_is_refused(prefix: object) -> None:
    """★ Unreachable from YAML, reachable from the public ``parse_config``.

    A YAML mapping has string keys, so this line can never be hit by a user editing a
    file -- which is exactly why no test covered it. It *is* reachable from the API the
    project declared stable in M10, and a caller building a config programmatically would
    get a message about a namespace prefix rather than about the type of their dict key.
    """
    with pytest.raises(ConfigError, match="must be a string"):
        parse_config(
            {
                "record": "/r",
                "fields": {"v": {"path": "p"}},
                "namespaces": {prefix: "urn:x"},
            }
        )


# --- writers.py: the writer's own state machine -------------------------------------


def test_a_writer_refuses_rows_after_a_batch_failed(tmp_path: pathlib.Path) -> None:
    """★ A writer that failed mid-run is unusable, and says so.

    The batch is flushed by ``batch_size=1`` so the key mismatch is actually checked --
    ``_check_batch`` runs at flush time, so with the default batch size nothing fails
    until ``close``, which is a construction worth writing down.
    """
    writer = create_writer(tmp_path / "out.csv", _config().fields, batch_size=1)
    with pytest.raises(WriterError):
        writer.write({"wrong": "keys"})
    with pytest.raises(WriterError, match="failed on an earlier batch"):
        writer.write({"v": "after the failure"})


def test_a_writer_refuses_rows_after_it_was_closed(tmp_path: pathlib.Path) -> None:
    """The other half of the same rule, and the one a library caller hits by accident."""
    writer = create_writer(tmp_path / "out.csv", _config().fields)
    writer.close()
    with pytest.raises(WriterError, match="already closed"):
        writer.write({"v": "too late"})


def test_abandon_keeps_the_partial_when_the_final_flush_itself_fails(
    tmp_path: pathlib.Path,
) -> None:
    """★ ``abandon`` swallows the flush failure on purpose, and marks the writer failed.

    An exception is already unwinding when ``abandon`` runs, so a second one from here
    would replace a useful message with a confusing one. The pending batch cannot be
    written -- its keys do not match -- and the right outcome is that the partial is
    closed and readable rather than that the caller sees an unrelated error.
    """
    target = tmp_path / "out.csv"
    writer = create_writer(target, _config().fields)
    writer._batch.append({"wrong": "keys"})  # a pending batch that cannot be flushed
    writer.abandon()

    assert writer._failed
    assert not target.exists(), "abandon must not publish"
    assert writer.partial_path.exists(), "but the partial is still worth keeping"


# --- writers.py: JSON encoding and the Parquet open ----------------------------------


def test_the_json_fallback_refuses_an_object_it_cannot_serialise() -> None:
    """``default=`` is only called for types json does not know, so this is the last line.

    Reaching it needs a value that is neither ``None``, a number, a string, a bool, a
    list nor a dict -- a plain object is the smallest such value.
    """
    with pytest.raises(TypeError, match="not JSON serialisable"):
        _json_default(object())


def test_the_json_fallback_renders_decimal_and_date() -> None:
    """The two branches ``_json_default`` exists for, which the M12 round trip only
    reaches through a writer configured for decimal and date fields."""
    import datetime as _datetime
    import decimal as _decimal

    assert _json_default(_decimal.Decimal("1.50")) == "1.50", "trailing zeros must survive"
    assert _json_default(_datetime.date(2024, 5, 6)) == "2024-05-06"
    assert _json_default(_datetime.datetime(2024, 5, 6, 7, 8, 9)) == "2024-05-06T07:08:09"


def test_the_parquet_writer_reports_a_partial_it_cannot_open(tmp_path: pathlib.Path) -> None:
    """The Parquet open failure, which is the format's twin of the CSV one.

    Built by putting a **directory** at the partial path: the writer opens
    ``<target>.tmp``, so blocking that name is what makes the open fail.
    """
    target = tmp_path / "out.parquet"
    (tmp_path / "out.parquet.tmp").mkdir()
    with pytest.raises(WriterError, match="cannot open"):
        create_writer(target, _config().fields, output_format=WriterFormat.PARQUET)


# --- the three public getters, and one cleanup branch --------------------------------


def test_the_reader_exposes_the_spec_it_parsed(tmp_path: pathlib.Path) -> None:
    """``record_spec`` is part of the stable reader's surface and no test called it."""
    document = tmp_path / "doc.xml"
    document.write_text("<catalog><product><n>A</n></product></catalog>", encoding="utf-8")
    reader = StreamingRecordReader(document, "/catalog/product", {})
    assert reader.record_spec.chain == ("catalog", "product")
    assert reader.record_spec.anchored


def test_the_writer_exposes_its_field_names_in_order(tmp_path: pathlib.Path) -> None:
    """The order is the contract: it decides the CSV header and the Parquet column order."""
    writer = create_writer(tmp_path / "out.csv", _config(one_field=False).fields)
    assert writer.field_names == ("v", "w")
    writer.close()


def test_the_parquet_writer_exposes_the_schema_it_committed_to(tmp_path: pathlib.Path) -> None:
    """The schema comes from the config and never from the data, so it is worth reading."""
    writer = create_writer(
        tmp_path / "out.parquet", _config().fields, output_format=WriterFormat.PARQUET
    )
    try:
        assert [field.name for field in writer.schema] == ["v"]
    finally:
        writer.close()


def test_a_record_that_is_the_document_root_is_still_cleaned_up(tmp_path: pathlib.Path) -> None:
    """The ``parent is None`` branch in the reader's cleanup.

    Reached when the record element *is* the root, so there is no parent to clear its
    siblings from. Without the branch the reader would raise on the very first record of
    a document whose root is the record.
    """
    document = tmp_path / "root-is-the-record.xml"
    document.write_text("<product id='1'><name>A</name></product>", encoding="utf-8")

    # ★ Read inside the loop, not after it. The reader clears each record as the walk
    # ends, so an element kept in a list and inspected afterwards has already been emptied
    # -- which is the bounded-memory design, not a defect. A first version of this test
    # collected the elements with list() and saw `id` come back as None.
    seen: list[str | None] = []
    for record in StreamingRecordReader(document, "/product", {}):
        seen.append(record.get("id"))
    assert seen == ["1"]
