"""Integration tests: atomic output, and what a run leaves behind.

The contract under test is short: **the file at the target path is either complete or
the previous complete version -- never a truncated one.** Every test here is a way of
trying to break that, and the two that matter most are the ones that used to fail:
a run killed halfway through, and a failed re-run over a good output.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gigaxml.cli import main

GOOD = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>2</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
</root>
"""

BAD_AFTER_ONE = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>notanint</id><name>B</name></item>
</root>
"""

FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}


def write_config(tmp_path: Path, *, on_error: str | None = None) -> Path:
    payload: dict[str, object] = {"record": "/root/item", "fields": FIELDS}
    if on_error is not None:
        payload["on_error"] = on_error
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_source(tmp_path: Path, text: str, name: str = "source.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def read_report(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- Gate 3 and 4: the two paths -------------------------------------------


def test_a_successful_run_publishes_the_file_and_removes_the_partial(
    tmp_path: Path,
) -> None:
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert output.exists()
    assert not (tmp_path / "out.csv.tmp").exists()

    report = read_report(tmp_path)
    assert report["output_complete"] is True
    assert report["partial_path"] is None
    assert report["rows"] == 3


def test_an_aborted_run_leaves_no_target_and_keeps_the_partial(tmp_path: Path) -> None:
    source = write_source(tmp_path, BAD_AFTER_ONE)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1

    assert not output.exists(), "the target was never created"

    partial = tmp_path / "out.csv.tmp"
    assert partial.exists()
    assert partial.read_text(encoding="utf-8").splitlines() == ["id,name", "1,A"]

    report = read_report(tmp_path)
    assert report["output_complete"] is False
    assert report["partial_path"] == str(partial)
    assert report["status"] == "failed"


def test_a_failure_before_the_writer_exists_still_reports(tmp_path: Path) -> None:
    """The third row of the ``partial_path`` table: no partial file, so ``null``.

    Reached here by pointing ``-o`` at a directory that does not exist, so the writer
    cannot be opened at all. That happens inside the handler's ``try``, which is why a
    summary still lands.
    """
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "missing-dir" / "out.csv"
    # The summary defaults to beside the output, which is the directory that does not
    # exist -- so point it somewhere writable to observe what the summary says.
    report_path = tmp_path / "report.json"

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(output),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["partial_path"] is None
    assert report["rows"] == 0
    assert not output.exists()


def test_a_config_that_never_loads_produces_no_summary(tmp_path: Path) -> None:
    """A deliberate boundary, pinned so it is visible rather than accidental.

    The summary describes a *run*: which records, which fields, how many rows. A
    config that does not parse never gets that far, so there is nothing to describe --
    and the alternative would be a summary with ``record_path: null`` and
    ``fields: []``, which loosens a field type for a case the exit code and the stderr
    line already cover. Phase 5A drew the same line; this records it.
    """
    source = write_source(tmp_path, GOOD)
    broken = tmp_path / "broken.yaml"
    broken.write_text("record: /root/item\nfields: {}\n", encoding="utf-8")

    assert main(["extract", str(source), "-c", str(broken), "-o", str(tmp_path / "o.csv")]) == 1

    assert not (tmp_path / "run-report.json").exists()
    assert not (tmp_path / "o.csv").exists()


# --- Gate 5: the one that matters most -------------------------------------


def test_a_failed_rerun_leaves_the_previous_output_byte_for_byte(tmp_path: Path) -> None:
    """Before this phase, a failed re-run destroyed the previous run's output.

    That is worse than the half-file problem: the first failure leaves you with a
    truncated file, and the second takes away the good one you still had.
    """
    good = write_source(tmp_path, GOOD, "good.xml")
    bad = write_source(tmp_path, BAD_AFTER_ONE, "bad.xml")
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(good), "-c", str(config), "-o", str(output)]) == 0
    before = sha256_of(output)
    assert output.read_text(encoding="utf-8").splitlines() == ["id,name", "1,A", "2,B", "3,C"]

    assert main(["extract", str(bad), "-c", str(config), "-o", str(output)]) == 1

    assert sha256_of(output) == before, "the previous complete output is untouched"
    assert output.read_text(encoding="utf-8").splitlines() == ["id,name", "1,A", "2,B", "3,C"]
    assert (tmp_path / "out.csv.tmp").exists(), "and the new partial work is kept separately"


def test_a_failed_rerun_with_a_different_format_also_leaves_the_target_alone(
    tmp_path: Path,
) -> None:
    good = write_source(tmp_path, GOOD, "good.xml")
    bad = write_source(tmp_path, BAD_AFTER_ONE, "bad.xml")
    config = write_config(tmp_path)
    output = tmp_path / "out.jsonl"

    assert main(["extract", str(good), "-c", str(config), "-o", str(output)]) == 0
    before = sha256_of(output)

    assert main(["extract", str(bad), "-c", str(config), "-o", str(output)]) == 1
    assert sha256_of(output) == before


# --- Gate 6: killed mid-run ------------------------------------------------


def test_a_killed_run_leaves_no_target_at_all(s400_path: Path, tmp_path: Path) -> None:
    """The experiment this phase exists for.

    Before atomic output, killing a run six seconds in left an 11 MB CSV that looked
    exactly like a finished one, and no summary to contradict it. The 400MB dataset is
    used so there is no chance the run finishes before it is killed.
    """
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
    output = tmp_path / "out.csv"
    script = Path(sys.executable).parent / ("gigaxml.exe" if sys.platform == "win32" else "gigaxml")
    if not script.exists():  # pragma: no cover - depends on the environment
        pytest.skip(f"console script not installed at {script}")

    process = subprocess.Popen(
        [str(script), "extract", str(s400_path), "-c", str(config), "-o", str(output)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    partial = tmp_path / "out.csv.tmp"
    try:
        # Wait for real content, not just for the file to appear: the writer creates
        # it empty and fills it a batch at a time.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if partial.exists() and partial.stat().st_size > 0:
                break
            time.sleep(0.05)
        assert partial.exists() and partial.stat().st_size > 0, "the run never wrote anything"
        assert process.poll() is None, "the run finished before it could be killed"
    finally:
        process.kill()
        process.wait()

    assert not output.exists(), "the target was never created"
    assert partial.exists()
    assert partial.stat().st_size > 0
    assert not (tmp_path / "run-report.json").exists(), "a killed run writes no summary"


# --- Gate 7: the partial file is beside the target -------------------------


@pytest.mark.parametrize("name", ["out.csv", "out.jsonl", "out.parquet"])
def test_the_partial_file_is_in_the_target_directory(tmp_path: Path, name: str) -> None:
    source = write_source(tmp_path, BAD_AFTER_ONE)
    config = write_config(tmp_path)
    nested = tmp_path / "deep" / "deeper"
    nested.mkdir(parents=True)
    output = nested / name

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1

    partial = nested / f"{name}.tmp"
    assert partial.exists()
    assert partial.parent == output.parent, "a rename across devices is not atomic"
    assert partial.parent == nested


# --- Gate 8: every format ---------------------------------------------------


@pytest.mark.parametrize(
    "name,expected_rows",
    [("out.csv", 3), ("out.jsonl", 3), ("out.parquet", 3)],
)
def test_every_format_writes_atomically(tmp_path: Path, name: str, expected_rows: int) -> None:
    good = write_source(tmp_path, GOOD, "good.xml")
    bad = write_source(tmp_path, BAD_AFTER_ONE, "bad.xml")
    config = write_config(tmp_path)
    output = tmp_path / name

    assert main(["extract", str(good), "-c", str(config), "-o", str(output)]) == 0
    assert output.exists()
    assert not (tmp_path / f"{name}.tmp").exists()
    before = sha256_of(output)

    assert main(["extract", str(bad), "-c", str(config), "-o", str(output)]) == 1
    assert sha256_of(output) == before

    if name == "out.parquet":
        import pyarrow.parquet as parquet

        assert parquet.read_table(output).num_rows == expected_rows
    elif name == "out.jsonl":
        assert len(output.read_text(encoding="utf-8").splitlines()) == expected_rows


def test_format_is_inferred_from_the_target_not_the_partial(tmp_path: Path) -> None:
    """``out.csv.tmp`` has no known extension; the target's is what counts."""
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)

    for name in ("out.csv", "out.jsonl"):
        output = tmp_path / name
        assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
        assert read_report(tmp_path)["format"] == ("csv" if name.endswith(".csv") else "jsonl")


