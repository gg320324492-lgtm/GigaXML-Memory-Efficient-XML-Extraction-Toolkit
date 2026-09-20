"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from lxml import etree

from gigaxml import __version__
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import GigaXMLError
from gigaxml.generate import generate_dataset
from gigaxml.inspect import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PATHS,
    generate_config,
    inspect_document,
)
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import (
    DEFAULT_REJECTION_FILENAME,
    DEFAULT_RUN_REPORT_FILENAME,
    RejectionLog,
    RunStats,
    consume_records,
    elapsed_since,
)
from gigaxml.sample import sample_records
from gigaxml.writers import (
    BATCH_SIZE_WARN_THRESHOLD,
    DEFAULT_BATCH_SIZE,
    PARTIAL_SUFFIX,
    RowWriter,
    WriterFormat,
    batch_size_warning,
    create_writer,
)

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="gigaxml",
        description="Memory-efficient XML extraction toolkit.",
    )
    parser.add_argument("--version", action="version", version=f"gigaxml {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate",
        help="Generate a deterministic synthetic XML dataset.",
        description="Generate a deterministic synthetic XML dataset plus a manifest.",
    )
    generate.add_argument("--size", required=True, help="Target size, e.g. 10MB, 1GB.")
    generate.add_argument("--seed", type=int, default=0, help="Deterministic seed.")
    generate.add_argument("-o", "--output", required=True, help="Output XML path.")
    generate.add_argument(
        "--namespace",
        default=None,
        help="Emit a default-namespace variant using this URI.",
    )
    generate.set_defaults(handler=_handle_generate)

    extract = subparsers.add_parser(
        "extract",
        help="Extract records from an XML file into CSV, JSONL or Parquet.",
        description=(
            "Stream records out of an XML file and write them in batches. "
            "Memory stays flat on both sides: the reader releases each record as "
            "the next one arrives, and the writer flushes each batch as it fills."
        ),
    )
    extract.add_argument("source", help="Input XML file (optionally gzipped).")
    extract.add_argument(
        "-c",
        "--config",
        required=True,
        help="YAML config describing the record path, namespaces and fields.",
    )
    extract.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output file. The extension picks the format (.csv, .jsonl, .parquet).",
    )
    extract.add_argument(
        "--format",
        default=None,
        choices=[member.value for member in WriterFormat],
        help="Force the output format instead of inferring it from the extension.",
    )
    extract.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=(
            f"Rows per write batch (default {DEFAULT_BATCH_SIZE}). Output-side memory is "
            f"linear in this: about 0.95 KB per buffered row for a 6-field config, so "
            f"{DEFAULT_BATCH_SIZE} rows is ~5 MiB while 100000 is ~90 MiB. A value above "
            f"{BATCH_SIZE_WARN_THRESHOLD} warns."
        ),
    )
    extract.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help=(
            "Where to write the machine-readable run summary. Defaults to "
            "run-report.json beside --output. Written on success *and* on failure, so "
            "a caller can tell a partial output from a complete one. It is a side "
            "artefact: if it cannot be written the run still succeeds, with a warning, "
            "because the exit code reports whether the data is usable."
        ),
    )
    extract.set_defaults(handler=_handle_extract)

    inspect = subparsers.add_parser(
        "inspect",
        help="Report the structure of an XML file and propose record paths.",
        description=(
            "Walk an XML file once, without knowing the record path in advance, and "
            "report every element path it contains, how often each repeats, and which "
            "paths look like repeating records. Memory stays flat: elements are "
            "released as they end. Use --generate-config to turn a candidate into a "
            "runnable config."
        ),
    )
    inspect.add_argument("source", help="Input XML file (optionally gzipped).")
    inspect.add_argument(
        "--json",
        action="store_true",
        help=(
            "Emit the report as JSON instead of human-readable text. To tell whether "
            "the top candidate sits inside another one, read "
            "`candidates[*].nested_inside` -- it names the containing path, or is null. "
            "Do not parse the stderr warning: that is written for a human reading a "
            "terminal, and its wording is not a contract."
        ),
    )
    inspect.add_argument(
        "--max-paths",
        type=_positive_int,
        default=DEFAULT_MAX_PATHS,
        help=(
            f"Distinct paths tracked before the table stops growing "
            f"(default {DEFAULT_MAX_PATHS:,}). Hitting the cap is reported, never silent."
        ),
    )
    inspect.add_argument(
        "--max-depth",
        type=_positive_int,
        default=DEFAULT_MAX_DEPTH,
        help=(
            f"Nesting depth beyond which paths are counted but not tracked "
            f"(default {DEFAULT_MAX_DEPTH})."
        ),
    )
    inspect.add_argument(
        "--generate-config",
        metavar="PATH",
        default=None,
        help="Write a runnable YAML config for a candidate to PATH.",
    )
    inspect.add_argument(
        "--candidate",
        type=_positive_int,
        default=1,
        help="Which candidate to generate a config for, 1-based (default 1, the highest score).",
    )
    inspect.add_argument(
        "--infer-types",
        action="store_true",
        help=(
            "With --generate-config, narrow int/float/bool/date from sampled values. "
            "Off by default: the default product is all string, which is lossless. "
            "`decimal` is never inferred."
        ),
    )
    inspect.set_defaults(handler=_handle_inspect)

    sample = subparsers.add_parser(
        "sample",
        help="Write the first N records of a file, using a config.",
        description=(
            "Write a deterministic slice of a document so real rows can be inspected "
            "before committing to a full run. The slice is the first N records in "
            "document order -- reproducible, and biased, which the report says."
        ),
    )
    sample.add_argument("source", help="Input XML file (optionally gzipped).")
    sample.add_argument("-c", "--config", required=True, help="YAML config to apply.")
    sample.add_argument(
        "-n",
        "--limit",
        type=_positive_int,
        required=True,
        help="How many records to write.",
    )
    sample.add_argument(
        "-o",
        "--output",
        required=True,
        help="Output file. The extension picks the format (.csv, .jsonl, .parquet).",
    )
    sample.add_argument(
        "--format",
        default=None,
        choices=[member.value for member in WriterFormat],
        help="Force the output format instead of inferring it from the extension.",
    )
    sample.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Rows per write batch (default {DEFAULT_BATCH_SIZE}).",
    )
    sample.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help=(
            "Where to write the machine-readable run summary. Defaults to "
            "run-report.json beside --output. Written on success *and* on failure. It "
            "is a side artefact: if it cannot be written the run still succeeds, with "
            "a warning."
        ),
    )
    sample.set_defaults(handler=_handle_sample)

    return parser


