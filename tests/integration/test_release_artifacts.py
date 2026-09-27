"""The release notes must not describe a different build from the one being shipped.

Moved here from ``tests/performance/test_frozen_binary.py``: the claim it pins is about
two *documents* -- the notes and the package metadata -- agreeing with each other, and it
needs neither the frozen artefact nor a display. Its original home sat behind a
``_require_artifact()`` skip, which on a machine with no ``dist/`` meant the claim was
never checked at all; a claim that silently depends on an optional artefact is a claim
nobody is guarding.
"""

from __future__ import annotations

import pathlib
import re

from gigaxml import __version__

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _unsigned_binaries_section(notes: str) -> str:
    """Everything from the ``Unsigned binaries`` heading to the next top-level heading."""
    lowered = notes.lower()
    start = lowered.find("## unsigned binaries")
    if start < 0:
        return ""
    next_heading = lowered.find("\n## ", start + 1)
    end = next_heading if next_heading > 0 else len(lowered)
    return lowered[start:end]


def test_the_release_notes_do_not_claim_a_version_the_build_does_not_have() -> None:
    """The notes must not describe a different build from the one being shipped.

    Added after the first draft said ``0.9.0`` while the binary said ``0.1.0``. That is the
    same failure as writing "signed" about an unsigned build: a document that is wrong
    about the thing a reader most needs to check. The number in the notes is read back and
    compared with the package, so the two cannot drift apart quietly.

    **The signing half pins *semantics, not wording*.** It once demanded the literal phrase
    "not code-signed" -- and broke, in the safe direction, the moment the notes were
    rewritten to say precisely which signing layer is absent (a Developer ID certificate)
    and which is present (the ad-hoc signature the application needs to run). So this test
    requires the *claims*, in whatever words: the summary row must state the absence of a
    Developer ID certificate -- so a row that says a certificate is present fails, which a
    contains-check cannot catch -- and the section must keep both warnings and both signing
    layers, each verified against deliberate mutations of the notes: section deleted, row
    deleted, absence flipped to presence, absence reworded, notes untouched, version row
    falsified.
    """
    notes = (REPO_ROOT / "packaging" / "RELEASE_NOTES.md").read_text(encoding="utf-8")

    # The version cell, not any mention: the body is allowed to talk about other releases.
    row = next(
        (line for line in notes.splitlines() if line.strip().startswith("| **Version**")),
        None,
    )
    assert row is not None, "the release notes have no version row"
    assert __version__ in row, f"the notes claim {row.strip()!r} but this build is {__version__}"

    signed_row = next(
        (line for line in notes.splitlines() if line.strip().startswith("| **Signed**")),
        None,
    )
    assert signed_row is not None, "the release notes have no Signed row"
    lowered_row = signed_row.lower()
    assert "developer id" in lowered_row, (
        f"the Signed row does not talk about the Developer ID layer: {signed_row.strip()!r}"
    )
    assert re.search(
        r"\bno developer id\b|not signed with a developer id|developer id certificate is absent",
        lowered_row,
    ), (
        "the Signed row does not state that the Developer ID certificate is absent: "
        f"{signed_row.strip()!r}"
    )

    section = _unsigned_binaries_section(notes)
    assert section, "the release notes no longer carry an 'Unsigned binaries' section"
    for warning in ("smartscreen", "gatekeeper"):
        assert warning in section, f"the notes never mention {warning}"
    assert "ad-hoc" in section and "developer id" in section, (
        "the notes no longer distinguish ad-hoc signing from Developer ID signing"
    )
    assert re.search(
        r"developer id[^.]*\babsent\b|not signed with a developer id|\bno developer id\b",
        section,
    ), "the section does not state that the Developer ID layer is the one that is absent"
