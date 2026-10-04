# Competitor comparison suite

Four implementations of the **same extraction task** — six fields from every
`/catalog/products/product`, into identically-ordered CSV — written so that a reviewer
can open any one file, read it top to bottom, and see exactly what it does:

| File | What it is |
|---|---|
| [`raw_lxml.py`](raw_lxml.py) | hand-written `lxml.etree.iterparse`, streaming, clearing **and unlinking** every consumed element |
| [`via_xmltodict.py`](via_xmltodict.py) | `xmltodict` in its streaming `item_depth` mode |
| [`via_pandas.py`](via_pandas.py) | `pandas.read_xml` (cannot reach the nested `manufacturer/name` field — see REPORT) |
| [`via_gigaxml.py`](via_gigaxml.py) | the GigaXML CLI, driven by [`gigaxml-config.yaml`](gigaxml-config.yaml), run **in the sampled process** so its memory is actually measured |

The measured comparison and what it says — including the sizes where the hand-written
script wins — is in [`REPORT.md`](REPORT.md).

## Running it

```bash
pip install -e ".[dev,gui,bench]"     # bench pulls xmltodict + pandas; see pyproject.toml
python benchmarks/compare/run_comparison.py --sizes 100MB 1GB 4GB --repeats 5
```

This writes `results.json` (raw per-run values plus median/min/max/stdev/p95) next to
this file. The datasets are generated on demand:

```bash
gigaxml generate --size 100MB -o data/b100m.xml
gigaxml generate --size 1GB   -o data/b1g.xml
gigaxml generate --size 4GB   -o data/b4g.xml
```

Run any implementation by itself:

```bash
python benchmarks/compare/raw_lxml.py data/b100m.xml out.csv
```

## Verifying the outputs agree

Every implementation must produce byte-identical CSV (the fourth, pandas, is identical
on the five columns it can extract — its nested-field limit is documented in REPORT):

```bash
python benchmarks/compare/verify_outputs.py
```

## Verifying the memory numbers

An output that is wrong is obvious; a peak that is wrong is not. This suite has been
wrong about memory three times — once reporting a frozen 4.1 MB for every
implementation, once reporting a flat ~19 MB for GigaXML because the extraction ran in
a grandchild process the sampler could not see, and once reporting 14.4 MB for a child
that had allocated 384 MB because `peak_wset` is a Windows-only field and the
`or info.rss` fallback beside it quietly answered on every other platform. All three
looked like good news. The first two were found by `verify_peaks.py`; the third by a
CI runner, and it is why the wrapper now asks `gigaxml.run.peak_rss_mb()` — the
product's own per-platform high-water mark — rather than a field that exists on one
platform and a fallback that pretends to on the other two. `BENCHMARK-METHODOLOGY.md`
section 4 has the measurements.

```bash
python benchmarks/compare/verify_peaks.py
```

The guard measures an empty script's peak through the runner's own wrapper and fails on
any reading that is below it — or, less obviously, on any reading that is only just
above it. A value sitting on the floor usually means the sampled process never did the
work, and a flat curve across input sizes is the tell, because a real extraction's
curve is not perfectly level. It exits non-zero either way, so it can gate a run.

## The guard is red on raw_lxml, and that is a fact about the data, not the guard

As of the 2026-10-04 re-recording (`results.json`, the 2.0.0 sweep), `verify_peaks.py`
exits **rc=1** and flags **15 of the 55** peak readings it checks — every one of them
`raw_lxml` (100 MB, 1 GB and 4 GB, five each, 23.4–24.6 MiB). The fifteen are *not* a
harness failure and the numbers are not wrong. The evidence is measured, not asserted:
when the payload inside a single record is varied while the record *count* is held at
2,000 — 50 bytes per record against about 1 MiB, a 5,000-fold change in document length —
`raw_lxml`'s peak moves from 23.0 to 25.8 MiB, +2.8 MiB. A streaming parser holds one
record at a time and releases it (`clear()` plus unlink) before the next, so its peak is
the interpreter plus one record and does not grow with the document; `xmltodict`, which is
not streaming, moves 40 → 189 → 705 MiB across the same three sizes. So the near-floor
reading is the correct signature of a correct streaming implementation, and the guard’s
3 MiB discrimination band is simply larger than the ~1.1–2.1 MiB that implementation
actually costs over an idle interpreter.

**The intended repair, not yet implemented.** The guard conflates two jobs with different
failure modes and should report them with different severities:

1. *A physical-possibility check* — a peak **below** the idle floor is impossible and means
   the sampler is broken. This stays a hard failure (rc 1). It did not fire here.
2. *A heuristic near-floor warning* — a peak **within** the band is a heuristic, not a
   proof, and it produces a false positive as soon as a correct streaming implementation’s
   marginal cost falls under the band. It should be **downgraded to a notice when the same
   run wrote the whole document** (`rows_written == rows_in_document`, which the runner
   already records in `results.json` and the guard currently throws away), and kept a
   failure when the row count is missing, short, or the run exited non-zero. That second
   axis — rows written — is what separates a correct streaming parser from a dead sampler;
   the memory axis alone cannot, which is the finding.

**Explicitly rejected: raising `MIN_MEANINGFUL_GAP_MIB`.** That would make the guard blind
to any implementation whose real cost is between 1.7 and 3.0 MiB — the very class of
streaming implementations the guard exists to protect — and it contradicts the guard’s own
docstring (“the answer to a reading it flags is to re-measure it, never to move the
number”).

**Why it is not done yet.** This is a change to the guard’s contract (two verdicts where
there is now one, and a new input read from `results.json`), and it is deliberately kept
out of the re-recording task so the re-recorded numbers and the guard change can be
reviewed separately — a red guard that is then edited in the same change is exactly the
kind of “moved the threshold until it went green” the guard is meant to prevent. Until the
split lands, the guard stays out of CI and stays red on unchanged code; that is the honest
state, and the state is visible here rather than in a green build that measured nothing.
