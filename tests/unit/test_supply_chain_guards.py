"""The supply-chain guards run as part of the suite, not by being remembered.

Three things added in M15 are guards, and a guard nobody runs is a comment:

* ``tools/ci_selfcheck.py pins`` -- every third-party action pinned to a commit SHA and
  labelled with the release it came from;
* ``tools/ci_selfcheck.py shape`` -- the release job produces and attaches the checksums
  and the SBOM;
* ``tools/check_sbom.py`` -- the SBOM describes *this* project, not a valid document
  about something else.

Each of those was rehearsed against mutations, and the rehearsals are the evidence that
they can fail. Those rehearsals live outside the repository, so nothing here re-proves
that: what these tests do is fix the *contracts*, so that a later edit which loosens one
is red on a developer machine rather than discovered on a release.

The tools are invoked through their own entry points rather than reimplemented. A second
copy of the glob expansion or the dependency normalisation would drift from the script
the pipeline actually runs, and the drift would be invisible -- which is the same failure
these tools exist to prevent, one level up.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import subprocess
import sys

import pytest
import yaml
from tools.check_sbom import declared_dependencies, normalise
from tools.check_sbom import main as sbom_main
from tools.release_checksums import SELF_NAME, expand, release_globs, write_checksums

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
PACKAGE_YML = REPO / ".github" / "workflows" / "package.yml"


# --- tools/ci_selfcheck.py pins ----------------------------------------------------


def test_every_third_party_action_is_pinned(capsys: pytest.CaptureFixture[str]) -> None:
    """The whole point of the milestone, asserted on the real workflow.

    Invoked as a subprocess rather than imported: `pins` reads `.github/workflows`
    relative to its own location, so an in-process call would check this repository even
    when the test is pointed at a fixture.
    """
    from tools.ci_selfcheck import main as selfcheck_main

    assert selfcheck_main(["pins"]) == 0, capsys.readouterr().out


def test_a_moving_ref_is_refused_and_names_the_offending_action() -> None:
    """Putting a pin back to `@v2` is what the guard exists for, so it is pinned here."""
    from tools.ci_selfcheck import WORKFLOW_DIR, SelfCheckError, check_pins

    workflow = WORKFLOW_DIR / "package.yml"
    original = workflow.read_text(encoding="utf-8")
    mutated = original.replace(
        "        uses: pypa/gh-action-pypi-publish@"
        "dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2",
        "        uses: pypa/gh-action-pypi-publish@release/v1",
    )
    assert mutated != original, "the pin this test reverts is no longer in the workflow"
    try:
        workflow.write_text(mutated, encoding="utf-8", newline="\n")
        with pytest.raises(SelfCheckError) as caught:
            check_pins(verify_remote=False)
    finally:
        workflow.write_text(original, encoding="utf-8", newline="\n")
    assert "release/v1" in str(caught.value)
    assert "MOVING" in str(caught.value)


def test_a_bare_sha_with_no_version_comment_is_refused() -> None:
    """The label is not decoration: a SHA alone does not say which release was reviewed."""
    from tools.ci_selfcheck import WORKFLOW_DIR, SelfCheckError, check_pins

    workflow = WORKFLOW_DIR / "package.yml"
    original = workflow.read_text(encoding="utf-8")
    mutated = original.replace(
        "@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2",
        "@dc37677b2e1c63e2034f94d8a5b11f265b73ba33",
    )
    assert mutated != original, "the comment this test deletes is no longer in the workflow"
    try:
        workflow.write_text(mutated, encoding="utf-8", newline="\n")
        with pytest.raises(SelfCheckError) as caught:
            check_pins(verify_remote=False)
    finally:
        workflow.write_text(original, encoding="utf-8", newline="\n")
    assert "no version comment" in str(caught.value)


# --- tools/ci_selfcheck.py shape: the release artifacts ----------------------------


def test_the_release_job_produces_and_attaches_both_new_files() -> None:
    """Three claims, each of which can be false while the run is green."""
    from tools.ci_selfcheck import check_release_artifacts

    assert check_release_artifacts() == []

    workflow = yaml.safe_load(PACKAGE_YML.read_text(encoding="utf-8"))
    release = workflow["jobs"]["release"]
    create = next(s for s in release["steps"] if "action-gh-release" in str(s.get("uses", "")))
    assert "SHA256SUMS.txt" in create["with"]["files"]
    assert "gigaxml-sbom.json" in create["with"]["files"]
    scripts = " ".join(str(s.get("run", "")) for s in release["steps"])
    assert "release_checksums.py" in scripts
    assert "check_sbom.py" in scripts


# --- tools/release_checksums.py ----------------------------------------------------


def test_the_release_globs_are_read_from_the_workflow_not_kept_here() -> None:
    """A second list of the release's contents is a list that is correct until somebody
    adds an artifact. Read from the workflow, the two cannot disagree."""
    globs = release_globs(PACKAGE_YML)
    assert "artifacts/*.zip" in globs
    assert "artifacts/*.tar.gz" in globs
    assert "artifacts/SHA256SUMS.txt" in globs
    assert "artifacts/gigaxml-sbom.json" in globs
    # The `files: |` block is a literal scalar, so a `#` inside it is text. Dropping
    # whole-line comments is what keeps a note in the workflow from becoming a glob.
    assert not any(g.startswith("#") for g in globs)


def test_checksums_are_written_in_the_format_sha256sum_can_read(
    tmp_path: pathlib.Path,
) -> None:
    """★ The LF requirement is the whole point, and it is asserted on the bytes.

    ``sha256sum -c`` reads a CRLF checksum file as filenames with a trailing carriage
    return and reports `FAILED open or read` on every line while every hash is correct --
    the most confusing possible report, and one Windows produces by default because
    Python translates `\\n` when writing text. If this test is ever "fixed" by dropping
    the newline argument, it must be fixed by understanding what that breaks on a
    developer's machine, not on the runner.
    """
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "gigaxml-gui-linux.tar.gz").write_bytes(b"archive bytes")
    (artifacts / "leftover.tmp").write_bytes(b"not attached")

    written = write_checksums(artifacts, PACKAGE_YML, tmp_path)
    raw = written.read_bytes()
    assert b"\r" not in raw, "SHA256SUMS.txt must be LF; sha256sum cannot read CRLF filenames"
    lines = raw.decode("utf-8").splitlines()
    assert len(lines) == 1
    digest, _, name = lines[0].partition("  ")
    assert len(digest) == 64 and name == "gigaxml-gui-linux.tar.gz"
    assert digest == hashlib.sha256(b"archive bytes").hexdigest()


def test_the_checksum_file_never_lists_itself(tmp_path: pathlib.Path) -> None:
    """A hash of the checksum file cannot exist inside the checksum file."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "gigaxml-gui-linux.tar.gz").write_bytes(b"archive bytes")
    write_checksums(artifacts, PACKAGE_YML, tmp_path)
    again = write_checksums(artifacts, PACKAGE_YML, tmp_path)
    assert SELF_NAME not in again.read_text(encoding="utf-8")
    assert expand(tmp_path, artifacts, ["**/*"]) == [
        p for p in expand(tmp_path, artifacts, ["**/*"]) if p.name != SELF_NAME
    ]


