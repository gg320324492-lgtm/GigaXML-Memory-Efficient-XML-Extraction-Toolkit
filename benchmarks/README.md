# Benchmarks

The scripts behind every performance number in the README and in the benchmark report.
They are committed on purpose: a number nobody else can re-run is not evidence, and
"reproducible measurement harness" is the claim this project is making.

**How a number here is traced back to the run that produced it** — the identity block,
the raw per-run values, the two thresholds, and how the memory measurement is enforced —
is in [`BENCHMARK-METHODOLOGY.md`](../BENCHMARK-METHODOLOGY.md) at the repository root.
What was measured at each released version, **including the four versions nobody
measured**, is in [`history/`](history/).

## Datasets

Generated, never committed — a 4 GB file does not belong in a repository:

```bash
gigaxml generate --size 100MB -o data/b100m.xml    # 100.57 MiB,   290,900 records
gigaxml generate --size 1GB   -o data/b1g.xml      # 1033.65 MiB, 2,978,816 records
gigaxml generate --size 4GB   -o data/b4g.xml      # 4142.72 MiB, 11,915,264 records
```

**`--size` is approximate.** `--size 1GB` produces 1033.65 MiB, not 1024. Quote the
measured size, not the argument.

## Running

```bash
python benchmarks/bench_extraction.py     # three sizes x two configs -- the main table
python benchmarks/bench_resident.py       # import-by-import footprint, batch-size sweep
python benchmarks/bench_inspect.py        # inspect throughput and profile
```

Each prints a table and writes `results.json` next to its scratch files.

## The regression baseline — the one performance number CI asserts

The rest of this directory produces numbers that are **recorded, never asserted**: a
benchmark that fails when a machine is busy is worse than no benchmark, and this project
decided that (roadmap A11) after paying for it. `perf_baseline.py` is the deliberate
exception, and it is loose on purpose.

```bash
python benchmarks/perf_baseline.py             # check against the recorded baseline
python benchmarks/perf_baseline.py --update    # re-record it, and say why
```

It generates a 10 MB document, extracts it five times in fresh interpreters that measure
their **own** peak working set, and requires two things:

| | |
|---|---|
| throughput | at least **60%** of the recorded reference, taken as the median of five runs |
| peak memory | at most **60 MiB** — absolute, not relative |

The memory ceiling is the one that matters. Bounded memory is the property the tool is
sold on, it belongs to the code rather than to the hardware, and a regression in it does
not need a reference to be visible. The throughput floor exists to catch a change that
made the tool twice as slow, which is the size of regression a person would defend and
the size nobody ships on purpose.

**What it was verified against.** Adding a per-record loop that does no useful work and
changes no output at all — byte-for-byte identical CSV, so all 1,288 functional tests
stay green — drops throughput to 51% of the reference and turns the check red. That is
the whole reason this file exists: the failure it catches is invisible to every other
test in the repository.

If it goes red on a change you believe is harmless, read the numbers it printed before
touching the baseline. A shared runner under load and a real 50% regression look
identical from here; re-recording the baseline is how you tell them apart, and the file
records the machine the reference came from.

## Two configs, and why both

`bench_extraction.py` runs a **six-field** config and a **one-field** control.

```yaml
# The benchmark. An attribute, a leaf, a nested path, a type conversion.
record: /catalog/products/product
fields:
  id:           {path: '@id'}
  type:         {path: '@type'}
  name:         {path: name}
  category:     {path: category}
  price:        {path: price, type: float}
  manufacturer: {path: manufacturer/name}
```

The one-field config is a control, never the headline. It is the cheapest member of this
family of configs and therefore the most flattering; field count costs about **2.5x** in
throughput, so quoting the easy config alone would overstate what a real config costs.

## How memory is measured

Peak RSS is read with `psutil` **inside a fresh interpreter**, not in the process running
the harness. On Windows there is no `resource.getrusage`, and measuring in-process would
attribute the profiler's own allocations to the run.

**The baseline matters, and it is the same everywhere in this repository: it is taken
after every import.** `pyarrow` alone costs 14.5 MiB to import, so a delta measured
before it is a different quantity — roughly 30 MiB larger on the same run. Both numbers
are true; they are not comparable, and only the post-import one is published.

The reports give **both** peak and delta. Peak includes the interpreter and every import
(32-34 MiB of which is overhead that has nothing to do with the data); delta is the part
the workload is responsible for. A single number would let either be read as the other.

## What these scripts do not do

- **They do not repeat runs.** Every cell is one measurement. Memory figures sit in a
  noise band of a couple of MiB, which is why the criteria are additive bands rather than
  ratios.
- **They do not run in CI.** Memory and throughput measurements take minutes and are not
  a pass/fail signal; `tests/performance/` holds the assertions that are.
- **They do not measure 10 GB.** 4 GB is the largest measured size.
