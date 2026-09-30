# ERP — item master and order lines out of one document

A synthetic export shaped like the ones a warehouse or a parts catalogue hands you:
a **product master** and an **order log**, in the same file, pointing at each other by
number. It is generated rather than downloaded, so the row counts are exact and do not
move — which is what makes this the example to check first.

It is also the one that shows what this tool is *not*. See
["gigaxml does not join"](#gigaxml-does-not-join) below.

## Run it

```bash
# 1. make the export. --seed fixes the output byte for byte, which is why every
#    number in this file can be quoted exactly.
python -m gigaxml.cli generate --size 50MB --seed 42 -o out/erp.xml

# 2. the item master
python -m gigaxml.cli extract out/erp.xml -c examples/erp/products.yaml \
    -o out/products.csv --format csv --report out/products-report.json

# 3. the order lines -- same document, different record path
python -m gigaxml.cli extract out/erp.xml -c examples/erp/orders.yaml \
    -o out/orders.csv --format csv --report out/orders-report.json
```

Run these from the repository root, which is where the config paths in them point.

`generate` also writes `out/erp.xml.manifest.json` beside the file, carrying the seed
and a sha256 — so "which document was this" is answerable later without keeping the 50 MB.

## What you should see

Everything below is fixed by `--seed 42` and was measured on this machine:

| | |
|---|---|
| Document | 52,708,114 bytes, sha256 `f8feb02d8a2345b448eb5a1eaa859b5d133e33a65ea435ff0e88947143c53f8d` |
| `products.csv` rows | **145,600** |
| `orders.csv` rows | **48,533** |

```bash
python -c "import json; \
  print(json.load(open('out/erp.xml.manifest.json'))['record_count'])"   # -> 145600
python -c "import json; \
  print(json.load(open('out/products-report.json'))['rows'], \
        json.load(open('out/orders-report.json'))['rows'])"               # -> 145600 48533
```

The first line is the generator's own count and the second is what the extractor
produced. They are independent — one comes from writing the file, the other from reading it
back — so their agreeing is a real check rather than a tautology.

## The two things this config is for

**`decimal` for money.** See [`precision.py`](precision.py), which is the actual proof:

```bash
python examples/erp/precision.py
```

```
 sku               source              decimal                float
   1  123456789.123456789  123456789.123456789   123456789.12345679
   2                0.100                0.100                  0.1

decimal matches the source on 2 of 2 rows.
float differs from the source on 2 of 2 rows.
```

`inspect --infer-types` will never choose `decimal` for you, and its docstring says why: a
sampled `49.90` cannot be told from `49.9`, so narrowing money from sampled text is a
guess between two readings of the same value. Decimal is a decision, so the config makes
it.

**An attribute on a nested element.** `currency` is `price/@currency`, not a child of
`price`. A path that stopped at `price` would produce the number without the currency,
which for a money column is half the answer.

## gigaxml does not join

The two tables reference each other, and the obvious next step is a report by category.
That step is **not** gigaxml's: it is an extractor, not a query engine, and a config that
did joins would be a promise this tool does not make. Here is the whole of it, in
ordinary Python, reading the two CSVs the commands above produced:

```python
import csv
from collections import defaultdict
from decimal import Decimal

items = {}
with open("out/products.csv", encoding="utf-8", newline="") as handle:
    for row in csv.DictReader(handle):
        items[row["sku"]] = row

totals = defaultdict(lambda: [0, Decimal("0")])
for order in csv.DictReader(open("out/orders.csv", encoding="utf-8", newline="")):
    item = items[order["product_ref"]]
    entry = totals[item["category"]]
    entry[0] += int(order["quantity"])
    entry[1] += Decimal(item["price"]) * int(order["quantity"])

for name in sorted(totals):
    units, revenue = totals[name]
    print(f"{name:<12} {units:>10,} {revenue:>18,}")
```

which gives, on the same data:

```
category          units            revenue
audio            53,388     267,830,992.62
computing        56,334     278,784,679.98
cooling          50,918     258,551,020.57
electrical       50,301     249,472,223.06
...
tools            52,487     260,311,065.20
```

**631,508 units, 12 categories, and 0 orders whose `product_ref` matched nothing.** That
last number is the one worth checking: it is what says the two extractions actually line
up, and it is a count from the join rather than from either tool.

The arithmetic uses `Decimal` rather than `float` for the same reason the price column
does — the point of extracting a decimal is not lost by the person who multiplies it.
