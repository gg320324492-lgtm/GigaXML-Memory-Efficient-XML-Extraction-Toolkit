"""The ``gigaxml extract`` command, including ``--checkpoint-every`` and ``--resume``.

**The largest of the four, and legitimately so.** Everything a resumable run has to
know lives here: how parts are named, when the manifest is written relative to the part
it names, what a report says when a run stopped halfway. Splitting it further would
mean a resume whose pieces live in different files, which is the same thing as a resume
whose ordering is not visible in any one of them.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from itertools import chain, islice
from pathlib import Path

from lxml import etree

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
from gigaxml.cli_pkg.common import (
    build_checkpoint_info,
    open_source,
    reject_output_that_is_the_input,
    reject_stdin_if_unsupported,
    rejection_log,
    run_report_path,
    warn_on_batch_size,
    warn_on_format_mismatch,
    write_report_safely,
)
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import CheckpointError, GigaXMLError, RunInterruptedError
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.run import ProgressReporter, RunStats, consume_records
from gigaxml.writers import PARTIAL_SUFFIX, RowWriter, WriterFormat, create_writer

__all__ = [
    "_extract_checkpointed",
    "_extract_summary",
    "_handle_extract",
    "_part_partial",
    "_progress_reporter",
    "_report_rejections",
]


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

    rejections = rejection_log(
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
            write_report_safely(
                report_path,
                args=args,
                config=config,
                writer=None,
                rejections=rejections,
                error=None,
                started=started,
                partial=None,
                checkpoint_info=build_checkpoint_info(
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
        write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=None,
            rejections=rejections,
            error=exc,
            started=started,
            partial=None,
            checkpoint_info=build_checkpoint_info(
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
        write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=writer,
            rejections=rejections,
            error=exc,
            started=started,
            partial=_part_partial(current_part),
            checkpoint_info=build_checkpoint_info(
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

    write_report_safely(
        report_path,
        args=args,
        config=config,
        writer=writer,
        rejections=rejections,
        error=None,
        started=started,
        partial=None,
        checkpoint_info=build_checkpoint_info(
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
    warn_on_format_mismatch(args.output, args.format)
    warn_on_batch_size(args.batch_size)

    checkpointing = args.checkpoint_every is not None
    report_path = run_report_path(args, checkpoint=checkpointing)
    started = time.perf_counter()
    started_at = datetime.now(UTC)
    progress = _progress_reporter(args)

    reject_stdin_if_unsupported(args, args.source)
    reject_output_that_is_the_input(args, args.source)
    if args.resume and args.checkpoint_every is None:
        raise CheckpointError(
            "--resume needs --checkpoint-every too: the part size is a parameter of "
            "the run, and the checkpoint does not record it"
        )
    if checkpointing:
        return _extract_checkpointed(args, config, report_path, started, progress)

    writer: RowWriter | None = None
    rejections = rejection_log(Path(args.output).parent)

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
                open_source(args.source), config.record_path, config.namespaces or None
            )
            stats = consume_records(
                reader, config, writer, rejections=rejections, progress=progress
            )
    except (GigaXMLError, OSError, etree.XMLSyntaxError) as exc:
        # The summary goes out before the error is re-raised. Without it, a caller who
        # only sees a non-zero exit code has no way to tell a partial output from a
        # complete one -- which is the failure this phase exists to remove.
        write_report_safely(
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

    write_report_safely(
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


def _part_partial(part: Path | None) -> Path | None:
    """The partial file of a part that was being written when a run failed."""
    if part is None:
        return None
    return part.with_name(part.name + PARTIAL_SUFFIX)


def _progress_reporter(args: argparse.Namespace) -> ProgressReporter | None:
    """Build a reporter when ``--progress`` was asked for, else ``None``.

    Returning ``None`` is what keeps the default path byte-identical: every call site
    already treats a missing collaborator as "do nothing extra".
    """
    if not args.progress:
        return None
    return ProgressReporter(sys.stderr, every=args.progress_every)


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
