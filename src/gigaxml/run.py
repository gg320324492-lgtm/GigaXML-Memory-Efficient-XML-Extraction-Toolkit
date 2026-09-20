"""The extraction loop shared by ``extract`` and ``sample``, and the rejection log.

**Why this is a module and not two loops.** ``extract`` and ``sample`` differ only in
whether they stop early; everything else -- reading records, extracting them, writing
them, and deciding what to do when one is bad -- is the same. Two copies of that would
drift, and the drift would be exactly in the error policy: a config that says
``on_error: quarantine`` and a ``sample`` that still dies on the first bad record is
the kind of trap this phase exists to remove.

**Why the rejection log is streamed.** The number of rejected records is the one
quantity a quarantine run is most likely to have a lot of -- that is what makes the
policy worth having. Buffering them and writing once at the end would make memory
proportional to that number, which is precisely the failure mode Phase 1 removed from
the reader. Each rejection is written as its own line as it happens, so memory stays
flat whether there are three rejections or a million.

**The log is created on the first rejection, not up front.** A clean run must leave no
``rejected.jsonl`` behind: an empty file is indistinguishable from a file whose
contents were lost, and either way it invites the reader to conclude that something
was rejected when nothing was.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from lxml import etree

from gigaxml.config import ErrorPolicy, ExtractionConfig
from gigaxml.errors import FieldTypeError, MissingRequiredFieldError
from gigaxml.fields import extract_record
from gigaxml.writers import RowWriter

__all__ = [
    "DEFAULT_REJECTION_FILENAME",
    "DEFAULT_RUN_REPORT_FILENAME",
    "QUARANTINABLE",
    "RejectionLog",
    "RunStats",
    "consume_records",
]

#: File name of the rejection log, written beside the output file.
DEFAULT_REJECTION_FILENAME: Final = "rejected.jsonl"

#: File name of the machine-readable run report, written beside the output file.
DEFAULT_RUN_REPORT_FILENAME: Final = "run-report.json"

#: The errors that mean "this record is bad" rather than "this run is broken".
#:
#: Deliberately narrow. A ``RecordPathError`` means the config does not describe this
#: document, and a ``WriterError`` means the output is unusable -- neither is fixed by
#: skipping a record, and swallowing them would turn a misconfiguration into a run
#: that reports success and writes nothing.
QUARANTINABLE: Final = (FieldTypeError, MissingRequiredFieldError)

#: Bytes buffered by the rejection log's handle. Bounded, so a run with a million
#: rejections costs no more memory than one with a hundred.
_REJECTION_BUFFER_BYTES: Final = 1 << 20

#: Rejections written between explicit flushes.
#:
#: **Why not flush every line.** Flushing after each write makes every rejection
#: durable, and it was measured before choosing: on 1,164,800 rejections it cost
#: **+17.29%** (20.599s against 17.563s, best of three, alternating between the two
#: variants). That is over the 10% the design allows for durability, so the log
#: flushes every :data:`_REJECTION_FLUSH_LINES` lines instead, which bounds what a
#: crash can lose to 255 lines out of a million.
#:
#: The counter follows the flush, not the write -- see :meth:`RejectionLog.count` --
#: so the number in the run summary is always the number of lines actually in the
#: file, whether the run ended cleanly or died.
_REJECTION_FLUSH_LINES: Final = 256

#: Bytes written between explicit flushes, kept well under the handle's own buffer so
#: the file object never flushes behind our back and makes the count wrong. Long
#: error messages are what this guards against; short ones hit the line threshold
#: first.
_REJECTION_FLUSH_BYTES: Final = _REJECTION_BUFFER_BYTES // 2


@dataclass(frozen=True, slots=True)
class RunStats:
    """What one pass over the records did.

    Attributes:
        records_processed: records taken from the reader and either written or
            rejected. A limited run that stopped early does not count the record it
            read to find out that more existed.
        rejected: records quarantined.
        rejected_path: where the rejection log was written, or ``None`` when nothing
            was rejected (in which case no file exists).
        limit_hit: the run stopped because it reached ``limit``, not because the
            source ran out.
    """

    records_processed: int
    rejected: int
    rejected_path: str | None
    limit_hit: bool


class RejectionLog:
    """Append-only JSONL log of records that could not be extracted.

    Use as a context manager, or call :meth:`close`.

    **Durability.** Rejections are buffered and flushed every
    :data:`_REJECTION_FLUSH_LINES` lines (or :data:`_REJECTION_FLUSH_BYTES` bytes).
    A crash therefore loses at most the last few hundred lines rather than all of
    them, and :attr:`count` follows the flush rather than the write, so it always
    agrees with what is actually in the file. Flushing every line would be exact but
    costs 17% of the throughput, which was measured rather than assumed.

    Args:
        path: where to write. The file is created lazily, on the first rejection.
        append: continue an existing log instead of replacing it. A resumed run uses
            this: the rejections from before the interruption are still real, and
            truncating them would lose the only record of what was skipped.
        initial: rejections already in the file. Needed because :attr:`count` means
            "lines in the file", and on an appended log that starts above zero.
    """

    __slots__ = (
        "_created",
        "_durable",
        "_handle",
        "_mode",
        "_path",
        "_pending",
        "_pending_bytes",
    )

    def __init__(self, path: str | Path, *, append: bool = False, initial: int = 0) -> None:
        self._path = Path(path)
        self._handle: IO[str] | None = None
        self._created = False
        self._mode = "a" if append else "w"
        self._durable = initial
        self._pending = 0
        self._pending_bytes = 0

    @property
    def path(self) -> Path:
        """Where the log would be written."""
        return self._path

    @property
    def written_path(self) -> Path | None:
        """Where the log was written, or ``None`` when nothing was rejected.

        Tracks creation separately from the handle: after :meth:`close` the handle is
        gone but the file is still there, and a report written afterwards has to name
        it.
        """
        return self._path if self._created else None

    @property
    def count(self) -> int:
        """How many rejections are **on disk**, including any appended ones.

        Deliberately not "how many times :meth:`reject` was called": after a crash the
        two differ, and the number a caller can act on is the one that matches the
        file. :meth:`flush` makes them equal on demand, and :meth:`close` always does.
        """
        return self._durable

    @property
    def total(self) -> int:
        """How many rejections have been recorded, flushed or still buffered."""
        return self._durable + self._pending

    def reject(self, index: int, record_path: str, error: BaseException) -> None:
        """Write one rejected record, as one line, now.

        Args:
            index: the record's 1-based ordinal in the source, counting rejected
                records. This is what lets a reader find the offending record again.
            record_path: the configured record path, so the log is self-describing.
            error: the error that rejected it.
        """
        if not self._created:
            self._handle = self._path.open(
                self._mode, encoding="utf-8", buffering=_REJECTION_BUFFER_BYTES
            )
            self._created = True
        payload = json.dumps(self._entry(index, record_path, error), ensure_ascii=False) + "\n"
        self._handle.write(payload)
        self._pending += 1
        self._pending_bytes += len(payload)
        if self._pending >= _REJECTION_FLUSH_LINES or self._pending_bytes >= _REJECTION_FLUSH_BYTES:
            self.flush()

    @staticmethod
    def _entry(index: int, record_path: str, error: BaseException) -> dict[str, object]:
        """The four required fields, plus the structured detail the errors carry.

        ``FieldTypeError`` already knows which field failed and what its raw text was,
        and its docstring says so for this purpose. Repeating them here means a reader
        does not have to re-parse the message to find out which column was wrong --
        which is the whole reason those attributes exist.
        """
        entry: dict[str, object] = {
            "index": index,
            "record_path": record_path,
            "error": type(error).__name__,
            "message": str(error),
        }
        if isinstance(error, (FieldTypeError, MissingRequiredFieldError)):
            entry["field"] = error.field
        if isinstance(error, FieldTypeError):
            entry["raw"] = error.raw
        return entry

    def flush(self) -> None:
        """Make everything written so far durable, and count it as such."""
        if self._handle is None:
            return
        self._handle.flush()
        self._durable += self._pending
        self._pending = 0
        self._pending_bytes = 0

    def close(self) -> None:
        """Flush and close. Idempotent, and safe when nothing was rejected."""
        if self._handle is not None:
            self.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self) -> RejectionLog:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False


def consume_records(
    reader: Iterable[etree._Element],
    config: ExtractionConfig,
    writer: RowWriter,
    *,
    limit: int | None = None,
    rejections: RejectionLog | None = None,
) -> RunStats:
    """Extract every record from ``reader`` into ``writer``.

    Args:
        reader: an iterable of record elements. Its elements are only valid inside
            the iteration step -- the reader clears them once it advances -- so
            every value is taken here and never held.
        config: the extraction config, including the error policy.
        writer: an open writer. The caller owns it, so that a failure still leaves
            it closeable and its ``rows_written`` readable.
        limit: stop after this many records.
        rejections: where to log rejected records. Required for
            :attr:`~gigaxml.config.ErrorPolicy.QUARANTINE` to have any effect; with
            ``None`` the policy degrades to aborting, because there is nowhere to
            record what was skipped.

    Returns:
        What the pass did.

    Raises:
        FieldTypeError: a value is not convertible, under
            :attr:`~gigaxml.config.ErrorPolicy.ABORT` (or with no rejection log).
        MissingRequiredFieldError: a required field is absent, under the same
            conditions.
    """
    index = 0
    limit_hit = False

    for record in reader:
        if limit is not None and index >= limit:
            # One record past the limit was yielded to establish that there is
            # more; it is discarded rather than written.
            limit_hit = True
            break
        index += 1
        try:
            values = extract_record(record, config.fields).values
        except QUARANTINABLE as exc:
            if config.on_error is not ErrorPolicy.QUARANTINE or rejections is None:
                raise
            rejections.reject(index, config.record_path, exc)
            continue
        writer.write(values)

    if rejections is not None:
        # The loop is over, so make the log durable and let the count mean what it
        # says. Without this a short run could report fewer rejections than it made.
        rejections.flush()
    rejected = 0 if rejections is None else rejections.count
    written_path = None if rejections is None else rejections.written_path
    return RunStats(
        records_processed=index,
        rejected=rejected,
        rejected_path=None if written_path is None else str(written_path),
        limit_hit=limit_hit,
    )


def elapsed_since(started: float) -> float:
    """Seconds since a :func:`time.perf_counter` reading, rounded for a report."""
    return round(time.perf_counter() - started, 3)
