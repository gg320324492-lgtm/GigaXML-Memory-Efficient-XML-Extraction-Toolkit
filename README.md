# GigaXML

A production-oriented CLI toolkit for profiling, validating and extracting structured data
from multi-gigabyte XML files with bounded memory usage.

## What this is

The point of this project is not "it can parse XML" — plenty of tools can. The point is
**constant, bounded peak memory while extracting from 4 GB / 10 GB files**, and being able
to prove it with a reproducible measurement harness.

Measured on this machine, on a generated 4.05 GiB file holding 11,915,264 records:

| | |
|---|---|
| **Input** | 4142.72 MiB, 11,915,264 records |
| **Time** | 274.38 s (15.1 MiB/s, 43,426 records/s) |
| **Peak RSS** | 32.277 MiB |
| **Increase over the post-import baseline** | **3.754 MiB** |

The same run at 1 GiB (2,978,816 records) added **5.168 MiB**. Four times the input, and
the increase went *down* — nothing accumulates per record. Every number here comes from a
script in this repository; see [Benchmarks](#benchmarks).

The configuration used above reads six fields, including a nested path and a type
conversion — the shapes a real config uses:

```yaml
record: /catalog/products/product
fields:
  id:           {path: '@id'}
  type:         {path: '@type'}
  name:         {path: name}
  category:     {path: category}
  price:        {path: price, type: float}
  manufacturer: {path: manufacturer/name}
```

## Install

Source only for now:

```bash
git clone https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit.git
cd GigaXML-Memory-Efficient-XML-Extraction-Toolkit
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # POSIX: .venv/bin/python
```

`lxml` and `pyyaml` are the only required dependencies. Parquet output needs the
`parquet` extra (`pip install -e ".[parquet]"`); CSV and JSONL do not.

## Thirty seconds

Point it at a document you know nothing about, let it propose a config, then run it:

```bash
# 1. What is in this file?
gigaxml inspect big.xml

# 2. Write a starting-point config for the highest-ranked record candidate
gigaxml inspect big.xml --generate-config config.yaml --infer-types

# 3. Extract
gigaxml extract big.xml -c config.yaml -o out.csv
```

`inspect` does not read the whole document — it reports the structure and the repeating
paths it found, and ranks them. The config it writes is explicitly a **starting point**,
not a conclusion; read the comments in it.

For a quick look at the data before committing to a full run:

```bash
gigaxml sample big.xml -c config.yaml -n 20 -o first20.jsonl
```

### What it looks like

![gigaxml inspect reading the structure of a document](assets/inspect.gif)

*Finding the records in a document that opens with a licence comment.*

![gigaxml extract processing a four-gigabyte file](assets/extract-4g.gif)

*Extracting 11.9 million records from 4.05 GiB.*

> **Both of these are animations rendered from the tools' real output, not screen
> recordings.** The text is what the tools actually printed and the timings are the
> measured ones, but the frames are drawn rather than captured — this machine's sandbox
> does not permit screen capture. Each frame carries the same note.

## Benchmarks

Three generated datasets, two configs, measured in a subprocess with `psutil`:

```
dataset   fields     input MiB      records        s   MiB/s     peak    delta
------------------------------------------------------------------------------
100MB     6 fields      100.57      290,900     6.61    15.2   32.742    4.297
100MB     1 field       100.57      290,900     2.65    38.0   30.582    2.676
1GB       6 fields     1033.65    2,978,816    67.17    15.4   33.328    5.168
1GB       1 field      1033.65    2,978,816    27.07    38.2   30.723    2.141
4GB       6 fields     4142.72   11,915,264   274.38    15.1   32.277    3.754
4GB       1 field      4142.72   11,915,264   107.27    38.6   31.227    2.797
```

`peak` and `delta` are MiB; `delta` is against the same process's post-import baseline.
The one-field rows are a control, not the headline: a single-field config is the easiest
member of this family to run, and quoting it alone would overstate what a real config
costs. Field count costs about **2.5×** in throughput.

To reproduce, generate the datasets and run the harness in [`benchmarks/`](benchmarks):

```bash
gigaxml generate --size 100MB -o data/b100m.xml
gigaxml generate --size 1GB   -o data/b1g.xml
gigaxml generate --size 4GB   -o data/b4g.xml
python benchmarks/bench_extraction.py
```

`--size` is approximate: `--size 1GB` produces 1033.65 MiB, not 1024, and the sizes above
are the measured ones. Peak and delta are both reported because either alone can be
misread — peak includes about 32 MiB of interpreter and library overhead, delta is what
the workload is responsible for, and both baselines in this repository are taken after
every import so that two deltas are comparable.

## Non-goals

- No full XPath 3.1 — XPath is evaluated only inside a single record subtree.
- No arbitrary byte-offset seek/resume — XML byte offsets are not a safe parse boundary.
- No AI/ML structure inference — confidence values are deterministic statistics.
- No real customer data — everything runs on synthetic, reproducible datasets.
- No fabricated benchmarks — every performance claim comes from a runnable script.

## Known limitations

- **`--resume` re-parses and skips; it does not seek.** XML cannot be re-entered
  mid-stream, so continuing a run means reading from the beginning and discarding the
  records already accounted for. On a 403 MB file that costs 8.7 s against 17.1 s to
  extract, so resuming saves roughly half of what you had already done. `--help` says so
  too.
- **`inspect` is slower than `extract`** — 17.3 MiB/s against 38.2 MiB/s on the same
  1 GB file. It maintains several parallel bookkeeping stacks per element. It is also the
  command you run once on a document, not in a loop.
- **An inferred config treats containers as leaves.** `--generate-config` proposes direct
  children and attributes; a field whose element has children of its own is read as
  concatenated text, so `<tags><tag>a</tag><tag>b</tag></tags>` becomes `ab`. Nested
  paths (`manufacturer/name`) have to be written by hand, as the generated comments say.
- **Types are inferred from a sample**, and `decimal` is never inferred. If a field is
  money, set `type: decimal` yourself — `float` cannot represent 49.90 exactly.
- **Parsing limits are not configurable.** Entities are never expanded, the network is
  never touched, and no DTD is loaded; documents nested deeper than 256 levels, carrying a
  single text node over about 10 MB, or amplified by entities are refused rather than
  partially read. These are deliberate and there are no flags to turn them off.
- **`--checkpoint-every` verifies the parts on disk before resuming**, which costs one
  pass over the output: **73 ms for 18.0 MiB of CSV** on a warm cache, negligible for
  Parquet, whose row counts come from file metadata. It grows with the size of the
  output, not the input. Measured by `benchmarks/bench_resident.py`.

## Development

```bash
pytest -q                                   # unit + integration
pytest -q tests/performance                 # memory and throughput; not in CI
ruff check .
ruff format --check .
```

Performance tests are excluded from CI: they measure memory and throughput, take minutes,
and are not a pass/fail signal.

## License

MIT — see [LICENSE](LICENSE).
