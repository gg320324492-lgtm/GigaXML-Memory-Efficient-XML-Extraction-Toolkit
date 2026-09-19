"""Command-line interface for :mod:`gigaxml`."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence

from gigaxml import __version__

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

    return parser


def _handle_generate(args: argparse.Namespace) -> int:
    """Handle ``gigaxml generate``. Implemented in Phase 1."""
    raise NotImplementedError("`gigaxml generate` is implemented in Phase 1")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``gigaxml`` console script."""
    args = build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)
