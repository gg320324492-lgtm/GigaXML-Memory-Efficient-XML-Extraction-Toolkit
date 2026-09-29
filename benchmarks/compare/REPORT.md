# GigaXML vs Alternatives — measured comparison

> Everything in this report comes from `results.json` in this directory, produced by
> `run_comparison.py` on the machine and date in the environment block below. The raw
> per-run values are in that file; the summaries here can be recomputed from them. No
> number is estimated, borrowed, or averaged across machines.

## Environment

| | |
|---|---|
| Recorded | 2026-09-29, `run_comparison.py` (5 repeats per data point) |
| CPU | Intel 13th gen (Model 183), 16 cores |
| Memory | 31.8 GB |
| OS | Windows 11 |
| Python | 3.13.14 |
| lxml 6.1.3 · xmltodict 1.0.4 · pandas 3.0.6 · pyarrow 25.0.1 | gigaxml source tree (`__version__` 1.1.0) |

Datasets: generated 100 MB / 1 GB / 4 GB catalogues (identical schema, 290,900 /
2,978,816 / 11,915,264 records), plus one **real-world** dataset — Simple English
Wikipedia's full article dump (1.58 GB XML, MediaWiki namespace) — see the real-data
section at the end.

> The `gigaxml` field in this run's `results.json` environment block reads `0.1.0`, and
> that is wrong: it is the stale editable-install metadata, not the code that ran. The
> harness now reads `gigaxml.__version__` instead, and reports 1.1.0; the fix postdates
> the sweep, so the number in the file is left as it was recorded rather than quietly
> edited afterwards.

## Dimension A — raw throughput

Wall-clock medians over 5 runs (raw per-run values, min/max/stdev/p95 in
`results.json`). Peak RSS is the high-water mark of the extracting process.

### 100 MB

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml (hand-written)** | **4.76 s** | **61,100** | 24.8 MB | 290,900 | 18.9 MB |
| xmltodict (streaming) | 5.70 s | 51,035 | 43.4 MB | 290,900 | 18.9 MB |
| pandas.read_xml | 5.78 s | 50,346 | 2050.0 MB * | 290,900 | 13.5 MB * |
| GigaXML | 6.71 s | 43,385 | 33.2 MB | 290,900 | 18.9 MB |

### 1 GB

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml** | **46.5 s** | **64,002** | 26.1 MB | 2,978,816 | 189 MB |
| xmltodict | 56.8 s | 52,410 | 191.7 MB | 2,978,816 | 189 MB |
| pandas.read_xml | **failed** | — | 6284 MB † | — | — |
| GigaXML | 67.9 s | 43,873 | 33.4 MB | 2,978,816 | 189 MB |

### 4 GB

| Implementation | Median | rec/s | Peak RSS | Rows written | Output |
|---|---|---|---|---|---|
| **raw lxml** | **173.7 s** | **68,616** | 25.1 MB | 11,915,264 | 758 MB |
| xmltodict | 224.4 s | 53,090 | 707.3 MB | 11,915,264 | 758 MB |
| pandas.read_xml | 935.8 s | 11,205 † | 26004.8 MB | **10,485,760 †** | 691 MB † |
| GigaXML | 268.6 s | 44,356 | 33.5 MB | 11,915,264 | 758 MB |

\* pandas writes an empty `manufacturer` column: `read_xml` cannot reach text of an
element nested inside a child element (`manufacturer/name`), which the task's sixth
field requires. Its peak of 2050 MB on a 100 MB document is the price: it builds the
whole document into a DataFrame first, which is the design, not a defect of the task.

† **pandas returned exit 0 and silently wrote 1,429,504 rows fewer than the document
contains** — 10,485,760 of 11,915,264, i.e. it stopped at exactly 10 × 2²⁰ and wrote
nothing further, with no error, no warning, and a structurally perfect CSV that any tool
will open. Its 11,205 rec/s above is therefore computed over the rows it *delivered*;
an earlier version of this report divided by the document's record count and reported
about 20% more throughput for a run that dropped 12% of the data. The runner now counts
the output file and records `rows_written` and `complete_output` per run, and
`verify_outputs.py` fails when a produced CSV does not match — which is how this was
caught. On the five columns it *can* extract it is still byte-for-byte correct for every
row it wrote, so the two findings are separate: the values it produced are right, and it
did not produce enough of them. **A rate measured over a truncated output is not a rate,
and neither is a CSV you can trust because it opens cleanly.**

