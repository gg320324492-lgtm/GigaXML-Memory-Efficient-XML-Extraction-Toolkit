"""Golden tests: the six command-line behaviours Stage 3 must not change.

Each case runs the CLI as a real subprocess and compares what a user could observe --
the exit code, one stream, and any artefact on disk -- against a file under
``expected/``. What a case asserts is decided by what a user's script could depend on,
not by what is convenient to check.

If one of these fails after a refactor, the refactor changed something observable. That
is either a bug or a deliberate decision, and the second one wants a commit message
saying so. ``regenerate.py`` rewrites the expectations for the deliberate case.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.golden.conftest import normalize, read_golden, run_cli

#: The run report's fields for an ordinary successful extraction, frozen as a set.
#:
#: Written out here rather than kept in a file because *this list is the contract*: a
#: reader should be able to see the whole shape without opening a second artefact, and a
#: diff against it is a reviewable answer to "what did that change".
#:
#: Three fields are **conditional** and are not in this set. ``checkpoint`` appears only
#: for a ``--checkpoint-every`` run. ``input_identity_error`` and
#: ``output_identity_error`` appear only when the corresponding file could not be hashed,
#: which is how a failed run usually reads. A run that adds one of them is behaving; a
#: run that drops one of these twenty-three has broken a caller.
RUN_REPORT_FIELDS = frozenset(
    {
        "config_hash",
        "elapsed_seconds",
        "environment",
        "error",
        "fields",
        "finished_at",
        "format",
        "input_identity",
        "output",
        "output_complete",
        "output_identity",
        "partial_path",
        "peak_rss_mb",
        "record_path",
        "records",
        "rejected",
        "rejected_path",
        "rows",
        "source",
        "started_at",
        "status",
        "throughput_records_per_s",
        "tool_version",
    }
)


def test_inspect_text_output_is_frozen(workdir: Path) -> None:
    """The human-readable report, byte for byte.

    This is the output a user reads to decide which path holds their records, so its
    wording is behaviour: the score, the ordering, and the sentence explaining what
    ``[nested inside]`` means are all things somebody has learned to read.
    """
    result = run_cli("inspect", str(workdir / "src.xml"), cwd=workdir)

    expected_code, expected_stream, expected_body = read_golden("inspect_basic.txt")
    assert result.returncode == expected_code
    assert expected_stream == "stdout"
    assert normalize(result.stdout, tmp_path=workdir) == expected_body
    assert result.stderr == ""


def test_inspect_json_for_a_namespaced_document_is_frozen(workdir: Path) -> None:
    """The machine-readable report for a document with a default namespace.

    Frozen whole, namespaces included: ``--json`` is the input to other tools, and a
    caller that resolves prefixes itself is depending on which prefix maps to which URI.
    """
    result = run_cli("inspect", str(workdir / "ns.xml"), "--json", cwd=workdir)

    expected_code, expected_stream, expected_body = read_golden("inspect_namespace.json")
    assert result.returncode == expected_code
    assert expected_stream == "stdout"
    assert normalize(result.stdout, tmp_path=workdir) == expected_body
    assert result.stderr == ""


def test_a_successful_extraction_prints_the_same_summary_and_writes_the_same_fields(
    workdir: Path,
) -> None:
    """The summary on stdout, and the run report's field set beside the output.

    The summary is frozen as bytes because it is what a shell script parses. The report's
    *field set* is asserted rather than its bytes on purpose: the report records the
    machine it ran on -- interpreter version, OS release, peak memory -- so freezing its
    bytes would freeze this computer into the repository and fail everywhere else. The
    names are the contract; the measurements inside them are not.
    """
    output = workdir / "out.csv"
    result = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-o",
        str(output),
        cwd=workdir,
    )

    expected_code, expected_stream, expected_body = read_golden("extract_success.json")
    assert result.returncode == expected_code
    assert expected_stream == "stdout"
    assert normalize(result.stdout, tmp_path=workdir) == expected_body
    assert result.stderr == ""

    report = json.loads((workdir / "run-report.json").read_text(encoding="utf-8"))
    assert set(report) == RUN_REPORT_FIELDS

    # A successful run reports itself as successful and complete. Two fields, because a
    # caller has to be able to ask the question without parsing stderr.
    assert report["status"] == "ok"
    assert report["output_complete"] is True
    assert report["error"] is None


def test_an_unmatched_record_path_exits_one_and_explains_the_matching_rule(workdir: Path) -> None:
    """The refusal path: exit code 1, and an error that says what matching means.

    Frozen whole, because this message is the tool teaching its own semantics. A user who
    hit it once knows that ``/`` is root-anchored and ``//`` is any-ancestor, and that
    ``namespaces=`` exists. Rewording it is a documentation change and needs to be
    deliberate.
    """
    result = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "unmatched.yaml"),
        "-o",
        str(workdir / "out.csv"),
        cwd=workdir,
    )

    expected_code, expected_stream, expected_body = read_golden("extract_failure.txt")
    assert result.returncode == expected_code
    assert expected_code == 1
    assert expected_stream == "stderr"
    assert normalize(result.stderr, tmp_path=workdir) == expected_body
    assert result.stdout == ""


def test_a_sample_is_written_byte_for_byte(workdir: Path) -> None:
    """The sample artefact itself, compared as bytes.

    The artefact rather than the summary, because a sample exists to be looked at: the
    row order, the decimal formatting and the line endings are all of it. ``\\r\\n`` is
    what the csv module writes on every platform, so it is part of the frozen bytes
    rather than something to normalise away.
    """
    output = workdir / "sample.csv"
    result = run_cli(
        "sample",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-n",
        "2",
        "-o",
        str(output),
        cwd=workdir,
    )

    expected_code, expected_stream, expected_body = read_golden("sample.csv")
    assert result.returncode == expected_code
    assert expected_code == 0
    assert expected_stream == "artifact"
    assert result.stdout != ""
    assert output.read_bytes().decode("utf-8") == expected_body


def test_resume_refuses_a_manifest_this_build_cannot_read(workdir: Path) -> None:
    """A manifest from another format version is refused, and nothing is written.

    The refusal happens before the run does any work, which is the point worth freezing:
    a resume that cannot trust its manifest must not start, and must not overwrite the
    report belonging to the run that *did* finish.
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
        cwd=workdir,
    )
    assert first.returncode == 0

    manifest_path = parts / "checkpoint.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 99
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    report_before = (parts / "run-report.json").read_bytes()
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
        cwd=workdir,
    )

    expected_code, expected_stream, expected_body = read_golden("resume_error.txt")
    assert result.returncode == expected_code
    assert expected_code == 1
    assert expected_stream == "stderr"
    assert normalize(result.stderr, tmp_path=workdir) == expected_body
    assert result.stdout == ""
    # A run that refused before doing anything has not earned the right to replace the
    # record of the run that finished.
    assert (parts / "run-report.json").read_bytes() == report_before
