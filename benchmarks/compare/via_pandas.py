"""Extraction via ``pandas.read_xml``.

``pandas.read_xml`` parses an XML document into a ``DataFrame``: one row per match of
its ``xpath``, one column per element or attribute. It is the DataFrame-native way to
get XML into pandas, and for documents that fit in memory and fields that sit directly
on the matched element it does the job in one call.

**Two task-level limits, recorded rather than papered over:**

1. **The nested field is out of reach.** The task's sixth column is
   ``manufacturer/name`` -- text of an element *inside* another child element.
   ``read_xml`` flattens only direct text and attributes of the matched element; there
   is no way to point a column two levels down. This script therefore produces the five
   columns ``read_xml`` can reach and leaves ``manufacturer`` empty -- and the report
   records that as the implementation's answer to the task, not as a bug.

2. **Streaming depends on the parser path.** ``read_xml`` accepts an ``iterparse``
   option that keeps memory bounded on large documents; whether the installed pandas
   honours it for this document shape is checked by measurement, not by documentation
   (its peak RSS is in the results file like every other implementation's).

Usage::

    python benchmarks/compare/via_pandas.py <input.xml> <output.csv>
"""

from __future__ import annotations

import sys

import pandas as pd

COLUMNS = ("id", "type", "name", "category", "price", "manufacturer")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input.xml> <output.csv>", file=sys.stderr)
        return 2
    frame = pd.read_xml(
        argv[1],
        xpath="//product",
        # Both attributes and child elements: `id`/`type` are attributes, the rest
        # are elements.
    )
    frame["price"] = frame["price"].astype(float)  # the task converts price to float
    for column in COLUMNS:
        if column not in frame.columns:
            frame[column] = ""  # documented above: manufacturer/name is unreachable
    frame = frame[[*COLUMNS]]
    frame.to_csv(argv[2], index=False, lineterminator="\n")
    print(f"{len(frame)} rows -> {argv[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