The 1 GB failure is the same shape of problem, one step earlier: every repeat died, and
each had already committed **6.3 GB** before it did.

### What dimension A says, plainly

**Hand-written lxml wins on raw throughput, at every size, by roughly 1.5×.** That is
the honest headline and there is no configuration or dataset in these runs where it
does not. If your task is fixed, your fields are known, and you will maintain the
script yourself, the 4.76 s per 100 MB is real and GigaXML does not beat it.

The margin was smaller before this suite was corrected twice, and the corrections are
themselves the point. The first version of the hand-written script used the idiomatic
``iterparse`` recipe from lxml's own documentation, which **leaks** — see the memory
note below. Making it fair cost about 35% of its throughput (3.53 s → 4.76 s at 100 MB),
and the 1.5× above is the honest number. A comparison that reports the leaky version's
speed would be measuring a defect.

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

- **The hand-written lxml script needs two non-obvious fixes to stay bounded, and it now
  has both** (the memory table has the numbers). This is a knowledge difference, not a
  capability one: `events=("start", "end")` with no `tag=` filter, releasing everything
  outside the record, *and unlinking rather than only clearing* are all lxml idioms once
  you have been bitten, and a hand-written script that applies them lands at 25–26 MB —
  within about 8 MB of GigaXML. GigaXML's contribution is that the user does not have to
  know any of it exists, let alone which of the three was missed.
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
| raw lxml | 145 |
| GigaXML script | 99 + 12 config lines |

The lines are comparable *for this fixed task*. What the line counts do not show: the
GigaXML script's 99 lines contain no validation, no error policy, no resume and no
format switching — that lives in the tool, shared, tested, and identical for every
task. Both scripts grew in this round, and neither growth is a cost of using the tool:
`raw_lxml.py` gained the unlink that makes it bounded, and `via_gigaxml.py` gained the
in-process execution that makes its memory measurable at all.

### Memory (all sizes, all implementations)

Peak RSS is the extracting process's own reading of the OS peak counter, and the floor
for an idle interpreter measured through the runner's own wrapper is **17.3 MiB** (it
moves with the machine, so it is re-measured every time rather than written down).
Every number below is that floor plus the work.

| Implementation | 100 MB | 1 GB | 4 GB | Grows with input? |
|---|---|---|---|---|
| raw lxml (hand-written) | 24.8 MB | 26.1 MB | 25.1 MB | **no** (see below) |
| xmltodict | 43.4 MB | 191.7 MB | 707.3 MB | **yes** |
| pandas.read_xml | 2050.0 MB | fails at 6284 MB | 26004.8 MB | yes, and 25 GB at 4 GB |
| **GigaXML** | **33.2 MB** | **33.4 MB** | **33.5 MB** | **no** |

**The hand-written script and GigaXML are both flat across a 40× range**, within about
8 MB of each other, and the report says that rather than implying the others are
"efficient" or that the gap is larger than it is. The hand-written script's three
readings scatter across 1.3 MB and are not monotonic — the 1 GB point is the highest of
the three — which is what flatness looks like: the spread is run-to-run noise, not a
trend. xmltodict and pandas do grow, and by a
lot.

#### What the previous version of this table got wrong

Two of the four numbers in the memory column were not measurements of the thing they
claimed to measure, and both errors flattered GigaXML. They are recorded here because the
shape of them is the transferable part.

