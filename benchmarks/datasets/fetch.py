"""Fetch and verify a real-world XML dataset for the real-data benchmark.

The dataset is Simple English Wikipedia's current article dump
(https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2)
-- every article on Simple Wikipedia, in MediaWiki's XML export format. It is chosen
because it is a large, real, publicly documented XML file, because
``dumps.wikimedia.org`` serves plain files to scripts (the first choice, DBLP, now
fronts its dumps with a JavaScript proof-of-work crawler check that returns a few
kilobytes of HTML to a plain client -- the size floor below caught that), and because
it is the size an interested reviewer can actually download and process.

It also brings what the synthetic datasets deliberately lack: **a namespace**. Every
element in the dump lives in ``http://www.mediawiki.org/xml/export-0.11/``, so the
extraction config exercises the namespace support end to end on a real document.

The dump is regenerated continuously, so the file's sha256 changes with every
refresh. The integrity contract is therefore: this script records the sha256 it
downloaded **at download time** into ``datasets/wikipedia.json``, and verifies the file
against that recorded value -- a corruption check, not a pin to one snapshot's bytes.

The data itself is not committed (``data/`` is gitignored); this script and the
recorded metadata are.

Usage::

    python benchmarks/datasets/fetch.py             # download, decompress, verify, record
    python benchmarks/datasets/fetch.py --keep-bz   # keep the downloaded .bz2 as well
"""

from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import ssl
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
HERE = Path(__file__).resolve().parent

URL = "https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2"
# The main site (dblp.org) now fronts the dump with a JavaScript proof-of-work
# anti-crawler page (Anubis) that returns 3242 bytes of HTML to plain HTTP clients --
# caught by the size floor below, which is why the mirror is the source this uses.
EXPECTED_MIN_BYTES = 50_000_000  # the bz2 has been >100 MB for years; less means truncation


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ssl_context() -> ssl.SSLContext:
    """A verified HTTPS context whose CA set is certifi's, when certifi is available.

    The certificate failure this works around is environmental: on a Windows install
    whose Python does not use the system certificate store, ``urlopen`` fails with
    ``CERTIFICATE_VERIFY_FAILED`` before a byte is downloaded. certifi ships the Mozilla
    CA set inside the venv, so pointing the context at it *keeps the verification* while
    giving it a trust store that exists. Verification is never disabled.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def fetch(url: str, target: Path) -> None:
    """Download ``url`` to ``target``, reporting progress in tens of megabytes."""
    request = urllib.request.Request(url, headers={"User-Agent": "gigaxml-benchmark/1.0"})
    context = _ssl_context()
    with (
        urllib.request.urlopen(request, timeout=60, context=context) as response,
        target.open("wb") as out,
    ):
        downloaded = 0
        while True:
            chunk = response.read(1 << 22)
            if not chunk:
                break
            out.write(chunk)
            downloaded += len(chunk)
            if downloaded // (10 << 20) != (downloaded - len(chunk)) // (10 << 20):
                print(f"  {downloaded / (1 << 20):.0f} MiB ...", flush=True)
    if downloaded < EXPECTED_MIN_BYTES:
        raise RuntimeError(
            f"downloaded {downloaded} bytes; a real dump is far larger -- truncated?"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-bz", action="store_true", help="keep the downloaded .bz2")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    bz_path = DATA_DIR / "wikipedia-articles.xml.bz2"
    xml_path = DATA_DIR / "wikipedia-articles.xml"
    metadata_path = HERE / "wikipedia.json"

    if xml_path.is_file() and metadata_path.is_file():
        recorded = json.loads(metadata_path.read_text(encoding="utf-8"))
        actual = sha256_of(xml_path)
        if actual == recorded["sha256_xml"]:
            print(
                "wikipedia-articles.xml already present and verified "
                f"({recorded['sha256_xml'][:16]}...)"
            )
            return 0
        print("existing file does not match its recorded sha256; re-fetching")

    print(f"downloading {URL} ...", flush=True)
    fetch(URL, bz_path)
    bz_sha = sha256_of(bz_path)
    print(f"bz2: {bz_path.stat().st_size:,} bytes, sha256 {bz_sha[:16]}...")

    print("decompressing ...", flush=True)
    with bz2.open(bz_path, "rb") as source, xml_path.open("wb") as out:
        while True:
            chunk = source.read(1 << 22)
            if not chunk:
                break
            out.write(chunk)
    xml_sha = sha256_of(xml_path)
    print(f"xml: {xml_path.stat().st_size:,} bytes, sha256 {xml_sha[:16]}...")

    metadata = {
        "url": URL,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sha256_bz2": bz_sha,
        "sha256_xml": xml_sha,
        "bytes_bz2": bz_path.stat().st_size,
        "bytes_xml": xml_path.stat().st_size,
    }
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(f"metadata: {metadata_path}")

    if not args.keep_bz:
        bz_path.unlink()

    if xml_path.stat().st_size == 0:
        print("the decompressed file is empty", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
