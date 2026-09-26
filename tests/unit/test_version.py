"""The version is written in three places, and only agreement makes them one number.

``pyproject.toml`` is what the package metadata, the frozen binary's file properties and
the sdist all carry; ``gigaxml.__version__`` is what ``--version`` prints and what the
window shows; the release notes name the version a user is about to download. A bump that
touches one and not the others ships three different answers to "what version is this" --
the classic release accident, and the reason the memory of this project says both files
must move together. These tests make that a test failure instead of a release note.
"""

from __future__ import annotations

import pathlib
import tomllib

from gigaxml import __version__

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: This release's version, spelled the way the tag spells it. The tag itself is pushed by
#: a human step outside the test suite, but everything the tag names has to match this
#: string, and this constant is where "what are we releasing" is written down once.
RELEASE_VERSION = "0.9.0"


def test_the_package_version_and_the_import_version_agree() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_both_spellings_are_the_release_version() -> None:
    assert __version__ == RELEASE_VERSION


def test_the_release_notes_announce_the_same_version() -> None:
    """The version a downloader reads first is the version the package reports."""
    notes = (REPO_ROOT / "packaging" / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    assert f"| **Version** | {RELEASE_VERSION}" in notes


def test_the_version_is_not_a_development_placeholder() -> None:
    """A version that parses but carries no release semantics has slipped through.

    ``0.9.0`` is a deliberate statement: usable, interface still allowed to move. What
    this refuses is the placeholder class -- a version left at a pre-release snapshot or
    one carrying a local suffix that no tag and no PyPI upload would match.
    """
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)
