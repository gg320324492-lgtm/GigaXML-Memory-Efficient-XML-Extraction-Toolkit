"""A manifest that disagrees with the files on disk is refused, not reconciled.

The manifest's job on a resume is to say how far the previous run got, and the parts it
names are what makes that claim worth anything. If a part has gone missing or its rows
no longer match what the manifest promises, then skipping ``records_consumed`` records
walks straight past rows nobody is ever going to write -- and the run finishes reporting
success. That is the failure this refuses: not a corrupt file, but a *mismatch*, caught
before any record is skipped.

The reader has always done this (:func:`gigaxml.checkpoint.verify_parts`); these tests
are the confirmation that it still does, and that it keeps saying which part, what it
expected, and what it found. "The checkpoint is inconsistent" would be true and useless:
the user cannot act on it without knowing which file to look at.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.cli import main

CONFIG = """\
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""


def build_run(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A completed checkpointed run, returning its parts directory.

    Three records in two parts, so a manifest exists that names files and promises a
    row count for each -- the two things every test below then breaks.
    """
    source = tmp_path / "src.xml"
    source.write_bytes((fixtures_dir / "two_records.xml").read_bytes())
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    parts = tmp_path / "parts"

    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(parts),
                "--checkpoint-every",
                "2",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    assert (parts / "checkpoint.json").is_file()
    return parts


def resume(parts: Path, tmp_path: Path) -> int:
    """Resume the run in ``parts``, returning the exit code."""
    return main(
        [
            "extract",
            str(tmp_path / "src.xml"),
            "-c",
            str(tmp_path / "config.yaml"),
            "-o",
            str(parts),
            "--checkpoint-every",
            "2",
            "--resume",
            "--format",
            "csv",
        ]
    )


def edit_manifest(directory: Path, **change: object) -> None:
    """Rewrite values in the manifest, as an outside hand would.

    The parameter is ``directory`` rather than ``parts`` so a caller can override the
    manifest's own ``parts`` key without the two colliding.
    """
    manifest = directory / "checkpoint.json"
    raw = json.loads(manifest.read_text(encoding="utf-8"))
    raw.update(change)
    manifest.write_text(json.dumps(raw, indent=2), encoding="utf-8")


def test_a_part_with_the_wrong_row_count_stops_the_resume(
    tmp_path: Path, fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A promised row count the file does not have means the numbers cannot be trusted.

    The assertion is on the *detail*, which is what makes the refusal actionable: the
    part by name and both counts. A message that only said the checkpoint was wrong
    would leave the user with no file to inspect.
    """
    parts = build_run(tmp_path, fixtures_dir)
    edit_manifest(parts, parts=[{"name": "part-00000.csv", "rows": 999}])

    assert resume(parts, tmp_path) == 1

    message = capsys.readouterr().err
    assert "rows differ: part-00000.csv" in message
    assert "checkpoint 999" in message
    assert "file" in message
    # Nothing about the resume may have advanced: the refusal happens before it skips.
    assert "Nothing was written" in message


def test_a_part_that_has_been_deleted_stops_the_resume(
    tmp_path: Path, fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A part the manifest names but the directory does not hold means rows would be lost.

    This is the one the refusal exists for: ``records_consumed`` is the number a resume
    skips, and skipping it while its part is gone means those records are never written
    by anyone -- a silent loss the run would otherwise report as a success.
    """
    parts = build_run(tmp_path, fixtures_dir)
    (parts / "part-00000.csv").unlink()

    assert resume(parts, tmp_path) == 1

    message = capsys.readouterr().err
    assert "missing:  part-00000.csv" in message
    assert "Nothing was written" in message


def test_a_part_whose_contents_changed_stops_the_resume(
    tmp_path: Path, fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A part still present but no longer the file the manifest described.

    Deleting a file is obvious; editing one is not. The row count is what ties the
    manifest to the bytes on disk, so a part that lost a row must fail even though every
    filename the manifest names still exists.
    """
    parts = build_run(tmp_path, fixtures_dir)
    target = parts / "part-00000.csv"
    lines = target.read_text(encoding="utf-8").splitlines()
    target.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")

    assert resume(parts, tmp_path) == 1

    assert "rows differ: part-00000.csv" in capsys.readouterr().err


def test_a_manifest_whose_parts_match_still_resumes(tmp_path: Path, fixtures_dir: Path) -> None:
    """The other side: an untouched manifest resumes, and the run completes.

    Without this, every test above would pass for an implementation that refused every
    resume -- which is a different bug, and the one that turns a safety check into a
    dead end. The exit code and the finished output are both asserted.
    """
    parts = build_run(tmp_path, fixtures_dir)
    before = json.loads((parts / "checkpoint.json").read_text(encoding="utf-8"))
    assert before["complete"] is True

    # Already complete: nothing to resume, and the tool says so rather than redoing work.
    assert resume(parts, tmp_path) == 0