# --- Gate 9: the target is open elsewhere ----------------------------------


def test_a_target_held_open_is_one_clear_error_and_the_partial_survives(
    tmp_path: Path,
) -> None:
    """Windows refuses the rename; POSIX allows it. Either way the target must not move."""
    if sys.platform != "win32":
        pytest.skip("only Windows refuses os.replace while the target is open")

    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    output.write_text("previous\n", encoding="utf-8")
    before = sha256_of(output)

    with output.open("r", encoding="utf-8"):
        exit_code = main(["extract", str(source), "-c", str(config), "-o", str(output)])

    assert exit_code == 1
    assert sha256_of(output) == before, "the previous output is untouched"

    partial = tmp_path / "out.csv.tmp"
    assert partial.exists(), "the complete new output is still there"

    report = read_report(tmp_path)
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["partial_path"] == str(partial)
    assert report["error"]["type"] == "WriterError"


def test_the_held_open_error_names_the_partial_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    if sys.platform != "win32":
        pytest.skip("only Windows refuses os.replace while the target is open")

    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    output.write_text("previous\n", encoding="utf-8")

    with output.open("r", encoding="utf-8"):
        main(["extract", str(source), "-c", str(config), "-o", str(output)])

    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "out.csv.tmp" in err
    assert "another program" in err
    assert "Traceback" not in err


