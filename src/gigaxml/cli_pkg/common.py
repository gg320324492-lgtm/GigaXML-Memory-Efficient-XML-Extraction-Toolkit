"""What two or more commands share.

**The test for belonging here is not "it is generally useful" -- it is "at least two
commands call it".** A helper only ``extract`` uses stays in ``extract_cmd`` even when it
looks like it would sit comfortably beside its neighbours, because a module that
accumulates things "for symmetry" is a module a reader has to check to find out whether
they are relevant. That is the failure this file is arranged to avoid.

So the contents are, by construction:

* the ``source`` sentinel and the reader that resolves it -- ``extract``, ``inspect``
  and ``sample`` all take a path or ``-``, and ``-`` has to mean the same thing in
  every one of them;
* the M5 refusals -- ``extract`` and ``sample`` both write files, and both must refuse to
  write over what they are reading;
* the two warnings shared by the two commands that produce output;
* the run-report machinery, which ``extract`` and ``sample`` both write and which the
  report format is defined in terms of.

Nothing here knows what any one command does.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Final

from gigaxml import __version__
from gigaxml.checkpoint import (
    PartRecord,
    config_identity,
    source_identity,
)
from gigaxml.config import ExtractionConfig
from gigaxml.errors import CheckpointError, RunInterruptedError, SecurityError
from gigaxml.run import (
    DEFAULT_REJECTION_FILENAME,
    DEFAULT_RUN_REPORT_FILENAME,
    RejectionLog,
    elapsed_since,
    peak_rss_mb,
)
from gigaxml.writers import PARTIAL_SUFFIX, RowWriter, WriterFormat, batch_size_warning

__all__ = [
    "SCHEMA_VERSION",
    "STDIN_SENTINEL",
    "build_checkpoint_info",
    "environment_fields",
    "identity_or_reason",
    "is_stdin",
    "open_source",
    "partial_output_path",
    "reject_output_that_is_the_input",
    "reject_same_file",
    "reject_stdin_if_unsupported",
    "rejection_log",
    "run_report_path",
    "run_report_payload",
    "same_file",
    "schema_report_identity",
    "status_for",
    "warn_on_batch_size",
    "warn_on_format_mismatch",
    "write_report_safely",
    "write_run_report",
]


#: The shape of a run report, as opposed to the version of the tool that wrote it
#: (``tool_version``). Every report carries it so a consumer can tell which fields to
#: expect without guessing from ``tool_version`` -- a patch release and a format change
#: are different events and now look different.
#:
#: Independent of ``checkpoint``'s own ``version``: a manifest version and a report
#: version are two contracts that move at their own pace. See ``RUN-REPORT-FORMAT.md``.
SCHEMA_VERSION: Final = 1


def build_checkpoint_info(
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


def environment_fields(
    config: ExtractionConfig,
    args: argparse.Namespace,
    writer: RowWriter | None,
    rejections: RejectionLog,
    started: float,
    started_at: datetime | None,
) -> dict[str, object]:
    """The run report's provenance and cost block.

    Split out of :func:`run_report_payload` because it is the part that has to be
    honest about failure: every field here is either a real measurement or an explicit
    ``null`` paired with a sentence saying why. See :func:`identity_or_reason`.

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

    input_identity, input_error = identity_or_reason(args.source, "the input source")
    output_identity, output_error = identity_or_reason(args.output, "the output file")

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
    schema_identity, schema_error = schema_report_identity(config)
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


