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


# --- tools/ci_selfcheck.py shape: a job installs what its tools import --------------


def test_every_workflow_job_satisfies_its_tools_imports(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real tree: every job in package.yml and test.yml installs what it runs.

    This is the M20 defect as an assertion. ``package.yml``'s release job declared
    ``setup-python`` and no ``pip install``, so the first tag that reached it died on
    ``ModuleNotFoundError: No module named 'yaml'``. The check is on the real workflows
    rather than a fixture, because a fixture would be a copy of the bug rather than the
    repository's own shape.
    """
    from tools.ci_selfcheck import check_python_dependencies
    from tools.ci_selfcheck import main as selfcheck_main

    assert check_python_dependencies() == []
    assert selfcheck_main(["deps"]) == 0, capsys.readouterr().out


def test_a_python_step_with_no_install_is_caught() -> None:
    """Remove the release job's install and the check names the script and the module.

    ★ This is the mutation that was rehearsed against the real workflow: the
    ``python -m pip install pyyaml`` line deleted from ``package.yml`` while the
    interpreter declaration stays. The check must go red, and it must say *which script*
    and *which module* -- a bare "a job is missing a dependency" is not actionable.
    """
    from tools.ci_selfcheck import WORKFLOW_DIR, check_python_dependencies

    workflow = WORKFLOW_DIR / "package.yml"
    original = workflow.read_text(encoding="utf-8")
    mutated = original.replace(
        "          python -m pip install --upgrade pip\n          python -m pip install pyyaml\n",
        "",
    )
    assert mutated != original, "the install step this test reverts is no longer in the workflow"
    try:
        workflow.write_text(mutated, encoding="utf-8", newline="\n")
        problems = check_python_dependencies()
    finally:
        workflow.write_text(original, encoding="utf-8", newline="\n")
    assert problems, "removing the only PyYAML install must be caught"
    joined = "\n".join(problems)
    assert "release_checksums.py" in joined
    assert "yaml" in joined
    assert "package.yml:release" in joined


def test_a_sibling_venv_install_does_not_satisfy_the_jobs_own_interpreter() -> None:
    """★ The failure of the naive version, pinned so it cannot come back.

    The release job installs PyYAML with ``python -m pip install pyyaml`` AND runs
    ``"$RUNNER_TEMP/sbomenv/bin/python" -m pip install .`` in a throwaway venv. A version
    of the check that merged every ``pip install`` in the job read that second line as
    satisfying PyYAML -- the project's runtime dependencies include it -- and stayed green
    on the tree where the first line had been deleted. ``installed_requirements`` is
    asserted directly so the distinction is a fact about the check, not a property that
    happens to hold because of what the current workflow contains.
    """
    from tools.ci_selfcheck import installed_requirements

    job = {
        "steps": [
            {"name": "Install", "run": "python -m pip install pyyaml\n"},
            {
                "name": "SBOM",
                "run": 'set -euo pipefail\n"$RUNNER_TEMP/sbomenv/bin/python" -m pip '
                "install --quiet .\n",
            },
        ]
    }
    requirements = installed_requirements(job)
    assert "pyyaml" in requirements
    # `pip install .` in the venv pulls `lxml` and `pyyaml` from pyproject's runtime
    # dependencies. If the sibling env is (wrongly) counted, they appear here; if it is
    # correctly skipped, they do not -- which is the whole point, because their absence is
    # what makes deleting the real install line fail this check.
    assert "lxml" not in requirements, (
        "a venv's `pip install .` was read as installing into the job's interpreter; the "
        "check would then pass on a job that can no longer import its own tools"
    )


def test_a_guarded_import_is_not_a_requirement_but_an_exiting_one_is() -> None:
    """★ Two scripts, both guarding a third-party import, judged differently -- on purpose.

    ``ci_selfcheck.py`` imports PySide6 inside ``try/except Exception`` and returns a facts
    dict; the ``ci-shape`` job installs no Qt and must not be asked to, or the check is red
    on a healthy repository. ``make_icon.py`` imports Pillow inside ``try/except
    ImportError`` and exits 2; the build job installs Pillow and that must stay required, or
    deleting ``pillow`` from the ``dev`` extra stops being visible here. The difference is
    what the handler does, and this pins it in both directions.
    """
    from tools.ci_selfcheck import REPO_ROOT, third_party_imports

    required, optional = third_party_imports(REPO_ROOT / "tools" / "ci_selfcheck.py")
    assert "yaml" in required
    assert "PySide6" in optional
    assert "PySide6" not in required

    required, optional = third_party_imports(REPO_ROOT / "tools" / "make_icon.py")
    assert "PIL" in required, "make_icon exits when Pillow is absent, so Pillow is required"
    assert "PIL" not in optional


def test_a_comment_in_a_run_block_is_not_a_requirement() -> None:
    """Reading prose as pip arguments produced a requirement set of English words.

    The release job's SBOM step is mostly a paragraph of explanation. A check whose evidence
    is nonsense cannot be argued with, whichever way its verdict falls.
    """
    from tools.ci_selfcheck import installed_requirements

    job = {
        "steps": [
            {
                "run": (
                    "python -m pip install --quiet cyclonedx-bom\n"
                    "# `pip install .` is what the README tells a CLI user to run, so it\n"
                    "# is the set of packages this release's library actually pulls.\n"
                )
            }
        ]
    }
    requirements = installed_requirements(job)
    assert "cyclonedx-bom" in requirements
    for word in ("it", "is", "the", "so", "a", "user"):
        assert word not in requirements, f"{word!r} was read out of a comment as a package"


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


# --- .github/dependabot.yml --------------------------------------------------------


def test_dependabot_covers_both_ecosystems_and_will_actually_run() -> None:
    """★ Dependabot failing silently is indistinguishable from not having it.

    A malformed file, an ecosystem typed wrong, or a schedule nobody set produces no error
    anywhere -- there is no failing job, because no job runs. Updates simply stop arriving,
    and the first evidence is the day a dependency ships a fix that this repository never
    received. That is why this is asserted rather than assumed, and why the schedule is
    checked as well as the ecosystems: a file with no `schedule` is a file that never runs.
    """
    path = REPO / ".github" / "dependabot.yml"
    assert path.is_file(), ".github/dependabot.yml is missing"

    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert config["version"] == 2

    by_ecosystem = {u["package-ecosystem"]: u for u in config["updates"]}
    assert set(by_ecosystem) == {"pip", "github-actions"}, (
        "dependabot must cover both: the project's Python dependencies, and the actions"
        " the release chain runs -- the second being what keeps the M15 pins alive"
    )

    for ecosystem, update in by_ecosystem.items():
        assert update["directory"] == "/", f"{ecosystem} must scan the repository root"
        schedule = update["schedule"]
        assert schedule["interval"] == "weekly", f"{ecosystem} is not scheduled"
        assert schedule.get("day") and schedule.get("time"), (
            f"{ecosystem} has no day or time, so `interval: weekly` picks one for you"
        )
        assert update["open-pull-requests-limit"] >= 1


def test_dependabot_and_the_pin_check_agree_about_what_a_pin_is() -> None:
    """The two halves of M15 have to fit together.

    The `github-actions` ecosystem rewrites SHA-pinned actions *and* the `# vX.Y.Z` comment
    beside them; `pins --verify-remote` then asks the remote whether that version is that
    commit. If a future Dependabot bumped the SHA without the comment, the check goes red
    -- which is the intended outcome, and the reason this arrangement is worth having. The
    test pins the arrangement rather than the behaviour: it cannot observe a bot here.
    """
    path = REPO / ".github" / "dependabot.yml"
    text = path.read_text(encoding="utf-8")
    assert "github-actions" in text

    from tools.ci_selfcheck import check_pins

    # The check accepts the version comment as a tag or a branch, which is what lets a
    # Dependabot bump of `softprops/action-gh-release@v2` -> a newer release land green
    # with its comment rewritten.
    assert check_pins(verify_remote=False) is None


# --- tools/ci_selfcheck.py report: designed skips vs unexpected ones ----------------


def _windows_only_cases() -> list[tuple[str, str, str]]:
    """``WINDOWS_ONLY_TESTS`` in the state this platform's report would carry them.

    ``check_report`` requires both of these to be present, so a synthetic report that left
    them out would fail on a different rule than the one under test. Off Windows they skip;
    on Windows they pass.
    """
    from tools.ci_selfcheck import WINDOWS_ONLY_TESTS

    state = "skipped" if sys.platform != "win32" else ""
    return [
        (
            target.partition("::")[0].replace("/", ".").removesuffix(".py"),
            target.partition("::")[2],
            state,
        )
        for target in WINDOWS_ONLY_TESTS
    ]


def _windows_only_ceiling() -> int:
    """The ``max_skips`` that lets ``WINDOWS_ONLY_TESTS`` through on this platform.

    Those two tests are a separate rule with its own platform logic -- off Windows they are
    expected to skip, and the real non-Windows legs allow for them with a ceiling of 40. A
    test about the designed/unexpected split has to give them their room, or it fails on the
    wrong rule: on a Linux leg the two windows-only skips are the report's only *unexpected*
    skips, and the ceiling under test would be counting them rather than the probe.
    """
    return 0 if sys.platform == "win32" else len(_windows_only_cases())


def _junit(tmp_path: pathlib.Path, cases: list[tuple[str, str, str]]) -> pathlib.Path:
    """A junit report holding ``(classname, name, skipped-reason)`` cases; reason '' = pass.

    Written out rather than captured from a real pytest run so the two cases can be put in
    one report on purpose: the point of this check is that it tells a named skip apart from
    an unnamed one, and a report where only one of them is present cannot show that.
    """
    rows = []
    for classname, name, reason in cases:
        skipped = f'<skipped message="{reason}"/>' if reason else ""
        rows.append(f'<testcase classname="{classname}" name="{name}">{skipped}</testcase>')
    path = tmp_path / "ci-report.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<testsuites><testsuite name="pytest" tests="{len(cases)}">'
        + "".join(rows)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def test_a_designed_skip_is_excused_and_an_unexpected_one_is_not(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """★ The contract of the ``--max-skips 0`` leg, pinned as a test rather than a memory.

    The Windows leg runs ``report ... --max-skips 0`` because a skip there is a hole in the
    platform claim. The tag guard skips on every pull request by design -- it has nothing to
    say off a tag -- so the ceiling counted a legitimate silence as a hole. ``DESIGNED_SKIPS``
    separates the two, and the separation is only real if BOTH halves hold: the named skip is
    excused, and a skip nobody named still fails at a ceiling of zero. A test that asserted
    only the first half would stay green if the whole ceiling had been deleted.

    The synthetic report is used rather than the real run because the real run cannot hold
    an unexpected skip without the tree being broken; the two must be seen side by side.

    ``WINDOWS_ONLY_TESTS`` is added in the state this platform would produce it, because
    ``check_report`` also insists those two are present -- leaving them out would fail on a
    different rule than the one under test, which is how a contract test comes to assert
    the wrong thing. The ceiling passed to the check is that baseline, so the only skip left
    for the ceiling to judge is the designed one and the probe.
    """
    from tools.ci_selfcheck import SelfCheckError, check_report

    named = (
        "tests.unit.test_version",
        "test_the_tag_at_head_names_the_version_this_build_reports",
        "HEAD is not a release tag: no tags point at it",
    )
    windows_only = _windows_only_cases()
    ceiling = _windows_only_ceiling()

    # Only the designed skip: at the platform's own ceiling, the report passes.
    only_named = _junit(tmp_path, [named, *windows_only])
    check_report(only_named, min_tests=1, max_skips=ceiling)
    assert "designed skips" in capsys.readouterr().out

    # ★ The mutation that must still be red: a skip that is NOT named. Because the two
    # sit in one report, the ceiling is proven to count this one and not the other.
    with_unexpected = _junit(
        tmp_path,
        [named, ("tests.unit.test_version", "test_probe", "temporary probe"), *windows_only],
    )
    with pytest.raises(SelfCheckError) as caught:
        check_report(with_unexpected, min_tests=1, max_skips=ceiling)
    message = str(caught.value)
    assert f"ceiling is {ceiling}" in message, message
    assert "HEAD is not a release tag" not in message, (
        "the ceiling must not be counting the designed skip; it counted the probe instead"
    )
    assert "temporary probe" not in message, (
        "the failure message is about the count, not a list of every reason"
    )


def test_the_designed_skip_is_matched_on_its_reason_not_only_its_name(
    tmp_path: pathlib.Path,
) -> None:
    """★ A named test that skips for a *different* reason is not excused.

    An id alone would be a licence for that test to skip on anything, including a capability
    that went missing -- the exact failure the ceiling exists to catch. The reason is what
    keeps the entry pinned to one line of one test.
    """
    from tools.ci_selfcheck import SelfCheckError, check_report

    ceiling = _windows_only_ceiling()
    drifted = _junit(
        tmp_path,
        [
            (
                "tests.unit.test_version",
                "test_the_tag_at_head_names_the_version_this_build_reports",
                "some other reason entirely",
            ),
            *_windows_only_cases(),
        ],
    )
    with pytest.raises(SelfCheckError) as caught:
        check_report(drifted, min_tests=1, max_skips=ceiling)
    assert f"ceiling is {ceiling}" in str(caught.value)


def test_every_named_skip_points_at_a_test_that_exists() -> None:
    """A name that matches nothing is a stale excuse waiting to excuse the wrong skip.

    ``DESIGNED_SKIPS`` keys are path-and-name, the same spelling ``WINDOWS_ONLY_TESTS``
    uses, and a rename that forgot this list would leave an entry that can never fire. The
    file is read here the same way pytest collects it, so the check is about the tree and
    not about a copy of the names.
    """
    from tools.ci_selfcheck import DESIGNED_SKIPS

    for target in DESIGNED_SKIPS:
        path, _, name = target.partition("::")
        module = REPO / path
        assert module.is_file(), f"{target}: {path} is not in the tree"
        assert f"def {name}(" in module.read_text(encoding="utf-8"), (
            f"{target}: no test of that name is defined in {path}, so the excuse can never "
            f"match -- either the test was renamed, or the entry is stale"
        )
