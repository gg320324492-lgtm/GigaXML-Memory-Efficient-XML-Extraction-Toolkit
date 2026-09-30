"""The three defects this milestone confirmed: two now pinned as repairs, one still open.

The file has changed shape once already, and the history is worth keeping.

**At M0** every test here asserted the *wrong* behaviour, on purpose -- "reading this
manifest accepts ``"complete": "false"``" -- so that a fix would have something to point
at. A fix proved only by its own new test proves nothing about whether the defect was
real.

**At M2** the manifest checks landed. The tests that pinned a coercion now assert the
refusal instead, keeping their names: ``test_a_known_defect_...`` records where each one
came from, and the docstring of each says what used to happen and what happens now. What
they protect has not changed -- only the direction of the assertion, from "it was
accepted" to "it is refused".

**The third defect is still open**, and its test still asserts today's behaviour. It is
marked with the milestone that owns it, because nothing here is a licence: pinning a
defect is what makes "fixed" mean something, and a defect nobody pinned can be removed
without anyone noticing.

**Why these call the library rather than the CLI.** Each defect lives in a data contract:
what a manifest is allowed to contain, what a part name may point at, and what a run's
identity covers. The CLI is where those contracts are *used*, but a test that reached
them through a subprocess would have to arrange a whole interrupted run to observe one
coercion, and would then be asserting on a report full of the very values it is trying to
probe. The command-line surface is frozen separately, in
:mod:`tests.golden.test_cli_golden`, and the one place where a manifest defect reaches
the user's screen -- a manifest that says "incomplete" being believed -- is tested there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    config_identity,
    read_checkpoint,
)
from gigaxml.config import parse_config
from gigaxml.errors import CheckpointError
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


# --- defect 1: a manifest was coerced rather than validated -------------------
#
# read_checkpoint applied str() / int() / bool() to each field. Every one of those
# conversions is total: it raised on nothing a JSON document can contain, so a manifest
# could say almost anything and still be believed. The field checks replace them with
# exact type tests, and these tests now record the refusal each one produces.


def test_a_known_defect_string_false_is_read_as_complete(tmp_path: Path) -> None:
    """``"complete": "false"`` is refused instead of being read as ``True``.

    **What it used to do**: ``bool("false")`` is ``True`` because the string is
    non-empty, so a manifest spelling out that the run did *not* finish was accepted and
    believed -- and a resume skipped the work nobody had done. That was the worst of the
    coercions, because it failed toward "we are finished" rather than toward an error.

    **Now**: the field must be a JSON boolean, and a string is not one. The refusal
    names the field and says why a string is dangerous here, because the mistake it
    prevents -- believing a finished run that is not -- is not obvious from the value
    alone.
    """
    path = write_manifest(tmp_path, complete="false")

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_numeric_strings_are_coerced_to_ints(tmp_path: Path) -> None:
    """Numeric text is refused for both a count and a row count.

    **What it used to do**: ``"records_consumed": "100"`` and ``"rows": "5"`` became
    numbers, so a manifest written by something else -- or edited by hand, or produced by
    a serialiser that quotes everything -- resumed as though this tool had written it.

    **Now**: both must be JSON integers. Tested as two separate manifests so that a fix
    covering one key but not the other still fails here.
    """
    for broken in (
        {"records_consumed": "100"},
        {"parts": [{"name": "part-00000.csv", "rows": "5"}]},
    ):
        path = write_manifest(tmp_path, **broken)

        with pytest.raises(CheckpointError, match="cannot be trusted"):
            read_checkpoint(path)


def test_a_known_defect_rejected_is_never_compared_with_records_consumed(
    tmp_path: Path,
) -> None:
    """``rejected`` above ``records_consumed`` is refused.

    **What it used to do**: nothing checked the two against each other, so a manifest
    claiming 999,999 rejections against 2,909 records was believed and the report's
    rejection total exceeded the document it described.

    **Now**: a run cannot have quarantined more records than it read, and a manifest
    that says otherwise describes a run that cannot have happened.

    Note what is *not* asserted: this is a relation between two fields, not a type check,
    so a manifest with both fields as the right types but the wrong relationship has to
    be caught by comparing them -- which is the part this pins.
    """
    path = write_manifest(tmp_path, rejected=999999)

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_a_config_hash_of_any_type_is_accepted(tmp_path: Path) -> None:
    """A config hash that is not a hash is refused.

    **What it used to do**: ``"config": 123`` became ``"123"`` through ``str()``, which
    will stringify anything. That hash is what a resume compares against to decide
    whether this would be the same run, so a manifest carrying something that was never a
    hash was compared as though it were one.

    **Now**: it must be 64 lowercase hexadecimal characters -- exactly what
    :func:`gigaxml.checkpoint.config_identity` writes. The case is checked too: this
    tool's digests are lowercase, so an uppercased one is a file somebody edited.
    """
    path = write_manifest(tmp_path, config=123)

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_an_incomplete_manifest_tells_resume_there_is_nothing_to_do(
    workdir: Path,
) -> None:
    """The refusal reaches the command line: the run stops instead of exiting 0.

    **What it used to do**: a manifest whose ``complete`` was the string ``"false"``
    ended a resumed run that had work left to do with exit code 0 and the words
    ``already complete; nothing to do`` -- which is why the coercion above mattered to
    anyone who was not reading the code.

    **Now**: the manifest is refused before the run decides anything, the exit code is
    non-zero, and the output the run was supposed to produce stays incomplete rather
    than being declared finished. The assertion is on all three, because any one of them
    alone would still let a partially-written output look done.
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

    assert result.returncode != 0
    assert "already complete" not in result.stderr
    assert "cannot be trusted" in result.stderr
    # And the record that was never extracted is still not in the output -- nothing
    # about the refusal may have advanced the run.
    rows = (parts / "part-00000.csv").read_text(encoding="utf-8")
    assert "Gamma Valve" not in rows


