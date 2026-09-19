# GigaXML

A production-oriented CLI toolkit for profiling, validating and extracting structured data
from multi-gigabyte XML files with bounded memory usage.

> **Status: Phase 1 (in progress).** This README is a placeholder. Full documentation,
> benchmarks and usage examples land in Phase 7.

## What this is

The point of this project is not "it can parse XML" — plenty of tools can. The point is
**constant, bounded peak memory while extracting from 4 GB / 10 GB files**, and being able
to prove it with a reproducible measurement harness.

## Non-goals

- No full XPath 3.1 — XPath is evaluated only inside a single record subtree.
- No arbitrary byte-offset seek/resume — XML byte offsets are not a safe parse boundary.
- No AI/ML structure inference — confidence values are deterministic statistics.
- No real customer data — everything runs on synthetic, reproducible datasets.
- No fabricated benchmarks — every performance claim comes from a runnable script.

## Development

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
pytest -q
ruff check .
ruff format --check .
```

## License

MIT — see [LICENSE](LICENSE).
