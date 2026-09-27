# GigaXML vs Alternatives — measured comparison

> Everything in this report comes from `results.json` in this directory, produced by
> `run_comparison.py` on the machine and date in the environment block below. The raw
> per-run values are in that file; the summaries here can be recomputed from them. No
> number is estimated, borrowed, or averaged across machines.

## Environment

| | |
|---|---|
| Recorded | 2026-09-28, `run_comparison.py` (5 repeats per data point) |
| CPU | Intel 13th gen (Model 183), 16 cores |
| Memory | 31.8 GB |
| OS | Windows 11 |
| Python | 3.13.14 |
| lxml 6.1.3 · xmltodict 1.0.4 · pandas 3.0.6 · pyarrow 25.0.1 | gigaxml source tree (`__version__` 1.1.0) |

Datasets: generated 100 MB / 1 GB / 4 GB catalogues (identical schema, 290,900 /
2,978,816 / 11,915,264 records), plus one **real-world** dataset — Simple English
Wikipedia's full article dump (1.58 GB XML, MediaWiki namespace) — see the real-data
section at the end.

## Dimension A — raw throughput

Wall-clock medians over 5 runs (raw per-run values, min/max/stdev/p95 in
`results.json`). Peak RSS is the high-water mark of the extracting process.

### 100 MB

| Implementation | Median | rec/s | Peak RSS | Output |
|---|---|---|---|---|
| **raw lxml (hand-written)** | **3.53 s** | **82,478** | 4.1 MB | 18.9 MB |
| xmltodict (streaming) | 5.60 s | 51,974 | 4.1 MB | 18.9 MB |
| pandas.read_xml | 5.70 s | 51,044 | 4.1 MB | 13.5 MB * |
| GigaXML | 6.95 s | 41,832 | 4.1 MB | 18.9 MB |

### 1 GB

| Implementation | Median | rec/s | Peak RSS | Output |
|---|---|---|---|---|
| **raw lxml** | **35.6 s** | **83,592** | 4.1 MB | 189 MB |
| xmltodict | 57.4 s | 51,868 | 4.1 MB | 189 MB |
| pandas.read_xml | **failed** | — | — | — |
| GigaXML | 69.2 s | 43,038 | 4.1 MB | 189 MB |

### 4 GB

| Implementation | Median | rec/s | Peak RSS | Output |
|---|---|---|---|---|
| **raw lxml** | **143.9 s** | **82,815** | 4.1 MB | 758 MB |
| xmltodict | 229.2 s | 51,993 | 4.1 MB | 758 MB |
| pandas.read_xml | not run | — | — | — |
| GigaXML | 281.2 s | 42,378 | 4.1 MB | 758 MB |

\* pandas writes an empty `manufacturer` column: `read_xml` cannot reach text of an
element nested inside a child element (`manufacturer/name`), which the task's sixth
field requires. On the five columns it can extract, its output is **line-for-line
identical** to the other implementations.

### What dimension A says, plainly

**Hand-written lxml wins on raw throughput, at every size, by roughly 1.9×.** That is
the honest headline and there is no configuration or dataset in these runs where it
does not. If your task is fixed, your fields are known, and you will maintain the
script yourself, the 3.5 s per 100 MB is real and GigaXML does not beat it.

## Dimension B — what the hand-written script does not have

The same seven experiments were run against every implementation. These are behaviours,
not adjectives: each was executed and the observed result recorded.

### A corrupted record in the middle of the document

A 100 MB document with one unparseable `price` value (`not-a-number`) was extracted by
every implementation:

| Implementation | Observed behaviour |
|---|---|
| raw lxml | `ValueError` — process exits 1; the CSV contains only the header. Every row after the bad record is lost. |
| xmltodict | `ValueError` — identical: exit 1, header only. |
| pandas | `ValueError` during conversion — exit 1; **no output file at all** (the full parse finishes before any row is written). |
| GigaXML, `on_error: abort` (default) | exit 1 with a message naming the field, the type and the raw value — and **the target file does not exist** (the atomic writer never moves a partial output into place). |
| GigaXML, `on_error: quarantine` | **exit 0. 290,899 rows written — every good record — and the one bad record in `rejected.jsonl`** with its index, field name, type error and raw value. |

### Resume after interruption

