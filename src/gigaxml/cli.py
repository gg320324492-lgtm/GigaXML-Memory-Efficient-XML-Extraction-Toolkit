"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from itertools import chain, islice
from pathlib import Path

from lxml import etree

from gigaxml import __version__
from gigaxml.checkpoint import (
    CHECKPOINT_FILENAME,
    DEFAULT_PART_FORMAT,
    Checkpoint,
    PartRecord,
    config_identity,
    part_name,
    read_checkpoint,
    require_intact_parts,
    source_identity,
    validate_resume,
    write_checkpoint,
)
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import CheckpointError, GigaXMLError
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
        "--checkpoint-every",
        type=_positive_int,
        metavar="N",
        default=None,
        help=(
            "Commit the output in parts of N records each, into the directory given "
            "by --output, so an interrupted run can be continued with --resume. "
            "N decides how much work an interruption costs you, NOT how much memory "
            "the run uses: parts are written a batch at a time and never accumulate. "
            "Each part is written atomically, so a part is either complete or absent."
        ),
    )
    extract.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Continue a checkpointed run instead of starting a new one. This does NOT "
            "seek: XML cannot be re-entered mid-stream, so the source is parsed again "
            "from the beginning and the records already accounted for are skipped. "
            "Skipping is not free -- measured on 403 MB / 1,164,800 records, skipping "
            "everything costs 8.7s against 17.1s to extract and write it, so resuming "
            "saves roughly half of what you had already done: about 49%% of the total "
            "if you were 90%% through, about 5%% if you were 10%% through. Refused "
            "outright if the source or the config has changed since the checkpoint."
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
        "--checkpoint-every",
        type=_positive_int,
        metavar="N",
        default=None,
        help=argparse.SUPPRESS,
    )
    sample.add_argument(
        "--resume",
        action="store_true",
        help=argparse.SUPPRESS,
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

    checkpointing = args.checkpoint_every is not None
    report_path = _run_report_path(args, checkpoint=checkpointing)
    started = time.perf_counter()

    if args.resume and args.checkpoint_every is None:
        raise CheckpointError(
            "--resume needs --checkpoint-every too: the part size is a parameter of "
            "the run, and the checkpoint does not record it"
        )
    if checkpointing:
        return _extract_checkpointed(args, config, report_path, started)

    writer: RowWriter | None = None
    rejections = _rejection_log(Path(args.output).parent)

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
    if args.checkpoint_every is not None or args.resume:
        raise CheckpointError(
            "sample does not support --checkpoint-every or --resume. A sample is a "
            "quick look at the front of a document, not a run worth resuming; use "
            "extract for work that needs to survive an interruption."
        )

    config = load_config(args.config)
    _warn_on_format_mismatch(args.output, args.format)
    _warn_on_batch_size(args.batch_size)

    report_path = _run_report_path(args)
    started = time.perf_counter()
    writer: RowWriter | None = None
    rejections = _rejection_log(Path(args.output).parent)

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


def _rejection_log(directory: Path, *, append: bool = False, initial: int = 0) -> RejectionLog:
    """The rejection log for a run, which lives beside the output it belongs to."""
    return RejectionLog(directory / DEFAULT_REJECTION_FILENAME, append=append, initial=initial)


def _run_report_path(args: argparse.Namespace, *, checkpoint: bool = False) -> Path:
    """Where the run summary goes: ``--report`` if given, else with the output.

    "With the output" means the output's own directory, which differs by mode only
    because ``--output`` means different things: a file in the ordinary case, and the
    parts directory when checkpointing. In the second case the summary belongs inside
    that directory, alongside the manifest and the rejection log, so the directory is
    a complete account of the run rather than one file short of it.
    """
    if args.report is not None:
        return Path(args.report)
    output = Path(args.output)
    if checkpoint:
        return output / DEFAULT_RUN_REPORT_FILENAME
    return output.parent / DEFAULT_RUN_REPORT_FILENAME


def _extract_checkpointed(
    args: argparse.Namespace,
    config: ExtractionConfig,
    report_path: Path,
    started: float,
) -> int:
    """Handle ``extract --checkpoint-every``, optionally continuing a run.

    The output is a directory of parts plus a manifest. Each part is written by the
    same atomic writer as a single-file run, so a part is complete or absent; the
    manifest is rewritten atomically after each part is committed, so it never
    describes a part that is not there.
    """
    parts_dir = Path(args.output)
    if parts_dir.is_file():
        raise CheckpointError(
            f"--checkpoint-every needs --output to be a directory, but "
            f"{str(parts_dir)!r} is an existing file"
        )
    extension = args.format or DEFAULT_PART_FORMAT
    manifest = parts_dir / CHECKPOINT_FILENAME

    try:
        parts_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckpointError(
            f"cannot create the parts directory {str(parts_dir)!r}: {exc}"
        ) from exc

    resumed_from = 0
    parts: list[PartRecord] = []
    index = 0
    checkpoint: Checkpoint | None = None
    if args.resume:
        checkpoint = read_checkpoint(manifest)
        validate_resume(checkpoint, args.source, config)
        if checkpoint.parts:
            # Continue in the format the checkpoint was started in, whatever --format
            # says now: the parts already on disk are the ones being added to.
            extension = checkpoint.parts[-1].name.rsplit(".", 1)[-1]
        # The manifest is only worth trusting if the parts it names are still there.
        # `records_consumed` is what a resume skips, so a part that has been deleted
        # means walking straight past rows nobody will ever write -- and finishing
        # with a success. Refuse instead.
        require_intact_parts(checkpoint, parts_dir, extension)
        resumed_from = checkpoint.records_consumed
        parts = list(checkpoint.parts)
        index = len(parts)
    elif manifest.is_file():
        raise CheckpointError(
            f"a checkpoint already exists at {str(manifest)!r}. Use --resume to "
            f"continue that run, or remove the directory to start a fresh one -- "
            f"writing over it would throw away work that has already been committed."
        )

    if checkpoint is not None and checkpoint.complete:
        # Reached only when the parts agree, because require_intact_parts has already
        # run: a complete checkpoint whose parts are gone is refused above rather than
        # waved through with a warning. The user is about to use that output, and a
        # warning on a command that exits 0 is the kind of thing that gets missed --
        # which is the same reasoning that made the incomplete case an error.
        print(
            f"the checkpoint at {str(manifest)!r} is already complete; nothing to do",
            file=sys.stderr,
        )
        _write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=None,
            rejections=_rejection_log(parts_dir),
            error=None,
            started=started,
            partial=None,
            checkpoint_info=_checkpoint_info(
                args, parts_dir, resumed_from, resumed_from, parts, 0, True
            ),
        )
        return 0

    # Hashed once: the source does not change during a run, and hashing 403 MB costs
    # a quarter of a second -- worth doing once, not once per part.
    identity = source_identity(args.source)
    config_hash = config_identity(config)

    rejections = _rejection_log(
        parts_dir,
        append=bool(args.resume),
        initial=0 if checkpoint is None else checkpoint.rejected,
    )
    writer: RowWriter | None = None
    current_part: Path | None = None
    records_consumed = resumed_from
    complete = False
    rows_this_run = 0

    try:
        reader = StreamingRecordReader(args.source, config.record_path, config.namespaces or None)
        # Hold ONE iterator. StreamingRecordReader.__iter__ is a generator function,
        # so every call to iter() starts a fresh parse from the beginning -- taking a
        # slice of the reader repeatedly would re-read the document from the top each
        # time and never reach the end.
        stream = iter(reader)
        if resumed_from:
            # The fast-forward: parse and discard. No extraction, no writing, and
            # nothing retained, so memory stays as flat as the read itself.
            for _ in islice(stream, resumed_from):
                pass

        with rejections:
            while True:
                # Take one record first, so an exhausted source ends the loop without
                # a writer ever being created -- an empty part file would be a lie
                # about work that was done.
                head = list(islice(stream, 1))
                if not head:
                    # The source ran out. When it does so exactly on a part boundary,
                    # no part was short, so nothing above has recorded that the run is
                    # finished -- the manifest would keep saying complete: false while
                    # the summary said true, and a resume would re-read the whole
                    # document to skip all of it, every time, for ever. Write it down.
                    complete = True
                    write_checkpoint(
                        manifest,
                        Checkpoint(
                            source=identity,
                            config=config_hash,
                            records_consumed=records_consumed,
                            rejected=rejections.count,
                            parts=tuple(parts),
                            complete=True,
                        ),
                    )
                    break

                current_part = parts_dir / part_name(index, extension)
                writer = create_writer(
                    current_part,
                    config.fields,
                    batch_size=args.batch_size,
                    output_format=extension,
                    # Only the first part carries a CSV header, so concatenating the
                    # parts in order yields exactly the single-file output.
                    header=index == 0,
                )
                with writer:
                    stats = consume_records(
                        chain(head, islice(stream, args.checkpoint_every - 1)),
                        config,
                        writer,
                        rejections=rejections,
                    )
                rows_this_run += writer.rows_written
                parts.append(PartRecord(name=current_part.name, rows=writer.rows_written))
                records_consumed += stats.records_processed
                index += 1

                if stats.records_processed < args.checkpoint_every:
                    complete = True
                write_checkpoint(
                    manifest,
                    Checkpoint(
                        source=identity,
                        config=config_hash,
                        records_consumed=records_consumed,
                        rejected=rejections.count,
                        parts=tuple(parts),
                        complete=complete,
                    ),
                )
                current_part = None
                if complete:
                    break
    except (GigaXMLError, OSError, etree.XMLSyntaxError) as exc:
        _write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=writer,
            rejections=rejections,
            error=exc,
            started=started,
            partial=_part_partial(current_part),
            checkpoint_info=_checkpoint_info(
                args,
                parts_dir,
                resumed_from,
                records_consumed,
                parts,
                rows_this_run,
                complete,
            ),
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
        partial=None,
        checkpoint_info=_checkpoint_info(
            args,
            parts_dir,
            resumed_from,
            records_consumed,
            parts,
            rows_this_run,
            complete,
        ),
    )
    print(
        json.dumps(
            {
                "output": str(parts_dir),
                "format": extension,
                "parts": len(parts),
                "rows_this_run": rows_this_run,
                "records_consumed": records_consumed,
                "rejected": rejections.count,
                "resumed_from": resumed_from or None,
                "complete": complete,
            },
            indent=2,
        )
    )
    if rejections.count:
        print(
            f"warning: {rejections.count:,} record(s) rejected; see {rejections.written_path}",
            file=sys.stderr,
        )
    return 0


