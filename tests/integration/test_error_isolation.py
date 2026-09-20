"""Integration tests: ``on_error``, the rejection log, and the run report.

The subject here is what a run leaves behind when it does not go perfectly. The
default (``abort``) leaves a half-written output that used to be indistinguishable
from a complete one, and the whole point of the run report is that it no longer is.
So the assertions are about files on disk and exit codes, not about return values.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import yaml

from gigaxml.cli import main

GOOD_AND_BAD = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>notanint</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
  <item><id>alsobad</id><name>D</name></item>
  <item><id>5</id><name>E</name></item>
</root>
"""

ALL_GOOD = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>2</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
</root>
"""

FIELDS = {
    "id": {"path": "id", "type": "int"},
    "name": {"path": "name"},
}


def write_config(
    tmp_path: Path, *, on_error: str | None = None, fields: dict | None = None
) -> Path:
    payload: dict[str, object] = {
        "record": "/root/item",
        "fields": fields if fields is not None else FIELDS,
    }
    if on_error is not None:
        payload["on_error"] = on_error
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def write_source(tmp_path: Path, text: str = GOOD_AND_BAD) -> Path:
    path = tmp_path / "source.xml"
    path.write_text(text, encoding="utf-8")
    return path


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rows_in(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()[1:]


# --- Gate 3: quarantine finishes the run ------------------------------------


def test_quarantine_writes_the_good_records_and_the_rejections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """3 good + 2 bad: exit 0, 3 rows out, 2 lines in the rejection log."""
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert rows_in(output) == ["1,A", "3,C", "5,E"]

    rejected = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rejected) == 2

    report = read_report(tmp_path / "run-report.json")
    assert report["status"] == "ok"
    assert report["rows"] == 3
    assert report["rejected"] == 2
    assert report["rejected_path"].endswith("rejected.jsonl")
    assert report["error"] is None
    assert report["output_complete"] is True

    err = capsys.readouterr().err
    assert err.count("warning:") == 1, "one summary line, not one per rejected record"
    assert "2 record(s) rejected" in err


def test_the_rejection_log_names_the_records_that_were_dropped(tmp_path: Path) -> None:
    """Gate 5: every line is valid JSON, and ``index`` is the real record ordinal."""
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.csv"
    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    lines = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    entries = [json.loads(line) for line in lines]

    assert [entry["index"] for entry in entries] == [2, 4]
    for entry in entries:
        assert entry["record_path"] == "/root/item"
        assert entry["error"] == "FieldTypeError"
        assert "not convertible" in entry["message"]
    assert "notanint" in entries[0]["message"]
    assert "alsobad" in entries[1]["message"]

    # The errors already know which field failed and what its text was, so the log
    # carries them rather than making a reader parse the message.
    assert [entry["field"] for entry in entries] == ["id", "id"]
    assert [entry["raw"] for entry in entries] == ["notanint", "alsobad"]

    # The errors already know which field failed and what its text was, so the log
    # carries them rather than making a reader parse the message.
    assert [entry["field"] for entry in entries] == ["id", "id"]
    assert [entry["raw"] for entry in entries] == ["notanint", "alsobad"]


def test_quarantine_handles_a_missing_required_field_too(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    config = write_config(
        tmp_path,
        on_error="quarantine",
        # `id` stays a string so the only thing wrong with every record is the
        # absent required field -- otherwise the first two fail on the int cast
        # first and the log mixes two error types.
        fields={"id": {"path": "id"}, "nope": {"path": "nope", "required": True}},
    )
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    entries = [
        json.loads(line)
        for line in (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(entries) == 5, "every record is missing 'nope'"
    assert {entry["error"] for entry in entries} == {"MissingRequiredFieldError"}
    assert read_report(tmp_path / "run-report.json")["rows"] == 0


def test_a_clean_run_leaves_no_rejection_log(tmp_path: Path) -> None:
    """An empty ``rejected.jsonl`` would suggest something was rejected when nothing was."""
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert not (tmp_path / "rejected.jsonl").exists()
    report = read_report(tmp_path / "run-report.json")
    assert report["rejected"] == 0
    assert report["rejected_path"] is None


# --- Gate 4: the default is unchanged, and now says so ----------------------


def test_abort_keeps_its_exit_code_and_its_one_line_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)  # no on_error -> abort
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1

    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "not convertible" in err
    assert "warning:" not in err


def test_abort_does_not_create_the_target_at_all(tmp_path: Path) -> None:
    """This used to assert the opposite, and the change is deliberate.

    Until output was written atomically, an aborted run left a truncated file at the
    target path that was byte-identical to what a shorter successful run would have
    produced. Now the target is only ever created by a run that finished, so a first
    run that fails leaves no output file at all.
    """
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1

    assert not output.exists(), "a failed first run leaves no output file"


def test_abort_keeps_the_partial_output_beside_the_target(tmp_path: Path) -> None:
    """The old test's real intent, which survives: the work is not thrown away.

    It used to be at the target path, which made it indistinguishable from a finished
    file. It is now beside it, under a name that says what it is, and the report names
    that file so a caller does not have to guess.
    """
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1

    partial = tmp_path / "out.csv.tmp"
    assert partial.exists(), "the rows written before the failure are still there"
    assert rows_in(partial) == ["1,A"]

    report = read_report(tmp_path / "run-report.json")
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["partial_path"] == str(partial)
    assert report["rows"] == 1
    assert report["error"]["type"] == "FieldTypeError"
    assert "not convertible" in report["error"]["message"]
    assert report["rejected"] == 0
    assert report["rejected_path"] is None


def test_an_explicit_abort_behaves_like_the_default(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="abort")
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1
    assert read_report(tmp_path / "run-report.json")["output_complete"] is False


# --- Gate 6: the report on both paths --------------------------------------


@pytest.mark.parametrize(
    "on_error,expected_exit,expected_status",
    [("quarantine", 0, "ok"), ("abort", 1, "failed")],
)
def test_the_report_lands_on_both_paths_with_every_field(
    tmp_path: Path, on_error: str, expected_exit: int, expected_status: str
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error=on_error)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == expected_exit

    report = read_report(tmp_path / "run-report.json")
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
    assert report["status"] == expected_status
    assert report["output_complete"] is (expected_status == "ok")
    assert report["record_path"] == "/root/item"
    assert report["fields"] == ["id", "name"]
    assert report["format"] == "csv"
    assert report["tool_version"]
    assert isinstance(report["elapsed_seconds"], float)


def test_the_report_goes_beside_the_output_by_default(tmp_path: Path) -> None:
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path)
    nested = tmp_path / "out"
    nested.mkdir()
    output = nested / "rows.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
    assert (nested / "run-report.json").exists()


def test_report_overrides_where_the_report_goes(tmp_path: Path) -> None:
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    elsewhere = tmp_path / "reports" / "r.json"
    elsewhere.parent.mkdir()

    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--report",
                str(elsewhere),
            ]
        )
        == 0
    )
    assert elsewhere.exists()
    assert not (tmp_path / "run-report.json").exists()


def test_the_stdout_json_is_unchanged_by_the_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` on stdout keeps its old shape; the report is an extra file."""
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    printed = json.loads(capsys.readouterr().out)
    assert set(printed) == {"source", "output", "format", "record_path", "fields", "rows"}
    assert printed["rows"] == 3


