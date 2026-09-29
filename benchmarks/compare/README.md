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
wrong about memory twice — once reporting a frozen 4.1 MB for every implementation,
and once reporting a flat ~19 MB for GigaXML because the extraction ran in a
grandchild process the sampler could not see. Both looked like good news.

```bash
python benchmarks/compare/verify_peaks.py
```

The guard measures an empty script's peak through the runner's own wrapper and fails on
any reading that is below it — or, less obviously, on any reading that is only just
above it. A value sitting on the floor usually means the sampled process never did the
work, and a flat curve across input sizes is the tell, because a real extraction's
curve is not perfectly level. It exits non-zero either way, so it can gate a run.
