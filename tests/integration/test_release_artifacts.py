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
import sys

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


#: The grammar ISCC 6 accepts in ``VersionInfoVersion``, measured on 2026-10-03 by
#: compiling the project's own ``packaging/gigaxml.iss`` once per candidate:
#: ``2.0.0rc1``, ``2.0.0.rc1`` and ``2.0.0-rc1`` each failed with ``rc=2`` and
#: ``Value of [Setup] section directive "VersionInfoVersion" is invalid``; ``2.0.0``,
#: ``2.0.0.0`` and ``2.0.0.1`` each compiled with ``rc=0``. One to four dot-separated
#: runs of digits, and nothing else. Restated here rather than imported from the tool
#: because a test that checks the tool against the tool is a tautology.
ISCC_VERSION_GRAMMAR = r"^[0-9]+(\.[0-9]+){0,3}$"


def iscc_accepts(value: str) -> bool:
    return re.fullmatch(ISCC_VERSION_GRAMMAR, value) is not None


def test_the_installer_version_resource_is_digits_and_the_display_name_is_not() -> None:
    """The installer carries two numbers, and neither is derived from the other by luck.

    ``AppVersion``/``AppVerName``/the output file name are what a person reads, and they
    say ``2.0.0rc1``. ``VersionInfoVersion`` is the Windows binary version resource,
    whose four words are 16-bit integers with nowhere to put ``rc1`` -- and handing it
    the pre-release string does not degrade the resource, it **aborts the compile**.
    Measured, not assumed: ISCC 6 on 2026-10-03, against this repository's own script,

    * ``/DAppVersion=2.0.0rc1 /DAppVerInfo=2.0.0.0`` -> ``rc=0``, "Successful compile",
      and the resulting ``GigaXML-Setup-2.0.0rc1.exe`` reports ``FileVersion 2.0.0.0``
      and ``ProductVersion 2.0.0rc1``.
    * the same script with ``VersionInfoVersion={#AppVersion}`` -> ``rc=2``,
      ``Value of [Setup] section directive "VersionInfoVersion" is invalid``.

    So the packaging job was going to fail, and it fails loudly rather than shipping a
    wrong number. This pins the shape so the next version bump is not the thing that
    discovers it.
    """
    script = (REPO_ROOT / "packaging" / "gigaxml.iss").read_text(encoding="utf-8")

    assert "VersionInfoVersion={#AppVerInfo}" in script, (
        "the installer feeds AppVersion straight into VersionInfoVersion again, so the "
        "next pre-release version fails the build"
    )
    assert "VersionInfoVersion={#AppVersion}" not in script, (
        "VersionInfoVersion is back on the pre-release string, which ISCC rejects outright"
    )
    # The user-visible half keeps the pre-release. A fix that flattened both numbers
    # would pass the shape check above and ship "GigaXML-Setup-2.0.0.0.exe".
    assert re.search(r"^AppVerName=\{#AppName\} \{#AppVersion\}$", script, re.M), (
        "AppVerName no longer shows the real version, so the installer would announce "
        "2.0.0.0 to the person installing it"
    )
    assert "OutputBaseFilename=GigaXML-Setup-{#AppVersion}" in script

    # The fallback default has to be valid too: a bare `iscc packaging/gigaxml.iss`
    # with no /D arguments must compile, and 0.0.0-dev would not.
    default = re.search(r'#ifndef AppVerInfo\s*#define AppVerInfo "([^"]+)"', script)
    assert default, "AppVerInfo has no default, so a bare ISCC run cannot compile"
    assert iscc_accepts(default.group(1)), (
        f"the default AppVerInfo {default.group(1)!r} is not a version ISCC accepts"
    )


def test_the_packaging_pipeline_asks_for_the_digits_only_number_from_the_one_place() -> None:
    """The workflow must not re-derive the rule inside a shell string.

    Two copies of a reduction is two things to rot, and the copy inside a ``run:`` block
    is the one no test can see. So the YAML asks the module that already reduces the
    version for the exe's resource, and this checks it went on asking.
    """
    import yaml

    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "package.yml").read_text(encoding="utf-8")
    )
    steps = yaml.safe_dump(workflow)
    assert "--print-version-info-version" in steps, (
        "the packaging workflow no longer asks tools.make_version_info for the digits-only version"
    )
    assert "/DAppVerInfo=" in steps, "the packaging workflow does not pass the second macro"
    assert "/DAppVersion=" in steps, "the packaging workflow stopped passing the real version"
    # And the value it passes is checked before ISCC sees it, so a bad reduction fails
    # with a message that names the problem rather than with ISCC's.
    assert "is not digits and dots" in steps


