# Benchmarks

The scripts behind every performance number in the README and in the benchmark report.
They are committed on purpose: a number nobody else can re-run is not evidence, and
"reproducible measurement harness" is the claim this project is making.

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