def _positive_int(text: str) -> int:
    """An ``argparse`` type for a strictly positive integer."""
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _handle_generate(args: argparse.Namespace) -> int:
    """Handle ``gigaxml generate``."""
    manifest = generate_dataset(
        args.output,
        size=args.size,
        seed=args.seed,
        namespace=args.namespace,
    )
    print(json.dumps(manifest.to_dict(), indent=2))
    return 0


def _handle_extract(args: argparse.Namespace) -> int:
    """Handle ``gigaxml extract``."""
    config = load_config(args.config)
    _warn_on_format_mismatch(args.output, args.format)
    _warn_on_batch_size(args.batch_size)

    report_path = _run_report_path(args)
    started = time.perf_counter()
    writer: RowWriter | None = None
    rejections = _rejection_log(args.output)

    try:
        # Creating the writer is inside the try on purpose: a run that cannot even
        # open its output should still leave a summary saying so.
        writer = create_writer(
            args.output,
            config.fields,
            batch_size=args.batch_size,
            output_format=args.format,
        )
        with rejections, writer:
            reader = StreamingRecordReader(
                args.source, config.record_path, config.namespaces or None
            )
            stats = consume_records(reader, config, writer, rejections=rejections)
    except (GigaXMLError, OSError, etree.XMLSyntaxError) as exc:
        # The summary goes out before the error is re-raised. Without it, a caller who
        # only sees a non-zero exit code has no way to tell a partial output from a
        # complete one -- which is the failure this phase exists to remove.
        _write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=writer,
            rejections=rejections,
            error=exc,
            started=started,
        )
        raise

    _write_report_safely(
        report_path,
        args=args,
        config=config,
        writer=writer,
        rejections=rejections,
        error=None,
        started=started,
    )
    print(json.dumps(_extract_summary(args, config, writer), indent=2))
    _report_rejections(stats)
    return 0


