# Wikipedia — a real 1.6 GB export, 560,605 articles

The benchmark suite generates its own documents. This example does not, and that is
the whole reason it is here: **the same config runs on Simple English Wikipedia's full
article dump**, which is a file nobody designed for this tool.

It also brings three things no generated fixture has:

* **a namespace.** Every element lives in MediaWiki's default namespace
  (`http://www.mediawiki.org/xml/export-0.11/`), so a path written without it does not
  resolve. The config declares it and every path carries the prefix.
* **a nested path.** The timestamp is three levels down, under `page/revision/`.
* **types worth converting.** The ids are numbers, not digits-as-text.

`ns` is MediaWiki's namespace number — `0` an article, `1` a talk page, `14` a category.
Extracting it as an `int` is what makes "give me only the articles" a filter rather than
a text search.

## Run it

Fetch the dump (this is the benchmark suite's own script, not a second copy of it — it
records the URL, the size and the sha256 it downloaded, and it verifies the file
against what it recorded):

```bash
python benchmarks/datasets/fetch.py
```

That writes `data/wikipedia-articles.xml` — about 356 MB compressed, **1.6 GB
decompressed** — and records the digest in `benchmarks/datasets/wikipedia.json`. The
data is gitignored; the script and the recorded metadata are not.

Then extract, from the repository root:

```bash
mkdir -p out
python -m gigaxml.cli extract data/wikipedia-articles.xml \
    -c examples/wikipedia/config.yaml \
    -o out/articles.csv --format csv --report out/articles-report.json
```

## What you should see

Measured on this machine, 2026-09-30, against the dump fetched 2026-09-27:

| | |
|---|---|
| Input | 1,696,517,417 bytes (1,617.92 MiB) |
| Rows | **560,605** |
| Time | 20.1 s (27,856 records/s) |
| **Peak RSS** | **~36 MiB — about 2.2% of the input** |

**Two of those four move and two do not, and the difference is worth keeping in your
head.** The row count moves because the dump is regenerated; the time moves with the
machine. The *input size* and the *order of magnitude of the peak* do not: that is the
claim, and a run whose peak grows with the input is a different program. The peak itself
is not reproducible to the decimal — two runs of this same command on this same file gave
35.78 and 36.82 MiB, which is why it is written "~36" rather than a figure to two places.

Three things to check, and the first is the one that catches a config that is wrong:

```bash
# 1. the row count agrees with what the analysis said the document holds
python -m gigaxml.cli inspect data/wikipedia-articles.xml --json | \
  python -c "import json,sys; print(next(c['count'] for c in json.load(sys.stdin)['candidates'] \
             if c['path']=='/mediawiki/page'))"
# -> 560605

# 2. the input really is the file that was recorded
python -c "import hashlib,pathlib; \
  print(hashlib.sha256(pathlib.Path('data/wikipedia-articles.xml').read_bytes()).hexdigest())"
# -> 2575aba62792ac2c1a907ac5196c0e3681b4d6cd5d1ba58c1c1a06df13629c64
#    which is `sha256_xml` in benchmarks/datasets/wikipedia.json

# 3. the run's own peak
python -c "import json; print(json.load(open('out/articles-report.json'))['peak_rss_mb'])"
# -> 35.8 ... 36.8, against 1,617.92 MiB of input
```

**The candidate list is ranked, and a rank is not a name.** On this document
`/mediawiki/page` is candidate **5**, behind `/mediawiki/page/redirect` at 124,218
occurrences — so grepping for "candidate 4" finds the redirect instead, and does so
quietly, because the output is a perfectly good line about a different element. Matching
on the path is why the command above spells it out. The same trap is written down in the
[PubMed example](../pubmed/), where the article is not in the top ten at the default
`--max-paths` at all.

**The row count will not be 560,605 forever, and it should not be.** This dump is
regenerated continuously, so the number moves with it. What does not move is the
relationship: the rows are the number of `<page>` elements, and the peak is a few tens
of megabytes whatever the file weighs. A run whose peak grew with the input is a
different program.

## A thing this config deliberately does not do

The timestamp is extracted as `string`, and `type: date` would fail on the first record.

`gigaxml`'s date type is a **date**: it is `datetime.date.fromisoformat`, which takes
`2026-05-18` and refuses anything carrying a time, and MediaWiki writes
`2026-05-18T12:42:53Z`. There is no datetime type. Carrying the text across unchanged is
the honest option; trimming the time off it or reformatting it would be inventing a
convention nobody asked for.

The `id` and `ns` fields *are* converted, because `datetime.date` is not involved and
`int` genuinely is what they are.
