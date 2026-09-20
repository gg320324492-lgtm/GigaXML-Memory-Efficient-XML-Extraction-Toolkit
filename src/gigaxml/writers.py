"""Batch writers for CSV, JSONL and Parquet.

This is the output-side half of the bounded-memory story. The reader keeps memory
flat on the way in; these writers keep it flat on the way out by holding at most
``batch_size`` rows and flushing each batch as it fills. Nothing here accumulates
across a whole run, and Parquet writes one row group per batch rather than
collecting a table and writing it at the end -- collecting would put the entire
output in memory and undo Phase 1.

**Parquet's schema comes from the config, never from the data.** Inferring it from
the first batch would let a single ``None`` decide the column type for the whole
file, and a first batch of integers followed by a float would fail halfway
through. The schema is built once from the configured field types, and every batch
is written against it.

**``decimal`` is written as a Parquet string.** A price converted to ``float`` has
already lost the value -- that is the whole reason :class:`~gigaxml.fields.FieldType`
has a ``decimal`` member -- so a lossy Parquet encoding would throw the guarantee
away at the last step. ``decimal128`` is the other defensible choice, but it
requires picking a precision and scale up front, and a value that does not fit
would then have to be rounded (silently wrong) or rejected (a run that dies on
data). A string is exact for every value, needs no guess, and round-trips through
:class:`decimal.Decimal` unchanged, trailing zeros included.

``pyarrow`` is imported lazily, inside the Parquet writer, so that
``import gigaxml.writers`` and the CSV/JSONL paths keep working with only the two
required dependencies installed.

**Output is written atomically.** Every writer opens ``<target>.tmp`` in the target's
own directory and only renames it into place once the last batch is flushed. The
guarantee that buys is worth stating plainly: **the file at the target path is either
complete or the previous complete version -- never a truncated one.** Before this, a
run killed halfway through left a file indistinguishable from a finished one, and a
*failed* re-run destroyed the previous run's output on its way to producing nothing.
A half-written output is worse than no output, because nothing about it says so.

Two consequences worth knowing:

* On failure the ``.tmp`` file is **left on disk**, not deleted. Six hours of partial
  output is worth keeping, and the name says what it is. The run summary names it, so
  a caller that reads the summary can find it.
* On Windows, ``os.replace`` fails with ``WinError 5`` if the target is open in
  another program -- POSIX allows it. That is reported as one clear line naming the
  partial file, rather than as a traceback from deep inside a writer.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import json
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import IO, Final

from gigaxml.errors import WriterError
from gigaxml.fields import FieldConfig, FieldType

__all__ = [
    "BATCH_SIZE_WARN_THRESHOLD",
    "DEFAULT_BATCH_SIZE",
    "PARTIAL_SUFFIX",
    "CsvWriter",
    "JsonlWriter",
    "ParquetWriter",
    "RowWriter",
    "WriterFormat",
    "batch_size_warning",
    "create_writer",
]

#: Rows held before a batch is flushed. Small enough to stay bounded, large enough
#: that per-batch overhead (a row group, a table build) is negligible.
DEFAULT_BATCH_SIZE: Final = 5_000

#: Rows per batch above which a warning is worth printing.
#:
#: Output-side memory is **linear in the batch size**: a buffered row costs about
#: **0.95 KB** for a six-field config (measured against ``tests/_mem.py``'s RSS
#: probe: 5 000 rows ~ 4.2 MiB, 100 000 ~ 89.8 MiB, 500 000 ~ 452.8 MiB). The
#: project's output-side budget is 32 MiB, which is reached at roughly 34 500
#: rows; this threshold fires earlier so that wider rows are still covered.
#:
#: Exceeding it is *warned about, never refused*: a config with very wide rows may
#: legitimately need a large batch, and what the user needs is to be told, not
#: blocked.
BATCH_SIZE_WARN_THRESHOLD: Final = 20_000

#: Measured resident memory per buffered row, in KB, for a six-field config.
_KB_PER_BUFFERED_ROW: Final = 0.95

#: Buffer for the text-mode handles, in bytes.
_WRITE_BUFFER_BYTES: Final = 1 << 16

#: Appended to the target name to get the path actually written to. Same directory
#: as the target on purpose: a rename across devices is a copy plus a delete, which
#: is not atomic, and ``tempfile``'s default directory is usually a different device.
#:
#: Public because a caller that only has the target path -- the run summary, for one
#: -- has to be able to work out where the partial output would be.
PARTIAL_SUFFIX: Final = ".tmp"

#: Suffix to format. ``.ndjson`` is accepted because it is the same thing under a
#: name half the ecosystem prefers.
_SUFFIX_TO_FORMAT: Final = {
    ".csv": "csv",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".parquet": "parquet",
    ".pq": "parquet",
}


class WriterFormat(Enum):
    """An output format, chosen from the file extension or explicitly."""

    CSV = "csv"
    JSONL = "jsonl"
    PARQUET = "parquet"

    @classmethod
    def from_name(cls, name: str) -> WriterFormat:
        """Look up a format by its ``--format`` name.

        Raises:
            WriterError: ``name`` is not a known format.
        """
        try:
            return cls(name.strip().lower())
        except ValueError as exc:
            known = sorted(member.value for member in cls)
            raise WriterError(f"unknown output format {name!r}; known formats are {known}") from exc

    @classmethod
    def maybe_from_path(cls, path: str | Path) -> WriterFormat | None:
        """Infer the format from a file extension, or ``None`` if it is unknown.

        Used where an unknown extension is not an error -- an explicit
        ``--format`` makes the extension irrelevant.
        """
        name = _SUFFIX_TO_FORMAT.get(Path(path).suffix.lower())
        return None if name is None else cls(name)

    @classmethod
    def from_path(cls, path: str | Path) -> WriterFormat:
        """Infer the format from a file extension.

        Raises:
            WriterError: the extension does not name a supported format.
        """
        suffix = Path(path).suffix.lower()
        inferred = cls.maybe_from_path(path)
        if inferred is None:
            known = sorted(_SUFFIX_TO_FORMAT)
            raise WriterError(
                f"cannot infer an output format from {str(path)!r} (suffix {suffix!r}); "
                f"use one of {known}, or pass --format explicitly"
            )
        return inferred


class RowWriter(ABC):
    """Accumulate rows into batches and flush each batch as it fills.

    Use as a context manager, or call :meth:`close` yourself -- an unclosed writer
    leaves its last partial batch unwritten.

    **``batch_size`` decides output-side memory.** A batch holds that many rows in
    memory before it is written, so resident memory grows linearly with it: about
    **0.95 KB per buffered row** for a six-field config (measured: 5 000 rows
    ~ 4.2 MiB, 100 000 ~ 89.8 MiB, 500 000 ~ 452.8 MiB). The default of 5 000 keeps
    a run well inside this project's 32 MiB output budget; raising it to hundreds
    of thousands does not. See :func:`batch_size_warning`.

    The file is written to ``<path>.tmp`` and renamed into place by :meth:`close`.
    See the module docstring for what that guarantees and why the partial file is
    kept on failure.

    Args:
        path: the output file -- the *target*, which is what :attr:`path` and every
            error message name. Parent directories are not created.
        fields: the configured fields, in output order. They decide the CSV header,
            the JSONL key order and the Parquet schema.
        batch_size: rows per flush. Must be positive.
        header: whether a CSV writer writes its header row. Only CSV has one, and only
            the first part of a checkpointed run wants it -- see
            :func:`create_writer`.

    Raises:
        WriterError: ``batch_size`` is not positive, or the output cannot be opened.
    """

    def __init__(
        self,
        path: str | Path,
        fields: Sequence[FieldConfig],
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        header: bool = True,
    ) -> None:
        if batch_size < 1:
            raise WriterError(f"batch_size must be at least 1, got {batch_size!r}")
        if not fields:
            raise WriterError("a writer needs at least one field to write")

        self._path = Path(path)
        self._partial_path = self._path.with_name(self._path.name + PARTIAL_SUFFIX)
        self._published = False
        self._fields = tuple(fields)
        self._field_names = tuple(field.name for field in self._fields)
        self._field_name_set = frozenset(self._field_names)
        self._batch_size = batch_size
        self._header = header
        self._batch: list[Mapping[str, object]] = []
        self._rows_written = 0
        self._closed = False
        self._failed = False
        self._open()

    @property
    def path(self) -> Path:
        """The output file -- the target, not the partial file being written."""
        return self._path

    @property
    def partial_path(self) -> Path:
        """Where rows are written before :meth:`close` moves them into place.

        Exists on disk exactly when the run did not finish. A caller looking at a
        failed run should look here for whatever partial output there is.
        """
        return self._partial_path

    @property
    def published(self) -> bool:
        """Whether the finished output has been moved onto the target path.

        ``True`` only after :meth:`close` has replaced the target. A run that failed
        leaves this ``False`` and the target untouched.
        """
        return self._published

    @property
    def field_names(self) -> tuple[str, ...]:
        """The configured field names, in output order."""
        return self._field_names

    @property
    def rows_written(self) -> int:
        """Rows flushed so far. Rows still sitting in the current batch are not counted."""
        return self._rows_written

    def write(self, row: Mapping[str, object]) -> None:
        """Add one row, flushing if the batch is full."""
        if self._closed:
            raise WriterError(f"writer for {str(self._path)!r} is already closed")
        if self._failed:
            raise WriterError(
                f"writer for {str(self._path)!r} failed on an earlier batch and is unusable"
            )
        self._batch.append(row)
        if len(self._batch) >= self._batch_size:
            self._flush_batch()

    def write_all(self, rows: Iterable[Mapping[str, object]]) -> int:
        """Add many rows and return the number of rows **accepted**.

        "Accepted" means handed to :meth:`write`, whether or not the batch they
        landed in has been flushed: rows still sitting in the current batch are
        counted, because they are already committed to being written. This is
        deliberately **not** the same number as :attr:`rows_written`, which counts
        only what has reached the file.

        Reading those two as interchangeable is exactly what made an earlier
        version of this method lie: its docstring said "including the last batch"
        and its test name said the same, while the code returned the flushed count
        only. The behaviour was right; the name and the docstring were wrong, and
        a wrong contract is worse than a bug because it is believed.
        """
        for row in rows:
            self.write(row)
        return self._rows_written + len(self._batch)

    def close(self) -> None:
        """Flush the final batch, release the file, and move it onto the target.

        This is the "the run finished" path: the partial file becomes the output.
        When the block raised, :meth:`__exit__` calls :meth:`abandon` instead, which
        leaves the target alone.

        Idempotent. If an earlier flush failed, the pending batch is dropped rather
        than retried: the run has already reported an error, and raising a second one
        from ``close`` would replace a useful message with a confusing one.
        """
        if self._closed:
            return
        try:
            if not self._failed:
                self._flush_batch()
        finally:
            # The handle is released either way. On the failure path this is what
            # leaves a complete, readable .tmp behind.
            self._close()
            self._closed = True
        if not self._failed:
            self._publish()

    def _publish(self) -> None:
        """Move the finished partial file onto the target. The only step that does.

        **This assumes the rename is atomic, which holds only within one filesystem.**
        The partial file is therefore created beside the target rather than in a
        temporary directory, so the two are on the same volume by construction.
        ``os.replace`` across devices is not a rename at all: the implementation falls
        back to copying the bytes and then deleting the source, and a crash during the
        copy leaves a target that is neither the old file nor the new one -- exactly the
        state this mechanism exists to make impossible. Nothing here checks for that
        case, because the only way to reach it is to put the partial file somewhere
        else on purpose.

        Raises:
            WriterError: the target cannot be replaced -- on Windows most often
                because another program has it open.
        """
        try:
            self._partial_path.replace(self._path)
        except OSError as exc:
            raise WriterError(
                f"could not move the finished output onto {str(self._path)!r}: {exc} "
                f"The complete output is still at {str(self._partial_path)!r}. If the "
                f"target is open in another program, close it and rename that file by "
                f"hand."
            ) from exc
        self._published = True

    def __enter__(self) -> RowWriter:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        # An exception leaving the block means the run did not finish, so the partial
        # file must NOT be moved onto the target -- publishing here would overwrite
        # the previous output with a truncated one, which is the exact failure this
        # whole mechanism exists to prevent.
        if exc_info[0] is None:
            self.close()
        else:
            self.abandon()
        return False

    def abandon(self) -> None:
        """Finish writing but leave the partial file where it is.

        Called when the enclosing block raised. Everything successfully written so
        far is flushed -- it is real output and worth keeping -- but the target path
        is not touched, so whatever was there before the run is still there after it.

        Idempotent, and safe to call on a writer that already failed. Final, too: a
        later :meth:`close` is a no-op, because publishing after an abandonment is the
        one thing this must not do.
        """
        if self._closed:
            return
        try:
            if not self._failed:
                self._flush_batch()
        except Exception:
            # We are already unwinding an exception; a second one from here would
            # replace a useful message with a confusing one.
            self._failed = True
        finally:
            self._close()
            self._closed = True

    def _flush_batch(self) -> None:
        """Validate and write the current batch, then clear it."""
        if not self._batch:
            return
        try:
            self._check_batch()
            self._write_batch(self._batch)
        except Exception:
            # Leave the batch in place but mark the writer unusable, so a later
            # close() cannot re-raise the same failure.
            self._failed = True
            raise
        self._rows_written += len(self._batch)
        self._batch = []

    def _check_batch(self) -> None:
        """Fail loudly if a row's keys do not match the configured fields.

        A missing key would shift every later column in CSV, and an extra one would
        be dropped silently by JSONL -- both produce a file that looks fine and is
        wrong, so this is checked rather than tolerated.
        """
        for index, row in enumerate(self._batch):
            keys = set(row)
            if keys != self._field_name_set:
                missing = sorted(self._field_name_set - keys)
                unexpected = sorted(keys - self._field_name_set)
                raise WriterError(
                    f"row {index} of the current batch does not match the configured "
                    f"fields: missing {missing}, unexpected {unexpected}; "
                    f"configured fields are {list(self._field_names)}"
                )

    @abstractmethod
    def _open(self) -> None:
        """Prepare the output (open the handle, build the schema)."""

    @abstractmethod
    def _write_batch(self, batch: Sequence[Mapping[str, object]]) -> None:
        """Write one non-empty batch."""

    @abstractmethod
    def _close(self) -> None:
        """Release the output."""


class _TextWriter(RowWriter):
    """Shared handle handling for the two line-oriented formats."""

    _handle: IO[str]

    def _open(self) -> None:
        try:
            self._handle = self._partial_path.open(
                "w", encoding="utf-8", newline="", buffering=_WRITE_BUFFER_BYTES
            )
        except OSError as exc:
            raise WriterError(
                f"cannot open {str(self._partial_path)!r} for writing: {exc}"
            ) from exc

    def _close(self) -> None:
        self._handle.close()


class CsvWriter(_TextWriter):
    """Write rows as CSV, header in configured field order.

    Values are rendered so that reading the file back and re-coercing it with the
    same config reproduces the original value: ``None`` becomes an empty field,
    ``Decimal`` keeps its trailing zeros, dates are ISO, and booleans are
    ``true``/``false`` -- the same spellings :func:`gigaxml.fields.coerce_value`
    accepts.
    """

    def _open(self) -> None:
        super()._open()
        self._writer = csv.writer(self._handle)
        if self._header:
            self._writer.writerow(self._field_names)

    def _write_batch(self, batch: Sequence[Mapping[str, object]]) -> None:
        self._writer.writerows(
            [self._render(row.get(name)) for name in self._field_names] for row in batch
        )

    @staticmethod
    def _render(value: object) -> str:
        """Render one value as CSV text."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, decimal.Decimal):
            # str() keeps the scale: Decimal("49.90") is not "49.9".
            return str(value)
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        return str(value)


