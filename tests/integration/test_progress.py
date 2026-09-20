"""``--progress``: the machine-readable stream the GUI reads.

The contract this pins, and why each part of it is not arbitrary:

* **stderr, never stdout.** ``extract`` prints its run summary to stdout, and both a
  caller parsing it and a shell redirecting it depend on that being the only thing there.
* **One JSON object per line.** Warnings are plain text, so a consumer separates them by
  trying to parse; wrapping the warnings would have changed what existing callers see.
* **No total, no percentage.** The tool cannot know how many records a document holds
  without reading it, and a guessed denominator is a wrong number wearing a percentage
  sign. The consumer supplies it -- ``inspect`` reports an exact count.
* **Cumulative, not per-run.** A resumed run's progress bar has to continue rather than
  restart, because the consumer's denominator is the whole document.
* **Off by default, and byte-identical when off.** A caller that never asked for progress
  must not be able to tell it was added.
"""

from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

from gigaxml.cli import main

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
GIGAXML = REPO / ".venv/Scripts/gigaxml.exe"

CONFIG = {
    "record": "/catalog/products/product",
    "fields": {"id": {"path": "@id"}, "name": {"path": "name"}},
}


def write_config(tmp_path: pathlib.Path) -> pathlib.Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(CONFIG), encoding="utf-8")
    return path


def small_dataset(tmp_path: pathlib.Path, records: int) -> pathlib.Path:
    """A document small enough to run in a test and big enough to tick."""
    path = tmp_path / "small.xml"
    body = "".join(f'<product id="{n}"><name>N{n}</name></product>' for n in range(1, records + 1))
    path.write_text(f"<catalog><products>{body}</products></catalog>", encoding="utf-8")
    return path


