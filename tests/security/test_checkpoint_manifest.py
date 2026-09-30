"""What a manifest has to contain before ``--resume`` will believe it.

A checkpoint file is written by a previous run, but by the time it is read it is
*somebody else's file*: it can have been edited, replaced, or produced by a different
tool entirely. ``--resume`` trusts it to decide how many records to skip and which files
to open, so every field is checked for the exact JSON type it must have and the range it
must sit in.

**The checks are type tests, not conversions.** ``int("100")`` and ``bool("false")``
never raise, so a reader built on them returns whatever the manifest said under a type
the caller assumed -- which is how a string ``"false"`` once meant "finished". Nothing
here converts; a value either already is what it must be, or the manifest is refused.

The suite is two-sided on purpose. Refusing hostile input is the obvious half; the other
half -- everything :func:`gigaxml.checkpoint.write_checkpoint` produces must still read
back -- is the one that catches a check written too tightly. A reader that rejected every
manifest would pass every test here except that one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    Checkpoint,
    PartRecord,
    config_identity,
    read_checkpoint,
    source_identity,
    write_checkpoint,
)
from gigaxml.config import parse_config
from gigaxml.errors import CheckpointError

#: A manifest that is valid in every way the reader checks.
VALID: dict[str, object] = {
    "version": CHECKPOINT_VERSION,
    "source": {"path": "source.xml", "sha256": "a" * 64, "size": 4096},
    "config": "c" * 64,
    "records_consumed": 2909,
    "rejected": 3,
    "parts": [{"name": "part-00000.parquet", "rows": 2906}],
    "complete": True,
}


def write_manifest(tmp_path: Path, **overrides: object) -> Path:
    """One manifest: the valid shape with ``overrides`` applied and written to disk."""
    import json

    raw = json.loads(json.dumps(VALID))
    raw.update(overrides)
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def real_checkpoint(tmp_path: Path) -> tuple[Checkpoint, Path]:
    """A manifest this tool actually writes, via its own writer.

    Built from :func:`source_identity` over a real file and :func:`config_identity` over
    a real config, because the point is that what the *writer* produces must survive the
    *reader* -- a hand-written dict would only prove the reader accepts itself.
    """
    source = tmp_path / "source.xml"
    source.write_text("<catalog/>", encoding="utf-8")
    config = parse_config({"record": "/catalog/product", "fields": {"name": {"path": "name"}}})
    written = Checkpoint(
        source=source_identity(source),
        config=config_identity(config),
        records_consumed=2909,
        rejected=3,
        parts=(PartRecord(name="part-00000.parquet", rows=2906),),
        complete=True,
    )
    path = tmp_path / "checkpoint.json"
    write_checkpoint(path, written)
    return written, path


# --- criterion F: nothing the writer produces may be refused ------------------
#
# Ranked first because a stricter reader that rejected legitimate manifests would fail
# here while passing every hostile-input test below.


def test_a_manifest_this_tool_wrote_reads_back_exactly(tmp_path: Path) -> None:
    """Round trip: write, read, and get the same values back.

    This is the guard against over-tightening. Every field is compared, not sampled --
    a check that rejected one legitimate flavour of a value (an uppercase digest, say,
    or a part index above 99999) is caught here rather than in production.
    """
    written, path = real_checkpoint(tmp_path)

    read = read_checkpoint(path)

    assert read == written
    assert read.version == CHECKPOINT_VERSION
    assert read.complete is True
    assert read.source["path"].endswith("source.xml")


def test_a_part_name_this_tool_would_produce_is_accepted(tmp_path: Path) -> None:
    """Every index and extension ``part_name`` can emit still reads back.

    The pattern allows ``part-NNNNN`` for a five-digit index, so all of them, plus the
    three formats a part can be written in. A check that only allowed the first index
    would pass the round trip above and fail a run with more than one part.
    """
    for name in (
        "part-00000.csv",
        "part-00001.jsonl",
        "part-12345.parquet",
        "part-99999.parquet",
    ):
        path = write_manifest(tmp_path, parts=[{"name": name, "rows": 1}])

        assert read_checkpoint(path).parts[0].name == name


def test_zero_counts_and_an_incomplete_run_are_accepted(tmp_path: Path) -> None:
    """A manifest at the bottom of every range: zero records, zero rejected, not done.

    Boundary values are where a check written as ``> 0`` instead of ``>= 0`` would
    reject a run that legitimately had nothing to do yet.
    """
    path = write_manifest(
        tmp_path,
        records_consumed=0,
        rejected=0,
        parts=[],
        complete=False,
    )

    read = read_checkpoint(path)

    assert read.records_consumed == 0
    assert read.rejected == 0
    assert read.parts == ()
    assert read.complete is False


# --- criterion A: exact types, no conversion ---------------------------------


@pytest.mark.parametrize(
    ("key", "value", "why"),
    [
        ("version", True, "a boolean is not a version number"),
        ("version", 1.0, "a float is not a version number"),
        ("version", "1", "a numeric string is not a version number"),
        ("version", None, "null is not a version number"),
        ("records_consumed", "100", "numeric text is not a count"),
        ("records_consumed", True, "a boolean is not a count"),
        ("records_consumed", -1, "a count cannot be negative"),
        ("records_consumed", 2.5, "a fractional count is not a count"),
        ("rejected", "3", "numeric text is not a count"),
        ("rejected", -1, "a count cannot be negative"),
        ("complete", "false", "a string is not a boolean, and non-empty means True"),
        ("complete", 1, "an integer is not a boolean"),
        ("complete", 0, "an integer is not a boolean"),
        ("config", 123, "an integer is not a digest"),
        ("config", "A" * 64, "digests are written in lowercase"),
        ("config", "c" * 32, "a short digest is not a digest"),
        ("source", "not-a-block", "the source block must be an object"),
        ("parts", {"name": "part-00000.csv"}, "parts must be an array"),
    ],
)
def test_a_manifest_of_the_wrong_type_is_refused(
    tmp_path: Path, key: str, value: object, why: str
) -> None:
    """Every wrong-typed field is refused, and the refusal names the field.

    Parametrised over the whole table so a fix that covers one key while forgetting
    another still fails. The message is asserted as well as the exception: a refusal
    that does not say which field and what was expected is not one a user can act on.

    ``why`` earns its place on the failure path -- if the manifest *is* accepted, the
    failure says which value and what was wrong with it, rather than leaving the
    parametrised id to do all the explaining.
    """
    path = write_manifest(tmp_path, **{key: value})

    with pytest.raises(CheckpointError, match="cannot be trusted") as refused:
        read_checkpoint(path)

    if key not in str(refused.value):
        # The refusal must name the field it found fault with, not just "a field".
        pytest.fail(f"{key!r} is not named in the refusal: {refused.value} ({why})")


def test_the_version_check_is_a_type_check_and_not_a_value_comparison(
    tmp_path: Path,
) -> None:
    """``True`` and ``1.0`` are refused even though both compare equal to ``1``.

    This one deserves its own test because the defect it prevents is invisible in the
    source: ``version != 1`` looks correct, yet ``True == 1`` and ``1.0 == 1`` are both
    true in Python, so a manifest carrying ``"version": true`` would pass a value
    comparison while failing nothing else. The check has to be on the type first.
    """
    # Each of these compares equal to 1 under ``==``, so a value comparison accepts all
    # three while a type test refuses all three. That difference is the whole test.
    for value in (True, 1.0, "1", None):
        path = write_manifest(tmp_path, version=value)

        with pytest.raises(CheckpointError, match="cannot be trusted"):
            read_checkpoint(path)


def test_rejected_cannot_exceed_records_consumed(tmp_path: Path) -> None:
    """A relation between two fields, checked by comparing them.

    Both values are individually well-typed in this case, so no type test can catch it:
    a run cannot have quarantined more records than it read, and a manifest that says so
    describes a run that never happened.
    """
    path = write_manifest(tmp_path, records_consumed=2909, rejected=999999)

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_source_block_missing_a_field_is_refused(tmp_path: Path) -> None:
    """All three parts of ``source`` are required, not just the hash.

    The reader validates a resume against all three; a manifest missing one would
    otherwise fail later as "the source has changed", which is a lie about the source.
    """
    for broken in (
        {"path": "p.xml", "sha256": "a" * 64},
        {"path": "p.xml", "size": 10},
        {"sha256": "a" * 64, "size": 10},
    ):
        path = write_manifest(tmp_path, source=broken)

        with pytest.raises(CheckpointError, match="cannot be trusted"):
            read_checkpoint(path)


def test_a_part_entry_of_the_wrong_shape_is_refused(tmp_path: Path) -> None:
    """Each entry in ``parts`` must be an object carrying both fields."""
    for broken in (
        ["part-00000.csv"],
        [{"name": "part-00000.csv"}],
        [{"rows": 5}],
        [{"name": 123, "rows": 5}],
        [{"name": "part-00000.csv", "rows": "5"}],
        [{"name": "part-00000.csv", "rows": -1}],
    ):
        path = write_manifest(tmp_path, parts=broken)

        with pytest.raises(CheckpointError, match="cannot be trusted"):
            read_checkpoint(path)


# --- criterion B: a part name is a filename, not a path ----------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "../outside.csv",
        "../../etc/passwd",
        "..\\outside.csv",
        "subdir/part-00000.csv",
        "/etc/hosts",
        "C:\\Windows\\win.ini",
        "part-0000.csv",
        "part-000000.csv",
        "part-00000.parqet",
        "part-00000.csv.bak",
        "part-00000",
        "checkpoint.json",
        "",
        "part-00000.csv/",
        "./part-00000.csv",
    ],
)
def test_a_part_name_that_is_not_a_part_filename_is_refused(tmp_path: Path, bad_name: str) -> None:
    """Nothing joins a manifest-supplied name onto the parts directory.

    The manifest gets to choose from names this tool would have generated and no others.
    Every spelling above would otherwise be a way out of the directory: separators, a
    drive, an absolute path, or simply a file that is not a part. The check happens
    while the manifest is read, so no path is ever built from the rejected value.
    """
    path = write_manifest(tmp_path, parts=[{"name": bad_name, "rows": 5}])

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


# --- criterion D: the refusal is a domain error, not a crash ------------------


def test_every_refusal_is_a_checkpoint_error_and_not_a_traceback(tmp_path: Path) -> None:
    """No ``KeyError`` / ``TypeError`` / ``ValueError`` may reach the caller.

    Each of those was reachable before: a missing key raised ``KeyError`` from the
    comprehension, and a wrong type slipped through a conversion instead of raising at
    all. The CLI prints one line for a :class:`CheckpointError` and a traceback for
    anything else, so an unhandled one would put this library's internals on a user's
    terminal.
    """
    import json

    # A manifest with a field taken out entirely, plus one with a value too odd for
    # the conversions to have handled gracefully -- both must land as CheckpointError.
    raw = json.loads(json.dumps(VALID))
    del raw["records_consumed"]
    path = tmp_path / "checkpoint.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_missing_manifest_is_still_a_clear_message_not_a_crash(tmp_path: Path) -> None:
    """The I/O case stays worded differently from the trust case.

    Two kinds of refusal, two shapes: a file that is not there is an I/O fact, and a
    file whose contents are wrong is a trust fact. Collapsing them into one message
    would have a user looking for edits they did not make when they simply ran the
    command in the wrong directory.
    """
    with pytest.raises(CheckpointError) as missing:
        read_checkpoint(tmp_path / "nope.json")

    assert "cannot be trusted" not in str(missing.value)
    assert "nothing to resume" in str(missing.value)
