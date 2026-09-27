"""Verify that every implementation in this directory produced the same rows.

Compares the CSVs a run left in ``.scratch/cmp-<impl>-<size>.csv`` (the runner's output
locations). raw_lxml / xmltodict / gigaxml must be byte-identical; pandas is identical
on the five columns it can extract, with its nested-field column documented as empty in
REPORT.md.

Usage::

    python benchmarks/compare/verify_outputs.py --sizes 100MB
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import pathlib

HERE = pathlib.Path(__file__).resolve().parent
SCRATCH = HERE.parent.parent / ".scratch"

BYTE_IDENTICAL = ("raw_lxml", "xmltodict", "gigaxml")
FIVE_COLUMN_ONLY = "pandas"


def sha256_of(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", default=["100MB", "1GB", "4GB"])
    args = parser.parse_args()

    failures = 0
    for size in args.sizes:
        paths = {
            name: SCRATCH / f"cmp-{name}-{size}.csv" for name in (*BYTE_IDENTICAL, FIVE_COLUMN_ONLY)
        }
        missing = [name for name, path in paths.items() if not path.is_file()]
        if missing:
            print(
                f"{size}: missing outputs for {', '.join(missing)} -- run run_comparison.py first"
            )
            failures += 1
            continue
        hashes = {name: sha256_of(path) for name, path in paths.items()}
        groups: dict[str, list[str]] = {}
        for name, digest in hashes.items():
            groups.setdefault(digest, []).append(name)
        identical = next((names for names in groups.values() if len(names) >= 3), None)
        if identical is None:
            print(f"{size}: no three implementations agree byte-for-byte: {hashes}")
            failures += 1
            continue
        if sorted(identical) != sorted(BYTE_IDENTICAL):
            print(
                f"{size}: byte-identical group is {sorted(identical)}, "
                f"expected {sorted(BYTE_IDENTICAL)}"
            )
            failures += 1
            continue
        pandas_path = paths[FIVE_COLUMN_ONLY]
        five = [
            [cell for index, cell in enumerate(row) if index != 5] for row in csv_rows(pandas_path)
        ]
        reference = csv_rows(paths["raw_lxml"])
        five_match = [
            [cell for index, cell in enumerate(row) if index != 5] for row in reference
        ] == five
        print(
            f"{size}: raw_lxml == xmltodict == gigaxml (sha256 {hashes['raw_lxml'][:16]}...); "
            f"pandas five-column match: {five_match}"
        )
        if not five_match:
            failures += 1

    return 1 if failures else 0


def csv_rows(path: pathlib.Path) -> list[list[str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


if __name__ == "__main__":
    raise SystemExit(main())
