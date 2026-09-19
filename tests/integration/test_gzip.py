"""Integration tests: gzip-compressed sources stream without full decompression."""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

from gigaxml.generate import generate_dataset
from gigaxml.parser.streaming import StreamingRecordReader

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
