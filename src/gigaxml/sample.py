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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from gigaxml.config import ExtractionConfig
from gigaxml.fields import extract_record
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.writers import DEFAULT_BATCH_SIZE, WriterFormat, create_writer

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
        }


def sample_records(
    source: str | Path,
    config: ExtractionConfig,
    output: str | Path,
    *,
    limit: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    output_format: WriterFormat | str | None = None,
) -> SampleResult:
    """Write the first ``limit`` records of ``source`` to ``output``.

    Args:
        source: XML file, optionally gzipped.
        config: the extraction config.
        output: output file; the extension picks the writer unless ``output_format``
            says otherwise.
        limit: how many records to write. Must be at least 1.
        batch_size: rows per write batch.
        output_format: force the output format.

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
    writer = create_writer(
        output,
        config.fields,
        batch_size=batch_size,
        output_format=output_format,
    )

    written = 0
    document_exhausted = True
    with writer:
        for record in reader:
            if written == limit:
                # One record past the limit was yielded to find out that there is
                # more; it is discarded, which is what makes the "short of request"
                # note below trustworthy rather than a guess.
                document_exhausted = False
                break
            writer.write(extract_record(record, config.fields).values)
            written += 1

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
        note=_sample_note(written, limit, document_exhausted),
        batch_size=batch_size,
    )


def _sample_note(written: int, requested: int, document_exhausted: bool) -> str:
    """Say plainly which slice was taken, and whether the document ran out."""
    if document_exhausted and written < requested:
        return (
            f"the document holds only {written:,} record(s), fewer than the "
            f"{requested:,} requested; all of them were written"
        )
    return (
        f"the first {written:,} record(s) in document order. This slice is BIASED: "
        f"documents often carry structurally different records at the head or tail, "
        f"so a sample from the front will not show them."
    )
