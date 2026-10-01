"""Fetch the HL7 FHIR R4 XML schema set and its published examples, and record both.

**Why this example exists when the other three already carry real data.** MediaWiki's
namespace, PubMed's nesting and ERP's flat generated rows all live in the *document*.
None of them puts a schema anywhere. That is the shape the tool had never been pointed
at before M16, and pointing it at this set found a real defect within a minute --
recorded in ``tests/golden/test_known_defects.py``. The data here is small; the schema
is the point.

**The fetch is discovered, not listed.** ``fhir-all.xsd`` is 9,678 bytes and names 146 of
the files; the other four arrive only through what *those* files include, so a script
that copied its 146 lines would still be missing ``fhir-base.xsd`` and ``xml.xsd`` and
would fail with a message about a missing base type rather than a missing download. The
crawl below follows the include graph to its transitive closure and stops when the graph
stops -- which is the same rule the schema compiler applies, run in advance.

**What the closure measured on 2026-10-02**: 150 files, 3,077,184 bytes, 296
``xs:include`` / ``xs:import`` edges, every one of them a bare sibling filename. No
remote ``schemaLocation``, no ``../``, no DOCTYPE. That is a fact about *this* set, not
about real-world schemas in general, and ``fhir.json`` records it so a re-fetch can tell
whether it still holds.

**Two sizes, because a schema set is not one file.** The set is recorded per file, with
each file's own sha256, so "the schema changed" is answerable without re-reading 3 MB:
the ``set_sha256`` below is the digest of every file's name and bytes, concatenated in
name order. A single digest of a concatenated stream would be just as unforgiving but
would not say *which* file moved.

Usage::

    python examples/fhir/fetch.py            # download, verify, record
    python examples/fhir/fetch.py --force    # re-fetch even if it verifies

Where it writes: ``data/fhir/`` (gitignored) and ``examples/fhir/fhir.json`` (committed).

**Not a second copy of the download machinery**, for the reason
``examples/pubmed/fetch.py`` gives: the hashing and the TLS context -- including the
certifi fallback for a Windows install whose Python does not use the system certificate
store -- are imported from ``benchmarks/datasets/fetch.py`` rather than written again.
What is new here is the crawl.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "fhir"
XSD_DIR = DATA_DIR / "xsd"
EXAMPLES_DIR = DATA_DIR / "examples"
HERE = Path(__file__).resolve().parent
METADATA = HERE / "fhir.json"

BASE_URL = "https://www.hl7.org/fhir/R4/"
USER_AGENT = "gigaxml-examples/1.0 (+https://github.com/gg320324492-lgtm)"

#: The one file to start from. Everything else is reached from what it includes.
SEED = "fhir-all.xsd"

#: The published instances this example extracts. A recorded list, not a discovered one:
#: ``https://www.hl7.org/fhir/R4/`` serves an HTML page rather than a directory index, so
#: there is nothing to read a list out of, and a hand-maintained list that silently goes
#: stale is worse than a short one that is written down. Named explicitly here and
#: recorded in the metadata, so "which files" is answerable from the repository.
EXAMPLES = (
    "patient-example.xml",
    "bundle-example.xml",
    "observation-example.xml",
    "condition-example.xml",
    "practitioner-example.xml",
)

#: A schema file is a few hundred bytes to a few hundred kilobytes. A response under this
#: is an error page that happened to save, and a crawl that accepted one would compile a
#: 150-file schema out of 150 error pages and call it done.
MIN_XSD_BYTES = 512
MIN_EXAMPLE_BYTES = 512

#: Refuses a crawl that runs away. The real closure is 150 files; a site that answered
#: every miss with a generated page could otherwise send this into the thousands.
MAX_FILES = 600
MAX_WORKERS = 8

_LOC = re.compile(r'<xs:(?:include|import|redefine)\b[^>]*?schemaLocation="([^"]+)"', re.S)
_BARE_NAME = re.compile(r"[\w.-]+\.xsd")


def _benchmark_fetch() -> ModuleType:
    """Import ``benchmarks/datasets/fetch.py`` as a module, whatever it is named.

    Loaded by path rather than added to ``sys.path``, for the reason
    ``examples/pubmed/fetch.py`` states: the benchmark scripts have no package layout.
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


