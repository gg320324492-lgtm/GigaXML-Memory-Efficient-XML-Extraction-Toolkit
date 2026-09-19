"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from lxml import etree

from gigaxml import __version__
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import GigaXMLError
from gigaxml.fields import extract_record
from gigaxml.generate import generate_dataset
from gigaxml.inspect import (
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_PATHS,
    generate_config,
    inspect_document,
)
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.sample import sample_records
from gigaxml.writers import (
    BATCH_SIZE_WARN_THRESHOLD,
    DEFAULT_BATCH_SIZE,
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
        help="Emit the report as JSON instead of human-readable text.",
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
    writer = create_writer(
        args.output,
        config.fields,
        batch_size=args.batch_size,
        output_format=args.format,
    )
    record_path = config.record_path
    namespaces = config.namespaces or None

    with writer:
        for record in StreamingRecordReader(args.source, record_path, namespaces):
            writer.write(extract_record(record, config.fields).values)

    print(json.dumps(_extract_summary(args, config, writer), indent=2))
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
    """Handle ``gigaxml sample``."""
    config = load_config(args.config)
    _warn_on_format_mismatch(args.output, args.format)
    _warn_on_batch_size(args.batch_size)
    result = sample_records(
        args.source,
        config,
        args.output,
        limit=args.limit,
        batch_size=args.batch_size,
        output_format=args.format,
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0


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
