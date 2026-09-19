"""Deterministic synthetic XML dataset generator.

Nothing here is random in the "unrepeatable" sense: every byte of a generated
file is a pure function of ``(seed, size, namespace)``. That is what makes the
``sha256`` recorded in the manifest worth anything -- it lets a reviewer confirm
that two runs produced the identical artifact.

Two consequences worth spelling out:

* The wall clock cannot be stamped into ``<generated>``; see
  :func:`synthetic_timestamp`.
* Record ``i`` draws from its own RNG seeded by ``sha256(seed, i)``, so a record
  never depends on how many records came before it. Sizing samples and the real
  write therefore agree exactly, and generation could be parallelised later
  without changing a single byte.

Record counts scale **exactly** with the requested size: ``records_per_mb`` is
derived once from a fixed-size, seed-only probe, and the count is
``whole_mb * records_per_mb``. Every supported size (10MB / 100MB / 1GB / 4GB /
10GB) is a whole number of MiB, so 100MB always holds exactly 10x the records of
10MB -- no rounding drift.
"""

from __future__ import annotations

import hashlib
import json
import random
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

__all__ = [
    "MANIFEST_SUFFIX",
    "RECORD_PATH",
    "DatasetPlan",
    "Manifest",
    "build_record_plan",
    "generate_dataset",
    "manifest_path_for",
    "parse_size",
    "read_manifest",
    "render_order",
    "render_product",
    "synthetic_timestamp",
    "write_manifest",
]

#: Element path of a record inside a generated catalog.
RECORD_PATH: Final = "/catalog/products/product"

#: Appended to the output path to form the manifest path.
MANIFEST_SUFFIX: Final = ".manifest.json"

_BYTES_PER_MB: Final = 1024 * 1024

#: How many records the size probe renders before extrapolating.
#:
#: Deliberately a fixed constant rather than something derived from the requested
#: target size. If the probe range moved with the target, ``records_per_mb`` would
#: move with it too, and 100MB would no longer hold *exactly* 10x the records of
#: 10MB. The cost of pinning it is that ``id`` grows a digit every decade, so a
#: 10GB file overshoots its target by around 1%. Record-count exactness is the
#: gate; sub-percent size drift is the price, and the manifest records the truth.
_PROBE_RECORDS: Final = 4096

#: Orders are emitted at this ratio to products, so the document has a second,
#: differently shaped record type without doubling the sizing maths.
_ORDERS_PER_PRODUCT: Final = 3

#: Fixed epoch for synthetic timestamps: 2023-11-14T22:13:20Z.
_SYNTHETIC_EPOCH: Final = 1_700_000_000

#: One year, in seconds -- the spread of synthetic timestamps across seeds.
_YEAR_SECONDS: Final = 31_536_000

_SIZE_RE: Final = re.compile(r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]*)\s*$")

_SIZE_UNITS: Final[Mapping[str, int]] = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "KIB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "MIB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "GIB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
    "TIB": 1024**4,
}

#: Characters that would need escaping if they appeared in generated text. The
#: pools are validated at import time so the hot path can skip escaping entirely.
_XML_UNSAFE: Final = re.compile(r"[&<>\"']")

_PRODUCT_TYPES: Final = ("physical", "digital", "service")

_NAME_ADJECTIVES: Final = (
    "Aurora",
    "Basalt",
    "Cobalt",
    "Dune",
    "Ember",
    "Fjord",
    "Granite",
    "Harbor",
    "Indigo",
    "Juniper",
    "Kestrel",
    "Lumen",
    "Meridian",
    "Nimbus",
    "Onyx",
    "Peregrine",
    "Quartz",
    "Ridge",
    "Solstice",
    "Tundra",
)

_NAME_NOUNS: Final = (
    "Adapter",
    "Bracket",
    "Charger",
    "Dock",
    "Enclosure",
    "Filter",
    "Gasket",
    "Hub",
    "Injector",
    "Junction",
    "Kit",
    "Latch",
    "Module",
    "Nozzle",
    "Optics",
    "Panel",
    "Regulator",
    "Sensor",
    "Transducer",
    "Valve",
)