**GigaXML's was never measured at all.** `via_gigaxml.py` shelled out with
`subprocess.run()`, so the extraction happened in a *grandchild* process while the
sampler's wrapper only ever saw its own child. A grandchild's memory is not in its
parent's counter. The readings that reached this table were 19.4 / 19.4 / 19.1 MB at
100 MB / 1 GB / 4 GB — the wrapper's own overhead, reproduced three times, and equal to
what an empty script costs through the same harness. **The CLI now runs in the sampled
process** (`runpy.run_module`, entered through its own `__main__` guard with `sys.argv`
set to what a user would type), and reads 33.2 / 33.4 / 33.5 MB. That is the number the
tool actually costs, it agrees with the 33.7 MiB the repository's own
`benchmarks/bench_extraction.py` has been reporting from a completely separate harness,
and it is the same order as the hand-written script's.

**The hand-written script's was blamed on the wrong cause.** The 48.1 / 296.6 / 1222.0 MB
line was attributed to libxml2's SAX parse context being released when the `iterparse`
object is destroyed. That was wrong, and the evidence that killed it is worth stating:
the growth did not appear during the loop, it appeared in the last 5% of the file, and a
census of the elements still in the tree at that moment found **85,264 `<order>` elements
sitting under `<orders>`**. These documents put an `<orders>` section — 96,966 orders at
100 MB, 992,938 at 1 GB — after `</products>`, and every one of them is outside the
record, so every one took the release path. That path called `elem.clear()`, and
`clear()` empties an element without detaching it: the element stays a child of its
parent and lxml still holds it. Clearing is not unlinking. Adding the unlink took the
line to **24.8 / 26.1 / 25.1 MB** — flat, and within about 8 MB of GigaXML, for about
4% of the throughput.

**The generalisable form:** a reading that sits *on* the measurement apparatus's own
overhead is not a reading of the thing, and neither is a curve flatter than real work
produces. Both failure modes read as good news, which is what makes them dangerous.

#### The traps in the hand-written script, and what each cost

Written the way lxml's documentation shows — `events=("end",)` with a `tag=` filter, plus
the documented `clear()`-and-unlink idiom — the script grew with the file: **118.7 MB at
100 MB of input, 982.2 MB at 1 GB, 3850.1 MB at 4 GB**. Nothing warns about this: the
cleanup idiom in the docs is correct for the elements it visits, and `end`-only events
never deliver a closing event for any element *outside* the `tag=` filter, so the
containers are never visited and never released.

| Stage | 100 MB | 1 GB | 4 GB | Time per 100 MB |
|---|---|---|---|---|
| as lxml documents it (`end` + `tag=`) | 118.7 MB | 982.2 MB | 3850.1 MB | 3.53 s |
| \+ `("start", "end")`, release outside the record | 48.1 MB | 296.6 MB | 1222.0 MB | 4.59 s |
| \+ unlink rather than only clear | **24.8 MB** | **26.1 MB** | **25.1 MB** | 4.76 s |

**What this report does not claim:** that GigaXML's 33 MB is better than the
hand-written script's 26 MB. It is worse, and the tool loses that comparison. What the
table buys is the knowledge — two non-obvious traps, one of which the lxml documentation
does not mention in any form, and the second of which the previous version of this very
report got wrong. What GigaXML adds is that a user never has to find either of them.

## Known unfairnesses in this comparison

