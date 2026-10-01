"""The ``gigaxml sample`` command: write the first N records of a document."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from lxml import etree

from gigaxml.cli_pkg.common import (
    open_source,
    reject_output_that_is_the_input,
    rejection_log,
    run_report_path,
    warn_on_batch_size,
    warn_on_format_mismatch,
    write_report_safely,
)
from gigaxml.config import load_config
from gigaxml.errors import CheckpointError, GigaXMLError
from gigaxml.sample import sample_records
from gigaxml.writers import RowWriter, create_writer

__all__ = ["handle_sample"]


def handle_sample(args: argparse.Namespace) -> int:
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
    reject_output_that_is_the_input(args, args.source)
    warn_on_format_mismatch(args.output, args.format)
    warn_on_batch_size(args.batch_size)

    report_path = run_report_path(args)
    started = time.perf_counter()
    writer: RowWriter | None = None
    rejections = rejection_log(Path(args.output).parent)

    try:
        writer = create_writer(
            args.output,
            config.fields,
            batch_size=args.batch_size,
            output_format=args.format,
        )
        with rejections:
            result = sample_records(
                open_source(args.source),
                config,
                args.output,
                limit=args.limit,
                batch_size=args.batch_size,
                output_format=args.format,
                writer=writer,
                rejections=rejections,
            )
    except (GigaXMLError, OSError, etree.XMLSyntaxError) as exc:
        write_report_safely(
            report_path,
            args=args,
            config=config,
            writer=writer,
            rejections=rejections,
            error=exc,
            started=started,
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
    )
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return 0
