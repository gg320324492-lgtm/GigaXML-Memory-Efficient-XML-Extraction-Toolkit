"""Hand-written lxml extraction: the comparison this project has to answer to.

The task is the same one ``via_gigaxml.py`` performs -- six fields from every
``/catalog/products/product`` into a CSV with a fixed column order -- written the way a
practised developer would write it with ``lxml.etree.iterparse`` alone: streaming,
``clear()``-ing consumed elements so memory stays bounded, converting ``price`` to float
the way GigaXML's ``price: float`` config does.

This file is the core of the comparison suite, so two things are worth stating plainly.

**It is written to be good, not to lose.** A hand-written extractor is the right choice
when the task is fixed, the fields are known, and one script is the whole deliverable --
this implementation is that script, and on raw throughput it is expected to win, because
it does less: no config parsing, no rejection log, no run report, no checkpoint
machinery. Where it wins, the report says so in the first sentence.

**It deliberately leaves out the machinery around the task** -- that is the second
dimension of the comparison (error isolation, resume, configuration, format switching),
and the report quantifies it. The extraction loop below is the whole of this file; what
GigaXML does beyond this loop is the part being bought.

Usage::

    python benchmarks/compare/raw_lxml.py <input.xml> <output.csv>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

from lxml import etree

#: The column order every implementation in this suite writes, GigaXML config order.
COLUMNS = ("id", "type", "name", "category", "price", "manufacturer")


def extract(input_path: str, output_path: str) -> int:
    """Stream every ``product`` into ``output_path``; return the row count."""
    rows = 0
    # newline="" and utf-8: the same output conventions csv.writer documents and the
    # same ones GigaXML's writers use, so the files are comparable byte for byte.
    with Path(output_path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        context = etree.iterparse(input_path, events=("end",), tag="product")
        for _, product in context:
            manufacturer = product.find("manufacturer/name")
            price_text = product.findtext("price")
            writer.writerow(
                [
                    product.get("id", ""),
                    product.get("type", ""),
                    product.findtext("name") or "",
                    product.findtext("category") or "",
                    # The task converts price to float (GigaXML's config says
                    # `price: {type: float}`), and CSV writes str(float) -- "12.30"
                    # becomes "12.3" in every implementation that does the conversion.
                    str(float(price_text)) if price_text else "",
                    manufacturer.text if manufacturer is not None else "",
                ]
            )
            rows += 1
            # Release the element and everything consumed before it: without this the
            # whole document accumulates and a 4 GB run eats the machine. This cleanup
            # is the standard iterparse idiom, not a benchmark trick.
            product.clear()
            while product.getprevious() is not None:
                del product.getparent()[0]
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input.xml> <output.csv>", file=sys.stderr)
        return 2
    rows = extract(argv[1], argv[2])
    print(f"{rows} rows -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
