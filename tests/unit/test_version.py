"""The version is written in four places, and only agreement makes them one number.

``pyproject.toml`` is what the package metadata, the frozen binary's file properties and
the sdist all carry; ``gigaxml.__version__`` is what ``--version`` prints and what the
window shows; the release notes name the version a user is about to download; and the tag
is the name the release is published under. A bump that touches one and not the others
ships more than one answer to "what version is this" -- the classic release accident, and
the reason the memory of this project says both files must move together. These tests make
that a test failure instead of a release note.

**The tag is the fourth one because the other three cannot see it.** A build reads its
version from ``pyproject.toml``; the tag is cut by a separate human step and reaches the
build only as a ref. So a tag cut without the bump produces a binary, a release page and a
tag that disagree, and every check that compares the notes against ``gigaxml.__version__``
passes -- which is exactly what would have happened at ``v2.0.0rc2`` had this file not
changed. The tag guard below is the one that fails.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import tempfile
import tomllib

import pytest

from gigaxml import __version__

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: This release's version, spelled the way the tag spells it. The tag itself is pushed by
#: a human step outside the test suite, but everything the tag names has to match this
#: string, and this constant is where "what are we releasing" is written down once.
RELEASE_VERSION = "2.0.0rc4"


def test_the_package_version_and_the_import_version_agree() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["version"] == __version__


def test_both_spellings_are_the_release_version() -> None:
    assert __version__ == RELEASE_VERSION


def test_the_release_notes_announce_the_same_version() -> None:
    """The version a downloader reads first is the version the package reports."""
    notes = (REPO_ROOT / "packaging" / "RELEASE_NOTES.md").read_text(encoding="utf-8")
    assert f"| **Version** | {RELEASE_VERSION}" in notes


# --------------------------------------------------------------------------
# The tag. The one version source none of the three above can see.
# --------------------------------------------------------------------------

#: A tag that names a release, as opposed to a tag that names something else. Anchored on
#: three numeric components -- the shape every tag in this repository has -- and
#: deliberately looser than :data:`RELEASE_SPELLING`: whether a spelling is one a release
#: may carry is the question above, and repeating that judgement here would make this guard
#: fail on a tag whose version it was never asked to interpret. A tag that names no version
#: at all is skipped rather than failed, because a guard that goes red for a shape it was
#: not asked about is the "green on my machine, red on the runner" failure this repository
#: has paid for before.
VERSION_TAG = re.compile(r"\Av?\d+\.\d+\.\d+")


def tags_at_head(cwd: pathlib.Path | None = None) -> list[str] | None:
    """The tags pointing at HEAD, or ``None`` when git cannot be asked.

    **``git tag --points-at HEAD`` rather than ``git describe --exact-match``.** Describe
    searches *annotated* tags only unless ``--tags`` is passed, and this repository's tags
    are a mix: ``v0.9.0``, ``v1.0.0`` and ``v1.1.0`` are annotated tag objects, and
    ``v1.2.0``, ``v1.2.1`` and ``v2.0.0rc1`` are lightweight refs pointing straight at a
    commit. Measured here, on this repository, today::

        git describe --exact-match v2.0.0rc1    rc=128  "fatal: no tag exactly matches"
        git tag --points-at v2.0.0rc1           rc=0     v2.0.0rc1

    So a guard built on describe would pass on the majority of this repository's own tags,
    including every one cut since 1.2.0 -- it would be red exactly where the check is most
    wanted and quiet everywhere else. Reading ``.git/`` directly is worse rather than
    better: it means reimplementing git's own ref resolution across a plain repository, a
    linked worktree (where ``.git`` is a *file*), and a ``packed-refs`` file.

    ``None`` is the answer for every way this can fail to produce one -- ``git`` not on
    PATH, a tree that is not a repository, an unreadable object store -- because each of
    them means the same thing here: there is nothing to check. A shallow clone is not one
    of them. ``fetch-depth: 0`` is what ``test.yml`` uses and it fetches the tags; a local
    shallow clone has whatever tags it fetched, and a tag that is absent is the quiet
    case rather than an error. Reading tags is cheap and never mutates the repository, so
    there is nothing here that a CI runner could reach a different answer on.
    """
    if shutil.which("git") is None:
        return None
    completed = subprocess.run(
        ["git", "tag", "--points-at", "HEAD"],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    return completed.stdout.split()


def test_the_tag_at_head_names_the_version_this_build_reports() -> None:
    """A tag is the name a release is published under, and the build has to agree with it.

    **The accident this exists for.** A build reads its version from ``pyproject.toml``;
    the tag reaches the pipeline only as a ref, cut by a separate step. Cut ``v2.0.0rc2``
    on a tree still declaring ``2.0.0rc1`` and every other check in this file passes: the
    package, the import and the notes all agree with *each other*, on the wrong number.
    The tag, the binary's file properties, ``gigaxml-gui --version`` and the release page
    then describe three releases.

    **Quiet everywhere it has nothing to say.** On a branch, or in a clone holding no
    tags, this skips rather than fails -- which is the whole reason it is a test rather
    than a gate in ``ci-shape``. A check that goes red because a checkout is shallow is
    the failure mode this file's neighbours exist to prevent, and a guard that cries wolf
    on a pull request gets deleted.
    """
    tags = tags_at_head()
    if tags is None:
        pytest.skip("git could not be asked which tags point at HEAD")
    named = [tag for tag in tags if VERSION_TAG.match(tag)]
    if not named:
        pytest.skip(f"HEAD is not a release tag: {tags or 'no tags point at it'}")

    # Both sources the build reads, and both are asked separately on purpose.
    # pyproject.toml is what the metadata, the file properties and the sdist carry;
    # gigaxml.__version__ is what `--version` prints. Comparing only the import would let
    # a pyproject-only drift land on the *other* test in this file rather than here, and
    # comparing only pyproject would miss the import half. Each assert below names which
    # one is wrong, because "the versions disagree" is not an actionable failure message.
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    claimed = {
        "pyproject.toml": str(pyproject["project"]["version"]),
        "gigaxml.__version__": __version__,
    }
    for tag in sorted(named):
        tagged = tag.removeprefix("v")
        for source, value in claimed.items():
            assert tagged == value, (
                f"the tag {tag!r} at HEAD names version {tagged!r}, but {source} says "
                f"{value!r}: a build reads its version from the tree and not from the "
                f"tag, so this binary and this tag would ship as two different releases"
            )


def test_the_tag_check_would_not_have_looked_at_a_lightweight_tag(tmp_path: pathlib.Path) -> None:
    """The reason for ``--points-at``, proved rather than asserted in a comment.

    A guard that has quietly stopped watching is worse than one that is red, and a guard
    built on ``git describe --exact-match`` does exactly that on a lightweight tag: it
    reports no tag at all and the release goes out unchecked. So this builds a throwaway
    repository, tags it the lightweight way, and asks both questions -- the one this guard
    asks, and the one it does not.

    Nothing about this touches the real repository, which is why it is safe to run
    everywhere rather than only where the tags happen to be fetched.
    """
    if shutil.which("git") is None:  # pragma: no cover - git is present to run pytest
        pytest.skip("git is not on PATH")

    def git(*arguments: str, cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess[str]:
        # Every call carries an explicit cwd, defaulting to this test's own temporary
        # repository. A git invocation that inherits the working directory runs against the
        # real checkout, and `git add -A` there stages whatever this tree happens to have
        # modified -- which is how this test was written once, and had to be taken back
        # out of the index again.
        return subprocess.run(
            ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *arguments],
            cwd=str(cwd or tmp_path),
            capture_output=True,
            text=True,
            check=False,
        )

    def commit_one(root: pathlib.Path) -> None:
        """``root`` becomes a repository with exactly one commit and no tags."""
        assert git("init", "-q", cwd=root).returncode == 0
        (root / "a.txt").write_text("x", encoding="utf-8")
        assert git("add", "-A", cwd=root).returncode == 0
        done = git("commit", "-q", "-m", "c", cwd=root)
        assert done.returncode == 0, done.stderr

    commit_one(tmp_path)
    tagged = git("tag", "v9.9.9")  # a lightweight tag: no -a, no -m
    assert tagged.returncode == 0, tagged.stderr

    # What the guard asks, on a lightweight tag.
    assert tags_at_head(tmp_path) == ["v9.9.9"]

    # And the quiet branch, on a repository whose HEAD simply carries no tag: an empty
    # list, not None. That is the case every pull request in CI hits, and the distinction
    # is the guard's whole behaviour -- None means "could not ask" and [] means "asked, and
    # HEAD is not a release". Both skip; conflating them would hide the second. (A
    # repository with *no commits* answers None instead, because `git tag --points-at HEAD`
    # cannot resolve HEAD at all -- which is also true, and is the same answer.)
    with tempfile.TemporaryDirectory() as bare:
        root = pathlib.Path(bare)
        commit_one(root)
        assert tags_at_head(root) == []

    # The third quiet branch: a directory that is not a repository at all. Note this cannot
    # be a subdirectory of tmp_path -- git walks up, so a subdirectory *is* in the
    # repository, and asking there answers normally. It needs a directory outside any
    # repository, which the platform's own temporary area normally is. If some machine
    # keeps its temporary area inside a checkout, that cannot be staged and this test says
    # so out loud rather than failing on the ambient environment.
    with tempfile.TemporaryDirectory() as outside:
        probe = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=outside,
            capture_output=True,
            check=False,
        )
        if probe.returncode == 0:  # pragma: no cover - a temp dir inside a checkout
            pytest.skip(
                f"the temporary area is inside a repository ({outside}); the "
                "not-a-repository case cannot be staged here"
            )
        assert tags_at_head(pathlib.Path(outside)) is None

    # What the alternative would have answered, and why it was not used.
    describe = git("describe", "--exact-match", "HEAD")
    assert describe.returncode != 0, (
        f"git describe --exact-match found {describe.stdout.strip()!r}; if lightweight "
        "tags are now found by default this comment -- and the choice of --points-at -- "
        "is stale and should be re-argued"
    )


#: The spellings a release may carry: three numeric parts, optionally followed by a
#: release-candidate marker and its number. PEP 440 normalises ``2.0.0rc1`` to itself, so
#: the tag ``v2.0.0rc1`` and the PyPI name ``2.0.0rc1`` both name the same thing -- which
#: is the entire difference between a release candidate and the placeholder class this
#: test exists to refuse. Everything outside this shape is refused, including the
#: spellings that look close to it: ``2.0.0-rc1`` is a different string that a tag and an
#: upload would not both match, and ``2.0.0rc`` names no candidate at all.
RELEASE_SPELLING = re.compile(r"\A\d+\.\d+\.\d+(?:rc\d+)?\Z")

#: Spellings that must be refused, with the reason each one is in the placeholder class.
#: Pinned as data rather than left to one assertion, so widening the pattern above has
#: somewhere to show up: each of these is a version that would ship as a name no tag
#: matches, or as a pre-release snapshot nobody released.
REFUSED_SPELLINGS = [
    pytest.param("2.0.0.dev1", id="dev-snapshot"),
    pytest.param("2.0.0a1.dev2", id="dev-inside-an-alpha"),
    pytest.param("2.0.0+giga", id="local-suffix"),
    pytest.param("2.0.0.dirty", id="local-in-the-third-part"),
    pytest.param("2.0.0.1", id="four-numeric-parts"),
    pytest.param("2.0.0-rc1", id="un-normalised-rc"),
    pytest.param("2.0.0rc", id="rc-without-a-number"),
    pytest.param("2.0", id="two-parts"),
    pytest.param("2.0.0.post1", id="post-release"),
]


def test_the_version_is_not_a_development_placeholder() -> None:
    """A version that parses but carries no release semantics has slipped through.

    ``1.x`` is a deliberate statement: the interface in the README's Non-goals and Known
    limitations is the interface, and it is no longer allowed to move under a user --
    ``1.1.0`` added to the generator's output rather than changing the interface, which is
    exactly the difference the major number is there to record. ``0.x`` meant the
    opposite: usable, but still free to change. What this refuses is the placeholder class
    -- a version left at a pre-release snapshot or one carrying a local suffix that no tag
    and no PyPI upload would match.

    **A release candidate is not in that class, and the check used to say it was.**
    Requiring every part to be a digit refused ``2.0.0rc1`` -- which has a tag,
    ``v2.0.0rc1``, and a PyPI name, and is what 2.0 is actually being released as. So the
    pattern now states the whole vocabulary rather than one spelling of it, and
    :data:`REFUSED_SPELLINGS` is the rest of that vocabulary, kept as data so a later
    widening has somewhere to land visibly instead of passing quietly.
    """
    assert RELEASE_SPELLING.match(__version__), (
        f"{__version__!r} is not a spelling a tag and a PyPI upload would both match: "
        "three numeric parts, optionally followed by rc and a number"
    )


@pytest.mark.parametrize("candidate", REFUSED_SPELLINGS)
def test_the_placeholders_stay_refused(candidate: str) -> None:
    """The refusals above are the guard; this is what keeps them refusals.

    Without it, ``RELEASE_SPELLING`` is a pattern nobody has watched fail, and the way to
    make the current version legal -- loosening the pattern -- is also the way to make
    ``2.0.0.dev1`` legal, one character at a time.
    """
    assert not RELEASE_SPELLING.match(candidate), (
        f"{candidate!r} is a version no tag and no PyPI upload would match, and it must "
        "stay refused"
    )


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
