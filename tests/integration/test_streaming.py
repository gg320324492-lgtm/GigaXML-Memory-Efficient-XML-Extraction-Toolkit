"""Integration tests: full scans of real datasets against their manifests."""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.generate import Manifest, manifest_path_for
from gigaxml.parser.streaming import RecordPathError, StreamingRecordReader


def test_scan_count_matches_manifest_exactly(s10_path: Path, s100_path: Path) -> None:
    """The extracted count must equal the manifest exactly, for both datasets."""
    for path in (s10_path, s100_path):
        manifest = Manifest.read(manifest_path_for(path))
        counted = sum(1 for _ in StreamingRecordReader(path, manifest.record_path))
        assert counted == manifest.record_count, path.name


def test_100mb_holds_exactly_ten_times_the_records(s10_path: Path, s100_path: Path) -> None:
    small = Manifest.read(manifest_path_for(s10_path))
    large = Manifest.read(manifest_path_for(s100_path))
    assert large.record_count == 10 * small.record_count


def test_first_and_last_records_are_intact(s100_path: Path) -> None:
    """Clearing must not damage the record currently being yielded."""
    manifest = Manifest.read(manifest_path_for(s100_path))
    seen = 0
    first_id: str | None = None
    last_id: str | None = None
    for record in StreamingRecordReader(s100_path, manifest.record_path):
        current = record.get("id")
        if seen == 0:
            first_id = current
        last_id = current
        seen += 1
    assert seen == manifest.record_count
    assert first_id == "1"
    assert last_id == str(manifest.record_count)


def test_tiny_fixture_extracts_expected_fields(fixtures_dir: Path) -> None:
    extracted: list[tuple[str | None, str | None, str | None, list[str]]] = []
    reader = StreamingRecordReader(fixtures_dir / "tiny.xml", "/catalog/products/product")
    for record in reader:
        extracted.append(
            (
                record.get("id"),
                record.findtext("name"),
                record.find("price").get("currency"),  # type: ignore[union-attr]
                [tag.text for tag in record.findall("tags/tag")],
            )
        )
    assert extracted == [
        ("1", "Aurora Desk Lamp", "USD", ["desk", "led"]),
        ("2", "Pulse Audio Suite", "USD", ["audio", "plugin"]),
    ]


def test_yielded_element_is_recycled_after_advancing(fixtures_dir: Path) -> None:
    """Documents the contract: the element is only valid until the next step.

    This is the price of bounded memory, and it is deliberate -- anyone holding a
    reference to a record across iterations would otherwise keep the whole
    document alive.
    """
    records = list(StreamingRecordReader(fixtures_dir / "tiny.xml", "/catalog/products/product"))
    assert len(records) == 2
    assert records[0].findtext("name") is None


def test_unmatched_record_path_raises_instead_of_returning_zero(fixtures_dir: Path) -> None:
    reader = StreamingRecordReader(fixtures_dir / "tiny.xml", "/catalog/products/widget")
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_only_the_final_segment_is_matched(fixtures_dir: Path) -> None:
    """Documents a deliberate Phase 1 limitation.

    ``iterparse(tag=...)`` filters by qualified name only, so a record path with
    the right last segment but wrong ancestors still matches. This is asserted
    rather than left implicit: if a later phase starts verifying the full chain,
    this test should fail loudly and be updated on purpose.
    """
    reader = StreamingRecordReader(fixtures_dir / "tiny.xml", "/wrong/ancestors/product")
    assert reader.record_tag == "product"
    assert sum(1 for _ in reader) == 2


def test_malformed_record_path_fails_before_reading(fixtures_dir: Path) -> None:
    with pytest.raises(RecordPathError, match="absolute element path"):
        StreamingRecordReader(fixtures_dir / "tiny.xml", "catalog/products/product")


def test_missing_source_file_raises(tmp_path: Path) -> None:
    reader = StreamingRecordReader(tmp_path / "absent.xml", "/catalog/products/product")
    with pytest.raises(FileNotFoundError):
        list(reader)
