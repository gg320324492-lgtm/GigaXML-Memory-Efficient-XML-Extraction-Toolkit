"""Integration tests: ``--checkpoint-every`` and ``--resume``.

The two that matter most are here: a resumed run must produce exactly what a single
uninterrupted run would have, and a resume against a different source or config must
be refused rather than produce a plausible-looking wrong answer.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gigaxml.checkpoint import CHECKPOINT_FILENAME, Checkpoint
from gigaxml.cli import main
from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME

DOC = (
    '<?xml version="1.0"?><root>'
    + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, 13))
    + "</root>"
)
FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}


def write_config(tmp_path: Path, *, name: str = "config.yaml", fields: dict | None = None) -> Path:
    path = tmp_path / name
    path.write_text(
        json.dumps({"record": "/root/item", "fields": fields or FIELDS}), encoding="utf-8"
    )
    return path


def write_source(tmp_path: Path, text: str = DOC, *, name: str = "source.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def data_bytes(parts_dir: Path) -> dict[str, bytes]:
    """Every file in the parts directory except the run summary.

    The summary is excluded on purpose. It describes **this** run, so two runs over
    the same finished checkpoint legitimately differ in ``resumed_from``,
    ``rows_this_run`` and ``elapsed_seconds`` -- an earlier version of this test
    compared every file and therefore could never pass. The parts and the manifest
    are the data; those are what must not move.
    """
    return {
        path.name: path.read_bytes()
        for path in parts_dir.iterdir()
        if path.name != DEFAULT_RUN_REPORT_FILENAME
    }


def read_manifest(parts_dir: Path) -> dict:
    return json.loads((parts_dir / CHECKPOINT_FILENAME).read_text(encoding="utf-8"))


def part_names(parts_dir: Path, extension: str) -> list[str]:
    return sorted(path.name for path in parts_dir.glob(f"part-*.{extension}"))


def concat(parts_dir: Path, extension: str) -> list[str]:
    lines: list[str] = []
    for path in sorted(parts_dir.glob(f"part-*.{extension}")):
        lines.extend(path.read_text(encoding="utf-8").splitlines())
    return lines


# --- Gate 3: a checkpointed run --------------------------------------------


def test_a_checkpointed_run_commits_parts_and_a_manifest(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "5",
            "--format",
            "csv",
        ]
    )

    assert exit_code == 0
    assert part_names(parts, "csv") == [
        "part-00000.csv",
        "part-00001.csv",
        "part-00002.csv",
    ]
    manifest = read_manifest(parts)
    assert manifest["version"] == 1
    assert manifest["complete"] is True
    assert [part["rows"] for part in manifest["parts"]] == [5, 5, 2]
    assert manifest["records_consumed"] == 12
    assert sum(part["rows"] for part in manifest["parts"]) + manifest["rejected"] == 12
    assert not list(parts.glob("*.tmp")), "no partial part is left behind"
    assert (parts / "run-report.json").is_file(), "the summary lives with the parts"
    assert not (tmp_path / "run-report.json").exists()


def test_the_parts_concatenate_to_exactly_the_single_file_output(tmp_path: Path) -> None:
    """The property that makes parts useful: cat them in order and you are done."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "one.csv")]) == 0
    single = (tmp_path / "one.csv").read_text(encoding="utf-8").splitlines()

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
                "5",
                "--format",
                "csv",
            ]
        )
        == 0
    )

    assert concat(parts, "csv") == single


