"""Every relative link in the repository's markdown resolves, and every anchor it names
exists in the document it points at.

★ **The file list comes from `git ls-files`, not from walking the tree.** ``.gitignore``
names the reason in as many words: "enumerating patterns only creates ways to miss". A
walker here would need a list of everything to skip -- ``.venv``, ``data``, ``.scratch``,
``docs``, ``out``, ``state`` -- and the day a sixth one appears, this test quietly stops
covering it. The tracked set is exactly the set a reader of the repository can see, which
is the set criterion D is about, and git already knows what it is.

Two things make a naive version of this wrong, and both were true here before the check
existed:

* A heading inside a fenced block is not a heading. ``README.md`` has ``# 1. What is in
  this file?`` inside a ```` ```bash ```` block, and a checker that collects headings by
  regex would mint an anchor for it.
* GitHub's anchor is not "the heading, lowercased, with spaces turned into hyphens". It
  removes punctuation first and *then* replaces each remaining space, so a heading with an
  em-dash in the middle keeps a **double** hyphen: ``Layer 1 — a failed run publishes
  nothing`` is ``#layer-1--a-failed-run-publishes-nothing``. ``OUTPUT-DURABILITY.md``
  links itself that way, and a checker that collapsed runs of whitespace would report all
  three of those working links as broken -- and the obvious "fix" would break them for
  every reader on GitHub.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: ``[text](target)`` and ``![alt](target)``, plus an optional ``"title"``. The target is
#: non-greedy up to whitespace so a title is not swallowed into it.
LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")

#: A fence is three or more backticks, optionally opening with an info string.
FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")

HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def markdown_files() -> list[Path]:
    """Every tracked markdown file, as git itself lists them."""
    completed = subprocess.run(
        ["git", "ls-files", "-z", "*.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=120,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"git ls-files failed with {completed.returncode}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()[:200]}"
        )
    return [
        REPO_ROOT / p.decode("utf-8")
        for p in completed.stdout.split(b"\0")
        if p.decode("utf-8").endswith(".md")
    ]


def outside_fences(lines: list[str]) -> list[tuple[int, str]]:
    """``(line number, text)`` for the lines that are not inside a fenced block.

    Fences are matched by their delimiter character and length, as CommonMark requires:
    a ```` ``` ```` block is closed by three or more backticks, and a longer run inside it
    does not close it.
    """
    kept: list[tuple[int, str]] = []
    opening: str | None = None
    for number, text in enumerate(lines, start=1):
        match = FENCE.match(text)
        if opening is None:
            if match:
                opening = match.group(1)[0] * 3
                continue
            kept.append((number, text))
        elif match and match.group(1)[0] == opening[0] and len(match.group(1)) >= len(opening):
            opening = None
    return kept


def slug(heading: str) -> str:
    """The anchor GitHub generates for ``heading``.

    Punctuation is removed first and each remaining space becomes one hyphen, which is why
    a heading containing an em-dash yields a double hyphen rather than one.
    """
    text = "".join(c for c in heading.lower() if c.isalnum() or c in " -_")
    return text.replace(" ", "-")


def anchors_of(path: Path) -> set[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    found: set[str] = set()
    repeats: dict[str, int] = {}
    for _, text in outside_fences(lines):
        match = HEADING.match(text)
        if not match:
            continue
        name = slug(match.group(2))
        # GitHub disambiguates a repeated heading by appending -1, -2, ...
        if name in found:
            repeats[name] = repeats.get(name, 0) + 1
            name = f"{name}-{repeats[name]}"
        found.add(name)
    return found


def links_of(path: Path) -> list[tuple[int, str]]:
    """``(line number, target)`` for every markdown link outside fenced blocks."""
    found = []
    for number, text in outside_fences(path.read_text(encoding="utf-8").splitlines()):
        found.extend((number, match.group(1)) for match in LINK.finditer(text))
    return found


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "mailto:", "#")) or "://" in target


def local_targets() -> list[tuple[Path, int, str]]:
    """Every link that names something in this repository, absolute or relative."""
    pairs = []
    for path in markdown_files():
        for number, target in links_of(path):
            if not is_external(target):
                pairs.append((path, number, target))
    return pairs


RELATIVE_LINKS = local_targets()


def test_the_repository_has_markdown_to_check() -> None:
    """The check that follows is only worth anything if it read something."""
    assert len(RELATIVE_LINKS) > 20, f"only found {len(RELATIVE_LINKS)} relative links"


@pytest.mark.parametrize(
    ("source", "number", "target"),
    RELATIVE_LINKS,
    ids=[f"{p.relative_to(REPO_ROOT).as_posix()}:{n}:{t}" for p, n, t in RELATIVE_LINKS],
)
def test_every_relative_link_points_at_something_that_exists(
    source: Path, number: int, target: str
) -> None:
    path, _, _fragment = target.partition("#")
    resolved = (source.parent / path).resolve()
    assert resolved.exists(), (
        f"{source.relative_to(REPO_ROOT).as_posix()}:{number} points at {target!r}, "
        f"which is not in the repository"
    )


def test_every_anchor_names_a_heading_that_exists() -> None:
    dead = []
    for source in markdown_files():
        for number, target in links_of(source):
            path, _, fragment = target.partition("#")
            if not fragment:
                continue
            resolved = source if not path else (source.parent / path).resolve()
            if not resolved.exists():
                continue  # the file test above already reports this one
            available = anchors_of(resolved)
            if fragment not in available:
                dead.append(
                    f"{source.relative_to(REPO_ROOT).as_posix()}:{number} -> {target!r}; "
                    f"that document has {sorted(available)}"
                )
    assert not dead, "anchors that name nothing:\n  " + "\n  ".join(dead)


@pytest.mark.parametrize(
    "path", markdown_files(), ids=lambda p: p.relative_to(REPO_ROOT).as_posix()
)
def test_every_code_fence_is_closed(path: Path) -> None:
    """An unclosed fence turns every following section into a code block.

    ``README.md`` carried one for a while: a stray ``` after the benchmark table made the
    whole comparison section render as code, including the measured numbers it exists to
    show.
    """
    lines = _all_lines(path)
    fences = [(number, text) for number, text in lines if FENCE.match(text)]
    assert len(fences) % 2 == 0, (
        f"{path.relative_to(REPO_ROOT).as_posix()} has {len(fences)} fences; the last one, "
        f"{fences[-1][1].strip()!r} at line {fences[-1][0]}, is never closed"
    )


def _all_lines(path: Path) -> list[tuple[int, str]]:
    return list(enumerate(path.read_text(encoding="utf-8").splitlines(), start=1))


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Layer 1 — a failed run publishes nothing", "layer-1--a-failed-run-publishes-nothing"),
        (
            "Layer 3 — surviving a power cut is not promised",
            "layer-3--surviving-a-power-cut-is-not-promised",
        ),
        ("GigaXML vs Alternatives", "gigaxml-vs-alternatives"),
        ("What this is", "what-this-is"),
        ("`--resume` re-parses", "--resume-re-parses"),
        ("Known limitations", "known-limitations"),
    ],
)
def test_the_slug_is_the_one_github_generates(heading: str, expected: str) -> None:
    """Pinned because a stricter slug makes working links look broken.

    Two rows carry the weight. The em-dash ones: the repository already ships those
    anchors, and collapsing runs of whitespace -- which is what "lowercase and hyphenate"
    usually gets written as -- would report all three self-links in ``OUTPUT-DURABILITY.md``
    as dead and invite a "fix" that breaks them for everyone reading on GitHub. The
    ``--resume`` one: a hyphen is *kept*, because it is what turns the space after it into
    the anchor, so the flag's own leading dashes survive and the anchor starts ``--``.
    Writing that row's expectation as ``resume-re-parses`` is the mistake this row exists
    to catch, and it is the mistake that was made when this table was first written.
    """
    assert slug(heading) == expected


def test_a_heading_inside_a_fence_is_not_a_heading(tmp_path: Path) -> None:
    """README.md has ``# 1. What is in this file?`` inside a bash block."""
    probe = tmp_path / "probe.md"
    probe.write_text(
        "```bash\n# 1. What is in this file?\ngigaxml inspect big.xml\n```\n\n# Real\n",
        encoding="utf-8",
    )
    assert anchors_of(probe) == {"real"}