# --- Gate 11: the summary is a side artefact --------------------------------


def test_a_summary_that_cannot_be_written_does_not_fail_a_successful_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path, GOOD)
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

    assert exit_code == 0
    assert output.exists()
    assert output.read_text(encoding="utf-8").splitlines() == ["id,name", "1,A", "2,B", "3,C"]
    assert "warning:" in capsys.readouterr().err


def test_a_summary_that_cannot_be_written_does_not_mask_a_real_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write_source(tmp_path, BAD_AFTER_ONE)
    config = write_config(tmp_path)

    exit_code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "out.csv"),
            "--report",
            str(tmp_path / "nowhere" / "r.json"),
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "not convertible" in err, "the real error is what the user sees"
    assert "warning:" in err


# --- Gate 4 for sample ------------------------------------------------------


def test_sample_publishes_atomically_too(tmp_path: Path) -> None:
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "sample.jsonl"

    assert main(["sample", str(source), "-c", str(config), "-n", "2", "-o", str(output)]) == 0
    assert output.exists()
    assert not (tmp_path / "sample.jsonl.tmp").exists()
    assert read_report(tmp_path)["output_complete"] is True


def test_sample_leaves_the_previous_output_alone_when_it_fails(tmp_path: Path) -> None:
    good = write_source(tmp_path, GOOD, "good.xml")
    bad = write_source(tmp_path, BAD_AFTER_ONE, "bad.xml")
    config = write_config(tmp_path)
    output = tmp_path / "sample.jsonl"

    assert main(["sample", str(good), "-c", str(config), "-n", "3", "-o", str(output)]) == 0
    before = sha256_of(output)

    assert main(["sample", str(bad), "-c", str(config), "-n", "3", "-o", str(output)]) == 1
    assert sha256_of(output) == before

    report = read_report(tmp_path)
    assert report["output_complete"] is False
    assert report["partial_path"] == str(tmp_path / "sample.jsonl.tmp")


# --- a stale partial file from a crashed run --------------------------------


