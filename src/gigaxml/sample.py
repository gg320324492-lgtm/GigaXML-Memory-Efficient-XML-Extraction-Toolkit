"""``sample``: write the first N records of a document, and say what that means.

Sampling exists so a user can look at real rows before committing to a full run.
The sampling here is deliberately **deterministic and explained**: it takes the
first N records in document order, so the same input always produces byte-identical
output, and the result says out loud that "the first N" is a biased slice --
documents routinely carry structurally different records at the head or the tail
(a header block, a trailing summary row), and a sample drawn from the front will
not show them.

Nothing random is introduced. A random sample would be harder to reproduce and no
more honest unless the seed and the draw were both reported, and a reader who wants
a spread can take the first N and the last N and compare.

The extraction loop itself lives in :mod:`gigaxml.run`, shared with ``extract``, so
that ``on_error: quarantine`` means the same thing to both. A config that says
"quarantine" and a ``sample`` that still dies on the first bad record would be a trap.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gigaxml.config import ExtractionConfig
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import RejectionLog, consume_records
from gigaxml.writers import DEFAULT_BATCH_SIZE, RowWriter, WriterFormat, create_writer

__all__ = ["SampleResult", "sample_records"]


@dataclass(frozen=True, slots=True)
class SampleResult:
    """What one :func:`sample_records` call did."""

    source: str
    output: str
    output_format: str
    requested: int
    written: int
    document_exhausted: bool
    note: str
    batch_size: int
    rejected: int = 0
    rejected_path: str | None = None

    @property
    def short_of_request(self) -> bool:
        """``True`` when the document had fewer records than were asked for."""
        return self.written < self.requested

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view."""
        return {
            "source": self.source,
            "output": self.output,
            "format": self.output_format,
            "requested": self.requested,
            "written": self.written,
            "document_exhausted": self.document_exhausted,
            "short_of_request": self.short_of_request,
            "note": self.note,
            "batch_size": self.batch_size,
            "rejected": self.rejected,
            "rejected_path": self.rejected_path,
        }


def sample_records(
    source: str | Path,
    config: ExtractionConfig,
    output: str | Path,
    *,
    limit: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    output_format: WriterFormat | str | None = None,
    writer: RowWriter | None = None,
    rejections: RejectionLog | None = None,
) -> SampleResult:
    """Write the first ``limit`` records of ``source`` to ``output``.

    Args:
        source: XML file, optionally gzipped.
        config: the extraction config, including the error policy.
        output: output file; the extension picks the writer unless ``output_format``
            says otherwise.
        limit: how many records to write. Must be at least 1.
        batch_size: rows per write batch.
        output_format: force the output format.
        writer: an already-open writer to use instead of creating one. It is closed
            here either way, but passing one in lets a caller read ``rows_written``
            after a failure -- which is how a run report can say how much output
            exists when a run does not finish.
        rejections: where to log records rejected under
            :attr:`~gigaxml.config.ErrorPolicy.QUARANTINE`.

    Returns:
        A :class:`SampleResult` whose ``note`` states, in words, which slice was
        taken and whether the document ran out first.

    Raises:
        ValueError: ``limit`` is below 1. A programming error rather than a data
            one, so it is not wrapped in the :class:`~gigaxml.errors.GigaXMLError`
            hierarchy; the CLI rejects it before reaching here.
        OSError: the source cannot be read or the output cannot be written.
    """
    if limit < 1:
        raise ValueError(f"limit must be at least 1, got {limit!r}")

    reader = StreamingRecordReader(source, config.record_path, config.namespaces or None)
    if writer is None:
        writer = create_writer(
            output,
            config.fields,
            batch_size=batch_size,
            output_format=output_format,
        )

    with writer:
        stats = consume_records(reader, config, writer, limit=limit, rejections=rejections)

    written = writer.rows_written
    # The loop reads one record past the limit to establish that more exist; not
    # seeing that record means the document ended, which is what makes the
    # "short of request" note a fact rather than a guess.
    document_exhausted = not stats.limit_hit
    return SampleResult(
        source=str(source),
        output=str(writer.path),
        output_format=(
            output_format.value
            if isinstance(output_format, WriterFormat)
            else output_format or WriterFormat.from_path(output).value
        ),
        requested=limit,
        written=written,
        document_exhausted=document_exhausted,
        note=_sample_note(written, limit, document_exhausted, stats.rejected),
        batch_size=batch_size,
        rejected=stats.rejected,
        rejected_path=stats.rejected_path,
    )


def _sample_note(
    written: int,
    requested: int,
    document_exhausted: bool,
    rejected: int = 0,
) -> str:
    """Say plainly which slice was taken, and whether the document ran out."""
    if document_exhausted and written < requested:
        note = (
            f"the document holds only {written:,} record(s), fewer than the "
            f"{requested:,} requested; all of them were written"
        )
    else:
        note = (
            f"the first {written:,} record(s) in document order. This slice is BIASED: "
            f"documents often carry structurally different records at the head or tail, "
            f"so a sample from the front will not show them."
        )
    if rejected:
        note += f" {rejected:,} record(s) were rejected and are not in this sample."
    return note
