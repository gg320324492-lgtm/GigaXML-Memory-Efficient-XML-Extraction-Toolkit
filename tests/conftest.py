"""Shared fixtures for the gigaxml test suite."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from gigaxml.generate import (
    Manifest,
    build_record_plan,
    generate_dataset,
    manifest_path_for,
    parse_size,
    render_order,
    render_product,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Every dataset the Phase 1 gates are measured on uses this seed.
GATE_SEED = 42


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _generator_fingerprint(size: str) -> str:
    """Cheap probe of what the generator currently produces for ``size``.

    Caching ``data/`` is worth it -- regenerating 110MB on every run is pure
    waste -- but a manifest is self-consistent with the bytes *it* wrote, so
    re-hashing the file cannot tell you the file predates a code change. Hashing
    a couple of probe records plus the resolved counts closes that hole: edit the
    rendering or the sizing logic and the fingerprint changes, forcing a rebuild.
    """
    plan = build_record_plan(parse_size(size), seed=GATE_SEED)
    digest = hashlib.sha256()
    digest.update(render_product(0, seed=GATE_SEED))
    digest.update(render_product(9999, seed=GATE_SEED))
    digest.update(render_order(0, seed=GATE_SEED))
    digest.update(f"{plan.record_count}:{plan.order_count}".encode())
    return digest.hexdigest()


def ensure_dataset(name: str, size: str) -> Path:
    """Generate ``data/<name>`` once, reusing it only while it is still valid."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / name
    manifest_path = manifest_path_for(target)
    fingerprint_path = DATA_DIR / f"{name}.fingerprint"

    if target.exists() and manifest_path.exists() and fingerprint_path.exists():
        manifest = Manifest.read(manifest_path)
        if manifest.sha256 == _sha256_of(target) and fingerprint_path.read_text(
            encoding="ascii"
        ) == _generator_fingerprint(size):
            return target

    generate_dataset(target, size=size, seed=GATE_SEED)
    fingerprint_path.write_text(_generator_fingerprint(size), encoding="ascii")
    return target


@pytest.fixture(scope="session")
def s10_path() -> Path:
    """A 10MB synthetic catalog (seed 42)."""
    return ensure_dataset("s10.xml", "10MB")


@pytest.fixture(scope="session")
def s100_path() -> Path:
    """A 100MB synthetic catalog (seed 42), structurally identical to s10."""
    return ensure_dataset("s100.xml", "100MB")


@pytest.fixture(scope="session")
def s400_path() -> Path:
    """A 400MB synthetic catalog (seed 42).

    Not part of the Phase 1 gate. It exists so the memory suite can show that the
    increment stops growing well before 400MB -- "10x data did not cost 10x
    memory" is weaker evidence than "4x more data cost no more memory".
    """
    return ensure_dataset("s400.xml", "400MB")


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """Directory holding the small hand-written XML fixtures."""
    return FIXTURES_DIR


@pytest.fixture
def namespaced_dataset(tmp_path: Path) -> Iterator[Path]:
    """A small default-namespace dataset generated into a temp directory."""
    path = tmp_path / "namespaced.xml"
    generate_dataset(path, size="256KB", seed=GATE_SEED, namespace="urn:example:catalog")
    yield path
