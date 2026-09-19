"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence

from gigaxml import __version__
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import GigaXMLError
from gigaxml.fields import extract_record
from gigaxml.generate import generate_dataset
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.writers import DEFAULT_BATCH_SIZE, RowWriter, WriterFormat, create_writer

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
        help=f"Rows per write batch (default {DEFAULT_BATCH_SIZE}).",
    )
    extract.set_defaults(handler=_handle_extract)

    return parser


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