_CATEGORIES: Final = (
    "audio",
    "computing",
    "cooling",
    "electrical",
    "fasteners",
    "lighting",
    "networking",
    "optics",
    "power",
    "sensors",
    "software",
    "tools",
)

_MAKER_PREFIXES: Final = (
    "Northwind",
    "Kestrel",
    "Blue Harbor",
    "Ironwood",
    "Silverline",
    "Redstone",
    "Coldwater",
    "Pinegate",
    "Blackfell",
    "Larkspur",
)

_MAKER_SUFFIXES: Final = (
    "Works",
    "Labs",
    "Industries",
    "Systems",
    "Foundry",
    "Instruments",
    "Components",
    "Dynamics",
)

_COUNTRIES: Final = ("CN", "DE", "FR", "IT", "JP", "KR", "NL", "SE", "TW", "US")

_TAG_WORDS: Final = (
    "budget",
    "bulk",
    "certified",
    "compact",
    "export",
    "heavy-duty",
    "import",
    "legacy",
    "new",
    "oem",
    "premium",
    "refurbished",
    "rugged",
    "sample",
    "wholesale",
)


def _validate_pools() -> None:
    """Fail loudly at import time if a pool entry would break the XML.

    Cheaper than escaping every generated string, and it turns "someone added
    'AT&T' to a name pool" from a silent corruption into an import error.
    """
    pools: Mapping[str, tuple[str, ...]] = {
        "_PRODUCT_TYPES": _PRODUCT_TYPES,
        "_NAME_ADJECTIVES": _NAME_ADJECTIVES,
        "_NAME_NOUNS": _NAME_NOUNS,
        "_CATEGORIES": _CATEGORIES,
        "_MAKER_PREFIXES": _MAKER_PREFIXES,
        "_MAKER_SUFFIXES": _MAKER_SUFFIXES,
        "_COUNTRIES": _COUNTRIES,
        "_TAG_WORDS": _TAG_WORDS,
    }
    for name, pool in pools.items():
        for value in pool:
            if _XML_UNSAFE.search(value):
                raise ValueError(
                    f"{name} entry {value!r} contains XML-unsafe characters; "
                    f"either remove it or enable escaping in render_product()"
                )


_validate_pools()


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def parse_size(text: str) -> int:
    """Parse a human size such as ``"10MB"``, ``"1GB"`` or ``"512KB"`` to bytes.

    Raises:
        ValueError: the string is not a recognised size.
    """
    match = _SIZE_RE.match(text)
    if match is None:
        raise ValueError(f"unrecognised size {text!r}; expected e.g. '10MB', '1GB', '512KB'")
    unit = match.group("unit").upper()
    if unit not in _SIZE_UNITS:
        raise ValueError(f"unknown size unit {match.group('unit')!r}; supported: B, KB, MB, GB, TB")
    return round(float(match.group("value")) * _SIZE_UNITS[unit])