# --- Gate 1.5: sample honours the same policy ------------------------------


def test_sample_quarantines_the_same_way_extract_does(tmp_path: Path) -> None:
    """A config that says quarantine and a sample that still dies would be a trap."""
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "sample.jsonl"

    assert main(["sample", str(source), "-c", str(config), "-n", "5", "-o", str(output)]) == 0

    assert len(output.read_text(encoding="utf-8").splitlines()) == 3
    entries = (tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(entries) == 2
    report = read_report(tmp_path / "run-report.json")
    assert report["status"] == "ok"
    assert report["rejected"] == 2


def test_sample_aborts_the_same_way_extract_does(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path)
    output = tmp_path / "sample.jsonl"

    assert main(["sample", str(source), "-c", str(config), "-n", "5", "-o", str(output)]) == 1

    report = read_report(tmp_path / "run-report.json")
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["rows"] == 1


# --- Gate 9: config validation ---------------------------------------------


@pytest.mark.parametrize("bad", ["skip", "ignore", "quarantine!", "true"])
def test_an_unsupported_on_error_value_is_rejected(tmp_path: Path, bad: str) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error=bad)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1


def test_the_on_error_error_message_names_the_supported_values(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="skip")

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "o.csv")]) == 1

    err = capsys.readouterr().err
    assert "on_error" in err
    assert "'skip'" in err
    assert "abort" in err and "quarantine" in err


