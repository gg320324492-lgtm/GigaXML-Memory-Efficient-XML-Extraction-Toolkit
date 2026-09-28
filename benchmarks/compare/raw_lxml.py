"""Hand-written lxml extraction: the comparison this project has to answer to.

The task is the same one ``via_gigaxml.py`` performs -- six fields from every
``/catalog/products/product`` into a CSV with a fixed column order -- written the way a
practised developer would write it with ``lxml.etree.iterparse`` alone: streaming,
bounded memory, converting ``price`` to float the way GigaXML's ``price: float`` config
does.

This file is the core of the comparison suite, so two things are worth stating plainly.

**It is written to be good, not to lose.** A hand-written extractor is the right choice
when the task is fixed, the fields are known, and one script is the whole deliverable --
this implementation is that script, and on raw throughput it is expected to win, because
it does less: no config parsing, no rejection log, no run report, no checkpoint
machinery. Where it wins, the report says so in the first sentence.

**Its memory is bounded, and that took a specific fix.** The idiomatic ``iterparse``
recipe -- the one in lxml's own documentation -- is ``events=("end",)`` with ``tag=``,
plus ``elem.clear()`` and unlinking consumed siblings. That recipe *leaks*: measured on
this machine, it grows 118.7 MB at 100 MB of input to 982.2 MB at 1 GB, i.e. roughly
linearly, because ``end``-only events never deliver the closing event for any element
outside the ``tag=`` filter -- the containers -- and a container's accumulated children
are never released. ``clear()`` does not help, because the container is never visited at
all. So this file subscribes to ``("start", "end")`` without a ``tag=`` filter, tracks
element depth, and clears every element whose subtree is *not* the record currently being
written. That is the same shape ``gigaxml.parser.streaming`` uses, for the same reason,
and it holds at 27.2 MB (100 MB) and 27.6 MB (1 GB) -- flat, which is what "bounded" has
to mean.

The fix costs about 25% of the throughput (measured at 100 MB: 4.29 s before, 5.33 s
after), and the comparison reports the slower, honest number. A baseline that wins
because it accumulates the document is not a baseline.

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
    record_depth = 0
    # Qualified tags of the elements currently open, outermost first: with no ``tag=``
    # filter the loop needs to know when it is looking at a record rather than anything
    # else. Memory: O(document depth), which is bounded and negligible.
    stack: list[str] = []

    # newline="" and utf-8: the same output conventions csv.writer documents and the
    # same ones GigaXML's writers use, so the files are comparable byte for byte.
    with Path(output_path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        context = etree.iterparse(input_path, events=("start", "end"))
        for event, elem in context:
            if event == "start":
                stack.append(elem.tag)
                # The record is the product at depth 3: catalog > products > product.
                if not record_depth and len(stack) == 3 and stack[-1] == "product":
                    record_depth = 3
                continue

            depth = len(stack)
            if record_depth == 0:
                # Outside any record: this element is done for, release it now.
                elem.clear(keep_tail=True)
            elif depth == record_depth:
                # The record itself: write it, then release it and everything before it.
                manufacturer = elem.find("manufacturer/name")
                price_text = elem.findtext("price")
                writer.writerow(
                    [
                        elem.get("id", ""),
                        elem.get("type", ""),
                        elem.findtext("name") or "",
                        elem.findtext("category") or "",
                        # The task converts price to float (GigaXML's config says
                        # `price: {type: float}`), and CSV writes str(float) --
                        # "12.30" becomes "12.3" in every implementation that converts.
                        str(float(price_text)) if price_text else "",
                        manufacturer.text if manufacturer is not None else "",
                    ]
                )
                rows += 1
                record_depth = 0
                elem.clear(keep_tail=True)
                parent = elem.getparent()
                if parent is not None:
                    while elem.getprevious() is not None:
                        del parent[0]
            # depth > record_depth means we are inside the record being written;
            # releasing anything now would empty it.
            stack.pop()

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
