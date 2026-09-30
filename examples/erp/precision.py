"""Money, three ways -- the one check in this example that is not a row count.

Run it after the other two extracts, or on its own::

    python examples/erp/precision.py

It writes a two-record document to a temporary directory, extracts the same
column three times -- as ``decimal``, as ``float``, and as text -- and prints the
result side by side. Nothing is committed: the document is two lines long and it
is built here, so there is no fixture to keep in step with the code.

**What it is for.** "The price is a decimal" is a claim about correctness that a
row count cannot support, because the two produce identical files on every
ordinary price. They differ only where binary floating point is already wrong,
so the fixture uses two numbers that are wrong in it:

* ``123456789.123456789`` has nine decimal places, and ``float`` keeps seven of
  them -- the last two are gone before the value is ever written;
* ``0.100`` is three characters and ``float`` writes ``0.1``, which is the same
  number and not the same text, so a diff against the source shows a change that
  no arithmetic noticed.

``decimal`` keeps both, because that is what it is for. The third column is the
control: the text is carried across untouched, which is what ``decimal`` would
look like if it were not actually parsing anything.
"""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
from pathlib import Path

DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<catalog><products>
  <product id="1"><name>Precision</name><price currency="USD">123456789.123456789</price></product>
  <product id="2"><name>Trailing zeros</name><price currency="EUR">0.100</price></product>
</products></catalog>
"""

CONFIG = """record: /catalog/products/product
fields:
  sku: {path: '@id'}
  as_decimal: {path: price, type: decimal}
  as_float: {path: price, type: float}
  as_text: {path: price}
  currency: {path: 'price/@currency'}
"""


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="gigaxml-erp-precision-"))
    source = workdir / "money.xml"
    config = workdir / "config.yaml"
    output = workdir / "money.csv"
    source.write_text(DOCUMENT, encoding="utf-8")
    config.write_text(CONFIG, encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "gigaxml.cli",
            "extract",
            str(source),
            "-c",
            str(config),
            "-o",
            str(output),
            "--format",
            "csv",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr.strip()[-2000:], file=sys.stderr)
        return completed.returncode

    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    width = max(len(row["as_text"]) for row in rows)
    print(f"{'sku':>4}  {'source':>{width}}  {'decimal':>{width}}  {'float':>{width}}")
    for row in rows:
        print(
            f"{row['sku']:>4}  {row['as_text']:>{width}}  "
            f"{row['as_decimal']:>{width}}  {row['as_float']:>{width}}"
        )

    kept = sum(1 for row in rows if row["as_decimal"] == row["as_text"])
    print()
    print(f"decimal matches the source on {kept} of {len(rows)} rows.")
    if kept != len(rows):
        print("that is a bug, not a rounding difference", file=sys.stderr)
        return 1

    lost = [row for row in rows if row["as_float"] != row["as_text"]]
    print(f"float differs from the source on {len(lost)} of {len(rows)} rows.")
    print(f"output: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