# --- defect 2: a part name was used as a path, unexamined ---------------------
#
# verify_parts resolved each manifest part name against the parts directory with no check
# on what the name was, so a name containing separators or a drive letter reached a file
# outside it. The check now happens when the manifest is read, before any path is built.


def test_a_known_defect_a_part_name_can_reach_a_file_outside_the_parts_directory(
    tmp_path: Path,
) -> None:
    """A part name that is not a part filename is refused when the manifest is read.

    **What it used to do**: ``"../outside.csv"`` was joined onto the parts directory and
    opened. The file one level above was a real CSV, so it was counted, and the only
    complaint was that its row count disagreed -- the tool had read a file the manifest
    pointed at, from outside the directory it was supposed to stay in.

    **This was a read, not a write.** Part names are generated by ``part_name(index,
    extension)`` on the writing side and never come from a manifest, so no run could be
    made to write outside the directory. The exposure was a resume opening an arbitrary
    file.

    **Now**: the name must be one this tool would have produced -- ``part-00000.csv``,
    ``.jsonl`` or ``.parquet`` -- and the refusal happens while the manifest is being
    read, so no path is built from it at all. The outside file is still there and still
    readable; the assertion that matters is that reaching for it never starts.
    """
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id,name\n1,not this run's data\n", encoding="utf-8")

    manifest = write_manifest(parts_dir, parts=[{"name": "../outside.csv", "rows": 2906}])

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(manifest)


# --- defect 3: the schema is not part of what a run is ------------------------
#
# config_identity hashes what the run depends on -- record path, namespaces, error
# policy, fields -- and does not include the XSD, which decides how those fields are
# typed. So two runs that would produce different data can share one identity.
#
# ★ STILL OPEN. Owned by M4, not M2. This test must keep asserting equality until that
# milestone lands; a change here belongs to a change in config_identity, not to any work
# on manifest parsing.


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
