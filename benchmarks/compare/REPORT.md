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

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml (hand-written)** | **4.59 s** | **63,321** | 48.1 MB | 290,900 | 18.9 MB |
| xmltodict (streaming) | 5.61 s | 51,881 | 43.4 MB | 290,900 | 18.9 MB |
| pandas.read_xml | 5.89 s | 49,413 | 2050.0 MB * | 290,900 | 13.5 MB * |
| GigaXML | 7.05 s | 41,262 | 19.4 MB | 290,900 | 18.9 MB |

### 1 GB

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml** | **44.2 s** | **67,343** | 296.6 MB | 2,978,816 | 189 MB |
| xmltodict | 57.1 s | 52,202 | 191.7 MB | 2,978,816 | 189 MB |
| pandas.read_xml | **failed** | — | — | — | — |
| GigaXML | 70.5 s | 42,279 | 19.4 MB | 2,978,816 | 189 MB |

### 4 GB

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml** | **176.6 s** | **67,475** | 1222.0 MB | 11,915,264 | 758 MB |
| xmltodict | 226.2 s | 52,686 | 707.4 MB | 11,915,264 | 758 MB |
| pandas.read_xml | 859.9 s | 12,193 † | 26838.2 MB | **10,485,760 †** | 691 MB † |
| GigaXML | 266.2 s | 44,764 | 19.1 MB | 11,915,264 | 758 MB |

\* pandas writes an empty `manufacturer` column: `read_xml` cannot reach text of an
element nested inside a child element (`manufacturer/name`), which the task's sixth
field requires. Its peak of 2050 MB on a 100 MB document is the price: it builds the
whole document into a DataFrame first, which is the design, not a defect of the task.

† **pandas returned exit 0 and silently wrote 1,429,504 rows fewer than the document
contains** — 10,485,760 of 11,915,264, i.e. it stopped at exactly 10 × 2²⁰ and wrote
nothing further, with no error, no warning, and a structurally perfect CSV that any tool
will open. Its 12,193 rec/s above is therefore computed over the rows it *delivered*;
an earlier version of this report divided by the document's record count and reported
about 20% more throughput for a run that dropped 12% of the data. The runner now counts
the output file and records `rows_written` and `complete_output` per run, and
`verify_outputs.py` fails when a produced CSV does not match — which is how this was
caught. **A rate measured over a truncated output is not a rate, and neither is a CSV
you can trust because it opens cleanly.**

### What dimension A says, plainly

**Hand-written lxml wins on raw throughput, at every size, by roughly 1.5×.** That is
the honest headline and there is no configuration or dataset in these runs where it
does not. If your task is fixed, your fields are known, and you will maintain the
script yourself, the 4.6 s per 100 MB is real and GigaXML does not beat it.

The margin was larger before this suite was corrected, and the correction is itself the
point: the first version of the hand-written script used the idiomatic ``iterparse``
recipe from lxml's own documentation, which **leaks** -- see the memory row below. Making
it fair cost it about 24% of its throughput (3.53 s → 4.59 s at 100 MB), and the 1.5×
above is the honest number. A comparison that reports the leaky version's speed would be
measuring a defect.

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

**Two more behaviours, measured on the real runs rather than reasoned about:**

- **The hand-written lxml script's memory grows with the file unless you know the trap**
  (the table above has the numbers). This is a knowledge difference, not a capability
  one: `events=("start", "end")` plus releasing everything outside the record is a
  documented lxml idiom once you have been bitten, and a hand-written script that applies
  it lands near GigaXML's memory. GigaXML's contribution is that the user does not have
  to know it exists.
- **pandas can produce a complete-looking output that is missing data** (the 4 GB row
  above): exit 0, 12% of the records absent, nothing said. For a pipeline that reads its
  own output as fact, that is a worse failure than being slow.

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

Peak RSS is the child's own reading of the OS peak counter, and the floor for an idle
interpreter measured through the same wrapper is **18.2 MiB** — every number below is
that floor plus the work. `verify_peaks.py` checks exactly this and fails the suite if a
reading falls below the floor.

| Implementation | 100 MB | 1 GB | 4 GB | Grows with input? |
|---|---|---|---|---|
| raw lxml (hand-written) | 48.1 MB | 296.6 MB | 1222.0 MB | **yes, still** (see below) |
| xmltodict | 43.4 MB | 191.7 MB | 707.4 MB | **yes** |
| pandas.read_xml | 2050.0 MB | fails | 26838.2 MB | yes, and 27 GB at 4 GB |
| **GigaXML** | **19.4 MB** | **19.4 MB** | **19.1 MB** | **no** |

**Only GigaXML's memory is flat across a 40× range**, and the report says so rather
than implying the others are "efficient". The hand-written script is still the honest
competitor and still wins on time; it does not match this on memory, and the honest
reading is that the streaming discipline is not free to write by hand.

#### The trap, and what it cost to fix

Written the way lxml's documentation shows — ``events=("end",)`` with a ``tag=`` filter,
plus the documented ``clear()``-and-unlink idiom — the hand-written script grew with
the file: **118.7 MB at 100 MB of input, 982.2 MB at 1 GB, 3850.1 MB at 4 GB**. Nothing
warns about this: the cleanup idiom in the docs is correct for the elements it visits,
and ``end``-only events never deliver a closing event for any element *outside* the
``tag=`` filter, so the containers are never visited and never released. Switching to
``events=("start", "end")`` without a tag filter and releasing every element that is not
the record being written cut that to 48.1 / 296.6 / 1222.0 MB — a 68% reduction at 4 GB,
at a cost of about 24% throughput.

**Where the residual growth comes from — measured, not guessed.** Sampling `peak_wset`
every 500,000 records while the loop runs shows a flat 24.3–24.8 MB at every size, and
the full number appears only after the loop exits:

| Document | during the loop | after the iterparse object is released | reported above |
|---|---|---|---|
| 100 MB | 24.7 MB | 47.3 MB | 48.1 MB |
| 1 GB | 24.8 MB | 298.4 MB | 296.6 MB |
| 4 GB | 24.3 MB | — | 1222.0 MB |

**The record processing itself is flat at every size.** The growth is the lxml SAX parse
context being released when the `iterparse` object is destroyed: libxml2 allocates that
context in proportion to the document, and it comes back in one piece at the end. So the
honest description of the hand-written script is "bounded *while extracting*, with a
parser-context cost that scales with the file" — which is a real and common shape, and
not the same claim as GigaXML's, whose 19 MB at 4 GB includes no such term.

**What this report does not claim:** that a hand-written script cannot do better.
The knowledge is learnable, the record loop here is already the bounded version, and the
remaining cost is libxml2's rather than the script's. What the tool adds is that a user
never has to learn any of it.

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