def synthetic_timestamp(seed: int) -> str:
    """A stable pseudo-timestamp derived from ``seed``.

    Generated datasets must be byte-reproducible, so ``<generated>`` cannot hold
    the wall clock -- two runs a second apart would then differ. Same seed, same
    string, forever.
    """
    offset = abs(seed) % _YEAR_SECONDS
    moment = datetime.fromtimestamp(_SYNTHETIC_EPOCH + offset, tz=UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_seed(seed: int, index: int) -> int:
    """Derive a per-record RNG seed, independent of Python's ``hash()``.

    ``hash()`` is salted per process, so it must never reach persisted output.
    """
    digest = hashlib.sha256(f"gigaxml:{seed}:{index}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big")


# --------------------------------------------------------------------------- #
# Record rendering
# --------------------------------------------------------------------------- #


def render_product(index: int, *, seed: int) -> bytes:
    """Render product ``index`` (0-based) as one indented XML element line."""
    rng = random.Random(_record_seed(seed, index))
    product_type = rng.choice(_PRODUCT_TYPES)
    name = f"{rng.choice(_NAME_ADJECTIVES)} {rng.choice(_NAME_NOUNS)}"
    category = rng.choice(_CATEGORIES)
    price = f"{rng.uniform(1.0, 9999.0):.2f}"
    maker = f"{rng.choice(_MAKER_PREFIXES)} {rng.choice(_MAKER_SUFFIXES)}"
    country = rng.choice(_COUNTRIES)
    tags = "".join(f"<tag>{rng.choice(_TAG_WORDS)}</tag>" for _ in range(rng.randint(2, 5)))
    return (
        f'    <product id="{index + 1}" type="{product_type}">\n'
        f"      <name>{name}</name>\n"
        f"      <category>{category}</category>\n"
        f'      <price currency="USD">{price}</price>\n'
        f"      <manufacturer><name>{maker}</name><country>{country}</country></manufacturer>\n"
        f"      <tags>{tags}</tags>\n"
        f"    </product>\n"
    ).encode()


def render_order(index: int, *, seed: int) -> bytes:
    """Render order ``index`` (0-based) as one XML element line."""
    rng = random.Random(_record_seed(seed, index))
    product_ref = rng.randint(1, 10_000)
    quantity = rng.randint(1, 25)
    return (
        f'    <order id="{index + 1}">'
        f"<product-ref>{product_ref}</product-ref>"
        f"<qty>{quantity}</qty>"
        f"</order>\n"
    ).encode()


def _average_record_size(renderer: Callable[[int], bytes], count: int) -> float:
    """Mean byte length of ``renderer`` over the first ``count`` records."""
    return sum(len(renderer(index)) for index in range(count)) / count


def _bytes_per_product_unit(seed: int) -> float:
    """Average bytes contributed by one product plus its share of orders.

    A pure function of ``seed`` -- deliberately independent of the requested
    target size, so that ``records_per_mb`` is the same for 10MB and 100MB.
    """
    product = _average_record_size(lambda i: render_product(i, seed=seed), _PROBE_RECORDS)
    order = _average_record_size(lambda i: render_order(i, seed=seed), _PROBE_RECORDS)
    return product + order / _ORDERS_PER_PRODUCT


@dataclass(frozen=True, slots=True)
class DatasetPlan:
    """Everything needed to write a dataset, resolved before the first byte."""

    seed: int
    namespace: str | None
    target_bytes: int
    record_count: int
    order_count: int
    record_path: str = RECORD_PATH


def build_record_plan(
    target_bytes: int,
    *,
    seed: int = 0,
    namespace: str | None = None,
) -> DatasetPlan:
    """Resolve record and order counts for ``target_bytes``.

    ``records_per_mb`` is derived once from a fixed-size probe and then multiplied
    by the whole-MiB count, so 10MB and 100MB differ by exactly 10x in records --
    no rounding drift. Every supported size is a whole number of MiB.

    Raises:
        ValueError: ``target_bytes`` is not positive, or ``namespace`` is not a
            usable URI.
    """
    if target_bytes <= 0:
        raise ValueError(f"target size must be positive, got {target_bytes} bytes")
    if namespace is not None and (not namespace or _XML_UNSAFE.search(namespace)):
        raise ValueError(f"namespace {namespace!r} is empty or contains XML-unsafe characters")

    unit_bytes = _bytes_per_product_unit(seed)
    records_per_mb = max(1, round(_BYTES_PER_MB / unit_bytes))
    whole_mb, remainder = divmod(target_bytes, _BYTES_PER_MB)
    units = whole_mb * records_per_mb
    if remainder:
        units += max(1, round(remainder / unit_bytes))

    record_count = max(1, units)
    return DatasetPlan(
        seed=seed,
        namespace=namespace,
        target_bytes=target_bytes,
        record_count=record_count,
        order_count=max(1, record_count // _ORDERS_PER_PRODUCT),
    )


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Manifest:
    """Provenance record written next to every generated dataset."""

    path: str
    bytes: int
    sha256: str
    seed: int
    record_path: str
    record_count: int
    namespace: str | None

    def to_dict(self) -> dict[str, object]:
        """Return the manifest as a plain, JSON-ready dict."""
        return asdict(self)

    def write(self, destination: str | Path) -> None:
        """Write the manifest as pretty-printed JSON."""
        Path(destination).write_text(
            json.dumps(self.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> Manifest:
        """Build a manifest from a decoded JSON object.

        Raises:
            ValueError: a required key is missing.
        """
        missing = [key for key in _MANIFEST_FIELDS if key not in data]
        if missing:
            raise ValueError(f"manifest is missing required keys: {missing}")
        namespace = data["namespace"]
        return cls(
            path=str(data["path"]),
            bytes=int(data["bytes"]),  # type: ignore[arg-type]
            sha256=str(data["sha256"]),
            seed=int(data["seed"]),  # type: ignore[arg-type]
            record_path=str(data["record_path"]),
            record_count=int(data["record_count"]),  # type: ignore[arg-type]
            namespace=None if namespace is None else str(namespace),
        )

    @classmethod
    def read(cls, source: str | Path) -> Manifest:
        """Read and validate a manifest from disk."""
        return cls.from_dict(json.loads(Path(source).read_text(encoding="utf-8")))


_MANIFEST_FIELDS: Final = (
    "path",
    "bytes",
    "sha256",
    "seed",
    "record_path",
    "record_count",
    "namespace",
)


def manifest_path_for(output: str | Path) -> Path:
    """Return the manifest path that belongs to ``output``."""
    return Path(f"{output}{MANIFEST_SUFFIX}")


def write_manifest(output: str | Path, manifest: Manifest) -> Path:
    """Write ``manifest`` next to ``output`` and return its path."""
    destination = manifest_path_for(output)
    manifest.write(destination)
    return destination


def read_manifest(output: str | Path) -> Manifest:
    """Read the manifest that belongs to ``output``."""
    return Manifest.read(manifest_path_for(output))


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #


class _DigestWriter:
    """Streaming file writer that tracks byte count and sha256 as it goes.

    Hashing on the way out means the manifest's checksum costs one extra pass
    over data already in cache, instead of re-reading a 10GB file.
    """

    def __init__(self, path: Path) -> None:
        self._handle = path.open("wb")
        self._digest = hashlib.sha256()
        self._bytes = 0

    def write(self, data: bytes) -> None:
        self._handle.write(data)
        self._digest.update(data)
        self._bytes += len(data)

    def close(self) -> None:
        self._handle.close()

    @property
    def byte_count(self) -> int:
        return self._bytes

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def _write_document(writer: _DigestWriter, plan: DatasetPlan) -> None:
    """Stream the whole document to ``writer``, one record at a time."""
    namespace_attr = f' xmlns="{plan.namespace}"' if plan.namespace else ""
    writer.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
    writer.write(f'<catalog{namespace_attr} generated-by="gigaxml" seed="{plan.seed}">\n'.encode())
    writer.write(
        f"  <metadata><generated>{synthetic_timestamp(plan.seed)}</generated>"
        f"<records>{plan.record_count}</records></metadata>\n".encode()
    )
    writer.write(b"  <products>\n")
    for index in range(plan.record_count):
        writer.write(render_product(index, seed=plan.seed))
    writer.write(b"  </products>\n")
    writer.write(b"  <orders>\n")
    for index in range(plan.order_count):
        writer.write(render_order(index, seed=plan.seed))
    writer.write(b"  </orders>\n")
    writer.write(b"</catalog>\n")


def generate_dataset(
    output: str | Path,
    *,
    size: str | int,
    seed: int = 0,
    namespace: str | None = None,
) -> Manifest:
    """Generate a synthetic dataset and its manifest.

    The document is streamed straight to disk; no part of it is ever held as a
    single in-memory string.

    Args:
        output: destination XML path. ``<output>.manifest.json`` is written too.
        size: target size, either bytes or a string like ``"100MB"``.
        seed: determinism seed; identical inputs produce identical bytes.
        namespace: when set, the document declares this as its default namespace.

    Returns:
        The manifest that was written.
    """
    target = Path(output)
    target_bytes = parse_size(size) if isinstance(size, str) else int(size)
    plan = build_record_plan(target_bytes, seed=seed, namespace=namespace)

    if target.parent != Path():
        target.parent.mkdir(parents=True, exist_ok=True)

    writer = _DigestWriter(target)
    try:
        _write_document(writer, plan)
    finally:
        writer.close()

    manifest = Manifest(
        path=target.as_posix(),
        bytes=writer.byte_count,
        sha256=writer.hexdigest,
        seed=seed,
        record_path=plan.record_path,
        record_count=plan.record_count,
        namespace=namespace,
    )
    write_manifest(target, manifest)
    return manifest