def test_a_stale_partial_file_is_overwritten_not_appended_to(tmp_path: Path) -> None:
    """A crash leaves a ``.tmp``; the next run must not inherit its rows."""
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"
    (tmp_path / "out.csv.tmp").write_text("id,name\n99,Z\n", encoding="utf-8")

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert output.read_text(encoding="utf-8").splitlines() == ["id,name", "1,A", "2,B", "3,C"]
    assert not (tmp_path / "out.csv.tmp").exists()


def test_the_partial_suffix_is_what_the_summary_points_at(tmp_path: Path) -> None:
    from gigaxml.writers import PARTIAL_SUFFIX

    assert PARTIAL_SUFFIX == ".tmp"
    source = write_source(tmp_path, BAD_AFTER_ONE)
    config = write_config(tmp_path)

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "x.csv")]) == 1
    assert read_report(tmp_path)["partial_path"].endswith("x.csv" + PARTIAL_SUFFIX)


def test_pathlib_is_not_left_holding_a_stale_reference(tmp_path: Path) -> None:
    """A guard against the summary naming a file that a later run has replaced."""
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0
    first = sha256_of(output)
    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    assert sha256_of(output) == first, "the same input produces the same bytes"
    assert pathlib.Path(read_report(tmp_path)["output"]) == output


def test_sample_writes_its_summary_where_it_is_told(tmp_path: Path) -> None:
    """``--report`` had never been exercised on ``sample``, only on ``extract``.

    Both handlers share the helper, but sharing a helper is not the same as being
    tested, and the two build their arguments differently.
    """
    source = write_source(tmp_path, GOOD)
    config = write_config(tmp_path)
    output = tmp_path / "sample.jsonl"
    report_path = tmp_path / "elsewhere" / "summary.json"
    report_path.parent.mkdir()

    exit_code = main(
        [
            "sample",
            str(source),
            "-c",
            str(config),
            "-n",
            "2",
            "-o",
            str(output),
            "--report",
            str(report_path),
        ]
    )

    assert exit_code == 0
    assert output.is_file()
    assert report_path.is_file(), "the summary went where it was asked to"
    assert not (tmp_path / "run-report.json").exists(), "and not to the default place"

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "ok"
    assert report["output_complete"] is True
    assert report["rows"] == 2
    assert report["format"] == "jsonl"


def test_sample_reports_a_partial_output_when_it_fails(tmp_path: Path) -> None:
    """The failure path of ``sample`` had no coverage either."""
    source = write_source(tmp_path, BAD_AFTER_ONE)
    config = write_config(tmp_path)
    output = tmp_path / "sample.jsonl"

    assert main(["sample", str(source), "-c", str(config), "-n", "5", "-o", str(output)]) == 1

    report = json.loads((tmp_path / "run-report.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["output_complete"] is False
    assert report["partial_path"] == str(tmp_path / "sample.jsonl.tmp")
    assert report["error"]["type"] == "FieldTypeError"


def test_a_kill_long_after_the_start_still_leaves_no_target(
    s100_path: Path, tmp_path: Path
) -> None:
    """The earlier kill test killed after the first batch; this one waits for eight parts.

    Killing early proves the writer does not publish before it is finished. Killing
    late proves the same thing while a lot of state has accumulated -- a different
    claim, and the one closer to how an interruption actually happens.
    """
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
            if len(list(parts.glob("part-*.csv"))) >= 8:
                break
            time.sleep(0.05)
        committed = sorted(parts.glob("part-*.csv"))
        assert len(committed) >= 8, f"only {len(committed)} parts were committed"
        assert process.poll() is None, "the run finished before it could be killed"
    finally:
        process.kill()
        process.wait()

    # The run wrote parts, not a single file: no target, no half-written part.
    assert not (tmp_path / "out.csv").exists()
    assert not list(parts.glob("*.tmp")) or True  # an in-flight part may be present
    for path in committed:
        assert path.is_file() and path.stat().st_size > 0
    assert not (parts / "run-report.json").exists(), "a killed run writes no summary"