def run_cli(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run through the console script so stderr is a real pipe, not a captured object."""
    return subprocess.run(
        [str(GIGAXML), *args], capture_output=True, text=True, check=False, cwd=str(REPO)
    )


def progress_lines(stderr: str) -> list[dict]:
    return [json.loads(line) for line in stderr.splitlines() if line.startswith("{")]


# --- the stream itself -------------------------------------------------------


def test_progress_goes_to_stderr_and_never_stdout(tmp_path: pathlib.Path) -> None:
    """Both halves matter: stderr carries it, and stdout stays parseable."""
    source = small_dataset(tmp_path, 3000)
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(output),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    assert completed.returncode == 0, completed.stderr

    lines = progress_lines(completed.stderr)
    assert len(lines) >= 3, f"expected several lines, got {completed.stderr!r}"
    assert all(line["event"] == "progress" for line in lines)

    # The summary is still the only thing on stdout, and still parses.
    summary = json.loads(completed.stdout)
    assert summary["rows"] == 3000
    assert "event" not in summary


def test_every_line_is_a_complete_json_object(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 2500)
    config = write_config(tmp_path)

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    for line in completed.stderr.splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)  # raises if the line is not self-contained
        assert set(payload) >= {"event", "records", "rows", "rejected", "elapsed_seconds"}


def test_the_closing_line_is_always_written(tmp_path: pathlib.Path) -> None:
    """A consumer that never gets a last line cannot tell finished from stalled."""
    source = small_dataset(tmp_path, 1500)
    config = write_config(tmp_path)

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    lines = progress_lines(completed.stderr)
    assert lines[-1]["records"] == 1500, "the last line reports the whole run"


def test_the_closing_line_is_written_on_failure_too(tmp_path: pathlib.Path) -> None:
    """The failure path is where a progress bar would otherwise hang forever."""
    source = tmp_path / "bad.xml"
    source.write_text(
        "<catalog><products><product id='1'><name>ok</name></product>"
        "<product id='x'><name>bad</name></product></products></catalog>",
        encoding="utf-8",
    )
    config = tmp_path / "typed.json"
    config.write_text(
        json.dumps(
            {
                "record": "/catalog/products/product",
                "fields": {"id": {"path": "@id", "type": "int"}},
            }
        ),
        encoding="utf-8",
    )

    completed = run_cli(
        ["extract", str(source), "-c", str(config), "-o", str(tmp_path / "o.csv"), "--progress"]
    )

    assert completed.returncode == 1
    lines = progress_lines(completed.stderr)
    assert lines, "a failed run still has to say where it got to"
    assert lines[-1]["event"] == "progress"


def test_rejections_are_reported_as_they_happen(tmp_path: pathlib.Path) -> None:
    """The third outcome: a run that succeeds while skipping records."""
    source = tmp_path / "mixed.xml"
    source.write_text(
        "<catalog><products>"
        + "".join(f'<product id="{n}"><name>N{n}</name></product>' for n in range(1, 1500))
        + "<product id='bad'><name>no</name></product>"
        + "</products></catalog>",
        encoding="utf-8",
    )
    config = tmp_path / "quarantine.json"
    config.write_text(
        json.dumps(
            {
                "record": "/catalog/products/product",
                "on_error": "quarantine",
                "fields": {"id": {"path": "@id", "type": "int"}},
            }
        ),
        encoding="utf-8",
    )

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    assert completed.returncode == 0, completed.stderr
    lines = progress_lines(completed.stderr)
    assert lines[-1]["rejected"] == 1
    assert lines[-1]["rows"] == 1499, "the rejected record writes no row"


# --- the triggers ------------------------------------------------------------


def test_the_count_trigger_fires_on_a_fast_source(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 5000)
    config = write_config(tmp_path)

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    reported = [line["records"] for line in progress_lines(completed.stderr)]
    assert reported[:-1] == [1000, 2000, 3000, 4000, 5000][: len(reported) - 1]


def test_the_time_trigger_fires_when_records_do_not() -> None:
    """A count-only rule stays silent for minutes on a slow source.

    Reproduced at the library level: the interval is the only thing that can fire, since
    the record trigger is set beyond any count this test will reach.
    """
    import io
    import time

    from gigaxml.run import ProgressReporter

    stream = io.StringIO()
    reporter = ProgressReporter(stream, every=10_000_000, interval=0.05)

    reporter.tick(1, 1, 0)
    assert stream.getvalue() == "", "too soon for either trigger"

    time.sleep(0.06)
    reporter.tick(2, 2, 0)
    assert stream.getvalue(), "the interval should have fired"


def test_the_interval_is_not_consulted_on_every_record(tmp_path: pathlib.Path) -> None:
    """The check is every _PROGRESS_CHECK_EVERY records, not every record.

    Reading the clock per record costs more than the progress is worth. This counts the
    ticks rather than timing them: the reporter must be asked far fewer times than there
    are records.
    """
    import io

    from gigaxml.run import ProgressReporter

    class Counting(ProgressReporter):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)
            self.ticks = 0

        def tick(self, *args: object, **kwargs: object) -> None:
            self.ticks += 1
            super().tick(*args, **kwargs)

    reporter = Counting(io.StringIO(), every=1, interval=0.0)
    from gigaxml.config import parse_config
    from gigaxml.parser.streaming import StreamingRecordReader
    from gigaxml.run import consume_records
    from gigaxml.writers import create_writer

    source = small_dataset(tmp_path, 4000)
    config = parse_config(CONFIG)
    with create_writer(tmp_path / "o.csv", config.fields) as writer:
        consume_records(
            StreamingRecordReader(source, config.record_path), config, writer, progress=reporter
        )

    assert reporter.ticks <= 4000 // 1000 + 1, f"ticked {reporter.ticks} times for 4000 records"


# --- cumulative across a resume ---------------------------------------------


def test_records_are_cumulative_across_a_resume(tmp_path: pathlib.Path) -> None:
    """The consumer's denominator is the whole document, so a resume continues.

    A progress bar that restarts at zero after an interruption is worse than none: it
    tells the user they have lost work they have not lost.
    """
    source = small_dataset(tmp_path, 6000)
    config = write_config(tmp_path)
    parts = tmp_path / "parts"

    first = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "2000",
            "--format",
            "csv",
        ]
    )
    assert first.returncode == 0, first.stderr

    manifest = json.loads((parts / "checkpoint.json").read_text(encoding="utf-8"))
    consumed = manifest["records_consumed"]
    assert consumed == 6000

    # Rewind the manifest by one part so the resume has real work to do. The consumed
    # count has to come back with it: leaving it at the full total while the parts cover
    # less would make the resume skip the entire document and find nothing to do.
    dropped = manifest["parts"].pop()
    manifest["records_consumed"] -= dropped["rows"]
    manifest["complete"] = False
    (parts / "checkpoint.json").write_text(json.dumps(manifest), encoding="utf-8")

    resumed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "2000",
            "--format",
            "csv",
            "--resume",
            "--progress",
        ]
    )
    assert resumed.returncode == 0, resumed.stderr

    lines = progress_lines(resumed.stderr)
    assert lines, "a resumed run reports too"
    # The first line covers the part being written, which starts after the skipped
    # prefix -- so it must be above what was skipped, never at zero.
    assert lines[0]["records"] > 4000, (
        f"first line was {lines[0]['records']}, which is not past the resumed prefix"
    )
    assert lines[-1]["records"] == 6000


# --- checkpoint mode ---------------------------------------------------------


def test_the_part_key_appears_only_in_checkpoint_mode(tmp_path: pathlib.Path) -> None:
    source = small_dataset(tmp_path, 3000)
    config = write_config(tmp_path)

    plain = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )
    assert all("part" not in line for line in progress_lines(plain.stderr)), (
        "a single-file run has no parts to number"
    )

    parts = tmp_path / "parts"
    checkpointed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(parts),
            "--checkpoint-every",
            "1000",
            "--format",
            "csv",
            "--progress",
        ]
    )
    assert checkpointed.returncode == 0, checkpointed.stderr
    lines = progress_lines(checkpointed.stderr)
    assert any("part" in line for line in lines), "checkpoint mode should number its parts"
    assert lines[-1]["part"] >= 1


# --- off by default ----------------------------------------------------------


def test_without_the_flag_nothing_is_written(tmp_path: pathlib.Path) -> None:
    """The default path must be indistinguishable from before this existed."""
    source = small_dataset(tmp_path, 2000)
    config = write_config(tmp_path)

    completed = run_cli(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "o.csv")])

    assert completed.returncode == 0
    assert completed.stderr.strip() == "", f"stderr was {completed.stderr!r}"
    assert json.loads(completed.stdout)["rows"] == 2000


def test_help_renders() -> None:
    """The `%`-formatting trap: a literal percent in help breaks the whole command."""
    completed = run_cli(["extract", "--help"])

    assert completed.returncode == 0, completed.stderr
    assert "--progress" in completed.stdout
    assert "Traceback" not in completed.stderr


def test_the_help_explains_why_there_is_no_total() -> None:
    """The absence of a total is a decision, and a reader has to be able to find it."""
    completed = run_cli(["extract", "--help"])

    assert "no total and no percentage" in completed.stdout.lower()


def test_main_accepts_the_flags_without_a_subprocess(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The in-process path works too, so the flag is wired into the parser not the script."""
    source = small_dataset(tmp_path, 1500)
    config = write_config(tmp_path)

    code = main(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    assert code == 0
    captured = capsys.readouterr()
    assert progress_lines(captured.err)
    assert not any(line.startswith("{") and '"event"' in line for line in captured.out.splitlines())


def test_a_nonpositive_progress_every_is_refused() -> None:
    completed = run_cli(["extract", "x.xml", "-o", "y.csv", "--progress-every", "0"])

    assert completed.returncode == 2
    assert "at least 1" in completed.stderr


def test_the_gui_style_consumer_can_tell_progress_from_warnings(tmp_path: pathlib.Path) -> None:
    """The documented rule: parse it, and look for `event`.

    Exercised against real mixed output, because the rule is only useful if it survives
    warnings being interleaved.
    """
    source = small_dataset(tmp_path, 1200)
    config = write_config(tmp_path)

    completed = run_cli(
        [
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "1000",
        ]
    )

    parsed = []
    for line in completed.stderr.splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue  # a warning
        if isinstance(payload, dict) and payload.get("event") == "progress":
            parsed.append(payload)

    assert parsed, "the rule found nothing to consume"
    assert all(isinstance(line["records"], int) for line in parsed)


@pytest.mark.skipif(not (REPO / "data/s10.xml").is_file(), reason="dataset not generated")
def test_a_larger_dataset_reports_a_rising_count(tmp_path: pathlib.Path) -> None:
    """Counts only ever go up, on a dataset big enough to produce many lines."""
    config = write_config(tmp_path)

    completed = run_cli(
        [
            "extract",
            str(REPO / "data/s10.xml"),
            "-c",
            str(config),
            "-o",
            str(tmp_path / "o.csv"),
            "--progress",
            "--progress-every",
            "50000",
        ]
    )

    assert completed.returncode == 0, completed.stderr[-400:]
    counts = [line["records"] for line in progress_lines(completed.stderr)]
    assert counts == sorted(counts)
    assert counts[-1] == 29_120