class JsonlWriter(_TextWriter):
    """Write one JSON object per line, keys in configured field order.

    ``Decimal`` becomes a string and dates become ISO strings, so nothing is
    written as a JSON number that would come back as a float.
    """

    def _write_batch(self, batch: Sequence[Mapping[str, object]]) -> None:
        dumps = json.dumps
        default = _json_default
        names = self._field_names
        write = self._handle.write
        for row in batch:
            ordered = {name: row.get(name) for name in names}
            write(dumps(ordered, default=default, ensure_ascii=False))
            write("\n")

    def _close(self) -> None:
        self._handle.close()


def _json_default(value: object) -> str:
    """Serialise the two types :mod:`json` does not know about.

    Both are rendered as strings rather than numbers on purpose: a ``Decimal``
    written as a JSON number would be read back as a float, and a date has no JSON
    representation at all.
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    raise TypeError(f"object of type {type(value).__name__} is not JSON serialisable: {value!r}")


class ParquetWriter(RowWriter):
    """Write Parquet with a schema taken from the config and one row group per batch.

    See the module docstring for why ``decimal`` maps to a string and why the
    schema is never inferred.
    """

    def _open(self) -> None:
        self._pa, parquet = _import_pyarrow()
        self._schema = _parquet_schema(self._fields, self._pa)
        try:
            self._parquet_writer = parquet.ParquetWriter(self._partial_path, self._schema)
        except OSError as exc:
            raise WriterError(
                f"cannot open {str(self._partial_path)!r} for writing: {exc}"
            ) from exc

    def _write_batch(self, batch: Sequence[Mapping[str, object]]) -> None:
        columns: dict[str, list[object]] = {}
        for field in self._fields:
            name = field.name
            if field.type is FieldType.DECIMAL:
                # Exact text, so nothing is rounded on the way into the file.
                columns[name] = [None if row[name] is None else str(row[name]) for row in batch]
            else:
                columns[name] = [row[name] for row in batch]

        table = self._pa.Table.from_pydict(columns, schema=self._schema)
        # One row group per batch: the whole point is not to build the full table.
        self._parquet_writer.write_table(table)

    def _close(self) -> None:
        self._parquet_writer.close()

    @property
    def schema(self) -> object:
        """The Arrow schema this writer commits to, for inspection and tests."""
        return self._schema


def _import_pyarrow() -> tuple[object, object]:
    """Import ``pyarrow`` and ``pyarrow.parquet``, or explain how to get them."""
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise WriterError(
            "writing Parquet needs the optional 'pyarrow' dependency; install it with "
            "`pip install 'gigaxml[parquet]'` or `pip install pyarrow`"
        ) from exc
    return pyarrow, pyarrow.parquet


def _parquet_schema(fields: Sequence[FieldConfig], pa: object) -> object:
    """Build the Arrow schema from the configured field types.

    Every field is nullable: a missing optional field is a legitimate ``None``, and
    making columns non-nullable would turn that into a run-ending error.
    """
    arrow_types = {
        FieldType.STRING: pa.string(),
        FieldType.INT: pa.int64(),
        FieldType.FLOAT: pa.float64(),
        # Not decimal128: see the module docstring. Exact, and needs no precision guess.
        FieldType.DECIMAL: pa.string(),
        FieldType.BOOL: pa.bool_(),
        FieldType.DATE: pa.date32(),
    }
    return pa.schema([pa.field(field.name, arrow_types[field.type]) for field in fields])


def create_writer(
    path: str | Path,
    fields: Sequence[FieldConfig],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
    output_format: WriterFormat | str | None = None,
    header: bool = True,
) -> RowWriter:
    """Build the writer for ``path``, inferring the format from its extension.

    Args:
        path: output file.
        fields: configured fields, in output order.
        batch_size: rows per flush. See :func:`batch_size_warning` for how this
            trades against resident memory.
        output_format: force a format instead of inferring it from the extension.
            Accepts a :class:`WriterFormat` or its name.
        header: whether a CSV writer writes its header row. Parts of a checkpointed
            run are meant to be concatenated in order, and a header repeated in every
            part would put header rows in the middle of the data -- so only the first
            part asks for one. Ignored by JSONL and Parquet, which have no header.

    Returns:
        A writer that is already open; call :meth:`RowWriter.close` or use it as a
        context manager.

    Raises:
        WriterError: the format is unknown or the output cannot be opened.
    """
    if output_format is None:
        writer_format = WriterFormat.from_path(path)
    elif isinstance(output_format, WriterFormat):
        writer_format = output_format
    else:
        writer_format = WriterFormat.from_name(output_format)

    writers = {
        WriterFormat.CSV: CsvWriter,
        WriterFormat.JSONL: JsonlWriter,
        WriterFormat.PARQUET: ParquetWriter,
    }
    return writers[writer_format](path, fields, batch_size=batch_size, header=header)


def batch_size_warning(batch_size: int) -> str | None:
    """Return a warning for a batch size that threatens the memory budget, else ``None``.

    A warning rather than an error on purpose: a config whose rows are very wide
    may legitimately want a large batch, and the useful thing to give that user is
    information, not a refusal.
    """
    if batch_size <= BATCH_SIZE_WARN_THRESHOLD:
        return None
    estimated_mb = batch_size * _KB_PER_BUFFERED_ROW / 1024
    return (
        f"--batch-size {batch_size} buffers up to {batch_size:,} rows in memory "
        f"(~{estimated_mb:,.0f} MiB at the measured ~{_KB_PER_BUFFERED_ROW} KB per row for a "
        f"6-field config), which exceeds this project's 32 MiB output-side target. "
        f"The default {DEFAULT_BATCH_SIZE:,} is inside it; lower it unless the rows are "
        f"very wide."
    )