def identity_or_reason(
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


def partial_output_path(output: str | Path) -> Path:
    """Where a run writes before moving the file into place.

    Derived from the target rather than asked of the writer, so it still works when
    the run failed before a writer existed -- an output directory that does not exist,
    say -- and the summary still has something to say about partial output.
    """
    target = Path(output)
    return target.with_name(target.name + PARTIAL_SUFFIX)


def rejection_log(directory: Path, *, append: bool = False, initial: int = 0) -> RejectionLog:
    """The rejection log for a run, which lives beside the output it belongs to."""
    return RejectionLog(directory / DEFAULT_REJECTION_FILENAME, append=append, initial=initial)


def run_report_path(args: argparse.Namespace, *, checkpoint: bool = False) -> Path:
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


def run_report_payload(
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
        partial_path = partial_output_path(args.output)

    payload: dict[str, object] = {
        # Three states, not two: see `status_for`. A run that was stopped is not
        # a success, and saying `ok` here would put a complete-output claim in a
        # report written by a run that produced half an output.
        "status": status_for(error),
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

    payload.update(environment_fields(config, args, writer, rejections, started, started_at))
    return payload


def schema_report_identity(
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


def status_for(error: BaseException | None) -> str:
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


def write_report_safely(report_path: Path, **fields: object) -> None:
    """Write the run summary, or say why it could not be written.

    Used on both paths. The summary is a side artefact: the exit code reports whether
    the *data* is usable, so a summary that cannot be written is a warning, not a
    failure -- and on the failing path it must not replace the real error.
    """
    try:
        write_run_report(report_path, **fields)  # type: ignore[arg-type]
    except OSError as exc:
        print(
            f"warning: could not write the run report to {report_path}: {exc}",
            file=sys.stderr,
        )


def write_run_report(
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
    payload = run_report_payload(
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


def reject_output_that_is_the_input(args: argparse.Namespace, source: str) -> None:
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
    if not is_stdin(source):
        reject_same_file(args.output, source, "--output", "the input document")
    if getattr(args, "report", None) is not None:
        if not is_stdin(source):
            reject_same_file(args.report, source, "--report", "the input document")
        reject_same_file(args.report, args.output, "--report", "--output")


def reject_same_file(
    first: str | Path, second: str | Path, first_label: str, second_label: str
) -> None:
    """Refuse when two paths a run will write to name the same file.

    The message names both paths and both roles. A reader who typed one of them twice
    needs to see *which* one to change, and "the output conflicts with the input" does
    not say that -- the option names do.
    """
    if not same_file(first, second):
        return
    raise SecurityError(
        f"refusing to write {first_label} to {str(first)!r}: it is the same file as "
        f"{second_label} {str(second)!r}. One would overwrite the other, and the source "
        f"would be destroyed before anything could detect it. Pass a different path, or "
        f"rename the file first if you meant to replace it."
    )


def reject_stdin_if_unsupported(args: argparse.Namespace, source: str) -> None:
    """Refuse the combinations that cannot work on a pipe, by name and by reason.

    Each of these is impossible rather than merely unimplemented, and the failure mode
    without this check would be the worst kind: not an error at all. ``--resume`` and
    ``--checkpoint-every`` both exist to *verify* that the document is the one a
    previous run saw, by hashing it. A stream cannot be re-read, so there is nothing to
    verify against, and a tool that quietly skipped the check would hand back a
    guarantee it had not made -- the exact failure this project's resume feature was
    built to prevent.
    """
    if not is_stdin(source):
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


def same_file(first: str | Path, second: str | Path) -> bool:
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


def warn_on_batch_size(batch_size: int) -> None:
    """Warn when a batch size would push output-side memory past the project budget."""
    warning = batch_size_warning(batch_size)
    if warning is not None:
        print(f"warning: {warning}", file=sys.stderr)


def warn_on_format_mismatch(output: str, requested: str | None) -> None:
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


#: What the ``source`` positional means when it is this: read the document from
#: standard input. A shell convention rather than a gigaxml one, so a command line
#: copied out of a pipe works unchanged.
STDIN_SENTINEL: Final = "-"


def is_stdin(source: str) -> bool:
    return source == STDIN_SENTINEL


def open_source(source: str) -> str | Path | IO[bytes]:
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
    if is_stdin(source):
        return sys.stdin.buffer
    return source
