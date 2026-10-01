"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import signal
import sys
import time
import traceback
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from itertools import chain, islice
from pathlib import Path
from typing import IO, Final

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
from gigaxml.cli_pkg.generate_cmd import handle_generate
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import (
    CheckpointError,
    GigaXMLError,
    RunInterruptedError,
    SecurityError,
)
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
    PROGRESS_EVERY_DEFAULT,
    ProgressReporter,
    RejectionLog,
    RunStats,
    consume_records,
    elapsed_since,
    peak_rss_mb,
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
    generate.set_defaults(handler=handle_generate)

    extract = subparsers.add_parser(
        "extract",
        help="Extract records from an XML file into CSV, JSONL or Parquet.",
        description=(
            "Stream records out of an XML file and write them in batches. "
            "Memory stays flat on both sides: the reader releases each record as "
            "the next one arrives, and the writer flushes each batch as it fills."
        ),
    )
    extract.add_argument(
        "source",
        help=(
            "Input XML file (optionally gzipped), or - to read the document from "
            "standard input. A stream is parsed exactly as a file is, at the same "
            "bounded memory; it cannot be used with --resume or --checkpoint-every, "
            "which verify the source by hashing it."
        ),
    )
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
            "outright if the source or the config has changed since the checkpoint, or "
            "if any part it names is missing or a different size -- checking that reads "
            "the parts back, which costs about 4%% of a full extraction for CSV output "
            "and almost nothing for Parquet. The parts already on disk decide the "
            "format: a --format that disagrees is ignored, with a warning."
        ),
    )
    extract.add_argument(
        "--progress",
        action="store_true",
        help=(
            "Write machine-readable progress to stderr, one JSON object per line, so a "
            "caller can show a bar without guessing. A line is emitted after every "
            "--progress-every records, or every second, whichever comes first, and one "
            "final line is always written. Lines go to stderr because stdout carries "
            "the run summary. There is no total and no percentage: the tool cannot know "
            "how many records a document holds without reading it, and an invented "
            "denominator is worse than none. The count is cumulative, so a resumed run "
            "continues from where it left off rather than restarting at zero. Off by "
            "default, and nothing at all is written when it is off."
        ),
    )
    extract.add_argument(
        "--progress-every",
        type=_positive_int,
        metavar="N",
        default=PROGRESS_EVERY_DEFAULT,
        help=(
            f"Records between progress lines (default {PROGRESS_EVERY_DEFAULT}). A line "
            f"is also written once a second, so a slow source still reports. Ignored "
            f"without --progress."
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
    inspect.add_argument(
        "source",
        help=(
            "Input XML file (optionally gzipped), or - to read the document from "
            "standard input. A stream is walked exactly as a file is, at the same "
            "bounded memory; the reported input size is unknown, because a pipe has "
            "no length."
        ),
    )
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
    sample.add_argument(
        "source",
        help=(
            "Input XML file (optionally gzipped), or - to read the document from "
            "standard input. A stream is read exactly as a file is, at the same "
            "bounded memory."
        ),
    )
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


def _progress_reporter(args: argparse.Namespace) -> ProgressReporter | None:
    """Build a reporter when ``--progress`` was asked for, else ``None``.

    Returning ``None`` is what keeps the default path byte-identical: every call site
    already treats a missing collaborator as "do nothing extra".
    """
    if not args.progress:
        return None
    return ProgressReporter(sys.stderr, every=args.progress_every)


def _positive_int(text: str) -> int:
    """An ``argparse`` type for a strictly positive integer."""
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


#: What the ``source`` positional means when it is this: read the document from
#: standard input. A shell convention rather than a gigaxml one, so a command line
#: copied out of a pipe works unchanged.
STDIN_SENTINEL: Final = "-"


def _is_stdin(source: str) -> bool:
    return source == STDIN_SENTINEL


def _open_source(source: str) -> str | Path | IO[bytes]:
    """The reader's source: standard input's buffer, or the path as given.

    ``sys.stdin.buffer`` rather than ``sys.stdin``, because lxml parses bytes and a
    text-mode handle would have to be re-encoded by lxml one chunk at a time.

    **Every subcommand that reads a document goes through here**, so ``-`` means the
    same thing everywhere. It used not to: ``extract`` understood it while ``inspect``
    and ``sample`` treated the token as a file name and answered "no such file", which
    tells a user their path is wrong when in fact the tool had simply never been asked
    the question. A sentinel with one meaning in one place and another in the next two
    is a bug wearing a feature's clothes.
    """
    if _is_stdin(source):
        return sys.stdin.buffer
    return source


def _reject_stdin_if_unsupported(args: argparse.Namespace, source: str) -> None:
    """Refuse the combinations that cannot work on a pipe, by name and by reason.

    Each of these is impossible rather than merely unimplemented, and the failure mode
    without this check would be the worst kind: not an error at all. ``--resume`` and
    ``--checkpoint-every`` both exist to *verify* that the document is the one a
    previous run saw, by hashing it. A stream cannot be re-read, so there is nothing to
    verify against, and a tool that quietly skipped the check would hand back a
    guarantee it had not made -- the exact failure this project's resume feature was
    built to prevent.
    """
    if not _is_stdin(source):
        return
    if args.resume:
        raise CheckpointError(
            "--resume cannot read standard input: resuming works by verifying the "
            "source's sha256 against the checkpoint, and a stream cannot be read twice. "
            "Save the document to a file and pass its path instead."
        )
    if getattr(args, "checkpoint_every", None) is not None:
        raise CheckpointError(
            "--checkpoint-every cannot be used with standard input: a checkpoint records "
            "the source's sha256 so a resume can check it, and a stream cannot be read "
            "twice to compute one. Save the document to a file, or drop the flag."
        )


def _same_file(first: str | Path, second: str | Path) -> bool:
    """Do these two paths name the same file on disk?

    **Resolved first, and compared with ``samefile`` where both exist.** A string
    comparison would miss every spelling that reaches the same file by another route:
    ``./a.xml`` beside ``a.xml``, ``sub/../a.xml``, an absolute path where a relative
    one was given, a symlink, a hard link, or ``A.XML`` on a case-insensitive
    filesystem. The user does not have to mean to overwrite their input -- typing the
    path a second way is enough.

    Two checks rather than one, because they answer different questions and each has a
    case the other misses:

    * ``samefile`` asks the filesystem, which is the only authority on inodes. It is
      what catches a hard link, which no amount of path arithmetic can see.
    * ``resolve()`` asks what the path *means*, and works when one or both names do not
      exist yet -- ``samefile`` raises then, and an output file that does not exist is
      the ordinary case, not an error.

    Both are guarded: a path that cannot be stat'ed is not evidence of a conflict, and
    a check that crashed on a missing file would break every normal run.
    """
    first_path = Path(first)
    second_path = Path(second)
    try:
        if first_path.exists() and second_path.exists() and first_path.samefile(second_path):
            return True
    except OSError:  # pragma: no cover - a path that cannot be stat'ed proves nothing
        pass
    try:
        return first_path.resolve() == second_path.resolve()
    except OSError:  # pragma: no cover - resolve() is documented to be tolerant
        return False


def _reject_output_that_is_the_input(args: argparse.Namespace, source: str) -> None:
    """Refuse to write over the document being read.

    **The worst failure this tool can have, and the quietest.** The source is the one
    copy of the data the user has; an output that lands on it destroys it, and the run
    reports success while doing so. There is no warning to catch afterwards, because
    nothing goes wrong from the program's point of view -- it wrote the file it was
    asked to write.

    What made this reachable is that the format check is not a guard: it refuses an
    ``.xml`` output because it cannot *infer* a format from the suffix, and a user who
    passes ``--format csv`` explicitly gets past it and straight into the overwrite. A
    protection that disappears when the user is more specific is not a protection.

    Checked here, before any writer is created and before anything is opened -- a
    refusal that arrives after a ``.tmp`` was written has already touched the disk. The
    file's bytes are asserted unchanged by the tests, because "we refuse" and "we
    refuse before doing anything" are different claims.

    ``--report`` is checked too, and it is the same defect rather than a neighbour of
    one: it is a path the user names, written after the run, and pointing it at the
    input overwrites the input just as surely. Two further collisions are refused with
    it -- a report landing on the output, and on a checkpoint's directory -- because in
    those the two artefacts overwrite *each other*, and which of them survives would
    depend on the order the run happens to finish in.

    Raises:
        GigaXMLError: two of the paths this run will write name the same file.
    """
    # The output-versus-input check is skipped for a stream, which has no path to
    # collide with. The report checks are not: a report can still land on the output
    # when the document is being read from a pipe, and that collision has nothing to
    # do with where the input came from.
    if not _is_stdin(source):
        _reject_same_file(args.output, source, "--output", "the input document")
    if getattr(args, "report", None) is not None:
        if not _is_stdin(source):
            _reject_same_file(args.report, source, "--report", "the input document")
        _reject_same_file(args.report, args.output, "--report", "--output")


def _reject_same_file(
    first: str | Path, second: str | Path, first_label: str, second_label: str
) -> None:
    """Refuse when two paths a run will write to name the same file.

    The message names both paths and both roles. A reader who typed one of them twice
    needs to see *which* one to change, and "the output conflicts with the input" does
    not say that -- the option names do.
    """
    if not _same_file(first, second):
        return
    raise SecurityError(
        f"refusing to write {first_label} to {str(first)!r}: it is the same file as "
        f"{second_label} {str(second)!r}. One would overwrite the other, and the source "
        f"would be destroyed before anything could detect it. Pass a different path, or "
        f"rename the file first if you meant to replace it."
    )


def _handle_extract(args: argparse.Namespace) -> int:
    """Handle ``gigaxml extract``."""
    config = load_config(args.config)
    if config.schema:
        # Imported here rather than at module scope so that a config without a schema
        # -- which is every config on a machine without the extra -- never reaches the
        # optional dependency at all. See gigaxml.xsd for why this is deliberately not
        # in the parsing path.
        from gigaxml.xsd import apply_schema_types

        config = apply_schema_types(config)
    _warn_on_format_mismatch(args.output, args.format)
    _warn_on_batch_size(args.batch_size)

    checkpointing = args.checkpoint_every is not None
    report_path = _run_report_path(args, checkpoint=checkpointing)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    progress = _progress_reporter(args)

    _reject_stdin_if_unsupported(args, args.source)
    _reject_output_that_is_the_input(args, args.source)
    if args.resume and args.checkpoint_every is None:
        raise CheckpointError(
            "--resume needs --checkpoint-every too: the part size is a parameter of "
            "the run, and the checkpoint does not record it"
        )
    if checkpointing:
        return _extract_checkpointed(args, config, report_path, started, progress)

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
                _open_source(args.source), config.record_path, config.namespaces or None
            )
            stats = consume_records(
                reader, config, writer, rejections=rejections, progress=progress
            )
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
            started_at=started_at,
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
        started_at=started_at,
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
        _open_source(args.source),
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
    _reject_output_that_is_the_input(args, args.source)
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
                _open_source(args.source),
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
    progress: ProgressReporter | None = None,
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
        # **Read before the try below, deliberately.** Reading a manifest is cheap and it
        # failing is an ordinary refusal -- a missing one, a version this build does not
        # know -- and none of those should write a report: the report beside the output
        # belongs to whichever run last reached its end, and a run that refused before
        # doing anything has not earned the right to overwrite it.
        checkpoint = read_checkpoint(manifest)
    elif manifest.is_file():
        raise CheckpointError(
            f"a checkpoint already exists at {str(manifest)!r}. Use --resume to "
            f"continue that run, or remove the directory to start a fresh one -- "
            f"writing over it would throw away work that has already been committed."
        )

    rejections = _rejection_log(
        parts_dir,
        append=bool(args.resume),
        initial=0 if checkpoint is None else checkpoint.rejected,
    )

    # **This block is slow, and that is why a stopped run needs it handled.**
    # `validate_resume` hashes the source, `require_intact_parts` reads every part back,
    # and `source_identity` hashes the source again for the manifest -- so on a 4 GiB
    # document this is several seconds in which a Ctrl-C has somewhere to land. Measured
    # on that document: without this, interrupting at 2.5 s left 0 parts, no report, and
    # only the words `error: the run was interrupted by SIGINT` on stderr.
    #
    # **Only RunInterruptedError is caught here.** Every other failure keeps the path it
    # had, including the one that matters most: a refusal such as "a checkpoint already
    # exists" must not write a report, because the one on disk is the record of the run
    # that *did* finish, and overwriting it with a `failed` summary loses it.
    try:
        if args.resume:
            validate_resume(checkpoint, args.source, config)
            if checkpoint.parts:
                # Continue in the format the checkpoint was started in, whatever --format
                # says now: the parts already on disk are the ones being added to.
                extension = checkpoint.parts[-1].name.rsplit(".", 1)[-1]
            if args.format is not None and args.format != extension:
                # Say so rather than quietly doing the right thing. The parts are the
                # truth and they win, but a caller who asked for parquet and got csv has
                # been overruled, and silence about that is how a downstream tool ends up
                # calling a parquet reader on a CSV.
                print(
                    f"warning: --format {args.format} was ignored: this checkpoint holds "
                    f"{extension} parts, and the parts already written decide the format",
                    file=sys.stderr,
                )
            # The manifest is only worth trusting if the parts it names are still there.
            # `records_consumed` is what a resume skips, so a part that has been deleted
            # means walking straight past rows nobody will ever write -- and finishing
            # with a success. Refuse instead.
            require_intact_parts(checkpoint, parts_dir, extension)
            resumed_from = checkpoint.records_consumed
            parts = list(checkpoint.parts)
            index = len(parts)

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
                rejections=rejections,
                error=None,
                started=started,
                partial=None,
                checkpoint_info=_checkpoint_info(
                    args, parts_dir, extension, resumed_from, resumed_from, parts, 0, True
                ),
            )
            return 0

        # Hashed once: the source does not change during a run, and hashing 403 MB costs
        # a quarter of a second -- worth doing once, not once per part.
        identity = source_identity(args.source)
        config_hash = config_identity(config)
    except RunInterruptedError as exc:
        # The counts are the manifest's, not this run's: whatever the checkpoint recorded
        # is exactly how many parts were on disk when the signal arrived, and this run has
        # committed none of them. A report saying 0 parts for a resume that was checking
        # 193 of them would be the false measurement this whole report is about.
        recorded = checkpoint.records_consumed if checkpoint is not None else 0
        _write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=None,
            rejections=rejections,
            error=exc,
            started=started,
            partial=None,
            checkpoint_info=_checkpoint_info(
                args,
                parts_dir,
                extension,
                0,
                recorded,
                list(checkpoint.parts) if checkpoint is not None else [],
                0,
                False,
            ),
        )
        raise

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
                    if progress is not None:
                        # The count is cumulative, so a resumed run continues where it
                        # left off. `rows_this_run` is the same idea for rows.
                        progress.set_offsets(records_consumed, rows_this_run)
                        progress.set_part(index)
                    stats = consume_records(
                        chain(head, islice(stream, args.checkpoint_every - 1)),
                        config,
                        writer,
                        rejections=rejections,
                        progress=progress,
                    )
                rows_this_run += writer.rows_written
                parts.append(PartRecord(name=current_part.name, rows=writer.rows_written))
                records_consumed += stats.records_processed
                index += 1

                if stats.records_processed < args.checkpoint_every:
                    complete = True

                # --- the order here is deliberate: part first, manifest second ---
                #
                # By this point `with writer:` has already renamed the part into place,
                # and only now does the manifest that names it get written. Reversing the
                # two would be worse than it looks: the manifest would briefly list a part
                # that does not exist, and `verify_parts` -- which refuses a resume when a
                # part the manifest names is missing -- would then reject that resume. A
                # run interrupted in that window could not be continued at all.
                #
                # The cost of the order chosen is that a kill *here* leaves one part on
                # disk that the manifest does not mention. That side is safe. `verify_parts`
                # only ever checks manifest -> disk, so an extra part is invisible to it,
                # and a resume starts writing at `part-{len(parts)}` -- which is exactly
                # the unnamed one -- and overwrites it with the same bytes, because the
                # fast-forward is deterministic and lands in the same place.
                #
                # So the invariant is "the manifest is never ahead of the disk", not "the
                # two agree". Closing the window entirely is not possible across two files
                # without a journal, and would not be worth it: the direction it fails in
                # is the recoverable one.
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
                extension,
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
            extension,
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
    extension: str,
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

    ``extension`` is the format the parts are actually in, which on a resume is the
    checkpoint's format rather than whatever ``--format`` asked for. The summary is a
    record of what happened, so it reports that one -- reporting the request instead
    would put a format in the receipt that no file on disk has.
    """
    return {
        "format": extension,
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


def _identity_or_reason(
    target: str | Path, label: str
) -> tuple[dict[str, object] | None, str | None]:
    """``source_identity`` for a report field, or ``None`` and why it was impossible.

    **This never invents an identity.** If the path cannot be stat'ed or read -- because
    the run is reading standard input, because the file is gone, because the output was
    never created -- the report says so in words and leaves the value ``null``. Filling
    in a zero, an empty string, or a hash of nothing would produce a report that reads
    as complete and is not: a scanner looking for a source that changed would see an
    unchanged ``"sha256": ""``. A field that cannot be measured has to look
    unmeasurable, which is the only honest shape for it.

    Reuses :func:`gigaxml.checkpoint.source_identity` rather than hashing again, so
    "the hash in the report" and "the hash resume checks" cannot be two different
    functions that agree today.
    """
    try:
        return source_identity(target), None
    except (CheckpointError, OSError) as exc:
        return None, f"{label} could not be identified: {exc}"


def _schema_report_identity(
    config: ExtractionConfig,
) -> tuple[dict[str, object] | None, str | None]:
    """The schema a run used, for the report, or ``None`` and why there is none.

    Three outcomes, and the middle one is the reason this is not inlined:

    * no ``schema:`` in the config → ``(None, None)``: the report omits the key entirely,
      because a run without a schema has nothing to say about one;
    * the schema is readable → ``({"path": ..., "sha256": ...}, None)``: both, because the
      path is what a reader can go and look at while the digest is what actually decided
      the types;
    * the schema cannot be read → ``(None, "…")``: no identity, and a sentence saying so.

    The path appears here and not in :func:`gigaxml.checkpoint.config_identity` on
    purpose. The identity must be blind to location -- the same schema moved is the same
    run -- but a report is read by a person, and a digest alone tells them nothing about
    which file to open. This is a record of what happened, not a comparison.
    """
    if not config.schema:
        return None, None
    try:
        identity = source_identity(config.schema)
    except (CheckpointError, OSError) as exc:
        return None, f"the schema {str(config.schema)!r} could not be identified: {exc}"
    return identity, None


#: The shape of a run report, as opposed to the version of the tool that wrote it
#: (``tool_version``). Every report carries it so a consumer can tell which fields to
#: expect without guessing from ``tool_version`` -- a patch release and a format change
#: are different events and now look different.
#:
#: Independent of ``checkpoint``'s own ``version``: a manifest version and a report
#: version are two contracts that move at their own pace. See ``RUN-REPORT-FORMAT.md``.
SCHEMA_VERSION: Final = 1


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
    started_at: datetime | None = None,
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
        # Three states, not two: see `_status_for`. A run that was stopped is not
        # a success, and saying `ok` here would put a complete-output claim in a
        # report written by a run that produced half an output.
        "status": _status_for(error),
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
        "schema_version": SCHEMA_VERSION,
    }
    if checkpoint_info is not None:
        payload["checkpoint"] = checkpoint_info

    payload.update(_environment_fields(config, args, writer, rejections, started, started_at))
    return payload


def _environment_fields(
    config: ExtractionConfig,
    args: argparse.Namespace,
    writer: RowWriter | None,
    rejections: RejectionLog,
    started: float,
    started_at: datetime | None,
) -> dict[str, object]:
    """The run report's provenance and cost block.

    Split out of :func:`_run_report_payload` because it is the part that has to be
    honest about failure: every field here is either a real measurement or an explicit
    ``null`` paired with a sentence saying why. See :func:`_identity_or_reason`.

    ``accepted`` and ``rejected`` are counted, not inferred. ``rows`` is what the writer
    committed, ``rejections.count`` is what quarantine took, and a record that was
    neither -- a run that died mid-record -- shows up as the two not summing to
    ``records_seen`` rather than being absorbed into either number.
    """
    elapsed = elapsed_since(started)
    accepted = 0 if writer is None else writer.rows_written
    rejected = rejections.count
    finished = datetime.now(UTC)
    begin = started_at if started_at is not None else finished - timedelta(seconds=elapsed)

    input_identity, input_error = _identity_or_reason(args.source, "the input source")
    output_identity, output_error = _identity_or_reason(args.output, "the output file")

    block: dict[str, object] = {
        "environment": {
            "python": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "os": platform.system(),
            "os_release": platform.release(),
            "machine": platform.machine(),
        },
        "started_at": begin.isoformat(timespec="seconds"),
        "finished_at": finished.isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed,
        "records": {
            "accepted": accepted,
            "rejected": rejected,
            "seen": accepted + rejected,
        },
        "config_hash": config_identity(config),
        "peak_rss_mb": peak_rss_mb(),
        "input_identity": input_identity,
        "output_identity": output_identity,
    }
    # The schema is part of what this run *was*, so the report says which one it used.
    # A config naming no schema omits the key entirely rather than writing null: a null
    # would read as "there was a schema and it could not be identified", which is the
    # same confusion the identity fields avoid by pairing a null with a reason.
    schema_identity, schema_error = _schema_report_identity(config)
    if schema_identity is not None:
        block["schema_identity"] = schema_identity
    if schema_error is not None:
        block["schema_identity_error"] = schema_error
    # Throughput over the elapsed time of the whole run, and only when it is defined:
    # a run that took under a millisecond has no rate worth writing, and 0.0 there
    # would read as "infinitely slow".
    block["throughput_records_per_s"] = (
        round((accepted + rejected) / elapsed, 1) if elapsed > 0 else None
    )
    if input_error is not None:
        block["input_identity_error"] = input_error
    if output_error is not None:
        block["output_identity_error"] = output_error
    return block


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
    started_at: datetime | None = None,
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
        started_at=started_at,
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


def _install_interrupt_handlers() -> Callable[[], None]:
    """Make a stop signal unwind like any other error, and return how to undo it.

    **The signal handler does nothing but raise.** That is deliberate and it is the whole
    design: a handler that tried to write the report itself would be interrupted at any
    point, and what it left behind would be a half-written report -- worse than none,
    because a truncated ``run-report.json`` is not JSON and every reader has to cope with
    it. Raising instead unwinds through the ``except`` clauses the library already has, so
    the report is written on the ordinary way out with the ordinary code, where a second
    signal arriving finds the process already committing to its end.

    **Measured, and it is why the raise is not a nicety.** With no handler installed,
    Python's own :class:`KeyboardInterrupt` lands wherever the signal happens to arrive.
    Two runs of the same test, the same document, the same moment in the extraction:
    one put it inside ``os.replace`` in :func:`gigaxml.checkpoint.write_checkpoint`, the
    other inside ``elem.itertext()`` in :func:`gigaxml.fields.normalize_text`. Neither is
    catchable by the ``except`` clauses the library has -- ``KeyboardInterrupt`` descends
    from ``BaseException`` -- so both left no report at all. A stop is not something to be
    met halfway down a call stack: it has to arrive as the kind of exception the code on the
    way out already knows how to finish work for.

    **Both signals, and only both.** ``SIGINT`` is what Ctrl-C sends, on every platform,
    and Python would raise :class:`KeyboardInterrupt` for it anyway -- installing a handler
    for it is what makes the stop take the same path as everything else instead of a
    ``BaseException`` that none of the library's ``except`` clauses name.
    ``SIGTERM`` has no such default: it terminates the process outright, so without a
    handler there is nothing to catch.

    **What this does not reach.** ``TerminateProcess`` -- the Windows task manager, and
    ``os.kill(pid, SIGTERM)`` on Windows, which is the same call -- gives the process no
    code to run, so no handler in any language could fire. On POSIX ``kill`` is covered;
    on Windows there is no signal that reaches a handler from outside, and a stopped run
    there leaves parts and a manifest and no report. See
    :class:`gigaxml.errors.RunInterruptedError`, which says so where a reader of the
    report format will meet it.

    Returns:
        A callable that puts the previous handlers back. **Restored on the way out of
        :func:`main`,** because the CLI is a library function as well as a command: a test
        that calls ``main()`` in-process would otherwise hand its own Ctrl-C behaviour --
        and pytest's -- to whatever was installed here.
    """
    previous: dict[int, object] = {}

    def _raise(signum: int, frame: object) -> None:  # noqa: ARG001 - the signal API's shape
        name = signal.Signals(signum).name if signum in _signal_names() else str(signum)
        raise RunInterruptedError(f"the run was interrupted by {name}", signum=signum, signame=name)

    for name in ("SIGINT", "SIGTERM"):
        number = getattr(signal, name, None)
        if number is None:  # pragma: no cover - both exist on every supported platform
            continue
        try:
            previous[int(number)] = signal.getsignal(number)
            signal.signal(number, _raise)
        except (OSError, ValueError):  # pragma: no cover - not the main thread, or no signal
            previous.pop(int(number), None)

    def restore() -> None:
        for number, handler in previous.items():
            # Same tolerance as installing: a handler this process cannot put back is no
            # worse than one it could not install, and neither is worth raising over.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(number, handler)  # type: ignore[arg-type]

    return restore


def _signal_names() -> set[int]:
    """Every signal number this platform knows a name for."""
    try:
        return {int(member) for member in signal.Signals}
    except (TypeError, ValueError):  # pragma: no cover - no enum of signals here
        return set()


def _status_for(error: BaseException | None) -> str:
    """The report's ``status``, from the error the run ended with.

    **Three values, and the third is the one this exists for.** A run that finished and a
    run that failed were already distinguishable. A run that was *stopped* is neither, and
    reporting it as ``ok`` would be the worst of the three -- a summary claiming a complete
    output from a run that produced half of one, which is exactly what the field exists to
    prevent. Reporting it as ``failed`` is not wrong in the way ``ok`` is, but it collapses
    two different things: a record that could not be converted is a problem with the data,
    while a signal is somebody deciding the run is no longer wanted.

    Dispatched on the class, never on the message. Matching wording is the one thing worse
    than not classifying, because it breaks the first time a message is reworded and looks
    like it still works.
    """
    if error is None:
        return "ok"
    if isinstance(error, RunInterruptedError):
        return "interrupted"
    return "failed"


#: Exit code for a run that finished.
EXIT_OK: Final = 0

#: Exit code for a bad input, config, command or environment. **Unchanged since the
#: first release**: this is the code every existing script checks, and widening what it
#: means would be the breaking change -- not adding new codes beside it.
EXIT_ERROR: Final = 1

#: Exit code argparse gives for a malformed command line. Not ours to set, but named
#: here so the table in ``ERRORS.md`` and this module say the same thing.
EXIT_USAGE: Final = 2

#: Exit code for a run that was stopped by a signal. Distinguished from
#: :data:`EXIT_ERROR` because the two want opposite reactions from a script: an error
#: means "do not run this again", while an interrupted run means "this was going fine,
#: pick it up".
EXIT_INTERRUPTED: Final = 3

#: Exit code for a failure inside gigaxml. Reserved rather than used: see
#: :func:`_report_unexpected`.
EXIT_INTERNAL: Final = 4

#: The environment variable that turns a hidden traceback into a visible one.
DEBUG_ENV_VAR: Final = "GIGAXML_DEBUG"


def _debug_tracebacks_enabled() -> bool:
    """Whether an unexpected failure should print the full traceback.

    An environment variable rather than a flag, because this is a thing a developer
    sets once on a machine where they are about to file a bug -- not something to offer
    a user at the moment the tool has just failed.
    """
    return os.environ.get(DEBUG_ENV_VAR, "").strip().lower() in ("1", "true", "yes")


def _report_unexpected(exc: BaseException) -> int:
    """Say what happened for an exception the code did not expect, and return a code.

    **The default is one line and no traceback, which is a deliberate choice about who
    the output is for.** Someone who hits a bug in gigaxml needs to know it is not their
    fault and that nothing in their input needs fixing. A wall of Python frames says the
    opposite, and it is the thing they would paste into a report without being able to
    read it.

    So the traceback is available but hidden. Hidden, not absent: a tool whose failures
    cannot be diagnosed is worse than one that prints too much.
    """
    if _debug_tracebacks_enabled():
        traceback.print_exception(type(exc), exc, exc.__traceback__)
    print(
        f"error: gigaxml failed unexpectedly: {type(exc).__name__}: {exc}\n"
        f"  This is a bug in gigaxml, not a problem with your input. Re-run with "
        f"{DEBUG_ENV_VAR}=1 for the full traceback.",
        file=sys.stderr,
    )
    return EXIT_INTERNAL


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``gigaxml`` console script.

    The exit code is part of the command's contract -- see ``ERRORS.md``. It separates
    the things a script reacts to differently: retrying after a signal, giving up on a
    bad config, and reporting a bug in the tool are three different responses, and one
    code for all of them would force a script to parse stderr to tell them apart.
    """
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    restore_signals = _install_interrupt_handlers()
    try:
        try:
            return handler(args)
        except RunInterruptedError as exc:
            # **Before GigaXMLError, and the order is the whole point.**
            # RunInterruptedError descends from GigaXMLError and ``except`` matches in
            # order, so this clause placed below would never be reached -- the run would
            # exit 1 and a caller could not tell "stopped" from "failed", which is the
            # confusion this code exists to remove. The report is still written on the
            # way past; only the code that comes back changes.
            print(f"interrupted: {exc}", file=sys.stderr)
            return EXIT_INTERRUPTED
        except GigaXMLError as exc:
            # Every deliberate error descends from GigaXMLError, so this turns a bad
            # config, a wrong path or a missing optional dependency into one readable
            # line instead of a traceback.
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except OSError as exc:
            # A missing input file, an unwritable output directory, a bad gzip stream.
            # These are environmental rather than library errors, but a command-line
            # tool should still say what went wrong on one line.
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except etree.XMLSyntaxError as exc:
            # Malformed XML. Neither a GigaXMLError nor an OSError, but it is the most
            # likely thing to go wrong with an unknown file, and a traceback helps nobody.
            print(f"error: {exc}", file=sys.stderr)
            return EXIT_ERROR
        except Exception as exc:
            # Catching broadly is the point of this clause: everything above names an
            # exception the code expects, so what reaches here is by definition one it
            # did not. Re-raising would put a traceback in front of a user who cannot
            # act on it.
            return _report_unexpected(exc)
    finally:
        # The CLI is a library function as well as a command, and a caller in this
        # process keeps its own signal behaviour. See `_install_interrupt_handlers`.
        restore_signals()


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    # So that `python -m gigaxml.cli ...` works. The GUI launches the CLI as a child of
    # its own interpreter, and this is the form that keeps working when the whole thing
    # is frozen into an executable and there is no console script to call.
    raise SystemExit(main())