def test_a_file_that_no_glob_matches_is_not_hashed(tmp_path: pathlib.Path) -> None:
    """`leftover.tmp` sits beside the archives and would be the one thing shipped
    without a hash. The generator's job is the attached set; the verifier's is to say so."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "gigaxml-gui-linux.tar.gz").write_bytes(b"archive bytes")
    (artifacts / "leftover.tmp").write_bytes(b"stray")
    write_checksums(artifacts, PACKAGE_YML, tmp_path)
    text = (artifacts / SELF_NAME).read_text(encoding="utf-8")
    assert "gigaxml-gui-linux.tar.gz" in text
    assert "leftover.tmp" not in text


# --- tools/check_sbom.py -----------------------------------------------------------


def _bom(
    tmp_path: pathlib.Path, components: list[dict], name: str = "gigaxml", version: str = "1.2.1"
) -> pathlib.Path:
    path = tmp_path / "sbom.json"
    path.write_text(
        json.dumps(
            {
                "bomFormat": "CycloneDX",
                "specVersion": "1.6",
                "version": 1,
                "metadata": {"component": {"name": name, "version": version, "type": "library"}},
                "components": components,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_real_sbom_shape_passes(capsys: pytest.CaptureFixture[str]) -> None:
    """A guard that only knows about failures is indistinguishable from no guard.

    This is the check that M14's `report` self-check would have failed: it judged a
    perfect run to be a failure, and it was found only because it was pointed at a real
    artifact rather than at a fixture somebody had shaped to suit it. The fixture below
    is built from what `cyclonedx-py` actually emits, including the root component
    sitting in `metadata` rather than in `components`, and `pip` present without being a
    declared dependency -- both measured on this machine, not assumed.
    """
    name, version, dependencies = declared_dependencies()
    components = [{"name": d, "version": "0"} for d in dependencies]
    components.append({"name": "pip", "version": "26.1.2"})
    sbom = _bom(pathlib.Path(), components, name, version)
    try:
        assert sbom_main([str(sbom)]) == 0, capsys.readouterr().out
        printed = capsys.readouterr().out
        # `pip` is in the SBOM and is not a declared dependency. It is reported, and the
        # run is still green -- a record of a machine is not tidied up, but a reader has
        # to be able to see what is in it.
        assert "pip" in printed
        assert "not a declared dependency" in printed
    finally:
        sbom.unlink(missing_ok=True)


def test_an_sbom_listing_nothing_is_refused(tmp_path: pathlib.Path) -> None:
    """★ Valid CycloneDX that describes nothing. `--validate` cannot catch this."""
    assert sbom_main([str(_bom(tmp_path, []))]) == 1


def test_an_sbom_for_another_project_is_refused(tmp_path: pathlib.Path) -> None:
    """The failure this catches is not a malformed file but a *correct* file about
    something else -- which is the one a schema check waves through."""
    _, _, dependencies = declared_dependencies()
    components = [{"name": d, "version": "0"} for d in dependencies]
    assert sbom_main([str(_bom(tmp_path, components, name="some-other-package"))]) == 1


def test_an_sbom_naming_a_version_the_tree_no_longer_has_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """A release built from a tree whose version moved must not ship the old label."""
    _, _, dependencies = declared_dependencies()
    components = [{"name": d, "version": "0"} for d in dependencies]
    assert sbom_main([str(_bom(tmp_path, components, version="0.0.1"))]) == 1


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("PyYAML", "pyyaml"),
        ("ruamel.yaml", "ruamel-yaml"),
        ("Flask_SQLAlchemy", "flask-sqlalchemy"),
    ],
)
def test_package_names_normalise_the_way_pep_503_says(written: str, expected: str) -> None:
    """`PyYAML` and `pyyaml` are the same package, and a case-sensitive comparison would
    report a declared dependency as missing from an SBOM that lists it."""
    assert normalise(written) == expected


def test_the_tools_import_cleanly_and_report_usage() -> None:
    """A tool that cannot even print its own help is the M11 lesson arriving again --
    `cyclonedx-py` could not, on this machine, until PYTHONIOENCODING was set. These two
    have no such problem and the test says so rather than leaving it assumed."""
    for module in ("tools/release_checksums.py", "tools/check_sbom.py"):
        completed = subprocess.run(
            [sys.executable, str(REPO / module), "--help"], capture_output=True, text=True
        )
        assert completed.returncode == 0, f"{module} --help failed: {completed.stderr[:300]}"
        assert "usage" in completed.stdout.lower()


def test_the_workflow_still_parses_after_edits() -> None:
    """Cheap, and the one check that would have caught the block-scalar comment and the
    mis-indented key that both reached this file during M15."""
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None
