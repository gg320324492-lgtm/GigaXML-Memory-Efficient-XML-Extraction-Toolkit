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
RELEASE_VERSION = "1.0.0"


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

    ``1.0.0`` is a deliberate statement: the interface in the README's Non-goals and Known
    limitations is the interface, and it is no longer allowed to move under a user.
    ``0.x`` meant the opposite -- usable, but still free to change. What this refuses is
    the placeholder class -- a version left at a pre-release snapshot or one carrying a
    local suffix that no tag and no PyPI upload would match.
    """
    parts = __version__.split(".")
    assert len(parts) == 3
    assert all(part.isdigit() for part in parts)


def test_the_typed_classifier_is_backed_by_a_py_typed_marker() -> None:
    """A classifier is a promise, and this one is checkable from the source tree.

    ``Typing :: Typed`` tells a type checker that this package ships its own type
    information -- and the mechanism for saying so is the ``py.typed`` marker (PEP 561),
    not the classifier. Without the marker, mypy and pyright ignore every annotation in
    the package while PyPI keeps advertising that they need not, which is the same species
    of defect as a release note naming a version the build does not have: the artefact
    says something the code does not do.

    Read from the source tree rather than from an installed wheel so this stays a fast
    unit test; the wheel is where it would be noticed, and packaging only includes the
    marker when it is beside the package's ``__init__``, which is what the path asserts.
    """
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    classifiers = pyproject["project"]["classifiers"]
    marker = REPO_ROOT / "src" / "gigaxml" / "py.typed"

    if "Typing :: Typed" in classifiers:
        assert marker.is_file(), (
            f"the package advertises {next(c for c in classifiers if c.startswith('Typing'))!r} "
            f"but {marker.relative_to(REPO_ROOT)} does not exist, so type checkers will "
            "ignore every annotation in it"
        )
    else:
        assert not marker.is_file(), (
            f"{marker.relative_to(REPO_ROOT)} exists but no Typing classifier is declared, "
            "so the marker is dead weight no tool will look for"
        )
