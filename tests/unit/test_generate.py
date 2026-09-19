"""Unit tests for the synthetic dataset generator.

These use 1MB/10MB datasets so the suite stays fast; the 10MB/100MB pair the
Phase 1 gates actually measure lives in the integration and performance suites.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.generate import (
    Manifest,
    generate_dataset,
    manifest_path_for,
    parse_size,
    render_product,
    synthetic_timestamp,
)

MB = 1024 * 1024


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1B", 1),
        ("512KB", 512 * 1024),
        ("1.5MB", 1572864),
        ("10MB", 10 * MB),
        ("100MB", 100 * MB),
        ("1GB", 1024 * MB),
        ("4GB", 4096 * MB),
        ("10GB", 10240 * MB),
        (" 100 mb ", 100 * MB),
    ],
)
def test_parse_size(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "MB", "10XB", "-5MB", "10 MB extra"])
def test_parse_size_rejects_garbage(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_size(bad)


def test_same_seed_is_byte_identical(tmp_path: Path) -> None:
    first = generate_dataset(tmp_path / "a.xml", size="1MB", seed=42)
    second = generate_dataset(tmp_path / "b.xml", size="1MB", seed=42)
    assert first.sha256 == second.sha256
    assert first.bytes == second.bytes
    assert first.record_count == second.record_count


def test_different_seed_changes_bytes(tmp_path: Path) -> None:
    first = generate_dataset(tmp_path / "a.xml", size="1MB", seed=42)
    second = generate_dataset(tmp_path / "b.xml", size="1MB", seed=43)
    assert first.sha256 != second.sha256


def test_record_count_scales_exactly_with_size(tmp_path: Path) -> None:
    """The 10:1 record ratio is a construction guarantee, not a coincidence."""
    small = generate_dataset(tmp_path / "s1.xml", size="1MB", seed=42)
    large = generate_dataset(tmp_path / "s10.xml", size="10MB", seed=42)
    assert large.record_count == 10 * small.record_count


def test_generated_size_tracks_target(tmp_path: Path) -> None:
    target = 4 * MB
    manifest = generate_dataset(tmp_path / "s.xml", size="4MB", seed=42)
    assert abs(manifest.bytes - target) / target < 0.02


def test_manifest_is_written_next_to_output(tmp_path: Path) -> None:
    output = tmp_path / "s.xml"
    manifest = generate_dataset(output, size="256KB", seed=7)
    written = manifest_path_for(output)
    assert written.exists()
    assert Manifest.read(written) == manifest
    assert manifest.path == output.as_posix()


def test_namespace_variant_declares_default_namespace(tmp_path: Path) -> None:
    output = tmp_path / "ns.xml"
    manifest = generate_dataset(output, size="512KB", seed=42, namespace="urn:example:catalog")
    assert manifest.namespace == "urn:example:catalog"
    assert b'xmlns="urn:example:catalog"' in output.read_bytes()[:256]


def test_unsafe_namespace_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="XML-unsafe"):
        generate_dataset(tmp_path / "ns.xml", size="128KB", seed=1, namespace='urn:"evil"')


def test_non_positive_size_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        generate_dataset(tmp_path / "s.xml", size=0, seed=1)


def test_render_product_is_a_pure_function_of_seed_and_index() -> None:
    assert render_product(5, seed=42) == render_product(5, seed=42)
    assert render_product(5, seed=42) != render_product(5, seed=43)
    assert render_product(5, seed=42) != render_product(6, seed=42)


def test_synthetic_timestamp_is_stable_and_seed_dependent() -> None:
    assert synthetic_timestamp(42) == synthetic_timestamp(42)
    assert synthetic_timestamp(42) != synthetic_timestamp(43)
    assert synthetic_timestamp(42).endswith("Z")