def _warn_on_batch_size(batch_size: int) -> None:
    """Warn when a batch size would push output-side memory past the project budget."""
    warning = batch_size_warning(batch_size)
    if warning is not None:
        print(f"warning: {warning}", file=sys.stderr)


def _handle_inspect(args: argparse.Namespace) -> int:
    """Handle ``gigaxml inspect``."""
    report = inspect_document(
        args.source,
        max_paths=args.max_paths,
        max_depth=args.max_depth,
        collect_values=args.infer_types,
    )

    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.to_text())

    if args.generate_config is not None:
        text = generate_config(
            report,
            candidate_index=args.candidate,
            infer_types=args.infer_types,
        )
        target = Path(args.generate_config)
        target.write_text(text, encoding="utf-8")
        # On stderr so that --json output on stdout stays parseable.
        print(
            f"wrote a config for candidate {args.candidate} "
            f"({report.candidates[args.candidate - 1].path}) to {target}",
            file=sys.stderr,
        )
    return 0


def _handle_sample(args: argparse.Namespace) -> int:
    """Handle ``gigaxml sample``.

    Shares the extraction loop with ``extract``, so ``on_error: quarantine`` means
    the same thing here. It also writes the same run report, for the same reason: a
    ``sample`` that aborts leaves the same indistinguishable half-file.
    """
    config = load_config(args.config)
    _warn_on_format_mismatch(args.output, args.format)
    _warn_on_batch_size(args.batch_size)

    report_path = _run_report_path(args)
    started = time.perf_counter()
    writer: RowWriter | None = None
    rejections = _rejection_log(args.output)

    try:
        writer = create_writer(
            args.output,
            config.fields,
            batch_size=args.batch_size,
            output_format=args.format,
        )
        with rejections:
            result = sample_records(
                args.source,
                config,
                args.output,
                limit=args.limit,
                batch_size=args.batch_size,
                output_format=args.format,
                writer=writer,
                rejections=rejections,
            )
    except (GigaXMLError, OSError, etree.XMLSyntaxError) as exc:
        _write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=writer,
            rejections=rejections,
            error=exc,
            started=started,
        )
        raise

    _write_report_safely(
        report_path,
        args=args,
        config=config,
        writer=writer,
        rejections=rejections,
        error=None,
        started=started,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


def _rejection_log(output: str | Path) -> RejectionLog:
    """The rejection log, which sits beside the output file."""
    return RejectionLog(Path(output).parent / DEFAULT_REJECTION_FILENAME)


def _run_report_path(args: argparse.Namespace) -> Path:
    """Where the run report goes: ``--report`` if given, else beside the output."""
    if args.report is not None:
        return Path(args.report)
    return Path(args.output).parent / DEFAULT_RUN_REPORT_FILENAME


def _partial_output_path(output: str | Path) -> Path:
    """Where a run writes before moving the file into place.

    Derived from the target rather than asked of the writer, so it still works when
    the run failed before a writer existed -- an unreadable config, say -- and the
    summary still has something to say about partial output.
    """
    target = Path(output)
    return target.with_name(target.name + PARTIAL_SUFFIX)


def _run_report_payload(
    args: argparse.Namespace,
    config: ExtractionConfig,
    writer: RowWriter | None,
    rejections: RejectionLog,
    error: BaseException | None,
    started: float,
) -> dict[str, object]:
    """The run summary, which is written whether the run finished or not.

    ``output_complete`` is the field this exists for: it is true only once the
    finished file has actually been moved onto the target path. A run that aborts
    leaves whatever it managed to write in a ``.tmp`` beside the target, and the
    target itself keeps whatever it had -- so a caller reading this can tell a
    complete output from a partial one without parsing stderr, and can find the
    partial output if it wants it.
    """
    written_path = rejections.written_path
    partial = _partial_output_path(args.output)
    return {
        "status": "failed" if error is not None else "ok",
        "source": str(args.source),
        "output": str(args.output),
        "format": args.format
        if args.format is not None
        else WriterFormat.from_path(args.output).value,
        "record_path": config.record_path,
        "fields": list(config.field_names),
        "rows": 0 if writer is None else writer.rows_written,
        "rejected": rejections.count,
        "rejected_path": None if written_path is None else str(written_path),
        "error": None if error is None else {"type": type(error).__name__, "message": str(error)},
        "output_complete": writer is not None and writer.published,
        "partial_path": str(partial) if partial.exists() else None,
        "elapsed_seconds": elapsed_since(started),
        "tool_version": __version__,
    }


def _write_run_report(
    report_path: Path,
    *,
    args: argparse.Namespace,
    config: ExtractionConfig,
    writer: RowWriter | None,
    rejections: RejectionLog,
    error: BaseException | None,
    started: float,
) -> None:
    """Write the run summary. Raises ``OSError`` if the path cannot be written."""
    payload = _run_report_payload(args, config, writer, rejections, error, started)
    report_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_report_safely(report_path: Path, **fields: object) -> None:
    """Write the run summary, or say why it could not be written.

    Used on both paths. The summary is a side artefact: the exit code reports whether
    the *data* is usable, so a summary that cannot be written is a warning, not a
    failure -- and on the failing path it must not replace the real error.
    """
    try:
        _write_run_report(report_path, **fields)  # type: ignore[arg-type]
    except OSError as exc:
        print(
            f"warning: could not write the run report to {report_path}: {exc}",
            file=sys.stderr,
        )


def _report_rejections(stats: RunStats) -> None:
    """One line about quarantined records -- after the run, never per record.

    Per-record warnings would bury the terminal under a million lines on exactly the
    input quarantine is for. The per-record detail is in the rejection log.
    """
    if stats.rejected:
        print(
            f"warning: {stats.rejected:,} record(s) rejected; see {stats.rejected_path}",
            file=sys.stderr,
        )


def _warn_on_format_mismatch(output: str, requested: str | None) -> None:
    """Warn when ``--format`` disagrees with the extension it is overriding.

    ``--format`` winning is the documented behaviour, and an unknown extension
    needs it. But ``-o out.csv --format parquet`` writes Parquet bytes into a file
    called ``.csv``, which every downstream tool will misread -- worth one line on
    stderr rather than a silent surprise.
    """
    if requested is None:
        return
    inferred = WriterFormat.maybe_from_path(output)
    if inferred is not None and inferred.value != requested:
        print(
            f"warning: {output!r} has a {inferred.value!r} extension but "
            f"--format {requested!r} was given; writing {requested!r}",
            file=sys.stderr,
        )


def _extract_summary(
    args: argparse.Namespace,
    config: ExtractionConfig,
    writer: RowWriter,
) -> dict[str, object]:
    """The JSON the CLI prints when a run finishes."""
    return {
        "source": str(args.source),
        "output": str(writer.path),
        "format": args.format
        if args.format is not None
        else WriterFormat.from_path(args.output).value,
        "record_path": config.record_path,
        "fields": list(config.field_names),
        "rows": writer.rows_written,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``gigaxml`` console script."""
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        return handler(args)
    except GigaXMLError as exc:
        # Every deliberate error descends from GigaXMLError, so this turns a bad
        # config, a wrong path or a missing optional dependency into one readable
        # line instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        # A missing input file, an unwritable output directory, a bad gzip stream.
        # These are environmental rather than library errors, but a command-line
        # tool should still say what went wrong on one line.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except etree.XMLSyntaxError as exc:
        # Malformed XML. Neither a GigaXMLError nor an OSError, but it is the most
        # likely thing to go wrong with an unknown file, and a traceback helps nobody.
        print(f"error: {exc}", file=sys.stderr)
        return 1
