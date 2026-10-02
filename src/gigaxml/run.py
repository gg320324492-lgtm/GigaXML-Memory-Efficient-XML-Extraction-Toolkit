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
import sys
import time
from collections.abc import Callable, Iterable
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
    "PROGRESS_CHECK_EVERY",
    "PROGRESS_EVERY_DEFAULT",
    "QUARANTINABLE",
    "ProgressReporter",
    "RejectionLog",
    "RunStats",
    "_vmhwm_mib",
    "consume_records",
    "peak_rss_mb",
    "peak_rss_source",
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

_MB: Final = 1024 * 1024


def peak_rss_mb() -> float | None:
    """The OS high-water mark for this process, in MiB -- or ``None``.

    **``None`` means "this platform cannot tell", and it is load-bearing.** A run report
    that filled an unavailable peak with ``0.0`` would be worse than one that omitted it:
    a reader scanning for regressions would see a memory figure that is obviously
    impossible, or -- worse, once downstream code stopped being surprised by it -- would
    treat "we could not measure" as "it used nothing". A missing measurement has to look
    missing.

    Three platforms, and none of them guess:

    * **Windows** reads ``PeakWorkingSetSize`` through ``psapi``.
    * **Linux** reads ``VmHWM`` from ``/proc/self/status``, which the kernel labels in
      kibibytes. Not ``getrusage``: its ``ru_maxrss`` unit is kilobytes on Linux and
      bytes on the BSDs, and a runner that reports bytes where kilobytes were expected
      produces a number 1024 times too large, which looks like a 4 GiB peak in a process
      that never allocated 4 GiB. The kernel's own file says which unit it is in.
    * **macOS** is the one place ``getrusage`` is used, because there is no
      ``/proc``, and its ``ru_maxrss`` is documented in bytes.
    """
    chosen = _peak_reader()
    return None if chosen is None else chosen[1]()


def peak_rss_source() -> str | None:
    """The name of the OS counter :func:`peak_rss_mb` reads here, or ``None``.

    A measurement nobody can name is a measurement nobody can check, and a reader
    deciding whether a peak figure means anything has to know which counter produced
    it before they know what it was. Both functions go through :func:`_peak_reader`, so
    the number and the name describing it cannot come from different platforms: a
    harness that typed its own "which counter is this" string is the second copy of a
    fact that rots, which is how a run that quietly fell back to current RSS came to
    be filed as a peak measurement.
    """
    chosen = _peak_reader()
    return None if chosen is None else chosen[0]


def _peak_reader() -> tuple[str, Callable[[], float | None]] | None:
    """``(counter name, reader)`` for this platform, or ``None`` where there is neither.

    The one place the platform is decided. :func:`peak_rss_mb` and
    :func:`peak_rss_source` are two views of the same choice, so splitting the branch
    in two would let the name say one platform while the number came from another.
    """
    if sys.platform == "win32":
        return ("PeakWorkingSetSize (psapi GetProcessMemoryInfo)", _windows_peak_working_set_mb)
    if sys.platform.startswith("linux"):
        return ("VmHWM (/proc/self/status)", _linux_vmhwm_mb)
    try:
        import resource
    except ImportError:  # pragma: no cover - no getrusage on this platform
        return None
    ru_maxrss = lambda: resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # noqa: E731
    if sys.platform == "darwin":
        return ("ru_maxrss (getrusage RUSAGE_SELF, bytes)", lambda: ru_maxrss() / _MB)
    # The BSDs report kilobytes; see the docstring above for why this is not a constant.
    return ("ru_maxrss (getrusage RUSAGE_SELF, KiB)", lambda: ru_maxrss() / 1024)


def _vmhwm_mib(status_text: str) -> float | None:
    """``VmHWM`` out of ``/proc/self/status`` text, in MiB, or ``None``.

    Split out from the file read so the parsing and the unit conversion are testable on
    any platform. They are exactly the two things that can be wrong about a Linux
    reading, and a Linux-only branch cannot be exercised from a Windows checkout --
    which is how a wrong unit reached a runner and turned a fresh interpreter into a
    4.2 GiB peak with every other assertion still green.

    ``VmHWM`` is the kernel's peak resident set. The kernel prints it in kibibytes and
    says so on the same line, which is the whole reason this reads the file instead of
    calling ``getrusage``: that function's ``ru_maxrss`` unit is kilobytes on Linux and
    bytes on the BSDs, so code that assumes one is right on the other by a factor of
    1024, and 1024 looks like a plausible-looking number rather than an obvious bug.
    """
    for line in status_text.splitlines():
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) / 1024
    return None


def _linux_vmhwm_mb() -> float | None:
    """Read ``/proc/self/status`` and hand it to :func:`_vmhwm_mib`."""
    try:
        return _vmhwm_mib(Path("/proc/self/status").read_text(encoding="ascii"))
    except (OSError, ValueError, IndexError):
        return None


