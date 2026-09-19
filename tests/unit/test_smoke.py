"""Minimal smoke tests: the package imports and exposes its version."""

from __future__ import annotations

import gigaxml


def test_version_is_exposed() -> None:
    assert gigaxml.__version__ == "0.1.0"