def test_on_error_is_case_insensitive(tmp_path: Path) -> None:
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="QUARANTINE")
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
    assert rows_in(output) == ["1,A", "3,C", "5,E"]


def test_a_misspelled_top_level_key_still_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path, on_error="abort")
    config.write_text(
        config.read_text(encoding="utf-8").replace("on_error", "on_eror"), encoding="utf-8"
    )

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "o.csv")]) == 1
    assert "on_eror" in capsys.readouterr().err


# --- Gate 10: edges ---------------------------------------------------------


def test_a_rejection_log_that_cannot_be_written_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The log's directory is the output's, so a read-only one blocks it."""
    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="quarantine")
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "rejected.jsonl").mkdir()  # a directory where the file must go

    exit_code = main(["extract", str(source), "-c", str(config), "-o", str(blocked / "out.csv")])
    assert exit_code == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_a_report_that_cannot_be_written_does_not_fail_the_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """This used to assert exit code 1, and the change is deliberate.

    The exit code reports whether the *data* is usable. The output is the product and
    the summary is a side artefact, so a run whose data landed is a success even if
    the summary could not be written -- it says so on stderr and carries on.
    """
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(output),
            "--report",
            str(tmp_path / "nowhere" / "r.json"),
        ]
    )

    assert exit_code == 0, "the data is complete, so the run succeeded"
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "nowhere" in err
    assert rows_in(output) == ["1,A", "2,B", "3,C"]
    assert not (tmp_path / "run-report.json").exists()


def test_quarantine_to_parquet(tmp_path: Path) -> None:
    import pyarrow.parquet as parquet

    source = write_source(tmp_path)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.parquet"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
    assert parquet.read_table(output).num_rows == 3
    assert read_report(tmp_path / "run-report.json")["rejected"] == 2


def test_quarantine_on_a_gzipped_source(tmp_path: Path) -> None:
    source = tmp_path / "source.xml.gz"
    with gzip.open(source, "wt", encoding="utf-8") as handle:
        handle.write(GOOD_AND_BAD)
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
    assert rows_in(output) == ["1,A", "3,C", "5,E"]


def test_a_missing_source_still_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A failure before the first record has to leave a report too."""
    config = write_config(tmp_path, on_error="quarantine")
    output = tmp_path / "out.csv"

    assert main(["extract", str(tmp_path / "nope.xml"), "-c", str(config), "-o", str(output)]) == 1
    assert capsys.readouterr().err.startswith("error: ")

    report = read_report(tmp_path / "run-report.json")
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["error"]["type"] == "FileNotFoundError"


def test_a_large_batch_size_still_warns_alongside_the_report(tmp_path: Path) -> None:
    source = write_source(tmp_path, ALL_GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--batch-size",
                "1000000",
            ]
        )
        == 0
    )
    assert read_report(tmp_path / "run-report.json")["rows"] == 3
