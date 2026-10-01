"""A run must never write over the file it is reading.

The source document is the one copy of the user's data. An output that lands on it
destroys that copy, and the run reports success while doing it -- there is nothing to
notice afterwards, because from the program's point of view it wrote exactly the file
it was asked to write.

What made this reachable is that the existing guard is not one: an ``.xml`` output is
refused because no format can be *inferred* from that suffix, and a user who passes
``--format csv`` explicitly walks past it straight into the overwrite. A protection
that disappears when the user is more specific is not protecting anything.

**Every assertion here is on the file's bytes, not on the exit code.** "We refuse" and
"we refuse before touching the disk" are different claims, and only the second is worth
having: a refusal that arrives after a ``.tmp`` was written has already changed the
state of the filesystem.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gigaxml.cli import main

SOURCE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<catalog><products>"
    '<product id="1"><name>Alpha</name></product>'
    '<product id="2"><name>Beta</name></product>'
    "</products></catalog>"
)

CONFIG = 'record: /catalog/products/product\nfields:\n  id:\n    path: "@id"\n'


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A source document and a config, in a directory the run happens inside.

    ``main()`` resolves relative paths against the *process* working directory, so the
    fixture moves there rather than passing absolute paths everywhere. Relative and
    ``..`` spellings are half of what this file is about, and they can only be written
    as relative if there is a directory for them to be relative to.
    """
    (tmp_path / "a.xml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "cfg.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def extract(source: str, output: str, *extra: str) -> int:
    """Run ``extract`` from inside the workspace the fixture moved us to."""
    return main(["extract", source, "-c", "cfg.yaml", "-o", output, *extra])


# --- criterion A: the same path, refused before anything is written -----------


def test_writing_the_output_over_the_input_is_refused(workspace: Path) -> None:
    """The plain case, and the one that destroyed the file before this change.

    ``--format csv`` is passed deliberately: without it the run is stopped by the
    format-inference error, and a test that relied on that would be asserting the
    suffix check rather than the protection.
    """
    before = digest(workspace / "a.xml")

    assert extract("a.xml", "a.xml", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before
    # And it is still the document, not an empty file the refusal happened to leave.
    assert (workspace / "a.xml").read_text(encoding="utf-8").startswith("<?xml")


def test_the_source_is_untouched_even_by_a_temporary_file(workspace: Path) -> None:
    """Nothing is left behind: no ``.tmp``, no renamed original, no side effects.

    The writer publishes atomically by writing a sibling ``.tmp`` and renaming it. A
    refusal that happened *after* that write would leave the temporary behind, and on
    some paths would have already replaced the target. This asserts the refusal is
    where it claims to be -- before any writer exists.
    """
    extract("a.xml", "a.xml", "--format", "csv")

    assert sorted(p.name for p in workspace.iterdir()) == ["a.xml", "cfg.yaml"]


def test_the_refusal_says_which_two_paths_collide(
    workspace: Path,  # noqa: ARG001 -- the fixture is needed for its chdir side effect
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The message names both paths and both roles.

    "invalid output" would leave the user looking for a syntax problem in a path that
    is perfectly well formed. What they need is to see that the two names they gave are
    the same file.
    """
    extract("a.xml", "a.xml", "--format", "csv")

    message = capsys.readouterr().err
    assert "same file" in message
    assert "a.xml" in message
    assert "input" in message


# --- criterion B: every spelling of the same file ----------------------------


def test_a_relative_and_an_absolute_spelling_of_one_file_collide(workspace: Path) -> None:
    """``./a.xml`` and the absolute path are the same file; a string comparison says not."""
    before = digest(workspace / "a.xml")

    assert extract(str(workspace / "a.xml"), "./a.xml", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before


def test_a_path_containing_dot_dot_collides(workspace: Path) -> None:
    """``sub/../a.xml`` is ``a.xml``, and normalising is what sees that."""
    (workspace / "sub").mkdir()
    before = digest(workspace / "a.xml")

    assert extract("a.xml", "sub/../a.xml", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before


def test_a_symlink_to_the_input_collides(workspace: Path) -> None:
    """Following the link is what makes this one work -- the two names are different.

    Skipped where the link cannot be created, and the skip says so: a silently skipped
    test is indistinguishable from one that passed.
    """
    link = workspace / "link.xml"
    try:
        link.symlink_to(workspace / "a.xml")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified here: cannot create a symlink ({exc})")
    before = digest(workspace / "a.xml")

    assert extract("a.xml", "link.xml", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before


def test_a_case_difference_in_the_output_path_collides(workspace: Path) -> None:
    """On a case-insensitive filesystem ``A.XML`` and ``a.xml`` are one file.

    Only reaches the assertion where the filesystem folds case, so the test asks it
    rather than assuming: it writes through the differently-cased name and checks
    whether the source came back changed. On Linux the two names are two files, nothing
    collides, and the test says so instead of reporting a pass it did not earn.
    """
    variant = workspace / "A.XML"
    try:
        if not _same_case_insensitive(workspace):
            pytest.skip(
                "unverified here: this filesystem is case-sensitive, so the two names differ"
            )
    except OSError as exc:  # pragma: no cover - depends on the filesystem
        pytest.skip(f"unverified here: cannot probe case folding ({exc})")

    before = digest(workspace / "a.xml")

    assert extract("a.xml", "A.XML", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before
    assert variant.exists()


def _same_case_insensitive(directory: Path) -> bool:
    """Does this filesystem treat ``A.XML`` and ``a.xml`` as the same name?"""
    probe = directory / "caseprobe.txt"
    probe.write_text("probe", encoding="utf-8")
    try:
        return (directory / "CASEPROBE.TXT").exists()
    finally:
        probe.unlink(missing_ok=True)


def test_a_hard_link_to_the_input_collides(workspace: Path) -> None:
    """Two names, one inode, no path arithmetic that can tell.

    This is the case that needs ``samefile`` rather than ``resolve``: the two paths
    resolve to themselves and are genuinely different names. Only the filesystem knows
    they are one file. Skipped with a reason where hard links cannot be made -- Windows
    needs a privilege a plain user may not have.
    """
    hard = workspace / "hard.xml"
    try:
        os.link(workspace / "a.xml", hard)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified here: cannot create a hard link ({exc})")
    before = digest(workspace / "a.xml")

    assert extract("a.xml", "hard.xml", "--format", "csv") != 0

    assert digest(workspace / "a.xml") == before


# --- criterion C: the other paths a run writes -------------------------------


def test_a_report_landing_on_the_input_is_refused(workspace: Path) -> None:
    """``--report`` is the same defect, not a neighbour of one.

    It is a path the user names, written after the run, and pointing it at the source
    overwrites the source just as completely as ``-o`` does.
    """
    before = digest(workspace / "a.xml")

    assert main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv", "--report", "a.xml"]) != 0

    assert digest(workspace / "a.xml") == before


def test_a_report_landing_on_the_output_is_refused(workspace: Path) -> None:
    """Two artefacts of one run cannot share a path.

    Whichever is written last wins, and which that is depends on the order the run
    happens to finish in -- so the observable result would differ between runs of the
    same command. Refused for that reason rather than for data loss.
    """
    assert main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv", "--report", "out.csv"]) != 0

    assert not (workspace / "out.csv").exists()


def test_the_checkpoint_directory_cannot_be_the_input(workspace: Path) -> None:
    """``--checkpoint-every`` turns ``-o`` into a directory; it must not be the source."""
    before = digest(workspace / "a.xml")

    assert extract("a.xml", "a.xml", "--checkpoint-every", "1") != 0

    assert digest(workspace / "a.xml") == before


# --- criterion D: legitimate output keeps working -----------------------------


def test_an_output_beside_the_input_is_fine(workspace: Path) -> None:
    assert extract("a.xml", "out.csv", "--format", "csv") == 0
    assert (workspace / "out.csv").is_file()


def test_an_output_in_another_directory_is_fine(workspace: Path) -> None:
    (workspace / "elsewhere").mkdir()
    assert extract("a.xml", "elsewhere/out.csv", "--format", "csv") == 0
    assert (workspace / "elsewhere" / "out.csv").is_file()


def test_an_existing_unrelated_file_is_still_overwritten(workspace: Path) -> None:
    """The check is about *this run's input*, not about files that happen to exist.

    A guard that refused whenever the output existed would break the ordinary rerun --
    running the same extraction twice must overwrite yesterday's output, which is what
    the user asked for.
    """
    (workspace / "out.csv").write_text("stale content from a previous run\n", encoding="utf-8")

    assert extract("a.xml", "out.csv", "--format", "csv") == 0
    assert "stale" not in (workspace / "out.csv").read_text(encoding="utf-8")


def test_a_report_beside_the_output_is_fine(workspace: Path) -> None:
    assert (
        main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv", "--report", "rep.json"]) == 0
    )
    assert (workspace / "rep.json").is_file()


def test_sample_is_guarded_the_same_way(workspace: Path) -> None:
    """``sample`` shares the extraction loop, so it needs the same protection.

    A guard bolted only onto ``extract`` would leave the identical overwrite reachable
    through a command that writes the same kind of file.
    """
    before = digest(workspace / "a.xml")

    assert (
        main(["sample", "a.xml", "-c", "cfg.yaml", "-n", "1", "-o", "a.xml", "--format", "csv"])
        != 0
    )

    assert digest(workspace / "a.xml") == before


def test_reading_from_standard_input_is_not_blocked(workspace: Path) -> None:
    """A stream has no path, so there is nothing for the output to collide with.

    The report-versus-output check still runs -- those are both real paths -- but the
    input-versus-output one has nothing to compare and must not refuse on a missing
    argument.
    """
    import io
    import sys

    original = sys.stdin
    try:
        sys.stdin = io.TextIOWrapper(io.BytesIO(SOURCE.encode("utf-8")))
        sys.stdin.buffer.name = "<stdin>"  # type: ignore[attr-defined]
        assert main(["extract", "-", "-c", "cfg.yaml", "-o", "out.csv", "--format", "csv"]) == 0
    finally:
        sys.stdin = original
    assert (workspace / "out.csv").is_file()
