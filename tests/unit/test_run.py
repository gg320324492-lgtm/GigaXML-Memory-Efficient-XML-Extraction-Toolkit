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

    assert log.total == 1, "one rejection was recorded"
    assert log.written_path == tmp_path / "rejected.jsonl"
    assert (tmp_path / "rejected.jsonl").exists()
    log.close()
    assert log.count == 1, "and closing makes it durable"


def test_the_count_follows_the_flush_not_the_write(tmp_path: Path) -> None:
    """The count is what is in the file, which is not the same as what was written.

    A crash loses whatever is still buffered, so a count that ran ahead of the file
    would overstate how much was recorded. ``total`` is the write-side number and
    ``count`` is the file-side one; only the second is safe to publish.
    """
    log = RejectionLog(tmp_path / "rejected.jsonl")
    for index in range(1, 11):
        log.reject(index, "/root/item", ValueError("x"))

    assert log.total == 10
    assert log.count == 0, "ten lines written, none flushed yet"
    assert (tmp_path / "rejected.jsonl").read_text(encoding="utf-8") == ""

    log.flush()

    assert log.count == 10
    assert len((tmp_path / "rejected.jsonl").read_text(encoding="utf-8").splitlines()) == 10
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


def test_the_count_always_matches_the_file(tmp_path: Path) -> None:
    """The half of the old contract that survives: the count is never a guess.

    This replaces ``test_the_count_does_not_depend_on_the_file_being_flushed``, whose
    direction this phase inverted. It used to assert that the count ran ahead of the
    file, which was true and was the problem: a crash left a count with nothing behind
    it. The invariant worth keeping is the one asserted here -- whatever ``count``
    says, the file says the same -- and it is checked at every stage rather than only
    at the end.
    """
    path = tmp_path / "rejected.jsonl"
    log = RejectionLog(path)

    for index in range(1, 3001):
        log.reject(index, "/root/item", ValueError("x"))
        on_disk = len(path.read_text(encoding="utf-8").splitlines())
        assert log.count == on_disk, f"diverged after {index} rejections"

    log.close()
    assert log.count == len(path.read_text(encoding="utf-8").splitlines()) == 3000
    assert log.total == 3000


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


def test_the_rejection_log_flushes_on_bytes_when_lines_do_not(tmp_path: Path) -> None:
    """The line threshold is not the only one, and the other had no test.

    ``reject`` flushes at 256 lines **or** 512 KiB, whichever comes first. The byte
    threshold exists so that a long message cannot let the file object's own buffer
    fill first -- if it did, the log would hold more lines than ``count`` reports, and
    the two numbers this whole mechanism keeps in step would drift apart again. Short
    messages never reach it, so it needs messages that do.
    """
    from gigaxml.run import _REJECTION_FLUSH_BYTES, RejectionLog

    path = tmp_path / "rejected.jsonl"
    log = RejectionLog(path)
    # One message comfortably over the byte threshold, so a single rejection flushes.
    huge = "x" * (_REJECTION_FLUSH_BYTES + 1024)

    log.reject(1, "/root/item", ValueError(huge))

    assert log.total == 1
    assert log.count == 1, "the byte threshold should have flushed a single long line"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1
    log.close()


def test_the_byte_threshold_keeps_the_count_honest(tmp_path: Path) -> None:
    """Whatever mix of long and short messages, count never runs ahead of the file."""
    from gigaxml.run import _REJECTION_FLUSH_BYTES, RejectionLog

    path = tmp_path / "rejected.jsonl"
    log = RejectionLog(path)
    payload = "y" * (_REJECTION_FLUSH_BYTES // 4)

    for index in range(1, 13):
        log.reject(index, "/root/item", ValueError(payload))
        on_disk = len(path.read_text(encoding="utf-8").splitlines())
        assert log.count == on_disk, f"diverged after {index} long rejections"

    log.close()
    assert log.count == len(path.read_text(encoding="utf-8").splitlines()) == 12