def test_only_the_first_csv_part_carries_the_header(tmp_path: Path) -> None:
    """A header in every part would put header rows in the middle of a concatenation."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
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
                "5",
                "--format",
                "csv",
            ]
        )
        == 0
    )

    assert (parts / "part-00000.csv").read_text(encoding="utf-8").startswith("id,name")
    for name in ("part-00001.csv", "part-00002.csv"):
        assert not (parts / name).read_text(encoding="utf-8").startswith("id,name")


@pytest.mark.parametrize("extension", ["csv", "jsonl", "parquet"])
def test_every_format_can_be_checkpointed(tmp_path: Path, extension: str) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / f"parts-{extension}"

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
                "5",
                "--format",
                extension,
            ]
        )
        == 0
    )

    assert len(part_names(parts, extension)) == 3
    assert not list(parts.glob("*.tmp"))

    if extension == "parquet":
        import pyarrow.parquet as parquet

        rows = sum(
            parquet.read_table(path).num_rows for path in sorted(parts.glob("part-*.parquet"))
        )
        assert rows == 12


def test_parquet_is_the_default_part_format(tmp_path: Path) -> None:
    """A directory has no extension to infer from, so there is a default."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
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
                "5",
            ]
        )
        == 0
    )

    assert part_names(parts, "parquet")
    assert read_manifest(parts)["complete"] is True


# --- Gate 13: the default path is untouched --------------------------------


def test_without_the_flag_nothing_changes(tmp_path: Path) -> None:
    """No parts directory, no manifest, and a summary with the same shape as before."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert output.is_file()
    assert not (tmp_path / "parts").exists()
    assert not list(tmp_path.glob("*.tmp"))

    report = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
    assert "checkpoint" not in report, "the checkpoint block appears only when used"
    assert set(report) == {
        "status",
        "source",
        "output",
        "format",
        "record_path",
        "fields",
        "rows",
        "rejected",
        "rejected_path",
        "error",
        "output_complete",
        "partial_path",
        "elapsed_seconds",
        "tool_version",
    }


def test_the_output_bytes_match_a_5b1_run(tmp_path: Path) -> None:
    """Same input, same bytes -- the flag changes nothing about the output itself."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    first = tmp_path / "a.csv"
    second = tmp_path / "b.jsonl"

    assert main(["extract", str(source), "-c", str(config), "-o", str(first)]) == 0
    assert main(["extract", str(source), "-c", str(config), "-o", str(second)]) == 0

    assert first.read_text(encoding="utf-8").splitlines() == [
        "id,name",
        *[f"{n},N{n}" for n in range(1, 13)],
    ]
    assert len(second.read_text(encoding="utf-8").splitlines()) == 12


# --- Gate 6: refusal --------------------------------------------------------


def test_resuming_with_a_changed_config_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
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
                "100",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    before = sorted(path.name for path in parts.iterdir())

    other = write_config(tmp_path, name="other.yaml", fields={"id": {"path": "id"}})
    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(other),
            "-o",
            str(parts),
            "--checkpoint-every",
            "100",
            "--format",
            "csv",
            "--resume",
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "cannot resume" in err
    assert "config:" in err
    assert "Nothing was written" in err
    assert sorted(path.name for path in parts.iterdir()) == before


def test_resuming_with_a_changed_source_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
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
                "100",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    before = sorted(path.name for path in parts.iterdir())

    changed = write_source(tmp_path, DOC.replace("N1", "CHANGED"), name="changed.xml")
    exit_code = main(
        [
            "extract",
            str(changed),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "100",
            "--format",
            "csv",
            "--resume",
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "source content" in err
    assert "sha256" in err
    assert sorted(path.name for path in parts.iterdir()) == before


def test_a_fresh_run_over_an_existing_checkpoint_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Writing over committed parts would throw away work that was already done."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0

    assert main(args) == 1
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "--resume" in err


# --- Gate 7: an already complete checkpoint --------------------------------


def test_resuming_a_complete_checkpoint_does_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    before = data_bytes(parts)

    assert main([*args, "--resume"]) == 0

    assert "already complete" in capsys.readouterr().err
    assert data_bytes(parts) == before, "the parts and the manifest are untouched"


# --- Gate 11: edges ---------------------------------------------------------


def test_a_corrupt_manifest_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "100",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    (parts / CHECKPOINT_FILENAME).write_text("{ not json", encoding="utf-8")

    assert main([*args, "--resume"]) == 1
    assert "could not be read" in capsys.readouterr().err


def test_a_manifest_with_a_missing_key_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "100",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    payload = read_manifest(parts)
    del payload["parts"]
    (parts / CHECKPOINT_FILENAME).write_text(json.dumps(payload), encoding="utf-8")

    assert main([*args, "--resume"]) == 1
    assert "missing" in capsys.readouterr().err


def test_resume_without_a_manifest_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "empty"

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "5",
            "--format",
            "csv",
            "--resume",
        ]
    )

    assert exit_code == 1
    assert "nothing to resume" in capsys.readouterr().err


