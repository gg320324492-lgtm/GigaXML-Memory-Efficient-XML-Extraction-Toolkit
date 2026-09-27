"""Extraction via ``xmltodict`` in its streaming mode.

``xmltodict.parse(..., item_depth=N, item_callback=...)`` is the library's own streaming
entry point (1.0.x): at a depth of N it stops building one giant ``OrderedDict`` and
instead calls the callback for each element that closes at that depth, so memory stays
bounded. The items are plain dictionaries whose keys carry xmltodict's conventions --
attributes under ``@``, element text under ``#text``, repeated elements as lists.

**The conventions shape the code.** ``id`` and ``type`` are XML attributes, so they read
as ``product["@id"]``; ``price`` carries a ``currency`` attribute, so without disabling
attribute handling its *text* lives under ``product["price"]["#text"]``. That mapping
step is part of what using xmltodict costs on a task like this -- it is the library's
core idea (XML as dicts) applied to a document whose interesting fields are half
attributes and half nested elements.

Usage::

    python benchmarks/compare/via_xmltodict.py <input.xml> <output.csv>
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any, BinaryIO

import xmltodict

COLUMNS = ("id", "type", "name", "category", "price", "manufacturer")

#: Depth of <product> in this document: catalog (1) -> products (2) -> product (3).
PRODUCT_DEPTH = 3


def extract(stream: BinaryIO, output_path: str) -> int:
    """Stream every ``product`` into ``output_path``; return the row count."""
    rows = 0
    with Path(output_path).open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out)
        writer.writerow(COLUMNS)

        def on_item(path: list[Any], item: dict) -> bool:
            nonlocal rows
            # item_depth matches every element at that depth; this document has exactly
            # one kind, but the check is what keeps the handler honest about its target.
            if path[-1][0] != "product":
                return True
            price = item["price"]
            if isinstance(price, dict):  # an attribute on <price> moves the text to #text
                price = price["#text"]
            writer.writerow(
                [
                    item["@id"],
                    item["@type"],
                    item["name"],
                    item["category"],
                    str(float(price)),
                    item["manufacturer"]["name"],
                ]
            )
            rows += 1
            return True

        xmltodict.parse(stream, item_depth=PRODUCT_DEPTH, item_callback=on_item)
    return rows


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input.xml> <output.csv>", file=sys.stderr)
        return 2
    with Path(argv[1]).open("rb") as stream:
        rows = extract(stream, argv[2])
    print(f"{rows} rows -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
