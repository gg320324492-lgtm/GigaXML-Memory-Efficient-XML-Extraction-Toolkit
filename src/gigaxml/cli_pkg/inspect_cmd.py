"""The ``gigaxml inspect`` command: report a document's structure and propose record paths."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from gigaxml.cli_pkg.common import open_source
from gigaxml.inspect import generate_config, inspect_document

__all__ = ["handle_inspect"]


def handle_inspect(args: argparse.Namespace) -> int:
    """Handle ``gigaxml inspect``."""
    report = inspect_document(
        open_source(args.source),
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
