"""Mutation testing over the core subset: change one line, insist a test goes red.

**What this is for.** Coverage says a line *executed*; it does not say a line *tested*.
Four modules sit at 100% coverage while a branch inside them could still be dead code
that no assertion can reach. The way to tell the two apart is to break the code on
purpose and see whether anything notices. If a mutation survives, the guard that
claims to protect that property is not protecting it.

**The rule that makes the result mean anything: a surviving mutant is a failure.**
A mutation that no test catches is not scored as a pass, is not skipped, and is not
quietly reported as "no regressions". The run exits non-zero and names it. A harness
that reports survivors as passes is a harness that reports nothing.

**One command::

    python tools/mutation.py

Add ``--only NAME`` (repeatable) to run a subset while iterating, and ``--keep-going``
to finish every mutant before exiting non-zero rather than stopping at the first
survivor. The default stops at the first survivor, because the first one is the
finding and the rest is a re-run waiting for it to be read.

**How a mutant is applied and undone.** Source files here are CRLF except
``fields.py``, which is LF, and ``core.autocrlf`` is on, so the working tree is not
the file git holds. Everything here is therefore *byte* arithmetic: the anchor is
matched as bytes, the replacement is built with the same line ending the anchor had,
and the file is restored from a byte-for-byte copy and then checked against the sha256
that copy was taken with. A mutant is only believed once that count check passes --
``old`` occurrences minus one, ``new`` occurrences plus one -- because an anchor that
matched the wrong thing produces a green run that says nothing.

**Why the target tests are per-mutant.** Each mutant names the suites that own the
property it breaks. Running the whole tree 30 times would take hours; running the
owning suites takes minutes and is what actually answers "is this property guarded".
A mutant whose target suites are green is a survivor.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Test suites per mutant. Kept beside the mutant rather than in a global list so a
#: reviewer can see, for each one, which guard is supposed to catch it.
CONFIG_TESTS = ("tests/unit/test_config.py",)
CHECKPOINT_TESTS = (
    "tests/unit/test_checkpoint.py",
    "tests/security/test_checkpoint_manifest.py",
    "tests/integration/test_checkpoint_corruption.py",
)
WRITER_TESTS = (
    "tests/integration/test_atomic_output.py",
    "tests/integration/test_writers.py",
    "tests/integration/test_output_durability.py",
)
FIELD_TESTS = (
    "tests/unit/test_fields.py",
    "tests/unit/test_field_rows.py",
    "tests/unit/test_inspect.py",
)
STREAMING_TESTS = (
    "tests/integration/test_streaming.py",
    "tests/integration/test_element_lifecycle.py",
    "tests/integration/test_record_path_chain.py",
    "tests/integration/test_document_shapes.py",
    "tests/integration/test_security_limits.py",
)
#: The two load-bearing invariants of the streaming reader are guarded by a memory
#: measurement, not by an assertion on a value, so they live in the performance tree.
#: They are named individually because running the whole performance suite would
#: generate a 400 MB dataset per run.
#:
#: ★ The two need *different* tests, and swapping them is a mistake this harness made
#: and kept: ``test_non_record_subtrees_are_released`` builds its own 29.5 MB orders-heavy
#: document, where the cleaned scan costs 2.1 MiB and the uncleaned one 387 MiB -- a
#: 184x separation, so it kills *any* mutation that stops cleanup happening. It says
#: nothing about the ``_clean=False`` flag specifically, because no caller passes it.
#: The flag itself is caught by ``test_uncleaned_reader_blows_up``, which is the only
#: test that passes ``clean=False`` and therefore the only one that can notice the
#: flag doing nothing. Pointing the flag mutant at the first test gave a SURVIVOR,
#: which was wrong about the code and right about the harness.
NONRECORD_TESTS = ("tests/performance/test_memory.py::test_non_record_subtrees_are_released",)
UNCLEAN_TESTS = ("tests/performance/test_memory.py::test_uncleaned_reader_blows_up",)
OVERWRITE_TESTS = (
    "tests/security/test_filesystem_paths.py",
    "tests/integration/test_cli_extract.py",
)


@dataclass(frozen=True)
class Mutant:
    """One change to one line of one file.

    Attributes:
        name: identifier used by ``--only`` and printed in the report.
        path: repository-relative file.
        old: the exact anchor text, matched as bytes.
        new: its replacement. May be empty for a deletion.
        property_name: the property this mutant is meant to break.
        tests: the suites that own it.
    """

    name: str
    path: str
    old: str
    new: str
    property_name: str
    tests: tuple[str, ...]


MUTANTS: tuple[Mutant, ...] = (
    # --- config.py: unknown keys are a hard error, and the version is checked ----
    Mutant(
        "config-unknown-key-ignored",
        "src/gigaxml/config.py",
        "unknown = sorted(str(key) for key in keys if key not in allowed)",
        "unknown = sorted(str(key) for key in keys if False)",
        "an unknown key is a hard error at every level (red line 14)",
        CONFIG_TESTS,
    ),
    Mutant(
        "config-field-unknown-key-ignored",
        "src/gigaxml/config.py",
        '_FIELD_KEYS: Final = frozenset({"path", "type", "required"})',
        '_FIELD_KEYS: Final = frozenset({"path", "type", "required", "requried"})',
        "an unknown key inside a field entry is a hard error",
        CONFIG_TESTS,
    ),
    Mutant(
        "config-version-value-compare",
        "src/gigaxml/config.py",
        "if type(version) is int and version == 1:",
        "if version == 1:",
        "version is checked by type, so True and 1.0 are refused",
        CONFIG_TESTS,
    ),
    Mutant(
        "config-version-ignored",
        "src/gigaxml/config.py",
        'if "version" not in data:\n        return',
        "if True:\n        return",
        "a config declaring a version this build cannot read is refused",
        CONFIG_TESTS,
    ),
    Mutant(
        "config-required-coerced",
        "src/gigaxml/config.py",
        "if not isinstance(required, bool):",
        "if False:",
        "a non-boolean 'required' is refused rather than coerced",
        CONFIG_TESTS,
    ),
    # --- checkpoint.py: hostile input is validated, never coerced ---------------
    Mutant(
        "checkpoint-int-coerced-from-string",
        "src/gigaxml/checkpoint.py",
        "if type(value) is not int:",
        "if False:",
        "'records_consumed' must be a JSON integer, not bool/float/string",
        CHECKPOINT_TESTS,
    ),
    Mutant(
        "checkpoint-sha-case-folded",
        "src/gigaxml/checkpoint.py",
        "if type(value) is not str or not _SHA256_PATTERN.match(value):",
        "if type(value) is not str:",
        "a sha256 must be 64 lowercase hex digits",
        CHECKPOINT_TESTS,
    ),
    Mutant(
        "checkpoint-part-name-pattern",
        "src/gigaxml/checkpoint.py",
        "if type(name) is not str or not PART_NAME_PATTERN.match(name):",
        "if type(name) is not str:",
        "a part name is checked for separators, traversal and extension",
        CHECKPOINT_TESTS,
    ),
    Mutant(
        "checkpoint-version-by-value",
        "src/gigaxml/checkpoint.py",
        "if type(version) is not int or version != CHECKPOINT_VERSION:",
        "if version != CHECKPOINT_VERSION:",
        "the manifest format version is checked by type before value",
        CHECKPOINT_TESTS,
    ),
    Mutant(
        "checkpoint-complete-by-value",
        "src/gigaxml/checkpoint.py",
        "if type(complete) is not bool:",
        "if False:",
        "'complete' must be a JSON boolean; the string 'false' is not false",
        CHECKPOINT_TESTS,
    ),
    Mutant(
        "checkpoint-rejected-le-consumed",
        "src/gigaxml/checkpoint.py",
        "if rejected > records_consumed:",
        "if False:",
        "a run cannot reject more records than it consumed",
        CHECKPOINT_TESTS,
    ),
    # --- writers.py: publish atomically, never over the input, no half files ----
    Mutant(
        "writers-partial-suffix-dropped",
        "src/gigaxml/writers.py",
        "self._partial_path = self._path.with_name(self._path.name + PARTIAL_SUFFIX)",
        "self._partial_path = self._path.with_name(self._path.name)",
        "output is written to <target>.tmp and only then renamed into place",
        WRITER_TESTS,
    ),
    Mutant(
        "writers-exit-publishes-on-failure",
        "src/gigaxml/writers.py",
        "if exc_info[0] is None:\n"
        "            self.close()\n"
        "        else:\n"
        "            self.abandon()",
        "self.close()",
        "a failed or cancelled run leaves no half-written file on the target",
        WRITER_TESTS,
    ),
    Mutant(
        "writers-batch-keys-unchecked",
        "src/gigaxml/writers.py",
        "if keys != self._field_name_set:",
        "if False:",
        "a row whose keys do not match the configured fields is refused",
        WRITER_TESTS,
    ),
    Mutant(
        "writers-csv-bool-inverted",
        "src/gigaxml/writers.py",
        'return "true" if value else "false"',
        'return "false" if value else "true"',
        "CSV booleans round-trip through the same spellings coerce_value accepts",
        WRITER_TESTS,
    ),
    Mutant(
        "writers-parquet-decimal-as-double",
        "src/gigaxml/writers.py",
        "FieldType.DECIMAL: pa.string(),",
        "FieldType.DECIMAL: pa.float64(),",
        "decimal is written as an exact Parquet string, never a lossy float",
        WRITER_TESTS,
    ),
    # --- fields.py: type inference boundaries -----------------------------------
    Mutant(
        "fields-int-underscore-accepted",
        "src/gigaxml/fields.py",
        '_INTEGER_LITERAL: Final = re.compile(r"^[+-]?\\d+$")',
        '_INTEGER_LITERAL: Final = re.compile(r"^[+-]?[\\d_]+$")',
        "int('1_000') is 1000 in Python, so a bare int() would accept it",
        FIELD_TESTS,
    ),
    Mutant(
        "fields-absolute-path-accepted",
        "src/gigaxml/fields.py",
        'if text.startswith("/"):',
        "if False:",
        "a field path is relative to the record, never absolute",
        FIELD_TESTS,
    ),
    Mutant(
        "fields-normalize-no-strip",
        "src/gigaxml/fields.py",
        'return " ".join("".join(elem.itertext()).split())',
        'return "".join(elem.itertext())',
        "field text is whitespace-collapsed and stripped before coercion",
        FIELD_TESTS,
    ),
    Mutant(
        "fields-decimal-via-float",
        "src/gigaxml/fields.py",
        "return decimal.Decimal(text)",
        "return float(text)",
        "a decimal price keeps its exact value and trailing zeros",
        FIELD_TESTS,
    ),
    Mutant(
        "inference-returns-decimal",
        "src/gigaxml/inspection/inference.py",
        'return FieldType.STRING, f"values do not share a narrower type; {sample}"',
        'return FieldType.DECIMAL, f"values do not share a narrower type; {sample}"',
        "decimal is never inferred; a human opts in deliberately",
        FIELD_TESTS,
    ),
    # --- parser/streaming.py: the two load-bearing invariants -------------------
    Mutant(
        "streaming-iterparse-tag-filter",
        "src/gigaxml/parser/streaming.py",
        'events=("start", "end"),',
        'events=("end",), tag=self.record_tag,',
        "iterparse(tag=) would hide every non-record end event and leak the document",
        NONRECORD_TESTS,
    ),
    Mutant(
        "streaming-clear-without-keep-tail",
        "src/gigaxml/parser/streaming.py",
        "elem.clear(keep_tail=True)",
        "elem.clear()",
        "clear(keep_tail=True) plus unlink keeps the surviving tree well-formed",
        STREAMING_TESTS,
    ),
    Mutant(
        "streaming-root-unlink-unguarded",
        "src/gigaxml/parser/streaming.py",
        "if parent is None:\n            return",
        "if parent is None:\n            parent = elem",
        "the root has no parent to unlink from; a comment before the root is its preceding sibling",
        STREAMING_TESTS,
    ),
    Mutant(
        "streaming-huge-tree-enabled",
        "src/gigaxml/parser/streaming.py",
        '"huge_tree": False,',
        '"huge_tree": True,',
        "the five parser options are the whole defence against a hostile document",
        STREAMING_TESTS,
    ),
    Mutant(
        "streaming-entities-resolved",
        "src/gigaxml/parser/streaming.py",
        '"resolve_entities": False,',
        '"resolve_entities": True,',
        "entities are never expanded (XXE)",
        STREAMING_TESTS,
    ),
    Mutant(
        "streaming-zero-match-returns-empty",
        "src/gigaxml/parser/streaming.py",
        "if matched == 0:",
        "if False:",
        "a record path that matched nothing raises instead of returning empty",
        STREAMING_TESTS,
    ),
    Mutant(
        "streaming-release-everything",
        "src/gigaxml/parser/streaming.py",
        "if not self._clean:\n            return",
        "if False:\n            return",
        "every consumed element is released, not just the matched records",
        UNCLEAN_TESTS,
    ),
)


def _anchor_bytes(anchor: str, haystack: bytes) -> bytes:
    """The anchor spelled the way *this* file spells it.

    Source here is CRLF except ``fields.py``, which is LF, and the anchors below are
    written with ``\\n`` like the rest of this file. A multi-line anchor therefore has
    to be re-spelled before it can match, and which spelling is right is a fact about
    the target file rather than about the mutant. Both are tried, in the order CRLF
    then LF, and the one that occurs exactly once is the one used.
    """
    candidates = (anchor.replace("\n", "\r\n").encode("utf-8"), anchor.encode("utf-8"))
    for candidate in candidates:
        if haystack.count(candidate) == 1:
            return candidate
    # Neither is unique. Return the plain spelling so the caller's count check reports
    # the real number and refuses, rather than silently picking one.
    return candidates[-1]


def _run_pytest(targets: tuple[str, ...], timeout: int = 900) -> tuple[int, str]:
    """Run pytest over ``targets``. Returns ``(returncode, tail of output)``.

    The subprocess form is deliberate and not incidental: a mutant edits a tracked
    source file, and pytest's assertion-rewriting caches ``.pyc`` beside it. Running
    in-process would let a rewritten module survive into the next mutant. A fresh
    interpreter per mutant is what makes the runs independent.

    ``--tb=no`` because the traceback of a mutated module is noise, not evidence; what
    is evidence is the return code.
    """
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "-p",
        "no:cacheprovider",
        "--no-header",
        "--tb=no",
        *targets,
    ]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    tail = (completed.stdout or "") + (completed.stderr or "")
    lines = [line for line in tail.splitlines() if line.strip()]
    return completed.returncode, "\n".join(lines[-4:])


def _apply(mutant: Mutant) -> None:
    """Write the mutation into the file, after proving the anchor is unique.

    **The count check, not "the old text is gone".** An anchor that appears twice
    would still leave the other copy behind, and a test suite would run against a file
    that is only partly mutated -- which is how a broken mutant reports itself green.
    So: count before, count after, and require exactly -1 and +1.
    """
    target = REPO_ROOT / mutant.path
    original = target.read_bytes()
    old_bytes = _anchor_bytes(mutant.old, original)
    old_count = original.count(old_bytes)
    if old_count != 1:
        raise SystemExit(
            f"mutant {mutant.name!r}: anchor occurs {old_count} times in {mutant.path}, "
            f"expected exactly 1. Refusing to mutate -- an ambiguous anchor produces a "
            f"green run that means nothing."
        )
    # The replacement is spelled the same way the anchor was, not the way it is written
    # here: a CRLF file handed LF line endings compiles to a syntax error rather than to
    # a mutation, which would be a red run proving nothing about the property.
    crlf = b"\r\n" in old_bytes
    new_bytes = mutant.new.replace("\n", "\r\n").encode("utf-8") if crlf else mutant.new.encode()
    backup = target.with_suffix(target.suffix + ".mutation-backup")
    backup.write_bytes(original)
    target.write_bytes(original.replace(old_bytes, new_bytes, 1))
    mutated = target.read_bytes()
    # The count is the evidence, not "the old text is gone": an anchor that occurs
    # twice would still leave the other copy in place.
    if mutated.count(old_bytes) != old_count - 1:
        raise SystemExit(f"mutant {mutant.name!r}: the old anchor is still present after the edit")
    if mutated.count(new_bytes) < 1:
        raise SystemExit(f"mutant {mutant.name!r}: the new anchor is not present after the edit")


def _restore(mutant: Mutant, sha_before: str) -> None:
    """Put the file back byte for byte, and prove it."""
    target = REPO_ROOT / mutant.path
    backup = target.with_suffix(target.suffix + ".mutation-backup")
    target.write_bytes(backup.read_bytes())
    backup.unlink()
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    if digest != sha_before:
        raise SystemExit(
            f"mutant {mutant.name!r}: {mutant.path} was not restored "
            f"(sha {digest[:12]} != {sha_before[:12]}). Stopping rather than continuing "
            f"on a mutated tree."
        )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


#: Two synthetic mutants used by ``--self-test``. They are not properties of the
#: repository; they are the harness's own calibration, and the reason the tool can be
#: believed when it reports 28 kills and zero survivors.
#:
#: ``self-test-real-defect`` plants a genuine behaviour change nobody wrote a mutant for:
#: the default error policy becomes ``quarantine``, so a config that declares no
#: ``on_error`` no longer aborts on the first bad record. A harness that cannot see it
#: is not looking at behaviour.
#:
#: ``self-test-equivalent`` is a byte-different, behaviour-identical edit: one space
#: added inside a set literal. It **must** survive. A harness that reported it as killed
#: would be measuring "did pytest exit non-zero", which any edit at all achieves, and
#: every one of the 28 real kills would then be worthless.
SELF_TEST_REAL = Mutant(
    "self-test-real-defect",
    "src/gigaxml/config.py",
    '_DEFAULT_ERROR_POLICY: Final = "abort"',
    '_DEFAULT_ERROR_POLICY: Final = "quarantine"',
    "SYNTHETIC: a real behaviour change, which must be reported RED",
    CONFIG_TESTS,
)
SELF_TEST_EQUIVALENT = Mutant(
    "self-test-equivalent",
    "src/gigaxml/config.py",
    'frozenset(\n    {"record", "namespaces", "fields", "on_error", "schema", "version"}\n)',
    'frozenset(\n    { "record", "namespaces", "fields", "on_error", "schema", "version"}\n)',
    "SYNTHETIC: byte-different, behaviour-identical, which must be reported SURVIVED",
    CONFIG_TESTS,
)


def _self_test() -> int:
    """Prove the harness can fail, and that a survivor makes it exit non-zero.

    Three assertions, and the third is the one that matters most: the whole value of
    this tool is that a surviving mutant fails the run. So the self-test does not just
    check that the equivalent mutant is classified as a survivor -- it checks that
    :func:`main` *returns 1* when one is present, which is the behaviour a reviewer has
    to be able to trust without re-deriving it.
    """
    problems: list[str] = []
    print("self-test: planting a real defect, and an equivalent edit\n")
    for mutant, must_be_red in ((SELF_TEST_REAL, True), (SELF_TEST_EQUIVALENT, False)):
        target = REPO_ROOT / mutant.path
        sha_before = _sha(target)
        _apply(mutant)
        try:
            code, _detail = _run_pytest(mutant.tests)
        finally:
            _restore(mutant, sha_before)
        red = code != 0
        label = "RED" if red else "SURVIVED"
        expected = "RED" if must_be_red else "SURVIVED"
        agrees = red == must_be_red
        print(f"{mutant.name:<26}{label:<10}expected {expected:<9}{'ok' if agrees else 'WRONG'}")
        if not agrees:
            problems.append(
                f"{mutant.name}: reported {label}, expected {expected}. "
                f"The harness is not measuring the property it claims to measure."
            )

    # And the exit code, which is what CI reads.
    code = main(["--only", SELF_TEST_EQUIVALENT.name, "--survivor-only"])
    if code == 0:
        problems.append(
            "a surviving mutant did not make the run exit non-zero. A harness that "
            "reports survivors as passes is a harness that reports nothing."
        )
    print(f"\nmain() with one survivor returned {code} (required: non-zero)")

    if problems:
        print("\nself-test FAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nself-test: the harness reports a real defect and tolerates an equivalent edit.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        action="append",
        default=None,
        help="run only the named mutant (repeatable); for iterating on one",
    )
    parser.add_argument(
        "--keep-going",
        action="store_true",
        help="run every mutant before reporting, instead of stopping at the first survivor",
    )
    parser.add_argument(
        "--list", action="store_true", help="print every mutant and its property, then exit"
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="plant a known-bad and an equivalent mutant to prove this harness can fail",
    )
    parser.add_argument(
        "--survivor-only",
        action="store_true",
        help="internal: allow --only to name a synthetic self-test mutant",
    )
    args = parser.parse_args(argv)

    if args.self_test:
        return _self_test()

    known = MUTANTS
    if args.survivor_only:
        known = (*known, SELF_TEST_REAL, SELF_TEST_EQUIVALENT)

    selected = MUTANTS
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {mutant.name for mutant in known}
        if unknown:
            raise SystemExit(f"unknown mutant name(s): {sorted(unknown)}")
        selected = tuple(mutant for mutant in known if mutant.name in wanted)

    if args.list:
        for mutant in selected:
            print(f"{mutant.name}\t{mutant.path}\t{mutant.property_name}")
        return 0

    # A clean tree is a precondition, not a nicety: this script rewrites tracked files,
    # and starting from a dirty one would restore the wrong bytes.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if dirty:
        raise SystemExit(f"src/ has uncommitted changes; refusing to start:\n{dirty}")

    print(f"{len(selected)} mutants, each against the suites that own its property.\n")
    print(f"{'mutant':<38}{'red?':<7}{'rc':<5}{'tests':<9}seconds")
    print("-" * 100)

    survivors: list[Mutant] = []
    started = time.monotonic()
    for index, mutant in enumerate(selected, start=1):
        target = REPO_ROOT / mutant.path
        sha_before = _sha(target)
        _apply(mutant)
        try:
            code, detail = _run_pytest(mutant.tests)
        finally:
            _restore(mutant, sha_before)
        red = code != 0
        elapsed = time.monotonic() - started
        print(
            f"{mutant.name:<38}{'RED' if red else 'SURVIVED':<7}{code:<5}"
            f"{len(mutant.tests):<9}{elapsed:6.1f}"
        )
        if not red:
            survivors.append(mutant)
            if not args.keep_going:
                print(f"\n  detail: {detail}")
                print("\nstopping at the first survivor; pass --keep-going for the rest.")
                break
        print(f"  {index}. {mutant.property_name}")

    print("-" * 100)
    killed = len(selected) - len(survivors)
    print(f"{killed} killed, {len(survivors)} survived, in {time.monotonic() - started:.1f}s")
    if survivors:
        print("\nSURVIVORS -- these are the finding. Each one is a property no test checks:")
        for mutant in survivors:
            print(f"  {mutant.name}: {mutant.property_name}")
            print(f"    {mutant.path}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
