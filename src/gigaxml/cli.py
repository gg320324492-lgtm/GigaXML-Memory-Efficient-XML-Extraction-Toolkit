"""The ``gigaxml`` command: build the parser, dispatch, and turn an exception into a code.

**This module is the top level and nothing else.** It builds the argument parser, hands
the parsed arguments to whichever command handler was registered, and maps the
exception that comes back onto one of five exit codes. It does not open a file, hash a
document, write a row, or decide where a report goes -- those live with the commands
that do them (:mod:`gigaxml.cli_pkg.extract_cmd` and its neighbours) or, when two
commands need the same thing, in :mod:`gigaxml.cli_pkg.common`.

The split is by **who calls it**, not by size. A module with one stable job is one a
reader can hold in mind; this one used to have four (parse, dispatch, orchestrate,
catch), and the only way to know which of them a given line belonged to was to read all
of it.

``main`` and ``build_parser`` are re-exported from here, so ``from gigaxml.cli import
main`` -- what the desktop application does -- and the console script entry point in
``pyproject.toml`` both keep working without either changing.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sys
import traceback
from collections.abc import Callable, Sequence
from typing import Final

from lxml import etree

from gigaxml import __version__
from gigaxml.checkpoint import config_identity as _config_identity
from gigaxml.checkpoint import source_identity as _source_identity
from gigaxml.cli_pkg import common, extract_cmd, generate_cmd, inspect_cmd, sample_cmd
from gigaxml.errors import GigaXMLError, RunInterruptedError
from gigaxml.inspect import DEFAULT_MAX_DEPTH, DEFAULT_MAX_PATHS
from gigaxml.run import PROGRESS_EVERY_DEFAULT
from gigaxml.run import peak_rss_mb as _peak_rss_mb
from gigaxml.writers import BATCH_SIZE_WARN_THRESHOLD, DEFAULT_BATCH_SIZE, WriterFormat

#: Re-exported so the M6 exit-code suite keeps patching the name it patches. The
#: handler itself is looked up on the module at parse time (see the import note),
#: so this alias is what a reader finds, not what dispatch uses.
_status_for = common.status_for
_environment_fields = common.environment_fields
_identity_or_reason = common.identity_or_reason
_schema_report_identity = common.schema_report_identity
_run_report_payload = common.run_report_payload
_write_run_report = common.write_run_report
_write_report_safely = common.write_report_safely
source_identity = _source_identity
peak_rss_mb = _peak_rss_mb
config_identity = _config_identity

__all__ = [
    "DEBUG_ENV_VAR",
    "EXIT_ERROR",
    "EXIT_INTERNAL",
    "EXIT_INTERRUPTED",
    "EXIT_OK",
    "EXIT_USAGE",
    "build_parser",
    "main",
]


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
            "run-report.json beside --output. Written on success *and* on failure. It is "
            "a side artefact: if it cannot be written the run still succeeds, with a "
            "warning."
        ),
    )
    sample.set_defaults(handler=_handle_sample)

    return parser


def _handle_extract(args: argparse.Namespace) -> int:
    """Dispatch ``extract`` to :mod:`gigaxml.cli_pkg.extract_cmd`.

    **A forwarder on purpose.** The M6 exit-code suite substitutes this command's
    handler to reach the internal-error branch, and a substitution only means something
    if dispatch reads the name in this module. Binding ``extract_cmd.handle_extract``
    directly would work identically for every caller and silently ignore the patch --
    the run would report success while the test believed it had forced a failure.
    Measured, not assumed: patching the module attribute reached nothing here.
    """
    return extract_cmd._handle_extract(args)


def _handle_generate(args: argparse.Namespace) -> int:
    return generate_cmd.handle_generate(args)


def _handle_inspect(args: argparse.Namespace) -> int:
    return inspect_cmd.handle_inspect(args)


def _handle_sample(args: argparse.Namespace) -> int:
    return sample_cmd.handle_sample(args)


def _positive_int(text: str) -> int:
    """An ``argparse`` type for a strictly positive integer."""
    try:
        value = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from exc
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _install_interrupt_handlers() -> Callable[[], None]:
    """Make a stop signal unwind like any other error, and return how to undo it.

    **The signal handler does nothing but raise.** A handler that tried to write the report
    itself would be interrupted at any point, and a truncated ``run-report.json`` is worse
    than none -- it is not JSON, and every reader has to cope with it. Raising instead
    unwinds through the ``except`` clauses the library already has, so the report is written
    on the ordinary way out, where a second signal finds the process already committing to
    its end. ★ Measured: with no handler, :class:`KeyboardInterrupt` landed inside
    ``os.replace`` in one run and inside ``elem.itertext()`` in another -- ``BaseException``,
    which none of them catch, so both left no report at all. A stop has to arrive as the
    kind of exception the way out already knows how to finish work for.

    **Both signals, and only both.** ``SIGINT`` is what Ctrl-C sends, on every platform, and
    Python would raise :class:`KeyboardInterrupt` for it anyway -- installing a handler is
    what makes the stop take the same path as everything else instead of a ``BaseException``
    that none of the library's ``except`` clauses name. ``SIGTERM`` has no such default: it
    terminates the process outright, so without a handler there is nothing to catch.

    **What this does not reach.** ``TerminateProcess`` -- the Windows task manager, and
    ``os.kill(pid, SIGTERM)`` on Windows, which is the same call -- gives the process no code
    to run, so no handler in any language could fire. On POSIX ``kill`` is covered; on
    Windows there is no signal that reaches a handler from outside, and a stopped run there
    leaves parts and a manifest and no report. See :class:`gigaxml.errors.RunInterruptedError`,
    which says so where a reader of the report format will meet it.

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
    sets once on a machine where they are about to file a bug -- not something to offer to a
    user at the moment the tool has just failed.
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

    **Nothing here decides anything about the data.** Parsing the command line and
    turning the result into a number is the whole job; the work is done by the handler
    this dispatches to, and every byte of output, every file written and every report
    field comes from there.
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
    # is frozen into one executable with no module system to import from.
    raise SystemExit(main())