def test_the_version_reduction_keeps_the_numbers_and_drops_only_the_pre_release() -> None:
    """What the reduction must do, and what it must never do.

    The mutation is the second half: a reduction that returned the version unchanged
    would satisfy every "is it digits" check on a plain release and only fail once a
    pre-release shipped -- which is the shape of a bug that hides until the worst moment.
    So ``2.0.0rc1`` is asserted to come out *changed*, and to come out as the number a
    person would recognise.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tools.make_version_info import version_info_version

    cases = {
        "2.0.0rc1": "2.0.0.0",
        "2.0.0": "2.0.0.0",
        "2.0.0.1": "2.0.0.1",
        "1.2.1": "1.2.1.0",
        "3.0.0b2": "3.0.0.0",
        "0.1.0.dev4": "0.1.0.0",
    }
    for given, expected in cases.items():
        produced = version_info_version(given)
        assert produced == expected, f"{given} -> {produced}, expected {expected}"
        assert iscc_accepts(produced), f"{given} produced {produced!r}, which ISCC rejects"

    for pre_release in ("2.0.0rc1", "3.0.0b2", "1.0.0a1", "2.0.0.post1"):
        assert version_info_version(pre_release) != pre_release, (
            f"{pre_release!r} survived the reduction unchanged, so it would be handed to "
            f"VersionInfoVersion as-is and ISCC would reject it"
        )


def test_the_pyinstaller_spec_asks_for_the_quad_instead_of_reimplementing_it() -> None:
    """The second copy of the rule is gone, and the file that held it still executes.

    ★ ``packaging/gigaxml.spec`` carried its own four lines of a reduction that already
    lived in :func:`tools.make_version_info.version_info_version`, and nothing in the
    suite ever executed the spec. So on the first tag this repository built, all three
    packaging platforms failed with ``ValueError: invalid literal for int() with base
    10: '0rc1'`` while every test here was green -- which is the failure mode the
    installer's own comment warns about, now with the evidence attached.

    Two halves, and the second is the one that matters. The first reads the text and
    refuses a second implementation. The second **executes the file**: PyInstaller is
    stubbed and its four build calls are recorders, so what runs is the spec's own
    Python, at ``2.0.0rc1``, with the version supplied rather than read from
    ``pyproject.toml`` -- so this keeps testing the pre-release case after the next
    bump, instead of quietly testing whatever the repository says today.
    """
    sys.path.insert(0, str(REPO_ROOT))
    from tools.spec_check import load_spec

    spec = (REPO_ROOT / "packaging" / "gigaxml.spec").read_text(encoding="utf-8")

    assert "int(part)" not in spec, (
        "packaging/gigaxml.spec reduces the version itself again -- a second copy of a "
        "rule that already lives in tools.make_version_info, and the copy that breaks "
        "is the one no test executes"
    )
    assert "VERSION.split" not in spec, (
        "packaging/gigaxml.spec splits the version string again instead of asking for "
        "the quad; that split is what raised on 0rc1"
    )
    assert "tools.make_version_info" in spec, (
        "the spec no longer asks the module that already reduces the version"
    )

    for version in ("2.0.0rc1", "2.0.0", "2.0.0.1"):
        namespace = load_spec(version=version)
        assert str(namespace["VERSION"]) == version
        quad = namespace["VERSION_TUPLE"]
        assert isinstance(quad, tuple) and len(quad) == 4, quad
        assert all(isinstance(part, int) for part in quad), quad
        rendered = ".".join(str(part) for part in quad)
        assert iscc_accepts(rendered), f"{version} produced {rendered!r}"

    # The whole point of the case that broke: 2.0.0rc1 must come out as four numbers.
    pre_release = load_spec(version="2.0.0rc1")["VERSION_TUPLE"]
    assert ".".join(str(part) for part in pre_release) == "2.0.0.0"

    # And the version this checkout declares, with no override involved at all.
    live = load_spec()
    rendered = ".".join(str(part) for part in live["VERSION_TUPLE"])
    assert iscc_accepts(rendered), (
        f"pyproject.toml says {live['VERSION']} and the spec built {rendered!r}"
    )


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
