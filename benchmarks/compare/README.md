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

**The repair, implemented.** The guard used to conflate two jobs with different failure
modes; it now reports them with different severities, and the split is the whole change:

1. *A physical-positivity check* — a peak **below** the idle floor is impossible and means
   the sampler is broken. This stays a hard failure (rc 1). It did not fire here.
2. *A near-floor warning* — a peak **within** the band is a heuristic, not a proof, and it
   produces a false positive as soon as a correct streaming implementation’s marginal cost
   falls under the band. It is now **downgraded to a notice when the same run wrote the
   whole document** (`rows_written == rows_in_document`, which the runner already recorded in
   `results.json` and the guard used to throw away), and kept a **failure** when the row
   count is missing, short, or the document’s record count is unknown. That second axis —
   rows written — is what separates a correct streaming parser from a dead sampler; the
   memory axis alone cannot, which is the finding.

The fifteen near-floor readings are **still reported**, one line each, but as
`MAY NOT HAVE MEASURED THE WORK (accepted)`, because each one wrote its full document
(290,900 / 2,978,816 / 11,915,264 rows). The run now exits **rc 0**, and the summary line
says why: *“15 reading(s) within the noise of the floor but accepted — each one wrote the
entire document in the same run, which is what a correct bounded streaming parser looks
like, not a sampler that measured nothing.”* They are not removed from the output; they are
reclassified, with the evidence that accepts them printed beside them.

**Explicitly rejected: raising `MIN_MEANINGFUL_GAP_MIB`, and still rejected after the
implementation.** The threshold is unchanged. Raising it would make the guard blind to any
implementation whose real cost is between 1.7 and 3.0 MiB — the very class of streaming
implementations the guard exists to protect — and it contradicts the guard’s own docstring
(“the answer to a reading it flags is to re-measure it, never to move the number”). The band
marking a reading as *worth asking about* is the point; the row count is what answers the
question. The change adds an input and a second verdict; it moves no line.

**Why it was deferred, and why that is no longer the state.** The split was deliberately kept
out of the re-recording task so the re-recorded numbers and the guard change could be
reviewed separately — a red guard that is then edited in the same change is exactly the kind
of “moved the threshold until it went green” the guard is meant to prevent. That separation
has now happened: the numbers above were re-recorded and reviewed on their own, this change
lands after them, and it is reviewed on its own. The guard is now green on unchanged code and
is wired into CI (see below) — a green build that measured the records it was given, rather
than a red one nobody ran.

## The guard runs in CI now — and what that does and does not buy

`benchmarks/compare/verify_peaks.py` is a step in the `ci-shape` job of `test.yml` (the
“comparison suite’s recorded peaks” step). Until this change nothing in `.github/`, `tests/`
or `tools/` referenced it, which — as the mutation harness put it — makes it a guard with no
job: a sentence in a file, and a red one at that.

**What the CI step guards.** It reads `results.json`, the committed record of the 2.0.0
sweep, and checks the recorded peaks against an idle floor it measures live on the runner. So
it catches the record drifting from the runs it claims to describe: a `results.json`
hand-edited, an implementation quietly dropped from the file, a peak lowered into or below
the floor with no whole-document row count to justify it, or the wrapper’s floor moving out
from under a reading that was accepted.

**What it cannot guard, stated rather than left to be discovered.** The datasets the sweep
reads — `data/b100m.xml`, `b1g.xml`, `b4g.xml`, about 5.5 GB — are generated on demand and
not committed, so the CI step **does not re-measure anything**. No code path it runs touches
an implementation, so a future performance regression — a slower parser, a hungrier one —
cannot turn it red; only the record’s consistency with itself can. That is a real limit and
it is the reason the step is cheap enough to sit in the ordinary pull request. The honest
summary: it guards the *record*, not the *product*, and the one thing it is strongest against
is a number someone typed.

It lives in `ci-shape` rather than in a pytest job or the nightly mutation run because it is a
check about the repository — the same family as `tracked` and the `src/` comment bounds — it
needs no Qt, no lxml and no generated data, its answer does not vary with interpreter or
platform (so the three-way matrix and the nightly would each pay for a duplicate), and the
mutation harness has no mutant for it to run against, because it reads no `src/` behaviour.
