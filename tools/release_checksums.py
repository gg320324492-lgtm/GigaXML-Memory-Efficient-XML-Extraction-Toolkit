"""Write and check the release's ``SHA256SUMS.txt``.

★ **Why this exists.** A checksum file is only worth anything if the hashes in it can be
recomputed by someone who does not trust the tool that wrote it, and only worth shipping
if it covers exactly what was shipped. Both properties are easy to lose and neither
announces itself when it happens, so both are enforced here rather than assumed:

*Covering exactly what was shipped.* The list of files is not maintained by this script.
It is read out of ``package.yml`` -- the ``files:`` glob of the step that creates the
release -- and expanded there and now, so the checksum file and the attachment list are
the same strings. A hand-kept list is a list that is correct until someone adds an
artifact, at which point the one file a user most wants to check is the one file with no
hash next to it, and nothing says so.

*Being checkable by something else.* The output is GNU ``sha256sum``'s format, so
``sha256sum -c SHA256SUMS.txt`` in the download directory verifies it without any of this
project's code. ``tests/test_release_checksums.py`` checks the format and the round trip;
it does not check that the numbers are right, because the numbers are checked by the two
implementations of SHA-256 that have no relationship to this one.

The checksum file is excluded from its own contents. That is not a special case bolted on
-- it is the reason the SBOM is generated *before* the checksums: a hash of the checksum
file cannot exist inside the checksum file.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "package.yml"

#: The name of the file itself. Excluded from its own contents, which is the whole reason
#: the SBOM has to be written first.
SELF_NAME = "SHA256SUMS.txt"

CHUNK = 1 << 20  # read the archives a megabyte at a time; a few hundred MiB otherwise


def release_globs(workflow_path: pathlib.Path) -> list[str]:
    """The ``files:`` globs of the step that creates the release, read from the workflow.

    Read rather than duplicated, so the checksum file cannot disagree with what gets
    attached. If the step cannot be found this raises rather than returning something
    plausible: a checksum file covering the wrong set is worse than none, because it looks
    like evidence.
    """
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if "action-gh-release" not in str(step.get("uses", "")):
                continue
            with_ = step.get("with") or {}
            files = with_.get("files")
            if isinstance(files, str) and files.strip():
                # ★ Whole-line comments are dropped here, and the reason is not tidiness.
                # `files: |` is a *literal* block scalar, so a `#` on its own line inside
                # one is text, not a comment -- YAML hands it to this script verbatim and
                # every such line becomes a glob that matches nothing. The first rehearsal
                # run carried nine lines of explanatory prose into the file list, which
                # matched nothing and was harmless by luck: a comment that read like a
                # path (`artifacts/**/GigaXML-Setup-*.exe` quoted in prose) would have
                # matched something, and the checksum file would have claimed a file the
                # release does not attach. Comments in that block belong above it.
                return [
                    line.strip()
                    for line in files.splitlines()
                    if line.strip() and not line.strip().startswith("#")
                ]
    raise ValueError(
        f"{workflow_path} has no step using action-gh-release with a `files:` list, so "
        f"there is nothing to describe. Refusing to write a checksum file covering a set "
        f"of files this script had to invent."
    )


def expand(base: pathlib.Path, artifacts: pathlib.Path, globs: list[str]) -> list[pathlib.Path]:
    """Every file under ``artifacts`` that ``globs`` match, sorted, no duplicates.

    ★ ``base`` is the directory the globs are relative to, and it is *not* ``artifacts``.
    The globs are copied verbatim out of the workflow, where they read ``artifacts/*.zip``
    and are resolved by GitHub Actions against the step's working directory. Matching them
    against ``artifacts`` instead looks for ``artifacts/artifacts/*.zip``, finds nothing,
    and quietly produces a checksum file over an empty set. This was not reasoned out: the
    first rehearsal run did exactly that, and was stopped by the guard in
    ``write_checksums`` rather than by a reader.

    ``**`` is handled by ``Path.glob``, which treats ``**`` as "any depth including none",
    so ``**/GigaXML-Setup-*.exe`` matches both a top-level installer and one inside a
    platform directory. Directories are excluded: the archives are what get attached, and
    the platform directories are the intermediates they were built from.
    """
    found: set[pathlib.Path] = set()
    for pattern in globs:
        for path in base.glob(pattern):
            if not path.is_file() or path.name == SELF_NAME:
                continue
            try:
                path.relative_to(artifacts)
            except ValueError:
                continue  # matched, but outside the directory the release attaches from
            found.add(path)
    return sorted(found, key=lambda p: p.relative_to(artifacts).as_posix())


def present_in(artifacts: pathlib.Path) -> set[str]:
    """Files sitting loose in the release directory, for the unlisted-artifact check.

    ★ Top level only, and that restriction is the point rather than a simplification.
    ``artifacts/`` holds two different kinds of thing: the archives that ship, and the
    platform directories they were built from. A first version walked the whole tree and
    reported ``gigaxml-gui-linux/_internal/libfoo.so`` as an artifact shipping with no hash
    beside it -- which is not a defect, it is the *input* to an archive that does have a
    hash. Flagging those trains a reader to ignore the message, and the one case that
    matters (a stray file next to the archives) is lost in the noise. What ships from a
    subdirectory is only ever what a glob reached, and the globs are checked above.
    """
    return {path.name for path in artifacts.iterdir() if path.is_file() and path.name != SELF_NAME}


def show(path: pathlib.Path, base: pathlib.Path) -> str:
    """How to name a path in the output: relative to the working directory when it is
    under it, absolute otherwise.

    ★ Written because the first rehearsal run crashed *after* writing a correct checksum
    file, on a `relative_to` that only holds inside the repository. A tool that produces
    the right bytes and then exits non-zero is worse than one that fails cleanly: the file
    is on disk and the caller has been told it was not made.
    """
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def digest(path: pathlib.Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_checksums(
    artifacts: pathlib.Path, workflow_path: pathlib.Path, base: pathlib.Path
) -> pathlib.Path:
    globs = release_globs(workflow_path)
    # Both paths are made absolute against the working directory, without resolving
    # symlinks. `base.glob()` yields absolute paths, so a relative `--artifacts` would make
    # every `relative_to` below raise ValueError -- and the `except ValueError: continue`
    # that guards "matched outside the directory" would then swallow every single match and
    # report an empty set. That is what happened on the second rehearsal run: a relative
    # path turned into a check that could only ever say "no files", and it did.
    base = base if base.is_absolute() else pathlib.Path.cwd() / base
    artifacts = artifacts if artifacts.is_absolute() else pathlib.Path.cwd() / artifacts
    files = expand(base, artifacts, globs)
    if not files:
        raise ValueError(
            f"the release globs {globs} matched no file under {artifacts} (resolved against "
            f"{base}). The archives are written beside the platform directories by the "
            f"assemble step, so this means that step did not run or wrote somewhere else "
            f"-- and a checksum file over an empty set would go out looking like evidence."
        )
    lines = [f"{digest(path)}  {path.relative_to(artifacts).as_posix()}" for path in files]
    out = artifacts / SELF_NAME
    # ★ newline="\n", and this is the single most load-bearing character in the file.
    # Python translates "\n" to the platform's line ending when writing text, so on
    # Windows this would otherwise write CRLF -- and `sha256sum -c` reads a CRLF checksum
    # file as filenames with a trailing carriage return, which it cannot open. It fails
    # with "FAILED open or read" on every line while the hashes themselves are all
    # correct, which is the most confusing possible report: the file is right and the tool
    # is reading it wrong. Measured, not assumed -- the first rehearsal run produced
    # exactly that, with certutil agreeing with all five hashes while sha256sum rejected
    # all five files. On the Linux runner the same code would have written LF and passed,
    # which is what makes it worth pinning rather than inheriting.
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {show(out, base)}: {len(files)} file(s)")
    print(f"  globs read from {show(workflow_path, base)}: {', '.join(globs)}")
    for path in files:
        print(f"  {digest(path)[:16]}  {path.relative_to(artifacts).as_posix()}")
    return out


def verify_checksums(checksums: pathlib.Path) -> int:
    """Recompute every hash and report the differences.

    Three distinct failures, kept apart because they mean different things: a file that is
    missing cannot be checked at all, a file whose bytes changed does not match its hash,
    and a file the checksum file does not mention is an artifact that shipped unverified.
    The last is the one that matters most and the one a plain ``sha256sum -c`` cannot see,
    because it only knows about the lines it was given.
    """
    artifacts = checksums.parent
    if not artifacts.is_absolute():
        artifacts = pathlib.Path.cwd() / artifacts
    listed: dict[str, str] = {}
    problems: list[str] = []
    for number, line in enumerate(checksums.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        expected, separator, name = line.partition("  ")
        if not separator or len(expected) != 64:
            problems.append(f"line {number} is not `<64 hex>  <path>`: {line!r}")
            continue
        listed[name] = expected

    for name, expected in sorted(listed.items()):
        path = artifacts / name
        if not path.is_file():
            problems.append(f"{name} is in the checksum file but not on disk")
            continue
        actual = digest(path)
        if actual != expected:
            problems.append(
                f"{name} does not match: file says {actual[:16]}..., checksum says "
                f"{expected[:16]}..."
            )
        else:
            print(f"  ok  {actual[:16]}  {name}")

    covered = present_in(artifacts)
    for orphan in sorted(covered - set(listed)):
        problems.append(
            f"{orphan} is in the release directory but not in {SELF_NAME}, so it would "
            f"ship with no hash beside it"
        )

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"\n{len(listed)} file(s) verified against {checksums.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--artifacts", type=pathlib.Path, default=pathlib.Path("artifacts"))
    parser.add_argument("--workflow", type=pathlib.Path, default=WORKFLOW)
    parser.add_argument(
        "--base",
        type=pathlib.Path,
        default=pathlib.Path.cwd(),
        help="the directory the workflow's globs are resolved from (a step's working directory)",
    )
    parser.add_argument("--verify", type=pathlib.Path, help="check this SHA256SUMS.txt instead")
    args = parser.parse_args(argv)
    try:
        if args.verify:
            return verify_checksums(args.verify)
        write_checksums(args.artifacts, args.workflow, args.base)
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        print(f"\nCOULD NOT RUN\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
