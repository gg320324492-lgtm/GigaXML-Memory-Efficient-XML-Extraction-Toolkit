"""The ``gigaxml generate`` command: a deterministic synthetic document plus a manifest."""

from __future__ import annotations

import argparse
import json

from gigaxml.generate import generate_dataset

__all__ = ["handle_generate"]


def handle_generate(args: argparse.Namespace) -> int:
    """Handle ``gigaxml generate``."""
    manifest = generate_dataset(
        args.output,
        size=args.size,
        seed=args.seed,
        namespace=args.namespace,
    )
    print(json.dumps(manifest.to_dict(), indent=2))
    return 0