1. **GigaXML's time is measured without a fresh interpreter.** The CLI now runs inside
   the sampled process (see the memory section for why that is not optional), so it does
   not pay the ~0.3 s of interpreter start-up a real `gigaxml extract` invocation costs
   — roughly 4% of the 100 MB medians, and a share of the throughput gap. A user running
   the installed command pays that; the table above does not show it. This was previously
   listed as an unfairness *against* GigaXML; correcting the memory measurement inverted
   it, and the direction it moved is smaller than the memory error it fixed.
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
6. **This report is not the product of one run, and the still-perturbed readings are
   named rather than smoothed away.** Two separate things happened on this machine, and
   they hit different runs.

   The first sweep was discarded outright: a background application using 1.5 cores
   widened xmltodict's 1 GB runs to 62.6 / 71.0 / 82.0 s. Contention moves wall-clock
   and not memory — xmltodict's peak did not change by a byte across that spread.

   The sweep behind this report is the one after it, on an idle machine. It came back
   clean of contention for eleven of its twelve groups. The exception was **raw lxml's
   1 GB group, whose repeats 4 and 5 ran at 61.3 s and 57.3 s against 43.7–44.5 s for
   the other three** — a `stdev` of 8.4 s where GigaXML's simultaneous group sat at
   1.1 s. That
   asymmetry is the tell: a slower machine slows everything at once, so two groups
   holding steady while one alone jumps means something took the CPU for a few seconds,
   mid-group. That group was **re-measured on a separate pass** — 45.9 / 46.1 / 46.5 /
   47.6 / 47.4 s, `stdev` **0.74 s** — and it is what the 1 GB row above reports. Its
   median moved with it, 44.5 s to 46.5 s; the 1.5× conclusion did not, because a median
   is the middle of the sorted values and two outliers do not move it.

   **One group still in the data is genuinely not reproducible, and it is not the
   machine's doing: pandas at 4 GB.** Its five repeats run 818.2 / 780.5 / 952.4 /
   1074.0 / 935.8 s — a `stdev` of **116.8 s**, and a 38% spread between its fastest
   and slowest run, where the three implementations measured alongside it spread 0.6%,
   1.0% and 3.4%. By the reasoning above that is not contention: a busy machine would
   have widened all four groups, and three of them barely moved. It is pandas itself. Its
   peak on that file is 26,004.8 MB against this machine's 31.8 GB, so it runs with about
   6 GB of headroom and the jitter is memory pressure — allocation, page movement,
   collection — rather than a contended CPU.

   That belongs in the findings rather than in a footnote, because for anything that has
   to be scheduled it is the worst of the three things wrong with pandas here. It is not
   only the slowest, and not only quietly 12% short of the document; it is
   **unpredictable** — the same job on the same file ran nearly five minutes faster on
   one run than on another. Slowness can be waited out and provisioned for. A runtime
   that moves by 38% between identical runs cannot be put on a schedule at all. Its
   median is reported above with every other row's, and for this row the median is the
   least useful of the five numbers in the file.

   One smaller sample is also left in rather than tidied away: raw lxml's 100 MB first
   repeat, 6.29 s against a 4.58 s minimum. Per-run values, `stdev`, `min` and `max` for
   every group are in `results.json`, so a reader can see the spread rather than take the
   median on trust.

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

## How these numbers are checked

A report of measurements is worth what its checks are worth, so both of the suite's
checks are runnable and both have caught a real defect:

```bash
python benchmarks/compare/verify_outputs.py   # did every implementation write the same rows?
python benchmarks/compare/verify_peaks.py     # did the sampler measure the thing it claims to?
```

`verify_outputs.py` compares the CSVs byte for byte across the three implementations that
can produce identical output, and over the rows pandas actually delivered for the two it
can partly produce.

`verify_peaks.py` is the one that matters for the memory table, and it exists because
this suite has twice reported a memory number that was not a measurement. It runs an
empty script through the runner's *own* wrapper — imported from `run_comparison`, so the
two cannot drift apart — and fails on any reading below that floor. It also fails on any
reading that is only just above it, because that is the other shape the same blind spot
takes: a harness that sampled a process which never did the work reports the wrapper's
overhead, and a value sitting 1.5 MB above the floor would otherwise pass. Feeding this
report's previous `results.json` to it names all fifteen of GigaXML's phantom readings.

The floor is measured on every run and never written down, and the noise band around it
is the spread of repeated floor runs rather than a constant. A reading has to clear both
to be credited with having done the work.

## Raw outputs

Per-run wall-clock, exit codes, peak RSS samples, stdout/stderr tails and the full
summary statistics are in [`results.json`](results.json). The implementation scripts
are [`raw_lxml.py`](raw_lxml.py), [`via_xmltodict.py`](via_xmltodict.py),
[`via_pandas.py`](via_pandas.py), [`via_gigaxml.py`](via_gigaxml.py); the task config
is [`gigaxml-config.yaml`](gigaxml-config.yaml); the runner is
[`run_comparison.py`](run_comparison.py), which checkpoints `results.json` after every
implementation — a 4 GB pandas run peaks near this machine's entire memory budget, and a
sweep that dies on its last group should not take the previous twelve with it.
