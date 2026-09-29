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
from collections.abc import Iterator
from itertools import zip_longest

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
        present = {name: path for name, path in paths.items() if path.is_file()}
        missing = [name for name in paths if name not in present]
        if missing:
            # An implementation that could not produce an output at this size (pandas
            # fails outright on the 1 GB file) is not a reason to refuse the comparison:
            # the others still have to agree, and the failure is recorded in results.json
            # and REPORT.md. Said here so an absent file is never read as agreement.
            print(
                f"{size}: no output for {', '.join(missing)} "
                "(recorded as a failure in results.json)"
            )
        comparable = [name for name in BYTE_IDENTICAL if name in present]
        if len(comparable) < 3:
            print(f"{size}: only {len(comparable)} of the byte-identical group produced output")
            failures += 1
            continue
        hashes = {name: sha256_of(present[name]) for name in comparable}
        groups: dict[str, list[str]] = {}
        for name, digest in hashes.items():
            groups.setdefault(digest, []).append(name)
        identical = next((names for names in groups.values() if len(names) >= 3), None)
        if identical is None or sorted(identical) != sorted(BYTE_IDENTICAL):
            print(f"{size}: the three did not agree byte-for-byte: {hashes}")
            failures += 1
            continue
        line = f"{size}: raw_lxml == xmltodict == gigaxml (sha256 {hashes['raw_lxml'][:16]}...)"
        if FIVE_COLUMN_ONLY in present:
            matched, compared = compare_five_columns(present[FIVE_COLUMN_ONLY], present["raw_lxml"])
            line += f"; pandas matches those {compared:,} rows on its five columns: {matched}"
            if not matched:
                failures += 1
        print(line)

    return 1 if failures else 0


def five_columns(path: pathlib.Path) -> Iterator[list[str]]:
    """A CSV's rows, with the sixth column dropped -- the five pandas can extract."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        next(reader, None)  # header
        yield from ([cell for index, cell in enumerate(row) if index != 5] for row in reader)


def compare_five_columns(candidate: pathlib.Path, reference: pathlib.Path) -> tuple[bool, int]:
    """Do the candidate's first five columns agree with the reference's, row for row?

    Compared over the rows the candidate actually produced, because the two files are
    not the same length: pandas stops early on the 4 GB document (documented in
    REPORT.md, and visible as ``complete_output: false`` in results.json). Comparing
    whole lists would report a mismatch for what is really "identical for every row it
    delivered, and it delivered fewer" -- a different, and much less alarming, fact.

    Returns ``(matched, rows_compared)``. The shortfall is not this function's job:
    ``rows_written`` and ``complete_output`` in results.json already carry it exactly.
    """
    matched = True
    compared = 0
    pairs = zip_longest(five_columns(candidate), five_columns(reference))
    for index, (left, right) in enumerate(pairs):
        if left is None or right is None:
            break  # one file is longer; the extra rows are the candidate's shortfall
        if left != right:
            print(f"    first five-column mismatch at data row {index}: {left} vs {right}")
            matched = False
            break
        compared += 1
    return matched, compared


if __name__ == "__main__":
    raise SystemExit(main())
