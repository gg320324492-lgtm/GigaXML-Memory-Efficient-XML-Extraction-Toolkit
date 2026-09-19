"""Unit tests: the rejection log and the shared extraction loop.

The log is the one piece of this phase with a memory obligation attached, so most of
these are about what it does *not* do: it does not exist until something is rejected,
it does not hold rejected records anywhere but the file, and it does not lose its own
path when it is closed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import FieldTypeError
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import QUARANTINABLE, RejectionLog, RunStats, consume_records
from gigaxml.writers import create_writer

FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}

SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>bad</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
</root>
"""


def build_config(on_error: str = "abort") -> ExtractionConfig:
    return parse_config({"record": "/root/item", "fields": FIELDS, "on_error": on_error})


def write_source(tmp_path: Path) -> Path:
    path = tmp_path / "source.xml"
    path.write_text(SOURCE, encoding="utf-8")
    return path


# --- RejectionLog -----------------------------------------------------------


def test_nothing_is_created_until_something_is_rejected(tmp_path: Path) -> None:
    log = RejectionLog(tmp_path / "rejected.jsonl")

    assert log.count == 0
    assert log.written_path is None
    assert not (tmp_path / "rejected.jsonl").exists()

    log.reject(1, "/root/item", ValueError("nope"))

    assert log.count == 1
    assert log.written_path == tmp_path / "rejected.jsonl"
    assert (tmp_path / "rejected.jsonl").exists()
    log.close()


def test_the_path_survives_closing(tmp_path: Path) -> None:
    """A report written after the run has to be able to name the log."""
    log = RejectionLog(tmp_path / "rejected.jsonl")
    log.reject(1, "/root/item", ValueError("nope"))
    log.close()

    assert log.written_path == tmp_path / "rejected.jsonl"
    assert log.count == 1


def test_each_rejection_is_one_line_of_json(tmp_path: Path) -> None:
    path = tmp_path / "rejected.jsonl"
    with RejectionLog(path) as log:
        log.reject(2, "/root/item", FieldTypeError("bad int", field="id", raw="notanint"))
        log.reject(4, "/root/item", ValueError("other"))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    entries = [json.loads(line) for line in lines]
    assert entries[0] == {
        "index": 2,
        "record_path": "/root/item",
        "error": "FieldTypeError",
        "message": "bad int",
        "field": "id",
        "raw": "notanint",
    }
    # An error that carries no field/raw detail simply omits those keys rather than
    # inventing nulls for them.
    assert entries[1]["error"] == "ValueError"
    assert "field" not in entries[1]
    assert "raw" not in entries[1]


def test_the_count_does_not_depend_on_the_file_being_flushed(tmp_path: Path) -> None:
    """The count is the authoritative number; the report reads it, not the file."""
    log = RejectionLog(tmp_path / "rejected.jsonl")
    for index in range(1, 11):
        log.reject(index, "/root/item", ValueError("x"))

    assert log.count == 10
    log.close()
    assert len((tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()) == 10


def test_close_is_idempotent(tmp_path: Path) -> None:
    log = RejectionLog(tmp_path / "rejected.jsonl")
    log.close()
    log.reject(1, "/root/item", ValueError("x"))
    log.close()
    log.close()

    assert log.count == 1


# --- consume_records --------------------------------------------------------


def consume(
    tmp_path: Path,
    config: ExtractionConfig,
    *,
    limit: int | None = None,
    rejections: RejectionLog | None = None,
) -> RunStats:
    writer = create_writer(tmp_path / "out.csv", config.fields)
    reader = StreamingRecordReader(
        write_source(tmp_path), config.record_path, config.namespaces or None
    )
    with writer:
        return consume_records(reader, config, writer, limit=limit, rejections=rejections)


def test_abort_raises_the_original_error(tmp_path: Path) -> None:
    with pytest.raises(FieldTypeError):
        consume(tmp_path, build_config("abort"), rejections=RejectionLog(tmp_path / "r.jsonl"))


def test_quarantine_records_and_continues(tmp_path: Path) -> None:
    log = RejectionLog(tmp_path / "r.jsonl")
    with log:
        stats = consume(tmp_path, build_config("quarantine"), rejections=log)

    assert stats.records_processed == 3
    assert stats.rejected == 1
    assert stats.rejected_path == str(tmp_path / "r.jsonl")
    assert stats.limit_hit is False


def test_quarantine_without_a_log_aborts(tmp_path: Path) -> None:
    """The policy needs somewhere to record what it skips; with nowhere, it cannot."""
    with pytest.raises(FieldTypeError):
        consume(tmp_path, build_config("quarantine"), rejections=None)


def test_a_limit_stops_the_run_and_says_so(tmp_path: Path) -> None:
    """``limit=2`` stops after two records -- including a rejected one.

    Quarantine is used so that the second record, which is bad, does not end the run
    before the limit gets a chance to; that is a separate behaviour, tested above.
    """
    log = RejectionLog(tmp_path / "r.jsonl")
    with log:
        stats = consume(tmp_path, build_config("quarantine"), limit=2, rejections=log)

    assert stats.records_processed == 2
    assert stats.rejected == 1
    assert stats.limit_hit is True


def test_a_limit_past_the_end_reports_the_document_ran_out(tmp_path: Path) -> None:
    log = RejectionLog(tmp_path / "r.jsonl")
    with log:
        stats = consume(tmp_path, build_config("quarantine"), limit=99, rejections=log)

    assert stats.records_processed == 3
    assert stats.limit_hit is False


def test_only_the_two_record_errors_are_quarantinable() -> None:
    """A broken config or an unusable writer must not be swallowed as a bad record.

    Skipping a record cannot fix either of those, and reporting success while writing
    nothing is worse than failing.
    """
    from gigaxml.errors import ConfigError, RecordPathError, WriterError

    names = {error.__name__ for error in QUARANTINABLE}
    assert names == {"FieldTypeError", "MissingRequiredFieldError"}
    for unrelated in (ConfigError, RecordPathError, WriterError):
        assert unrelated not in QUARANTINABLE


def test_the_loop_takes_every_value_inside_the_iteration_step(tmp_path: Path) -> None:
    """The reader clears each element as it advances, so a deferred read yields ``None``.

    That failure is silent -- it produces a file full of nulls rather than an error --
    so the observable consequence is asserted directly: real values, not ``None``.
    """
    config = build_config("quarantine")
    log = RejectionLog(tmp_path / "r.jsonl")
    with log:
        consume(tmp_path, config, rejections=log)

    lines = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    assert lines == ["id,name", "1,A", "3,C"]