def _windows_peak_working_set_mb() -> float | None:  # pragma: no cover - Windows only
    """``PeakWorkingSetSize`` in MiB, or ``None`` if the call fails."""
    import ctypes
    from ctypes import wintypes

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = _Counters()
    counters.cb = ctypes.sizeof(counters)
    handle = ctypes.windll.kernel32.GetCurrentProcess()  # type: ignore[attr-defined]
    read = ctypes.windll.psapi.GetProcessMemoryInfo  # type: ignore[attr-defined]
    # The signatures are declared, and that is not decoration. Without them ctypes
    # assumes every argument is a 32-bit int, which truncates the 64-bit pseudo-handle
    # GetCurrentProcess returns; the call then fails, returns 0, and a reader that
    # treats that as "no measurement" silently falls back to current RSS -- which is a
    # different quantity that happens to be available. Declared, it returns TRUE.
    read.restype = wintypes.BOOL
    read.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
    ok = read(handle, ctypes.byref(counters), counters.cb)
    if not ok:  # pragma: no cover - the call has no failure mode in practice
        return None
    return counters.PeakWorkingSetSize / _MB


#: Rejections written between explicit flushes. Flushing each write was measured at
#: **+17.29%** on 1,164,800 rejections -- over the 10% the design allows -- so the log
#: flushes every :data:`_REJECTION_FLUSH_LINES` lines, bounding a crash's loss to 255.
#:
#: ★ **"Flushed" is not "durable"**: layer 3 of ``OUTPUT-DURABILITY.md``, not this
#: module's to close. Its guarantee is that the count never overstates what the OS has:
#: the counter follows the flush, not the write.
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

    **★ What "flushed" means here.** Rejections are buffered and flushed every
    :data:`_REJECTION_FLUSH_LINES` lines (or :data:`_REJECTION_FLUSH_BYTES` bytes).
    A crash therefore loses at most the last few hundred lines rather than all of
    them, and :attr:`count` follows the flush rather than the write, so it always
    agrees with what is actually in the file. Flushing every line would be exact but
    costs 17% of the throughput, which was measured rather than assumed.
    ★ **"In the file" means as far as the operating system is concerned.** A power cut can
    still take what a flush already counted; see layer 3 of ``OUTPUT-DURABILITY.md``. What
    this class guarantees is the weaker and still useful thing -- **the count never claims
    a line the operating system does not have.**

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
        """How many rejections the operating system has been given, including appended ones.

        Deliberately not "how many times :meth:`reject` was called": after a crash the
        two differ, and the number a caller can act on is the one that matches the
        file. :meth:`flush` makes them equal on demand, and :meth:`close` always does.
        ★ "The operating system has been given" is the honest phrasing and not a fussy
        one -- "on disk" would promise the device, which is layer 3 of
        ``OUTPUT-DURABILITY.md`` and is not this class's to promise.
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
        """Hand everything written so far to the operating system, and count it as such.

        ★ **This is not durability.** ``flush`` does not ask the device for anything, so a
        power cut can still lose bytes this method has already counted. Layer 3 of
        ``OUTPUT-DURABILITY.md`` is where that boundary is written down; what this method
        does promise is that :attr:`count` stops overstating the moment it returns.
        """
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


#: How often the cheap counter check runs. Calling the clock on every record would cost
#: more than the progress it reports; a thousand records is far cheaper than one tick.
PROGRESS_CHECK_EVERY: Final = 1000

#: Emit after this many further records, or after this many seconds, whichever comes
#: first. The count trigger keeps the line rate low on a fast source; the time trigger
#: keeps it non-zero on a slow one, where a count-only rule can stay silent for minutes.
PROGRESS_EVERY_DEFAULT: Final = 20000
_PROGRESS_INTERVAL_DEFAULT: Final = 1.0


class ProgressReporter:
    """Machine-readable progress, one JSON object per line, on a stream of your choosing.

    **Write to stderr, never stdout.** ``extract`` prints its run summary to stdout, and
    both a caller parsing it and a shell redirecting it depend on that being the only
    thing there. Progress interleaved with it would break both.

    **Lines are JSON, warnings are plain text**, so a consumer separates them by trying
    to parse. That is why the existing warnings are left alone: wrapping them would
    change what an existing caller sees.

    **No total, no percentage.** The tool cannot know how many records a document holds
    without reading it first, and a denominator it invented would be a number that looks
    authoritative and is not. The consumer supplies it -- ``inspect`` reports an exact
    count for each candidate -- so this reports only what it has actually done.

    **``records`` is cumulative across a resume.** A resumed run reports the records it
    skipped as well as the ones it processed, because the consumer's denominator is the
    whole document and a progress bar that restarts at zero after a resume is worse than
    no progress bar. ``elapsed_seconds`` is the opposite: it times this run only, so the
    two are deliberately not the same baseline.

    Args:
        stream: where to write. Pass ``sys.stderr``.
        every: emit once this many further records have gone by.
        interval: emit once this many seconds have gone by, even if few records have.
        records_offset: records already accounted for before this run -- a resume's
            skipped prefix. Added to every count reported.
        rows_offset: the same, for rows.
    """

    __slots__ = (
        "_check_every",
        "_emitted",
        "_every",
        "_interval",
        "_last_at",
        "_last_records",
        "_last_rows",
        "_part",
        "_records_offset",
        "_rows_offset",
        "_started",
        "_stream",
    )

    def __init__(
        self,
        stream: IO[str],
        *,
        every: int = PROGRESS_EVERY_DEFAULT,
        interval: float = _PROGRESS_INTERVAL_DEFAULT,
        records_offset: int = 0,
        rows_offset: int = 0,
    ) -> None:
        self._stream = stream
        self._every = max(1, every)
        # The check interval must be no coarser than the requested granularity, or a
        # caller asking for a line every 100 records gets one every 1000 and is never
        # told. The ceiling is what keeps the clock reads cheap on the default.
        self._check_every = max(1, min(PROGRESS_CHECK_EVERY, self._every))
        self._interval = interval
        self._records_offset = records_offset
        self._rows_offset = rows_offset
        self._part: int | None = None
        self._started = time.perf_counter()
        self._last_at = self._started
        self._last_records = 0
        self._last_rows = 0
        self._emitted = False

    def _emit(self, records: int, rows: int, rejected: int, part: int | None) -> None:
        payload: dict[str, object] = {
            "event": "progress",
            "records": records,
            "rows": rows,
            "rejected": rejected,
            "elapsed_seconds": round(time.perf_counter() - self._started, 3),
        }
        if part is not None:
            payload["part"] = part
        self._stream.write(json.dumps(payload) + "\n")
        self._stream.flush()
        self._last_records = records
        self._last_rows = rows
        self._last_at = time.perf_counter()
        self._emitted = True

    def set_offsets(self, records: int, rows: int) -> None:
        """Move the cumulative baseline. A checkpointed run calls this before each part.

        ``elapsed_seconds`` is deliberately not reset: it times the run, not the part.
        """
        self._records_offset = records
        self._rows_offset = rows

    @property
    def check_every(self) -> int:
        """How often the caller should offer a tick.

        At most :data:`PROGRESS_CHECK_EVERY`, and never coarser than the granularity the
        caller asked for.
        """
        return self._check_every

    def set_part(self, part: int | None) -> None:
        """Set the part number that goes on every line, or ``None`` for a single file.

        Only a checkpointed run has parts. Emitting ``part`` for a single-file run would
        invite a consumer to group lines that are not grouped.
        """
        self._part = part

    def tick(self, records: int, rows: int, rejected: int) -> None:
        """Maybe emit. Call once every ``check_every`` records, not on every one."""
        cumulative = self._records_offset + records
        # Either trigger is enough; both have to be quiet for this to be silent.
        if (
            cumulative - self._last_records < self._every
            and time.perf_counter() - self._last_at < self._interval
        ):
            return
        self._emit(cumulative, self._rows_offset + rows, rejected, self._part)

    def finish(self, records: int, rows: int, rejected: int) -> None:
        """Emit the closing line, whatever the outcome.

        Called on success and on failure alike: a consumer that never receives a last
        line cannot tell a finished run from a stalled one.

        **Silent when the last tick already reported these numbers.** The guarantee is
        that a consumer ends up holding a line with the final counts, not that a line is
        written for its own sake -- and a checkpointed run ends a part every time it
        writes one, so writing unconditionally here would double every line it produced.
        """
        cumulative = self._records_offset + records
        rows = self._rows_offset + rows
        if self._emitted and cumulative == self._last_records and rows == self._last_rows:
            return
        self._emit(cumulative, rows, rejected, self._part)


def consume_records(
    reader: Iterable[etree._Element],
    config: ExtractionConfig,
    writer: RowWriter,
    *,
    limit: int | None = None,
    rejections: RejectionLog | None = None,
    progress: ProgressReporter | None = None,
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
        progress: where to report progress. With ``None`` nothing is emitted and the
            run behaves exactly as it did before this existed.

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

    try:
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

            # Checked every so often rather than every record: reading the clock per record
            # costs more than the progress is worth, and the reporter decides for itself
            # whether enough has changed to be worth a line.
            if progress is not None and index % progress.check_every == 0:
                progress.tick(
                    index,
                    writer.rows_accepted,
                    0 if rejections is None else rejections.count,
                )
    finally:
        # In a finally block on purpose. This is the only place that knows how far the
        # pass got when it failed, and a consumer that never receives a closing line
        # cannot tell a finished run from a stalled one.
        if rejections is not None:
            # Flushed first so the closing line's count is the real one: `count` follows
            # the flush, and a last line that under-reports rejections is exactly the
            # kind of number that looks authoritative and is wrong.
            rejections.flush()
        if progress is not None:
            progress.finish(
                index,
                writer.rows_accepted,
                0 if rejections is None else rejections.count,
            )

    if rejections is not None:
        # The loop is over, so hand the log to the operating system and let the count
        # mean what it says. Without this a short run could report fewer rejections than
        # it made. (Not durability -- see RejectionLog.flush.)
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
