# Examples

Three documents, three different questions, each runnable by copying the commands out of
its README. None of the data is committed — two are downloaded, one is generated.

| | What it is for | Size | The number to check |
|---|---|---|---|
| **[Wikipedia](wikipedia/)** | the memory claim, on a real export | 1.6 GB | rows == `inspect`'s count; peak ≈ 2% of input |
| **[ERP](erp/)** | two tables in one document, and money as a decimal | 50 MB, generated | 145,600 / 48,533, fixed by `--seed 42` |
| **[PubMed](pubmed/)** | the awkward shapes: attributes, nesting, repetition | 13 MB gz | rows == `inspect`'s count; the file you fetched |

Start with **ERP** — it is generated, so it runs in seconds and its numbers are exact.
Read **Wikipedia** for the thing this tool exists to do. Read **PubMed** before deciding
whether it fits your XML, because two of what it does with that document are limits and
both are written down there.

## Running them

Each example's README starts with its own commands. Two of them need something first:

```bash
python -m gigaxml.cli generate --size 50MB --seed 42 -o out/erp.xml   # ERP: no network
python examples/pubmed/fetch.py                                        # PubMed: ~13 MB
python benchmarks/datasets/fetch.py                                    # Wikipedia: ~356 MB
```

The last one is the benchmark suite's own script, used as-is rather than copied — it
records the URL, the size and the sha256 it downloaded, and verifies the file against what
it recorded. `examples/pubmed/fetch.py` imports its hashing and TLS context from the same
module for the same reason.

## What the examples do not do

* **They do not join.** The ERP example extracts an item master and an order log and then
  shows a three-line join in ordinary Python. gigaxml is an extractor, not a query engine,
  and a config that joined would be a promise this tool does not make.
* **They do not reshape.** A date the document stores as `2026` / `01` / `28` comes out as
  three columns, because assembling it is a decision about what the data means.
* **They do not flatten repetition.** Several `<Author>` under one article is not a column;
  making the author the record is a choice the config makes, and `inspect` is what shows
  the options.

Each of those is a real limit, written down where somebody deciding on this tool would
find it, rather than left to be discovered.