def _download(url: str, target: Path, minimum: int) -> int:
    """Fetch one file, with a floor that turns a silent truncation into an error."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with (
        urllib.request.urlopen(request, timeout=90, context=_ssl_context()) as response,
        target.open("wb") as out,
    ):
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
    size = target.stat().st_size
    if size < minimum:
        # Deleted before raising, not after. ``examples/pubmed/fetch.py`` does the same
        # and for the same reason: a truncated file left in the dataset directory is one
        # every later run trusts, because ``crawl`` only downloads what is *missing*. Left
        # behind, the floor would fire once and then be silently defeated forever.
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"{target.name} came back as {size} bytes, under the {minimum}-byte floor; "
            f"refusing to leave it where a schema file is expected"
        )
    return size


def crawl() -> dict[str, bytes]:
    """Every schema file reachable from :data:`SEED`, by include or import.

    Returns ``{filename: bytes}``. Follows the graph to its fixed point rather than one
    level, because ``fhir-all.xsd`` names 146 of the 150 files and the rest arrive
    through what those include -- one of them twice, which is the interesting one:

    ``xml.xsd`` is the W3C XML-namespace schema, canonically published at
    ``http://www.w3.org/2001/xml.xsd``. HL7 ships a local copy beside the rest rather than
    pointing at that URL, which is what lets the whole set compile inside this project's
    schema sandbox; :func:`crawl` records whether it still does.
    """
    pending = [SEED]
    seen: dict[str, bytes] = {}
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if len(seen) >= MAX_FILES:
            raise RuntimeError(
                f"the include closure passed {MAX_FILES} files; refusing to keep going"
            )
        target = XSD_DIR / name
        if not target.is_file():
            _download(BASE_URL + name, target, MIN_XSD_BYTES)
        else:
            # A file already here is trusted, which is only safe because it is checked.
            # This costs one stat per file and closes the "a truncated file survives a
            # failed download" path, which the floor alone cannot close once the download
            # that failed left its partial write behind.
            if target.stat().st_size < MIN_XSD_BYTES:
                raise RuntimeError(
                    f"{target.name} is already present but is {target.stat().st_size} bytes, "
                    f"under the {MIN_XSD_BYTES}-byte floor; delete it and re-run rather than "
                    f"compiling a truncated schema"
                )
        seen[name] = target.read_bytes()
        text = seen[name].decode("utf-8", "replace")
        for referenced in _LOC.findall(text):
            if _BARE_NAME.fullmatch(referenced) and referenced not in seen:
                pending.append(referenced)
    return seen


def describe_references(files: dict[str, bytes]) -> dict[str, object]:
    """What the whole set's references actually look like, counted rather than assumed.

    Every one of these is a fact about *this* set that a re-fetch can re-derive, which is
    the point: a schema set whose includes quietly grew a remote URL is a different thing
    from the one the sandbox was measured against.
    """
    kinds: Counter[str] = Counter()
    remote: Counter[str] = Counter()
    escaping: Counter[str] = Counter()
    doctype: list[str] = []
    for name, raw in files.items():
        text = raw.decode("utf-8", "replace")
        if "<!DOCTYPE" in text:
            doctype.append(name)
        for match in re.finditer(r"<xs:(include|import|redefine)\b[^>]*>", text, re.S):
            kind = match.group(1)
            kinds[kind] += 1
            location = re.search(r'schemaLocation="([^"]+)"', match.group(0))
            if location is None:
                continue
            value = location.group(1)
            if value.startswith(("http://", "https://")):
                remote[value] += 1
            elif value.startswith(("../", "/")) or re.match(r"^[A-Za-z]:", value):
                escaping[value] += 1
    return {
        "edges": sum(kinds.values()),
        "by_kind": dict(sorted(kinds.items())),
        "remote_schema_locations": dict(sorted(remote.items())),
        "escaping_schema_locations": dict(sorted(escaping.items())),
        "files_with_doctype": sorted(doctype),
    }


def set_digest(entries: list[dict[str, object]]) -> str:
    """One digest over the whole set: every file's name and bytes, in name order.

    The order is part of the definition, so **the order is established here** rather than
    left to the caller. A first version trusted its caller to sort, and ``verify`` fed it
    the list straight back out of the recorded JSON: identical bytes stored in a different
    order would then have read as "the set changed", and the honest response to a set that
    did not change is to re-fetch, which is a slow way to be wrong. Sorting inside is one
    line and leaves no second place for the ordering to be got wrong.

    Written out rather than borrowed, because a library call over a concatenation of the
    same bytes in another order would be a different digest under the same name.
    Per-file hashes are recorded too -- this one answers "did anything change", those
    answer "what changed".
    """
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: str(item["name"])):
        digest.update(str(entry["name"]).encode("utf-8"))
        digest.update(bytes.fromhex(str(entry["sha256"])))
    return digest.hexdigest()


def verify(recorded: dict[str, object], actual: str) -> bool:
    """Does the set on disk still hash to what was recorded?"""
    return recorded.get("set_sha256") == actual


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-fetch even if it verifies")
    args = parser.parse_args()

    XSD_DIR.mkdir(parents=True, exist_ok=True)
    EXAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    if METADATA.is_file() and not args.force:
        recorded = json.loads(METADATA.read_text(encoding="utf-8"))
        entries = list(recorded.get("xsd_files", []))
        if entries and verify(recorded, set_digest(entries)):
            print(
                f"schema set already present and verified "
                f"({str(recorded['set_sha256'])[:16]}...), {len(entries)} files, "
                f"fetched {recorded['fetched_at']}"
            )
            return 0
        print("the schema set on disk does not match its recorded digests; re-fetching")

    files = crawl()
    entries = [
        {
            "name": name,
            "bytes": len(files[name]),
            "sha256": hashlib.sha256(files[name]).hexdigest(),
        }
        for name in sorted(files)
    ]

    print(f"crawled {len(entries)} schema files, {sum(e['bytes'] for e in entries):,} bytes")
    for entry in entries:
        (XSD_DIR / str(entry["name"])).write_bytes(files[str(entry["name"])])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        _download_examples = [
            pool.submit(_download, BASE_URL + name, EXAMPLES_DIR / name, MIN_EXAMPLE_BYTES)
            for name in EXAMPLES
        ]
        sizes = {
            name: future.result() for name, future in zip(EXAMPLES, _download_examples, strict=True)
        }

    example_entries = [
        {
            "name": name,
            "bytes": sizes[name],
            "sha256": sha256_of(EXAMPLES_DIR / name),
        }
        for name in sorted(sizes)
    ]
    print(f"downloaded {len(example_entries)} example instances, {sum(sizes.values()):,} bytes")

    metadata = {
        "source": "HL7 FHIR R4 (4.0.1), the official XML distribution",
        "url": BASE_URL + SEED,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": SEED,
        "crawl": "transitive closure of xs:include / xs:import schemaLocation, siblings only",
        "xsd_file_count": len(entries),
        "xsd_bytes_total": sum(int(e["bytes"]) for e in entries),
        "set_sha256": set_digest(entries),
        "references": describe_references(files),
        "xsd_files": entries,
        "examples": [
            {"name": e["name"], "url": BASE_URL + str(e["name"]), **e} for e in example_entries
        ],
        "note": (
            "The closure is discovered, not listed: fhir-all.xsd names 146 of these files "
            "and the rest arrive through what those include. fhir-base.xsd and xml.xsd are "
            "the two that are reachable only that way, and a copy of fhir-all.xsd's own "
            "include list is not enough to compile this schema. HL7 ships a local copy of "
            "the W3C xml.xsd rather than pointing at http://www.w3.org/2001/xml.xsd, which "
            "is what lets the set compile inside gigaxml's schema sandbox."
        ),
    }
    METADATA.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(f"recorded in {METADATA}")
    print(
        f"  {metadata['xsd_file_count']} schema files, set sha256 {metadata['set_sha256'][:16]}..."
    )
    print(f"  {metadata['references']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
