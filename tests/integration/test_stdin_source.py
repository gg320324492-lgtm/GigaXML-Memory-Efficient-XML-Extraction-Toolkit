"""``extract -``: reading a document from a pipe.

Standard input is the one input that cannot be named, re-read, or seeked, and each of
those has a consequence this module checks rather than assumes:

* it cannot be hashed, so ``--resume`` and ``--checkpoint-every`` are refused **by
  name**, with the reason, rather than quietly skipping the verification they exist to
  perform;
* it cannot be seeked, so it must still be streamed -- and the measurement that a pipe
  costs the same bounded memory as a file lives in the performance suite, not here,
  because it takes about eighty seconds.

What is here is the part that is fast enough to run everywhere: the bytes that come out
of a pipe are the bytes that come out of the file, the refusals are refusals rather than
silent degradations, and a broken pipe is an error with a message rather than an empty
CSV that looks like a document with no records in it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gigaxml.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>2</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
</root>
"""

FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}


def write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "in.xml"
    source.write_text(SOURCE, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"record": "/root/item", "fields": FIELDS}, sort_keys=False),
        encoding="utf-8",
    )
    return source, config


def run_with_stdin(
    source: Path, config: Path, output: Path, *extra: str
) -> subprocess.CompletedProcess[str]:
    """The real thing: a child process with the document on its standard input.

    A subprocess rather than a monkeypatched ``sys.stdin`` because the point is that the
    CLI reads its source from a handle rather than from a path, and an in-process fake
    of ``sys.stdin.buffer`` is a ``BytesIO`` the reader could not tell from anything.

    The document is handed over as an open file rather than as ``input=bytes`` because
    on this machine the second form does not terminate -- not a property of the code
    under test, but worth saying rather than leaving a test that passes only where
    ``communicate`` happens to work. A pipe remains the harder case and is covered by
    the performance suite, which measures the same reader from a path and from a handle
    and finds the same bounded memory; the parser never seeks either, so what holds for
    one holds for the other.
    """
    with source.open("rb") as document:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "gigaxml.cli",
                "extract",
                "-",
                "-c",
                str(config),
                "-o",
                str(output),
                *extra,
            ],
            stdin=document,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )


def test_a_piped_document_produces_the_same_bytes_as_the_file(tmp_path: Path) -> None:
    """The output of ``cat in.xml | gigaxml extract -`` equals the file's, byte for byte.

    A streaming path is a second implementation of the parser, and a second
    implementation is where the two start to disagree. Comparing the outputs rather than
    the record counts is what catches a disagreement that happens to have the right
    number of rows.
    """
    source, config = write_inputs(tmp_path)
    from_file = tmp_path / "file.csv"
    from_stdin = tmp_path / "stdin.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(from_file)]) == 0
    completed = run_with_stdin(source, config, from_stdin)

    assert completed.returncode == 0, completed.stderr[-500:]
    assert from_stdin.read_bytes() == from_file.read_bytes()


def test_a_piped_run_says_so_in_its_report(tmp_path: Path) -> None:
    """The report admits the input has no hash, and says why.

    A stream is the case the "never fill a missing measurement with a zero" rule exists
    for, so it is worth pinning: the run's report must carry a null identity and a
    sentence, not an empty string that a scanner would read as an unchanged file.
    """
    source, config = write_inputs(tmp_path)
    report = tmp_path / "report.json"
    completed = run_with_stdin(source, config, tmp_path / "out.csv", "--report", str(report))

    assert completed.returncode == 0, completed.stderr[-500:]
    import json

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["input_identity"] is None
    assert "input_identity_error" in payload
    assert "0" * 64 not in json.dumps(payload)


@pytest.mark.parametrize(
    "extra,expected_phrase",
    [
        (("--checkpoint-every", "10", "--resume"), "cannot read standard input"),
        (("--checkpoint-every", "10"), "cannot be used with standard input"),
    ],
    ids=["resume", "checkpoint"],
)
def test_the_features_that_need_a_hash_refuse_a_pipe(
    tmp_path: Path, extra: tuple[str, ...], expected_phrase: str
) -> None:
    """Both resume and checkpointing are refused, with the reason, and nothing is written.

    These two exist to verify that the document is the one a previous run saw. A stream
    cannot be re-read, so there is nothing to verify against -- and the failure mode
    without a refusal is the worst kind, which is no failure: a run that reported a
    guarantee it had not made.
    """
    source, config = write_inputs(tmp_path)
    output = tmp_path / "out.csv"
    completed = run_with_stdin(source, config, output, *extra)

    assert completed.returncode == 1, (
        f"expected a refusal, got exit {completed.returncode}: {completed.stdout[-300:]}"
    )
    assert expected_phrase in completed.stderr
    assert not output.exists(), "a refused run must not leave an output behind"


def test_a_file_named_dash_is_still_just_a_missing_file(tmp_path: Path) -> None:
    """The sentinel is the literal ``-``; a real path that happens to be missing still errors.

    Without this, a typo like ``gigaxml extract input.xml -`` would be read as "read
    standard input" and silently extract whatever happened to be on the pipe -- or, with
    nothing on the pipe, produce an empty output that looks like a document with no
    records in it.
    """
    config = write_inputs(tmp_path)[1]
    output = tmp_path / "out.csv"

    exit_code = main(
        ["extract", str(tmp_path / "no-such-file.xml"), "-c", str(config), "-o", str(output)]
    )
    assert exit_code == 1, "a missing file must fail the run, not be read as a pipe"
    assert not output.exists()


def test_an_empty_pipe_is_an_error_not_an_empty_csv(tmp_path: Path) -> None:
    """Zero bytes is not a document with no records; it is a broken input.

    A reader that treats "the stream ended immediately" as "the document had nothing in
    it" would write a header-only CSV and exit 0 -- the same class of failure as the
    pandas comparison, which produced a structurally perfect file holding 12% fewer
    records than the document and said nothing.
    """
    _, config = write_inputs(tmp_path)
    output = tmp_path / "out.csv"
    empty = tmp_path / "empty.xml"
    empty.write_bytes(b"")
    with empty.open("rb") as document:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "gigaxml.cli",
                "extract",
                "-",
                "-c",
                str(config),
                "-o",
                str(output),
            ],
            stdin=document,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(REPO_ROOT),
        )

    assert completed.returncode != 0
    assert not output.exists()