| Implementation | Capability |
|---|---|
| raw lxml / xmltodict / pandas | **None.** An interrupted run starts over from byte zero. |
| GigaXML | `--checkpoint-every N` commits the output in parts; `--resume` re-enters at the last committed part and **verifies the source file's sha256 and the config hash first — a changed source or config is refused, with both values printed**. |

### Configuration

| Implementation | Changing the six fields to different ones |
|---|---|
| raw lxml | Edit the column tuple and the row-construction code (2 places). |
| xmltodict | Edit the item-reading code. |
| pandas | Edit the column selection. |
| GigaXML | Edit `gigaxml-config.yaml` — the same file documents itself, and `gigaxml inspect <file>` proposes a starting point for any *new* document without code. |

### Output format switching

| Implementation | CSV → JSONL → Parquet |
|---|---|
| raw lxml | Replace the `csv.writer` with hand-rolled JSONL writing and a hand-rolled Arrow schema (the Parquet case is where the obvious approach collects the whole output in memory). |
| xmltodict | Same. |
| pandas | `to_csv` → `to_json` / `to_parquet` — one line each (for documents that fit; see the 1 GB failure). |
| GigaXML | `--format jsonl` / `--format parquet` — a flag, with the writer machinery, batch sizes and atomic move shared. |

### Lines of code for the task

| Implementation | Lines (honest `wc -l`, including the config where one exists) |
|---|---|
| pandas | 57 |
| xmltodict | 80 |
| raw lxml | 84 |
| GigaXML script | 86 + 12 config lines |

The lines are comparable *for this fixed task*. What the line counts do not show: the
GigaXML script's 86 lines contain no validation, no error policy, no resume and no
format switching — that lives in the tool, shared, tested, and identical for every
task.

### Memory (all sizes, all implementations)

Peak RSS of every *streaming* implementation stayed at **4.1 MB** at every size — the
hand-written lxml with proper `clear()`, xmltodict's `item_depth` streaming, and
GigaXML all keep memory flat, and the report says so rather than implying GigaXML is
unique in this. What GigaXML asserts about its own memory is the **ceiling with proof**
(a runnable harness in `benchmarks/bench_extraction.py`): 4 GB at 33.7 MiB peak, with a
reversed test that fails if a future change loads the document.

## Known unfairnesses in this comparison

1. **GigaXML runs as a subprocess** (`python -m gigaxml.cli`), so each run pays
   interpreter start-up (~0.3 s) — ~4 % of the 100 MB medians and negligible at 1 GB
   and above.
2. **The task was defined for this comparison.** Six fields of a catalogue record are a
   realistic extraction, but a real project's task may be smaller (favouring the
   scripts) or shaped differently.
3. **The comparison scripts were written by this project's author.** They follow each
   library's documented usage and were kept honest in plain sight (each is one
   readable file), but an xmltodict or pandas expert might squeeze more from them.
4. **Competitor versions are the newest available** (xmltodict 1.0.4, pandas 3.0.6,
   lxml 6.1.3). pandas 3.0's `read_xml` failure on the 1 GB file may be fixed in a
   later release.
5. **The synthetic datasets match the documented config** (they are produced by this
   repository's generator). The real-data benchmark below exists to answer exactly
   that objection.

## Real data — Simple English Wikipedia

[Fetch script](../datasets/fetch.py) → 356,186,307 B bz2 → **1,696,517,417 B XML**
(sha256 recorded at download time in `datasets/wikipedia.json`). Every element lives in
the MediaWiki namespace (`http://www.mediawiki.org/xml/export-0.11/`) — the one thing
the synthetic datasets cannot exercise.

Extraction of every `<page>` (560,605 of them: id, title, namespace, revision id,
sha1 — namespace-qualified config):

```
inspect: /mediawiki/page ... count=560,605
extract: report rows = 560,605 ; CSV data rows = 560,605
```

Three sources, one number — the dump itself (sha256), the structure report, and the
extracted CSV agree.

## Raw outputs

Per-run wall-clock, exit codes, peak RSS samples, stdout/stderr tails and the full
summary statistics are in [`results.json`](results.json). The implementation scripts
are [`raw_lxml.py`](raw_lxml.py), [`via_xmltodict.py`](via_xmltodict.py),
[`via_pandas.py`](via_pandas.py), [`via_gigaxml.py`](via_gigaxml.py); the task config
is [`gigaxml-config.yaml`](gigaxml-config.yaml); the runner is
[`run_comparison.py`](run_comparison.py).
