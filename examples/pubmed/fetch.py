"""Fetch one day of PubMed's baseline, verify it, and record what was downloaded.

PubMed's baseline is **one gzipped XML file per day**, each holding every citation
PubMed added that day. That shape is why this example is here and the full dump is not:
a single day's file is around 80 MB, it is a real bibliographic export, and it is
small enough to fetch and check in a minute.

**The file name is discovered, never hard-coded.** NCBI keeps a rolling window of
about three years and deletes what falls out of it -- ``pubmed22n0001.xml.gz`` was
gone by 2026-09, and the directory listing now starts at ``pubmed26n0001``. A script
with a pinned name in it would fail once every three years, quietly, for everyone who
did not notice the date. So this reads the directory, takes the newest file it lists,
and records which one that was.

Usage::

    python examples/pubmed/fetch.py            # download, verify, record
    python examples/pubmed/fetch.py --force    # re-fetch even if the file verifies

Where it writes: ``data/pubmed-baseline.xml.gz`` (gitignored, like every other dataset
here) and the recorded metadata beside this file, in ``pubmed.json`` (committed, like
``benchmarks/datasets/wikipedia.json``).

**Not a second copy of the download machinery.** The hashing and the TLS context --
including the certifi fallback for a Windows install whose Python does not use the
system certificate store -- are imported from ``benchmarks/datasets/fetch.py`` rather
than written again. What is new here is the directory listing and the fact that NCBI
wants a User-Agent; everything else about moving bytes over TLS is the same problem it
already solves.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
HERE = Path(__file__).resolve().parent

LISTING_URL = "https://ftp.ncbi.nlm.nih.gov/pubmed/baseline/"
FILE_URL = LISTING_URL + "{name}"
USER_AGENT = "gigaxml-examples/1.0 (+https://github.com/gg320324492-lgtm)"

TARGET = DATA_DIR / "pubmed-baseline.xml.gz"
METADATA = HERE / "pubmed.json"

#: A day of PubMed is tens of megabytes. Anything under this is a truncated transfer or
#: an error page that happened to be saved, and both are worth failing on -- the same
#: reason the Wikipedia fetcher has a floor of its own.
MIN_BYTES = 5_000_000

_NAME = re.compile(r'href="(pubmed\d{2}n\d{4}\.xml\.gz)"')


def _benchmark_fetch() -> ModuleType:
    """Import ``benchmarks/datasets/fetch.py`` as a module, whatever it is named.

    Loaded by path rather than added to ``sys.path`` so that the benchmark scripts stay
    exactly as they are: no ``__init__.py``, no package layout, and a file that is run
    directly and also imported.
    """
    path = REPO_ROOT / "benchmarks" / "datasets" / "fetch.py"
    spec = importlib.util.spec_from_file_location("gigaxml_benchmark_fetch", path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file is a bug
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_bench = _benchmark_fetch()
sha256_of = _bench.sha256_of
# Private in the module it lives in, and used here on purpose: see the module docstring.
_ssl_context = _bench._ssl_context


def newest_file_name() -> str:
    """The newest baseline file NCBI currently lists, or raise.

    **The listing is the source of truth and the maximum is the newest.** Both matter: the
    listing, because the pinned names in old documentation are deleted on a rolling
    schedule; the maximum rather than the last line, because a directory listing is not
    promised to be sorted and a site that shuffled it should not silently hand back
    yesterday's file.
    """
    request = urllib.request.Request(LISTING_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60, context=_ssl_context()) as response:
        listing = response.read().decode("utf-8", "replace")
    names = _NAME.findall(listing)
    if not names:
        raise RuntimeError(
            f"no pubmed*_*.xml.gz in the listing at {LISTING_URL}; the page format may "
            "have changed, and guessing a file name would be guessing wrong"
        )
    return max(names)


def download(name: str, target: Path) -> None:
    """Fetch one file, with a size floor that turns a silent truncation into an error."""
    request = urllib.request.Request(FILE_URL.format(name=name), headers={"User-Agent": USER_AGENT})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with (
        urllib.request.urlopen(request, timeout=120, context=_ssl_context()) as response,
        target.open("wb") as out,
    ):
        written = 0
        while True:
            chunk = response.read(1 << 22)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
            if written // (20 << 20) != (written - len(chunk)) // (20 << 20):
                print(f"  {written / (1 << 20):.0f} MiB ...", flush=True)
    if written < MIN_BYTES:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"{name} came back as {written:,} bytes, under the {MIN_BYTES:,} floor; "
            "refusing to leave a truncated file where a dataset is expected"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch even if it verifies")
    args = parser.parse_args()

    if TARGET.is_file() and METADATA.is_file() and not args.force:
        recorded = json.loads(METADATA.read_text(encoding="utf-8"))
        actual = sha256_of(TARGET)
        if actual == recorded.get("sha256_gz"):
            print(
                f"pubmed-baseline.xml.gz already present and verified "
                f"({recorded['sha256_gz'][:16]}...), from {recorded['name']}"
            )
            return 0
        print("existing file does not match its recorded sha256; re-fetching")

    name = newest_file_name()
    print(f"newest file listed: {name}")
    print(f"downloading {FILE_URL.format(name=name)} ...", flush=True)
    download(name, TARGET)

    digest = sha256_of(TARGET)
    size = TARGET.stat().st_size
    print(f"{size:,} bytes, sha256 {digest[:16]}...")

    metadata = {
        "url": FILE_URL.format(name=name),
        "name": name,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256_gz": digest,
        "bytes_gz": size,
        "note": (
            "NCBI keeps a rolling window of these and deletes older ones, so a name "
            "pinned in a script fails eventually. Re-run this to get whatever is current."
        ),
    }
    METADATA.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"recorded in {METADATA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
