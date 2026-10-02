"""Bound the size of the comments in ``src/``, and prove the bound can fail.

A comment earns its place by answering one question: *would the next person change
this code wrongly without it?* Nothing here can answer that, and this file does not
pretend to. What it can do is refuse the shape a comment takes when it stops
answering it -- the comment that grew into an essay, because nothing ever charged for
the growth. So the rule is a size limit, and the real work is two things this file
does for you rather than for itself: it prints the distribution the limit is derived
from (``--report``), and it mutates a copy to prove the limit still bites
(``--verify``).

**How comments are read, and why it is not a search.** ``#`` is not a comment marker in
Python, it is a token: ``x = "# not a comment"`` is a string, and a regular expression
cannot tell it from a comment. ``ast`` is worse for this purpose -- comments are not
nodes in the tree, so ``ast`` does not see them at all, and any tool that claims to have
counted comments by walking the AST has counted nothing. ``tokenize`` can: it emits
exactly one ``COMMENT`` token per physical line, and only for real comments. Docstrings
are the mirror image -- absent from the comment stream, but genuinely present in the
``ast`` as the first statement of a module, class or function -- so they are read that
way instead, and are held to a different limit because they are a different thing.

**What is grandfathered, and why it is keyed by content.** ``comment_exemptions.json``
lists comments already over the limit when the limit was introduced. It is keyed by a
hash of the block's text, never by a line number: a line number moves the moment anyone
edits the file above it, and an exemption that silently stops matching is worse than no
exemption at all. Hashing the text means editing a grandfathered comment makes it a new
comment again -- it must be justified afresh, which is the intended friction.

Usage::

    python tools/check_comments.py              # check src/ against the limits
    python tools/check_comments.py --report     # size distribution, for choosing limits
    python tools/check_comments.py --verify     # mutate a copy, prove the check goes red
    python tools/check_comments.py --prune      # drop exemptions whose comment is gone
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import pathlib
import shutil
import sys
import tempfile
import tokenize
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SOURCE_DIR = "src"
EXEMPTIONS = pathlib.Path("tools") / "comment_exemptions.json"

#: A run of adjacent ``#`` lines at one indentation is one comment, and this is how many
#: lines it may be. Derived from the measured distribution (``--report``): see
#: ``docs/STAGE3-M18-REPORT.md`` §2 for the numbers this came from.
MAX_COMMENT_LINES = 7

#: Characters across a whole comment block, independently of how it is wrapped. A block
#: can obey MAX_COMMENT_LINES and still be a wall of text, and this is the limit that
#: catches that: it measures the reading, not the shape.
MAX_COMMENT_CHARS = 480

#: A module, class or function docstring may be longer than a comment, because it is the
#: API's own description and has to survive being read on its own. Measured separately
#: from comments for the same reason: they are not the same kind of text.
MAX_DOCSTRING_LINES = 40


class CommentCheckError(Exception):
    """Raised when the check fails, carrying every problem rather than the first."""


def fingerprint(kind: str, lines: list[str]) -> str:
    """A short, stable id for one comment or docstring.

    Indentation is stripped per line before hashing, so re-indenting a block -- moving
    it into a function, out of one -- does not silently revoke its exemption. Changing a
    single character does, which is the point.
    """
    payload = "\n".join(line.strip() for line in lines)
    digest = hashlib.sha256(f"{kind}\n{payload}".encode()).hexdigest()
    return digest[:16]


def python_files(root: pathlib.Path) -> list[pathlib.Path]:
    """Every ``.py`` file under ``root/src``, in a stable order."""
    base = root / SOURCE_DIR
    if not base.is_dir():
        raise CommentCheckError(f"{base} does not exist")
    return sorted(p for p in base.rglob("*.py") if p.is_file())


def read_blocks(text: str) -> list[dict[str, Any]]:
    """Comment blocks, read from ``tokenize``.

    One ``COMMENT`` token is exactly one physical line, so a block is a run of those
    tokens that are adjacent in the file *and* start at the same column -- two comments
    one indent apart are two comments, not one long one. Blank lines produce no token,
    so a blank line between two comments ends the run without needing to be seen.
    """
    blocks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type != tokenize.COMMENT:
            continue
        line, column = token.start
        if current is not None and current["last_line"] + 1 == line and current["column"] == column:
            current["lines"].append(token.string)
            current["last_line"] = token.end[0]
            continue
        current = {
            "column": column,
            "first_line": line,
            "last_line": token.end[0],
            "lines": [token.string],
        }
        blocks.append(current)
    for block in blocks:
        block["chars"] = sum(len(line) for line in block["lines"])
        block["fingerprint"] = fingerprint("comment", block["lines"])
    return blocks


def read_docstrings(text: str, relative: str) -> list[dict[str, Any]]:
    """Module, class and function docstrings, read from the ``ast``.

    Each is the first statement of its owner when that statement is a bare string. An
    attribute docstring -- a bare string after a field assignment -- is deliberately not
    counted: it is invisible to readers of the source and is better expressed as the
    field's own annotation or a comment.
    """
    tree = ast.parse(text, filename=relative)
    found: list[dict[str, Any]] = []
    owners: dict[type, str] = {
        ast.Module: "<module>",
        ast.ClassDef: "class",
        ast.FunctionDef: "def",
        ast.AsyncFunctionDef: "def",
    }
    for node in ast.walk(tree):
        name = owners.get(type(node))
        if name is None:
            continue
        body: list[ast.stmt] = getattr(node, "body", [])
        if not body:
            continue
        first = body[0]
        if not (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            continue
        label = "<module>" if name == "<module>" else f"{name} {node.name}"
        segment = ast.get_source_segment(text, first) or ""
        lines = segment.splitlines() or [segment]
        found.append(
            {
                "owner": label,
                "first_line": first.lineno,
                "last_line": first.end_lineno or first.lineno,
                "lines": lines,
                "chars": len(segment),
                "fingerprint": fingerprint("docstring", lines),
            }
        )
    return found


def scan(root: pathlib.Path) -> dict[str, dict[str, Any]]:
    """Comment and docstring blocks for every source file under ``root/src``."""
    report: dict[str, dict[str, Any]] = {}
    for path in python_files(root):
        relative = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        physical = len(text.splitlines())
        blocks = read_blocks(text)
        docstrings = read_docstrings(text, relative)
        comment_lines = sum(len(b["lines"]) for b in blocks)
        docstring_lines = sum(len(d["lines"]) for d in docstrings)
        code_lines = physical - comment_lines - docstring_lines
        report[relative] = {
            "physical_lines": physical,
            "code_lines": code_lines,
            "comment_lines": comment_lines,
            "docstring_lines": docstring_lines,
            "comment_blocks": blocks,
            "docstrings": docstrings,
        }
    return report


def over_limit(entry: dict[str, Any]) -> str | None:
    """Why this block is over the limit, or ``None`` if it is within it."""
    lines = len(entry["lines"])
    if lines > MAX_COMMENT_LINES:
        return f"{lines} lines > {MAX_COMMENT_LINES}"
    if entry["chars"] > MAX_COMMENT_CHARS:
        return f"{entry['chars']} chars > {MAX_COMMENT_CHARS}"
    return None


def load_exemptions(root: pathlib.Path) -> dict[str, dict[str, list[str]]]:
    """The grandfathered fingerprints, per file and per kind."""
    path = root / EXEMPTIONS
    if not path.is_file():
        return {}
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {
        relative: {
            "comments": list(entry.get("comments", [])),
            "docstrings": list(entry.get("docstrings", [])),
        }
        for relative, entry in loaded.get("files", {}).items()
    }


def docstring_over_limit(entry: dict[str, Any]) -> str | None:
    """Why this docstring is over its limit, or ``None``."""
    lines = len(entry["lines"])
    if lines > MAX_DOCSTRING_LINES:
        return f"{lines} lines > {MAX_DOCSTRING_LINES}"
    return None


def check(root: pathlib.Path) -> tuple[list[str], list[str]]:
    """Check the tree. Returns ``(problems, grandfathered)``, both fully populated."""
    report = scan(root)
    exemptions = load_exemptions(root)
    problems: list[str] = []
    grandfathered: list[str] = []
    seen: dict[tuple[str, str, str], str] = {}

    for relative, data in sorted(report.items()):
        allowed = exemptions.get(relative, {"comments": [], "docstrings": []})
        for kind, entries, limit in (
            ("comments", data["comment_blocks"], over_limit),
            ("docstrings", data["docstrings"], docstring_over_limit),
        ):
            for entry in entries:
                reason = limit(entry)
                if reason is None:
                    continue
                key = (relative, kind, entry["fingerprint"])
                seen[key] = reason
                where = f"{relative}:{entry['first_line']}"
                head = entry["lines"][0].strip().lstrip("#").strip()[:60]
                if entry["fingerprint"] in allowed[kind]:
                    grandfathered.append(f"  - {where}  {reason}  [{kind}]  {head}")
                else:
                    problems.append(f"{where}  {reason}  [{kind}]  {head}")

    for relative, entry in sorted(exemptions.items()):
        if relative not in report:
            problems.append(f"{EXEMPTIONS.as_posix()} lists {relative}, which is not a source file")
            continue
        for kind in ("comments", "docstrings"):
            for value in entry[kind]:
                if (relative, kind, value) not in seen:
                    problems.append(
                        f"{EXEMPTIONS.as_posix()} exempts a {kind[:-1]} in {relative} "
                        f"({value}) that is no longer over the limit -- prune it, or the "
                        f"list starts granting permission to things nobody checked"
                    )
    return problems, grandfathered


def print_report(root: pathlib.Path) -> int:
    """The measured distribution, which is where the limits come from."""
    report = scan(root)
    totals = {
        "physical_lines": 0,
        "code_lines": 0,
        "comment_lines": 0,
        "docstring_lines": 0,
    }
    print("=" * 100)
    print(f"{'file':<46}{'lines':>7}{'comment':>9}{'docstr':>8}{'c%':>7}{'d%':>7}")
    print("-" * 100)
    for relative, data in sorted(
        report.items(), key=lambda kv: -(kv[1]["comment_lines"] / kv[1]["physical_lines"])
    ):
        physical = data["physical_lines"]
        c_pct = 100.0 * data["comment_lines"] / physical if physical else 0.0
        d_pct = 100.0 * data["docstring_lines"] / physical if physical else 0.0
        print(
            f"{relative:<46}{physical:>7}{data['comment_lines']:>9}"
            f"{data['docstring_lines']:>8}{c_pct:>6.1f}%{d_pct:>6.1f}%"
        )
        for key in totals:
            totals[key] += data[key]
    print("-" * 100)
    overall_c = 100.0 * totals["comment_lines"] / totals["physical_lines"]
    overall_d = 100.0 * totals["docstring_lines"] / totals["physical_lines"]
    print(
        f"{'TOTAL':<46}{totals['physical_lines']:>7}{totals['comment_lines']:>9}"
        f"{totals['docstring_lines']:>8}{overall_c:>6.1f}%{overall_d:>6.1f}%"
    )

    print()
    print("=" * 100)
    print("comment block sizes (lines) -- the distribution MAX_COMMENT_LINES comes from")
    print("=" * 100)
    sizes: dict[int, int] = {}
    for data in report.values():
        for block in data["comment_blocks"]:
            size = len(block["lines"])
            sizes[size] = sizes.get(size, 0) + 1
    for size in sorted(sizes):
        bar = "#" * min(60, sizes[size])
        print(f"  {size:>3} lines  {sizes[size]:>5}  {bar}")

    print()
    print("=" * 100)
    print("docstring sizes (lines) -- the distribution MAX_DOCSTRING_LINES comes from")
    print("=" * 100)
    doc_sizes: dict[int, int] = {}
    for data in report.values():
        for doc in data["docstrings"]:
            size = len(doc["lines"])
            doc_sizes[size] = doc_sizes.get(size, 0) + 1
    for size in sorted(doc_sizes):
        bar = "#" * min(60, doc_sizes[size])
        print(f"  {size:>3} lines  {doc_sizes[size]:>5}  {bar}")

    print()
    print()
    print("=" * 100)
    print("cumulative -- where the limits sit in the distribution they were taken from")
    print("=" * 100)
    all_comments = [b for d in report.values() for b in d["comment_blocks"]]
    all_docstrings = [d for d in report.values() for d in d["docstrings"]]
    comment_lines = sorted({len(b["lines"]) for b in all_comments})
    comment_chars = sorted({b["chars"] for b in all_comments})
    doc_lines = sorted({len(d["lines"]) for d in all_docstrings})
    # Character counts are dense -- almost every value in the range occurs -- so listing
    # them all would bury the answer. Round steps plus the limit itself is the readable
    # form of the same data, and it is the same data.
    char_steps = sorted({c for c in comment_chars if c % 50 == 0} | {MAX_COMMENT_CHARS})

    def cumulative(values: list[int], sizes: list[int], total: int, limit: int, label: str) -> None:
        for size in sizes:
            count = sum(1 for v in values if v <= size)
            mark = f"   <- {label}" if size == limit else ""
            print(
                f"  <= {size:>3}   {count:>5} of {total:>4}  ({100.0 * count / total:5.1f}%){mark}"
            )

    print(f"\ncomment blocks, by line count  ({len(all_comments)} blocks total)")
    cumulative(
        [len(b["lines"]) for b in all_comments],
        comment_lines,
        len(all_comments),
        MAX_COMMENT_LINES,
        "MAX_COMMENT_LINES",
    )
    print(f"\ncomment blocks, by character count  ({len(all_comments)} blocks total)")
    cumulative(
        [b["chars"] for b in all_comments],
        char_steps,
        len(all_comments),
        MAX_COMMENT_CHARS,
        "MAX_COMMENT_CHARS",
    )
    print(f"\ndocstrings, by line count  ({len(all_docstrings)} docstrings total)")
    cumulative(
        [len(d["lines"]) for d in all_docstrings],
        doc_lines,
        len(all_docstrings),
        MAX_DOCSTRING_LINES,
        "MAX_DOCSTRING_LINES",
    )

    print()
    print("=" * 100)
    print("what is over each limit right now")
    print("=" * 100)
    over_comments = sum(1 for b in all_comments if over_limit(b))
    over_docs = sum(1 for d in all_docstrings if docstring_over_limit(d))
    print(f"{'comment blocks over the comment limits:':<50}{over_comments} of {len(all_comments)}")
    print(f"{'docstrings over the docstring limit:':<50}{over_docs} of {len(all_docstrings)}")
    print(
        f"{'grandfathered in comment_exemptions.json:':<50}"
        f"{sum(len(v) for v in load_exemptions(root).values())}"
    )
    return 0


def build_exemption_drafts(root: pathlib.Path) -> list[tuple[str, str, str, str]]:
    """``(relative, kind, fingerprint, reason)`` for everything currently over a limit."""
    report = scan(root)
    drafts: list[tuple[str, str, str, str]] = []
    for relative, data in sorted(report.items()):
        for block in data["comment_blocks"]:
            reason = over_limit(block)
            if reason:
                drafts.append((relative, "comments", block["fingerprint"], reason))
        for doc in data["docstrings"]:
            reason = docstring_over_limit(doc)
            if reason:
                drafts.append((relative, "docstrings", doc["fingerprint"], reason))
    return drafts


def verify(root: pathlib.Path) -> int:
    """Prove the check can go red, on a copy, and prove the mutation landed.

    Three failure modes are guarded against here, all of which have bitten work in this
    repository before. A mutation that silently does nothing, so "the check goes red" is
    really "the check was already red for an unrelated reason". A mutation that lands on
    the real tree instead of the copy. And a mutation inserted at a fixed line, which in
    a module whose first line opens a docstring is a syntax error rather than a
    comment -- so the block is appended at the end of the file, where a ``#`` line is
    valid wherever it lands.
    """
    originals = python_files(root)
    if not originals:
        print("VERIFY FAILED: no source files to copy", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as work:
        sandbox = pathlib.Path(work) / "tree"
        shutil.copytree(root / SOURCE_DIR, sandbox / SOURCE_DIR)
        # The exemptions travel with it, or the copy would report a different set of
        # problems than the tree does and the comparison below would prove nothing.
        # Optional, because the check is meant to be runnable before they exist -- that
        # is how the limit was first shown to be red on a tree nobody had edited.
        if (root / EXEMPTIONS).is_file():
            shutil.copy2(root / EXEMPTIONS, sandbox / EXEMPTIONS)
        subject = sandbox / SOURCE_DIR / "gigaxml" / "run.py"
        relative = subject.relative_to(sandbox).as_posix()

        before_bytes = subject.read_bytes()
        before_problems, _ = check(sandbox)

        injected_lines = [
            f"# injected line {n:>2} of a comment nobody asked for"
            for n in range(1, MAX_COMMENT_LINES + 4)
        ]
        marker = "# --- injected by check_comments --verify ---"
        # Appended as bytes, never as text. ``read_text`` opens in universal-newline mode,
        # so reading and writing back would translate this repository's CRLF checkouts to
        # LF on the way through -- the same line-ending rewrite that left a working tree
        # dirty in M17, and the reason this copy would report a *negative* byte count
        # after gaining a comment. Appending bytes cannot touch what is already there.
        if before_bytes and not before_bytes.endswith(b"\n"):
            before_bytes += b"\n"
        lines_before = before_bytes.count(b"\n")
        block_line = lines_before + 2  # 1-based: the file's last line, then a blank one
        addition = ("\n" + marker + "\n" + "\n".join(injected_lines) + "\n").encode("utf-8")
        after_bytes = before_bytes + addition
        subject.write_bytes(after_bytes)

        decoded = after_bytes.decode("utf-8")
        if after_bytes == before_bytes:
            print("VERIFY FAILED: the mutation changed nothing", file=sys.stderr)
            return 1
        if marker not in decoded:
            print("VERIFY FAILED: the marker is not in the mutated file", file=sys.stderr)
            return 1
        written = [line for line in decoded.splitlines() if "injected line" in line]
        if len(written) != len(injected_lines):
            print(
                f"VERIFY FAILED: the file holds {len(written)} injected lines, "
                f"meant to hold {len(injected_lines)}",
                file=sys.stderr,
            )
            return 1
        parse_error: SyntaxError | None = None
        try:
            compile(decoded, relative, "exec")
        except SyntaxError as exc:  # pragma: no cover - would be a bug in this file
            parse_error = exc
        if parse_error is not None:
            print(
                f"VERIFY FAILED: the mutated file no longer parses: {parse_error}", file=sys.stderr
            )
            return 1

        after_problems, _ = check(sandbox)

    at_block = [p for p in after_problems if p.startswith(f"{relative}:{block_line} ")]
    introduced = [p for p in after_problems if p not in before_problems]

    print(f"subject              {relative}")
    print(
        f"mutation landed      {len(written)} comment lines at line {block_line}, "
        f"{len(after_bytes) - len(before_bytes):+d} bytes, marker found, file parses"
    )
    print(f"problems before      {len(before_problems)}")
    print(f"problems after       {len(after_problems)}")
    print()
    print("what the mutated copy reports that the clean one did not:")
    for problem in introduced:
        print(f"  + {problem}")
    print()

    if not at_block:
        print(
            f"VERIFY FAILED: nothing reported at {relative}:{block_line}, the line the "
            f"injected block starts on",
            file=sys.stderr,
        )
        return 1
    if not introduced:
        print(
            "VERIFY FAILED: the mutated tree reports the same problems as the clean one",
            file=sys.stderr,
        )
        return 1
    print(
        f"VERIFY: the limit bites. {len(introduced)} new problem(s) from a "
        f"{len(written) + 1}-line block; the real tree was not touched."
    )
    return 0


def prune(root: pathlib.Path) -> int:
    """Drop exemptions whose block is no longer over a limit."""
    exemptions = load_exemptions(root)
    drafts = {(rel, kind, value) for rel, kind, value, _ in build_exemption_drafts(root)}
    kept: dict[str, dict[str, list[str]]] = {}
    dropped = 0
    for relative, entry in sorted(exemptions.items()):
        for kind in ("comments", "docstrings"):
            survivors = [v for v in entry[kind] if (relative, kind, v) in drafts]
            dropped += len(entry[kind]) - len(survivors)
            if survivors:
                kept.setdefault(relative, {"comments": [], "docstrings": []})[kind] = survivors
    path = root / EXEMPTIONS
    if dropped == 0:
        print(f"nothing to prune in {EXEMPTIONS.as_posix()}")
        return 0
    payload = {
        "_comment": EXEMPTION_NOTE,
        "limits": {
            "MAX_COMMENT_LINES": MAX_COMMENT_LINES,
            "MAX_COMMENT_CHARS": MAX_COMMENT_CHARS,
            "MAX_DOCSTRING_LINES": MAX_DOCSTRING_LINES,
        },
        "files": kept,
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"pruned {dropped} stale exemption(s); {len(kept)} file(s) still have any")
    return 0


EXEMPTION_NOTE = (
    "Comments that were already over the limit when it was introduced. Keyed by a hash "
    "of the block's text, never by line number: an exemption keyed to a line stops "
    "matching the moment anyone edits above it, and one that silently stops matching "
    "is worse than none. Editing a listed comment makes it a new comment and it must be "
    "justified afresh. Run `python tools/check_comments.py --prune` after deleting one."
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--root", type=pathlib.Path, default=REPO_ROOT)
    parser.add_argument("--report", action="store_true", help="print the size distribution")
    parser.add_argument("--verify", action="store_true", help="mutate a copy, prove it fails")
    parser.add_argument("--prune", action="store_true", help="drop exemptions that are stale")
    parser.add_argument(
        "--list-exemptable", action="store_true", help="print fingerprints over the limit"
    )
    args = parser.parse_args(argv)

    try:
        if args.report:
            return print_report(args.root)
        if args.verify:
            return verify(args.root)
        if args.prune:
            return prune(args.root)
        if args.list_exemptable:
            for relative, kind, value, reason in build_exemption_drafts(args.root):
                print(f"{relative}\t{kind}\t{value}\t{reason}")
            return 0
        problems, grandfathered = check(args.root)
        if grandfathered:
            print(f"{len(grandfathered)} comment(s) are over a limit and grandfathered:")
            for line in grandfathered:
                print(line)
            print()
        if problems:
            print(f"\nCOMMENTS: {len(problems)} problem(s)", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            print(
                "\nA comment longer than the limit is not automatically wrong -- the ones "
                "that are not are listed in "
                f"{EXEMPTIONS.as_posix()} by the hash of their text. Shorten this one, or "
                "move it to the documentation where it belongs, or say why it stays.",
                file=sys.stderr,
            )
            return 1
        print(
            f"comments: clean. No comment over {MAX_COMMENT_LINES} lines / "
            f"{MAX_COMMENT_CHARS} chars, no docstring over {MAX_DOCSTRING_LINES} lines."
        )
        return 0
    except CommentCheckError as exc:
        sys.stdout.flush()
        print(f"\nCOMMENT CHECK COULD NOT RUN\n{exc}", file=sys.stderr)
        sys.stderr.flush()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
