"""Unit tests for the manifest contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.generate import Manifest, manifest_path_for, read_manifest, write_manifest

SPEC_KEYS = ["path", "bytes", "sha256", "seed", "record_path", "record_count", "namespace"]


def _manifest() -> Manifest:
    return Manifest(
        path="data/s100.xml",
        bytes=104857600,
        sha256="a" * 64,
        seed=42,
        record_path="/catalog/products/product",
        record_count=51234,
        namespace=None,
    )


def test_roundtrip_through_disk(tmp_path: Path) -> None:
    destination = tmp_path / "s.manifest.json"
    _manifest().write(destination)
    assert Manifest.read(destination) == _manifest()


def test_json_has_exactly_the_spec_keys_in_order(tmp_path: Path) -> None:
    destination = tmp_path / "s.manifest.json"
    _manifest().write(destination)
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert list(payload) == SPEC_KEYS


def test_manifest_path_suffix() -> None:
    assert manifest_path_for("data/s10.xml") == Path("data/s10.xml.manifest.json")
    assert manifest_path_for(Path("data/s10.xml")) == Path("data/s10.xml.manifest.json")


def test_write_manifest_places_file_next_to_output(tmp_path: Path) -> None:
    output = tmp_path / "s10.xml"
    written = write_manifest(output, _manifest())
    assert written == tmp_path / "s10.xml.manifest.json"
    assert written.exists()
    assert read_manifest(output) == _manifest()


def test_missing_key_is_rejected() -> None:
    payload = _manifest().to_dict()
    del payload["sha256"]
    with pytest.raises(ValueError, match="missing required keys"):
        Manifest.from_dict(payload)


def test_namespaced_manifest_keeps_the_uri() -> None:
    payload = _manifest().to_dict()
    payload["namespace"] = "urn:example:catalog"
    assert Manifest.from_dict(payload).namespace == "urn:example:catalog"
