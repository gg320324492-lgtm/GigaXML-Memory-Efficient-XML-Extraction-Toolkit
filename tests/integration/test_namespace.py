"""Integration tests: namespace handling.

Both halves matter. The positive half proves a namespaced document can be read at
all; the negative half proves that omitting the namespace map fails *loudly*
rather than quietly reporting zero records.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.generate import Manifest, manifest_path_for
from gigaxml.parser.streaming import RecordPathError, StreamingRecordReader

URI = "urn:example:catalog"


def test_default_namespace_hits_with_map_and_misses_without(fixtures_dir: Path) -> None:
    # --- positive: default namespace supplied ---
    qualified = StreamingRecordReader(
        fixtures_dir / "namespaced.xml",
        "/catalog/products/product",
        {"": URI},
    )
    assert qualified.record_tag == f"{{{URI}}}product"
    assert [record.get("id") for record in qualified] == ["1", "2"]

    # --- negative: identical path, namespace map omitted ---
    bare = StreamingRecordReader(fixtures_dir / "namespaced.xml", "/catalog/products/product")
    assert bare.record_tag == "product"
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(bare)


def test_prefixed_record_path_hits(fixtures_dir: Path) -> None:
    """A prefixed path hits -- and it needs the ``//`` prefix to do so.

    Semantic change in Phase 1.6: a path starting with a single ``/`` is
    **root-anchored**, so it must name the document root too. This path names
    only ``products/product``, so it has to say ``//`` (any ancestors) to match
    a document whose root is ``catalog``. Both spellings below are asserted, so
    the distinction cannot silently regress.
    """
    suffix = StreamingRecordReader(
        fixtures_dir / "namespaced.xml",
        "//c:products/c:product",
        {"c": URI},
    )
    assert suffix.record_tag == f"{{{URI}}}product"
    assert sum(1 for _ in suffix) == 2

    # The same chain without the root segment must NOT match.
    anchored = StreamingRecordReader(
        fixtures_dir / "namespaced.xml",
        "/c:products/c:product",
        {"c": URI},
    )
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(anchored)


def test_wrong_uri_also_misses(fixtures_dir: Path) -> None:
    """Matching is by URI, not by prefix name or local name."""
    reader = StreamingRecordReader(
        fixtures_dir / "namespaced.xml",
        "/catalog/products/product",
        {"": "urn:example:wrong"},
    )
    assert reader.record_tag == "{urn:example:wrong}product"
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_generated_namespaced_dataset_scans_completely(namespaced_dataset: Path) -> None:
    manifest = Manifest.read(manifest_path_for(namespaced_dataset))
    assert manifest.namespace == URI
    counted = sum(
        1
        for _ in StreamingRecordReader(
            namespaced_dataset,
            manifest.record_path,
            {"": URI},
        )
    )
    assert counted == manifest.record_count
