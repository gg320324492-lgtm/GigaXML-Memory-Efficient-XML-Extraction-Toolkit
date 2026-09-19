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
_REJECTION_BUFFER_BYTES: Final = 1 << 16


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

    Args:
        path: where to write. The file is created lazily, on the first rejection.
    """

    __slots__ = ("_count", "_created", "_handle", "_path")

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._handle: IO[str] | None = None
        self._created = False
        self._count = 0

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
        """How many records have been rejected so far."""
        return self._count

    def reject(self, index: int, record_path: str, error: BaseException) -> None:
        """Write one rejected record, as one line, now.

        Args:
            index: the record's 1-based ordinal in the source, counting rejected
                records. This is what lets a reader find the offending record again.
            record_path: the configured record path, so the log is self-describing.
            error: the error that rejected it.
        """
        if not self._created:
            self._handle = self._path.open("w", encoding="utf-8", buffering=_REJECTION_BUFFER_BYTES)
            self._created = True
        self._count += 1
        self._handle.write(json.dumps(self._entry(index, record_path, error), ensure_ascii=False))
        self._handle.write("\n")

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

    def close(self) -> None:
        """Flush and close. Idempotent, and safe when nothing was rejected."""
        if self._handle is not None:
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
