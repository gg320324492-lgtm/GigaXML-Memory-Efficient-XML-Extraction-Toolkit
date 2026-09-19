"""Integration tests: gzip-compressed sources stream without full decompression.

The record path is root-anchored since Phase 1.6, so ``RECORD_PATH`` names the
document root (``catalog``) explicitly. All three sources here -- the fixture, the
generated dataset and the inline document -- share that root, so no ``//``
any-ancestor prefix is needed.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from gigaxml.generate import generate_dataset
from gigaxml.parser.streaming import RecordPathError, StreamingRecordReader

RECORD_PATH = "/catalog/products/product"


def test_gzip_fixture_streams(fixtures_dir: Path) -> None:
    reader = StreamingRecordReader(fixtures_dir / "gzip_sample.xml.gz", RECORD_PATH)
    assert [record.get("id") for record in reader] == ["1", "2"]


def test_gzipped_generated_dataset_streams(tmp_path: Path) -> None:
    plain = tmp_path / "s.xml"
    manifest = generate_dataset(plain, size="1MB", seed=42)

    compressed = tmp_path / "s.xml.gz"
    with plain.open("rb") as source, gzip.open(compressed, "wb") as destination:
        shutil.copyfileobj(source, destination)

    assert compressed.stat().st_size < plain.stat().st_size
    counted = sum(1 for _ in StreamingRecordReader(compressed, RECORD_PATH))
    assert counted == manifest.record_count


def test_gzip_suffix_is_case_insensitive(tmp_path: Path) -> None:
    compressed = tmp_path / "s.xml.GZ"
    with gzip.open(compressed, "wb") as handle:
        handle.write(
            b'<?xml version="1.0"?><catalog><products><product id="1"/></products></catalog>'
        )
    assert sum(1 for _ in StreamingRecordReader(compressed, RECORD_PATH)) == 1


def test_gzip_source_still_enforces_root_anchoring(fixtures_dir: Path) -> None:
    """Root anchoring applies to compressed sources too (Phase 1.6).

    Dropping the root segment must raise rather than silently matching the tail
    of the chain, whether the bytes arrive plain or through a gzip decoder.
    """
    with pytest.raises(RecordPathError):
        list(StreamingRecordReader(fixtures_dir / "gzip_sample.xml.gz", "/products/product"))