def test_resume_without_checkpoint_every_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)

    exit_code = main(
        ["extract", str(source), "-c", str(config), "-o", str(tmp_path / "p"), "--resume"]
    )

    assert exit_code == 1
    assert "--checkpoint-every" in capsys.readouterr().err


@pytest.mark.parametrize("bad", ["0", "-1"])
def test_a_non_positive_part_size_is_refused(tmp_path: Path, bad: str) -> None:
    """argparse rejects it before the run starts, the same way it rejects ``sample -n``."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)

    with pytest.raises(SystemExit) as info:
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(tmp_path / "p"),
                "--checkpoint-every",
                bad,
                "--format",
                "csv",
            ]
        )

    assert info.value.code == 2


def test_a_file_where_the_parts_directory_should_be_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    existing = tmp_path / "already-a-file.csv"
    existing.write_text("id,name\n", encoding="utf-8")

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(existing),
            "--checkpoint-every",
            "5",
            "--format",
            "csv",
        ]
    )

    assert exit_code == 1
    assert "existing file" in capsys.readouterr().err
    assert existing.read_text(encoding="utf-8") == "id,name\n"


# --- sample does not support it --------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [["--checkpoint-every", "5"], ["--resume"]],
)
def test_sample_refuses_the_checkpoint_flags(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)

    exit_code = main(
        ["sample", str(source), "-c", str(config), "-n", "2", "-o", str(tmp_path / "s.csv"), *extra]
    )

    assert exit_code == 1
    assert "sample does not support" in capsys.readouterr().err


# --- Gate 9: the rejection log is appended on resume -----------------------


def test_the_rejection_log_is_appended_not_truncated(tmp_path: Path) -> None:
    """Rejections from before the interruption are still real."""
    body = (
        (
            '<?xml version="1.0"?><root>'
            + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, 13))
            + "</root>"
        )
        .replace("<id>3</id>", "<id>bad</id>")
        .replace("<id>9</id>", "<id>worse</id>")
    )
    source = write_source(tmp_path, body)
    config = write_config(tmp_path)
    config.write_text(
        json.dumps({"record": "/root/item", "on_error": "quarantine", "fields": FIELDS}),
        encoding="utf-8",
    )
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
                "5",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    first = (parts / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(first) == 2

    # Resume with the same source and config: the run is complete, so nothing is
    # rewritten -- but the log must still hold both rejections.
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
                "5",
                "--format",
                "csv",
                "--resume",
            ]
        )
        == 0
    )
    assert (parts / "rejected.jsonl").read_text(encoding="utf-8").splitlines() == first


# --- Gates 4 and 5: interruption, then resume ------------------------------


def test_a_killed_run_keeps_committed_parts_and_resumes_to_the_same_result(
    s100_path: Path, tmp_path: Path
) -> None:
    """The headline claim, tested the way it will actually be used.

    A 100MB run is started, killed after two parts are committed, and then resumed.
    The concatenated result must equal what a single uninterrupted run produces.
    """
    script = Path(sys.executable).parent / ("gigaxml.exe" if sys.platform == "win32" else "gigaxml")
    if not script.exists():  # pragma: no cover - depends on the environment
        pytest.skip(f"console script not installed at {script}")

    config = tmp_path / "big.yaml"
    config.write_text(
        json.dumps(
            {
                "record": "/catalog/products/product",
                "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
            }
        ),
        encoding="utf-8",
    )
    whole = tmp_path / "whole.csv"
    parts = tmp_path / "parts"
    every = "20000"

    completed = subprocess.run(
        [str(script), "extract", str(s100_path), "-c", str(config), "-o", str(whole)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    expected = whole.read_text(encoding="utf-8").splitlines()

    process = subprocess.Popen(
        [
            str(script),
            "extract",
            str(s100_path),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            every,
            "--format",
            "csv",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if len(part_names(parts, "csv")) >= 2:
                break
            time.sleep(0.05)
        assert len(part_names(parts, "csv")) >= 2, "the run never committed two parts"
        assert process.poll() is None, "the run finished before it could be killed"
        time.sleep(0.3)
    finally:
        process.kill()
        process.wait()

    manifest = read_manifest(parts)
    assert manifest["complete"] is False, "it was killed, so it cannot be complete"
    assert manifest["records_consumed"] > 0

    # What the design guarantees, in both directions.
    #
    # A part is renamed into place **before** the manifest that names it is written, and
    # that order is deliberate. The other way round the manifest would list a part that
    # does not exist yet, and verify_parts -- which checks that every part the manifest
    # names is present with the row count it claims -- would refuse the resume, leaving a
    # run that cannot be continued at all. The cost of the order chosen is that a kill
    # inside the window between the two leaves one part on disk the manifest does not
    # mention. That side is safe: verify_parts only ever checks manifest -> disk, so the
    # extra part is ignored, and a resume starts at part-{len(parts)} and overwrites it
    # with the same bytes. So the guarantee is "the manifest is never ahead", not "the
    # two are equal" -- and asserting equality made this test fail whenever the kill
    # happened to land in that window.
    from gigaxml.checkpoint import verify_parts

    committed = part_names(parts, "csv")
    listed = [part["name"] for part in manifest["parts"]]

    assert verify_parts(_as_checkpoint(manifest), parts, "csv") == [], (
        "the manifest must never name a part that is missing or a different size"
    )
    assert 0 <= len(committed) - len(listed) <= 1, (
        f"disk holds {len(committed)} parts and the manifest lists {len(listed)}: the "
        f"manifest must never be ahead of the disk, and the disk can only lead by one"
    )
    for name in committed:
        assert (parts / name).is_file(), "every committed part is readable"

    # Resume.
    resumed = subprocess.run(
        [
            str(script),
            "extract",
            str(s100_path),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            every,
            "--format",
            "csv",
            "--resume",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr

    final = read_manifest(parts)
    assert final["complete"] is True
    assert final["records_consumed"] == 291_200
    assert sum(part["rows"] for part in final["parts"]) == 291_200

    combined = concat(parts, "csv")
    assert len(combined) == len(expected), "the same number of lines as one uninterrupted run"
    assert combined == expected, "and the same lines, in the same order"


# --- the summary reports the format the parts are really in ----------------


def test_the_summary_reports_the_real_part_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The parts on disk decide the format, so the summary must say what they are.

    A resume given a different ``--format`` correctly keeps writing the format the
    checkpoint was started in -- but the summary used to echo the *request*, so a
    downstream tool reading ``format: parquet`` would point a Parquet reader at CSV
    files. Reporting the request instead of the fact is the same class of mistake as
    a manifest that says a run is complete when it is not.
    """
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0

    assert main([*args, "--resume"]) == 0
    capsys.readouterr()

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "5",
            "--format",
            "parquet",
            "--resume",
        ]
    )

    assert exit_code == 0, "the run itself is fine: the parts are the truth and they win"
    report = json.loads((parts / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    on_disk = sorted({path.suffix.lstrip(".") for path in parts.glob("part-*")})
    assert on_disk == ["csv"], "the checkpoint's format is unchanged"
    assert report["format"] == "csv", "the summary reports the files, not the request"

    err = capsys.readouterr().err
    assert "--format parquet was ignored" in err
    assert "already complete" in err, "and the run still says it had nothing to do"


def test_an_incomplete_resume_with_a_mismatched_format_is_also_reported(
    tmp_path: Path, s100_path: Path
) -> None:
    """The same guarantee on the path that actually writes more parts."""
    import subprocess
    import sys
    import time

    script = Path(sys.executable).parent / ("gigaxml.exe" if sys.platform == "win32" else "gigaxml")
    if not script.exists():  # pragma: no cover - depends on the environment
        pytest.skip(f"console script not installed at {script}")

    config = tmp_path / "big.yaml"
    config.write_text(
        json.dumps(
            {
                "record": "/catalog/products/product",
                "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
            }
        ),
        encoding="utf-8",
    )
    parts = tmp_path / "parts"
    process = subprocess.Popen(
        [
            str(script),
            "extract",
            str(s100_path),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "20000",
            "--format",
            "csv",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if len(part_names(parts, "csv")) >= 2:
                break
            time.sleep(0.05)
        assert len(part_names(parts, "csv")) >= 2, "the run never committed two parts"
    finally:
        process.kill()
        process.wait()

    resumed = subprocess.run(
        [
            str(script),
            "extract",
            str(s100_path),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "20000",
            "--format",
            "parquet",
            "--resume",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert "--format parquet was ignored" in resumed.stderr
    report = json.loads((parts / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["format"] == "csv"
    assert sorted({path.suffix.lstrip(".") for path in parts.glob("part-*")}) == ["csv"]


@pytest.mark.parametrize("extra", [[], ["--format", "csv"]])
def test_no_warning_when_the_format_agrees_or_is_omitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], extra: list[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    capsys.readouterr()

    assert main([*args, "--resume", *extra]) == 0

    err = capsys.readouterr().err
    assert "was ignored" not in err
    report = json.loads((parts / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["format"] == "csv"


def test_a_fresh_run_reports_the_format_it_was_asked_for(tmp_path: Path) -> None:
    """Without a checkpoint there is nothing to be overruled by."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)

    for extension in ("csv", "jsonl"):
        parts = tmp_path / f"parts-{extension}"
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
                    "5",
                    "--format",
                    extension,
                ]
            )
            == 0
        )
        report = json.loads((parts / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
        assert report["format"] == extension
        assert report["checkpoint"]["format"] == extension


# --- a deleted or altered part must not be skipped over ---------------------


def test_a_deleted_part_is_refused_rather_than_skipped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The failure this guards against finishes reporting success.

    ``records_consumed`` is how many records a resume skips. If a part that number
    was derived from is gone, skipping walks straight past rows nobody will ever
    write, and the run exits 0 with an empty stderr -- a silent loss, and not one the
    caller can detect afterwards.
    """
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    manifest_before = read_manifest(parts)

    (parts / "part-00001.csv").unlink()
    exit_code = main([*args, "--resume"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "cannot resume" in err
    assert "part-00001.csv" in err
    assert "missing" in err
    assert "Nothing was written" in err
    assert read_manifest(parts) == manifest_before, "and nothing was rewritten"


def test_a_deleted_part_is_reported_even_when_the_checkpoint_is_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A finished run whose output is incomplete must not be waved through.

    The user is about to use that output, and the manifest still claims the full row
    count, so silence here would be the same silent loss in a different disguise.
    """
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    assert read_manifest(parts)["complete"] is True

    (parts / "part-00001.csv").unlink()
    exit_code = main([*args, "--resume"])

    assert exit_code == 1, "an incomplete output is not reported as merely idle"
    err = capsys.readouterr().err
    assert "part-00001.csv" in err
    assert "missing" in err


def test_a_part_with_the_wrong_row_count_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A part that exists but is not the size the manifest says is just as bad."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0

    part = parts / "part-00001.csv"
    part.write_text(part.read_text(encoding="utf-8") + "99,N99\n", encoding="utf-8")
    exit_code = main([*args, "--resume"])

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "rows differ" in err
    assert "part-00001.csv" in err
    assert "checkpoint 5, file 6" in err


def test_intact_parts_are_not_an_obstacle(tmp_path: Path) -> None:
    """The check must not refuse a healthy checkpoint -- including the completed one."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    assert main([*args, "--resume"]) == 0


@pytest.mark.parametrize("extension", ["csv", "jsonl", "parquet"])
def test_the_part_check_works_for_every_format(tmp_path: Path, extension: str) -> None:
    """Row counting differs by format -- lines for the text ones, metadata for Parquet."""
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    parts = tmp_path / f"parts-{extension}"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        extension,
    ]
    assert main(args) == 0
    assert main([*args, "--resume"]) == 0, "a healthy checkpoint of any format resumes"

    (parts / f"part-00001.{extension}").unlink()
    assert main([*args, "--resume"]) == 1


# --- the manifest and the summary must agree about completion --------------


@pytest.mark.parametrize("count,every", [(12, 3), (12, 4), (12, 6), (12, 12), (12, 5), (13, 5)])
def test_the_manifest_and_the_summary_agree_about_completion(
    tmp_path: Path, count: int, every: int
) -> None:
    """They used to disagree whenever the record count was an exact multiple.

    The source ending exactly on a part boundary meant no part was short, so nothing
    ever recorded that the run had finished: the manifest kept ``complete: false``
    while the summary said true, and every later ``--resume`` re-read the whole
    document to skip all of it, for ever, with no message.
    """
    document = (
        '<?xml version="1.0"?><root>'
        + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, count + 1))
        + "</root>"
    )
    source = write_source(tmp_path, document)
    config = write_config(tmp_path)
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
                str(every),
                "--format",
                "csv",
            ]
        )
        == 0
    )

    manifest = read_manifest(parts)
    report = json.loads((parts / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert manifest["complete"] is report["checkpoint"]["complete"]
    assert manifest["complete"] is True, f"{count} records in parts of {every} is a finished run"
    assert manifest["records_consumed"] == count


@pytest.mark.parametrize("every", [3, 4, 6, 12])
def test_a_divisible_run_resumes_without_going_round_again(
    tmp_path: Path, every: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """With the manifest telling the truth, a resume stops instead of re-reading."""
    document = (
        '<?xml version="1.0"?><root>'
        + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, 13))
        + "</root>"
    )
    source = write_source(tmp_path, document)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        str(every),
        "--format",
        "csv",
    ]
    assert main(args) == 0
    before = data_bytes(parts)

    for _ in range(3):
        assert main([*args, "--resume"]) == 0
        assert "already complete" in capsys.readouterr().err

    assert data_bytes(parts) == before


@pytest.mark.parametrize("exit_kind", ["success", "failure", "killed"])
def test_the_manifest_always_matches_the_disk(
    tmp_path: Path, s100_path: Path, exit_kind: str
) -> None:
    """Whatever way a run ends, what it says about itself matches what it wrote.

    FIX-3 was fixed at one exit point -- the source running out -- and that is exactly
    the kind of fix that leaves a sibling path broken. This walks three genuinely
    different endings and checks the same invariant on each: every part the manifest
    names exists with the number of rows it claims, ``records_consumed`` agrees with
    them, and ``complete`` tells the truth about whether the run finished.

    The failure case really fails: the eighth record cannot be converted and the run
    aborts, so the first part is committed and the second is not.
    """
    import subprocess
    import sys
    import time

    from gigaxml.checkpoint import verify_parts

    parts = tmp_path / "parts"

    if exit_kind == "success":
        document = (
            '<?xml version="1.0"?><root>'
            + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, 13))
            + "</root>"
        )
        source = write_source(tmp_path, document)
        config = write_config(tmp_path)
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
                    "5",
                    "--format",
                    "csv",
                ]
            )
            == 0
        )
    elif exit_kind == "failure":
        document = (
            '<?xml version="1.0"?><root>'
            + "".join(
                f"<item><id>{'bad' if n == 8 else n}</id><name>N{n}</name></item>"
                for n in range(1, 13)
            )
            + "</root>"
        )
        source = write_source(tmp_path, document)
        config = write_config(tmp_path)
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
                    "5",
                    "--format",
                    "csv",
                ]
            )
            == 1
        )
        assert len(part_names(parts, "csv")) == 1, "the first part committed, the second did not"
    else:
        script = Path(sys.executable).parent / (
            "gigaxml.exe" if sys.platform == "win32" else "gigaxml"
        )
        if not script.exists():  # pragma: no cover - depends on the environment
            pytest.skip(f"console script not installed at {script}")
        config = tmp_path / "big.yaml"
        config.write_text(
            json.dumps(
                {
                    "record": "/catalog/products/product",
                    "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
                }
            ),
            encoding="utf-8",
        )
        process = subprocess.Popen(
            [
                str(script),
                "extract",
                str(s100_path),
                "-c",
                str(config),
                "-o",
                str(parts),
                "--checkpoint-every",
                "20000",
                "--format",
                "csv",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if len(part_names(parts, "csv")) >= 3:
                    break
                time.sleep(0.05)
            assert len(part_names(parts, "csv")) >= 3
            assert process.poll() is None
        finally:
            process.kill()
            process.wait()

    manifest = read_manifest(parts)
    extension = manifest["parts"][-1]["name"].rsplit(".", 1)[-1] if manifest["parts"] else "csv"

    assert verify_parts(_as_checkpoint(manifest), parts, extension) == [], (
        "every part the manifest names is on disk with the row count it claims"
    )

    total = sum(part["rows"] for part in manifest["parts"])
    assert manifest["records_consumed"] == total + manifest["rejected"], (
        "records_consumed counts what was written plus what was rejected"
    )
    assert manifest["complete"] is (exit_kind == "success"), (
        "only a run that reached the end of the source says it is complete"
    )


def _as_checkpoint(manifest: dict) -> Checkpoint:
    from gigaxml.checkpoint import PartRecord

    return Checkpoint(
        source=manifest["source"],
        config=manifest["config"],
        records_consumed=manifest["records_consumed"],
        rejected=manifest["rejected"],
        parts=tuple(PartRecord(name=p["name"], rows=p["rows"]) for p in manifest["parts"]),
        complete=manifest["complete"],
    )


# --- the ordering that makes an interrupted run recoverable -----------------


def test_a_part_on_disk_that_the_manifest_does_not_name_is_recoverable(
    tmp_path: Path,
) -> None:
    """The window between a part landing and the manifest naming it, built directly.

    Reproducing it by killing at the right moment is a coin flip -- the window is a few
    milliseconds wide -- so the state is constructed instead. This is the state the
    ordering in the loop can produce, and the point of the test is that a resume handles
    it: the extra part is ignored, the run continues, and the result is the same as if
    the kill had landed a moment later.
    """
    document = (
        '<?xml version="1.0"?><root>'
        + "".join(f"<item><id>{n}</id><name>N{n}</name></item>" for n in range(1, 13))
        + "</root>"
    )
    source = write_source(tmp_path, document)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"
    args = [
        "extract",
        str(source),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--checkpoint-every",
        "5",
        "--format",
        "csv",
    ]
    assert main(args) == 0
    whole = concat(parts, "csv")

    # Rewind the manifest by one part, leaving the part itself on disk. This is exactly
    # what a kill between the rename and the manifest write leaves behind.
    manifest = read_manifest(parts)
    dropped = manifest["parts"].pop()
    manifest["records_consumed"] -= dropped["rows"]
    manifest["complete"] = False
    (parts / CHECKPOINT_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    assert (parts / dropped["name"]).is_file(), "the part is still there, unnamed"

    from gigaxml.checkpoint import verify_parts

    assert verify_parts(_as_checkpoint(manifest), parts, "csv") == [], (
        "the extra part is not a problem: the check only looks manifest -> disk"
    )

    assert main([*args, "--resume"]) == 0

    final = read_manifest(parts)
    assert final["complete"] is True
    assert final["records_consumed"] == 12
    assert concat(parts, "csv") == whole, "and the result is what one pass would have written"
