"""Tests that pin the three defects this milestone confirmed, as they behave today.

**These assert the wrong behaviour on purpose.** Each one records what the tool does now
so that a fix has something to point at: without a test that says "this used to be
accepted", a fix proves only that the new test passes, not that the defect was real.
Every test here carries a comment naming the milestone that repairs it and what to assert
once it has.

None of this is a licence. The defects are real and are scheduled for repair -- pinning
them is what makes "fixed" mean something.

**Why these call the library rather than the CLI.** Each defect lives in a data contract:
what a manifest is allowed to contain, what a part name may point at, and what a run's
identity covers. The CLI is where those contracts are *used*, but a test that reached
them through a subprocess would have to arrange a whole interrupted run to observe one
coercion, and would then be asserting on a report full of the very values it is trying to
probe. The command-line surface is frozen separately, in
:mod:`tests.golden.test_cli_golden`, and the one place where a defect does reach the
user's screen -- a manifest that says "incomplete" being believed -- is tested there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    config_identity,
    read_checkpoint,
    verify_parts,
)
from gigaxml.config import parse_config
from tests.golden.conftest import run_cli

#: A manifest that is valid in every way the reader checks, so each test can break
#: exactly one thing and attribute the outcome to that thing.
VALID_MANIFEST: dict[str, Any] = {
    "version": CHECKPOINT_VERSION,
    "source": {"path": "source.xml", "sha256": "a" * 64, "size": 4096},
    "config": "c" * 64,
    "records_consumed": 2909,
    "rejected": 3,
    "parts": [{"name": "part-00000.csv", "rows": 2906}],
    "complete": True,
}


def write_manifest(directory: Path, **overrides: object) -> Path:
    """Write a manifest built from the valid one, with the given keys replaced."""
    raw = json.loads(json.dumps(VALID_MANIFEST))
    raw.update(overrides)
    path = directory / "checkpoint.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


# --- defect 1: a manifest is coerced rather than validated ------------------
#
# read_checkpoint applies str() / int() / bool() to each field. Every one of those
# conversions is total: it raises on nothing a JSON document can contain, so a manifest
# can say almost anything and still be believed.


def test_a_known_defect_string_false_is_read_as_complete(tmp_path: Path) -> None:
    """``"complete": "false"`` is read as ``True`` -- the worst of the coercions.

    ``bool("false")`` is ``True`` because the string is non-empty. So a manifest that
    spells out that the run did *not* finish is accepted and believed, and a resume
    skips the work that was never done. Everything about this is the defect; the
    reader's own version check is the only thing standing between it and a wrong
    answer.

    **When M2 repairs this, assert the refusal instead**: that reading this manifest
    raises :class:`CheckpointError`, and that ``complete`` is never ``True`` for it.
    """
    path = write_manifest(tmp_path, complete="false")

    checkpoint = read_checkpoint(path)

    assert checkpoint.complete is True


def test_a_known_defect_numeric_strings_are_coerced_to_ints(tmp_path: Path) -> None:
    """``"records_consumed": "100"`` and ``"rows": "5"`` are accepted as numbers.

    A number written as text still becomes a number, so a manifest written by a
    different tool -- or edited by hand, or produced by a serialiser that quotes
    everything -- resumes as though it were produced by this one.

    **When M2 repairs this, assert the refusal for both keys.**
    """
    path = write_manifest(
        tmp_path,
        records_consumed="100",
        parts=[{"name": "part-00000.csv", "rows": "5"}],
    )

    checkpoint = read_checkpoint(path)

    assert checkpoint.records_consumed == 100
    assert checkpoint.parts[0].rows == 5


def test_a_known_defect_rejected_is_never_compared_with_records_consumed(tmp_path: Path) -> None:
    """``rejected`` is accepted at any magnitude, including above the records consumed.

    The two counts are not independent: a run cannot have rejected more records than
    it read. Nothing checks that, so a manifest claiming 999,999 rejections against
    2,909 records is believed, and the rejection total in the run report becomes
    larger than the document it describes.

    **When M2 repairs this, assert the refusal.**
    """
    path = write_manifest(tmp_path, rejected=999999)

    checkpoint = read_checkpoint(path)

    assert checkpoint.rejected == 999999
    assert checkpoint.rejected > checkpoint.records_consumed


def test_a_known_defect_a_config_hash_of_any_type_is_accepted(tmp_path: Path) -> None:
    """``"config": 123`` is accepted and becomes ``"123"``.

    The one coercion with a consequence beyond this module: the hash is what a resume
    compares against to decide whether the run would be the same run, so a manifest
    carrying something that was never a hash is compared as though it were one.

    **When M2 repairs this, assert the refusal.**
    """
    path = write_manifest(tmp_path, config=123)

    checkpoint = read_checkpoint(path)

    assert checkpoint.config == "123"


def test_a_known_defect_an_incomplete_manifest_tells_resume_there_is_nothing_to_do(
    workdir: Path,
) -> None:
    """The coercion's consequence, as a user meets it: exit 0, "already complete".

    This is the end of defect 1, and it is why the defect matters. A manifest whose
    ``complete`` is the string ``"false"`` ends a resumed run that has work left to do,
    with a success code and a message saying there is nothing to do.

    **When M2 repairs this, the run should refuse instead**, with a non-zero exit code
    and an error naming the manifest.
    """
    parts = workdir / "parts"
    first = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-o",
        str(parts),
        "--checkpoint-every",
        "2",
        "--format",
        "csv",
        cwd=workdir,
    )
    assert first.returncode == 0

    manifest_path = parts / "checkpoint.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Half the run: one part committed, the record after it never read.
    manifest["complete"] = "false"
    manifest["records_consumed"] = 1
    manifest["parts"] = manifest["parts"][:1]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-o",
        str(parts),
        "--checkpoint-every",
        "2",
        "--resume",
        "--format",
        "csv",
        cwd=workdir,
    )

    assert result.returncode == 0
    assert "already complete" in result.stderr
    # The record that was never extracted is not in the output.
    rows = (parts / "part-00000.csv").read_text(encoding="utf-8")
    assert "Gamma Valve" not in rows


# --- defect 2: a part name is used as a path, unexamined ---------------------
#
# verify_parts resolves each manifest part name against the parts directory with no
# check on what the name is. A name containing separators or a drive letter therefore
# reaches a file outside the directory.


def test_a_known_defect_a_part_name_can_reach_a_file_outside_the_parts_directory(
    tmp_path: Path,
) -> None:
    """``"../outside.csv"`` is opened and read, and the bytes are believed.

    The file is a real CSV one level above the parts directory, so the check finds it,
    counts its rows, and reports a mismatch. That is the shape of the problem: the tool
    reads a file the manifest named, from wherever the manifest pointed.

    **This is a read, not a write.** Part names are generated by ``part_name(index,
    extension)`` on the writing side and never come from a manifest, so no run can be
    made to write outside the directory. The exposure is a manifest being able to make a
    resume open and parse an arbitrary file.

    **When M2 repairs this, assert the refusal instead of the read**: a part name that
    is not a plain file name in the parts directory should raise
    :class:`CheckpointError` *before* the path is opened, so the row count in this
    assertion is never computed.
    """
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id,name\n1,not this run's data\n", encoding="utf-8")

    manifest = write_manifest(parts_dir, parts=[{"name": "../outside.csv", "rows": 2906}])
    checkpoint = read_checkpoint(manifest)

    problems = verify_parts(checkpoint, parts_dir, "csv")

    # It was found, and read: the problem is about row counts, not about a missing file,
    # which is exactly what "it looked outside the parts directory" looks like from here.
    assert problems == ["rows differ: ../outside.csv (checkpoint 2906, file 1)"]


# --- defect 3: the schema is not part of what a run is ------------------------
#
# config_identity hashes what the run depends on -- record path, namespaces, error
# policy, fields -- and does not include the XSD, which decides how those fields are
# typed. So two runs that would produce different data can share one identity.


def test_a_known_defect_the_xsd_is_absent_from_the_run_identity() -> None:
    """Two different schemas hash identically, so a resume cannot notice the change.

    Changing an XSD's contents changes how every field is typed and therefore what the
    extraction writes, while leaving the run identity untouched. ``validate_resume``
    compares that identity and nothing else about the schema, so ``--resume`` proceeds
    and appends rows of a different shape to parts written under the old one.

    **When M4 repairs this, assert the opposite**: that the two hashes differ. A test
    that asserts equality is a test that has to be deleted, so it is written here to be
    found and changed rather than quietly removed.
    """
    base = {"record": "/catalog/products/product", "fields": {"name": {"path": "name"}}}
    with_schema_a = parse_config({**base, "schema": "catalog-a.xsd"})
    with_schema_b = parse_config({**base, "schema": "catalog-b.xsd"})

    assert config_identity(with_schema_a) == config_identity(with_schema_b)