def _part_partial(part: Path | None) -> Path | None:
    """The partial file of a part that was being written when a run failed."""
    if part is None:
        return None
    return part.with_name(part.name + PARTIAL_SUFFIX)


def _checkpoint_info(
    args: argparse.Namespace,
    parts_dir: Path,
    resumed_from: int,
    records_consumed: int,
    parts: Sequence[PartRecord],
    rows_this_run: int,
    complete: bool,
) -> dict[str, object]:
    """The checkpoint block of the run summary.

    ``records_consumed`` is cumulative and comes from the loop, which is the only
    place that knows how many records were rejected as well as written -- deriving it
    from the part row counts would silently drop the rejected ones.
    """
    return {
        "format": args.format or DEFAULT_PART_FORMAT,
        "directory": str(parts_dir),
        "resumed_from": resumed_from if args.resume else None,
        "records_consumed": records_consumed,
        "rows_this_run": rows_this_run,
        "parts": [part.to_dict() for part in parts],
        "complete": complete,
    }


def _partial_output_path(output: str | Path) -> Path:
    """Where a run writes before moving the file into place.

    Derived from the target rather than asked of the writer, so it still works when
    the run failed before a writer existed -- an output directory that does not exist,
    say -- and the summary still has something to say about partial output.
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
    *,
    partial: Path | None = None,
    checkpoint_info: dict[str, object] | None = None,
) -> dict[str, object]:
    """The run summary, which is written whether the run finished or not.

    ``output_complete`` is the field this exists for: it is true only once the
    finished file has actually been moved onto the target path. A run that aborts
    leaves whatever it managed to write in a ``.tmp`` beside the target, and the
    target itself keeps whatever it had -- so a caller reading this can tell a
    complete output from a partial one without parsing stderr, and can find the
    partial output if it wants it.

    In checkpoint mode the meaning shifts in one place: ``output_complete`` follows
    the *manifest's* ``complete`` rather than the last part's writer. Every committed
    part is complete by construction, so the writer's flag would say "yes" for a run
    that stopped halfway -- which is exactly the confusion this field exists to
    prevent.

    The checkpoint block is added only when there is one, so a run that does not use
    the feature produces the same summary bytes it did before it existed.
    """
    written_path = rejections.written_path
    if checkpoint_info is not None:
        output_format = str(checkpoint_info["format"])
        complete = bool(checkpoint_info["complete"]) and error is None
        partial_path = partial
    else:
        output_format = (
            args.format if args.format is not None else WriterFormat.from_path(args.output).value
        )
        complete = writer is not None and writer.published
        partial_path = _partial_output_path(args.output)

    payload: dict[str, object] = {
        "status": "failed" if error is not None else "ok",
        "source": str(args.source),
        "output": str(args.output),
        "format": output_format,
        "record_path": config.record_path,
        "fields": list(config.field_names),
        "rows": 0 if writer is None else writer.rows_written,
        "rejected": rejections.count,
        "rejected_path": None if written_path is None else str(written_path),
        "error": None if error is None else {"type": type(error).__name__, "message": str(error)},
        "output_complete": complete,
        "partial_path": (
            str(partial_path) if partial_path is not None and partial_path.is_file() else None
        ),
        "elapsed_seconds": elapsed_since(started),
        "tool_version": __version__,
    }
    if checkpoint_info is not None:
        payload["checkpoint"] = checkpoint_info
    return payload


def _write_run_report(
    report_path: Path,
    *,
    args: argparse.Namespace,
    config: ExtractionConfig,
    writer: RowWriter | None,
    rejections: RejectionLog,
    error: BaseException | None,
    started: float,
    partial: Path | None = None,
    checkpoint_info: dict[str, object] | None = None,
) -> None:
    """Write the run summary. Raises ``OSError`` if the path cannot be written."""
    payload = _run_report_payload(
        args,
        config,
        writer,
        rejections,
        error,
        started,
        partial=partial,
        checkpoint_info=checkpoint_info,
    )
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
