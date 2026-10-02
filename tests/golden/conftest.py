"""Golden tests: the command line frozen before anything is refactored.

Stage 3 is nineteen milestones of refactoring, and a refactor's only real failure
mode is changing behaviour by accident. So this directory holds the evidence that it
did not: every case runs the CLI as a **real subprocess** and compares its exit code
and its bytes against a checked-in expectation.

**What is asserted is behaviour, never implementation.** No test here names a function,
asserts a call count, or imports a private attribute. A test that did would have to be
rewritten by the first milestone that moves code, and a rewritten test is a test that
proves nothing about whether the behaviour survived.

Two things move between machines and are normalised before comparison -- where the run
happened, and which line endings the platform picked. They are the only two. Everything
else, down to the score a candidate gets and the order of the path table, is asserted
as it came out, because a value that drifts is a behaviour that drifted.

**The known defects have tests of their own** in :mod:`tests.golden.test_known_defects`.
They assert today's *wrong* behaviour on purpose: a fix that arrives without a test
proving the behaviour used to be broken cannot be shown to have fixed anything. Each one
carries a comment saying what to assert once the fix lands.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

GOLDEN_DIR = Path(__file__).resolve().parent
EXPECTED_DIR = GOLDEN_DIR / "expected"

#: The document every golden case reads. Fixed content, so every count in every
#: expectation is a fact about the tool rather than about a regenerated dataset.
SOURCE_FIXTURE = "two_records.xml"

#: A document carrying a default namespace, for the cases that need one resolved.
NAMESPACED_FIXTURE = "namespaced.xml"

#: The config the extraction cases use. Three fields, one of them converted, so a
#: change in conversion is visible in the output bytes.
CONFIG_TEXT = """\
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
  price:
    path: price
    type: decimal
"""

#: A config whose ``record`` matches nothing -- the documented way to ask the tool to
#: explain its own record-path semantics.
UNMATCHED_CONFIG_TEXT = """\
record: /nope/nothing
fields:
  product_id:
    path: "@id"
"""

#: The placeholder that replaces the run directory in every expectation. Chosen because
#: it cannot occur in a path the tool would print.
TMP_PLACEHOLDER = "<TMP>"


def run_cli(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run the CLI in a real subprocess and return the finished process.

    ``-m gigaxml.cli`` rather than the console script: it enters the same ``main()``,
    and it does not depend on how this checkout happens to be installed, so the case a
    developer runs locally is the case CI runs.

    ``PYTHONIOENCODING`` is pinned because the comparisons are byte-for-byte and the
    default stdout encoding is a property of the machine, not of the tool.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-m", "gigaxml.cli", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env=env,
        check=False,
    )


def normalize(text: str, *, tmp_path: Path) -> str:
    """Erase the two things that differ between machines: where the run happened, and
    which line endings the platform picked.

    The run directory reaches the output in three spellings -- the path as given on the
    command line, the path through ``repr`` with every backslash doubled, and the path
    through JSON with them doubled again -- so all three roots are matched. Each match
    takes the *whole* path, not just the directory: a repr'd path doubles its separators
    too, so stopping at the directory would leave a tail like ``parts\\\\checkpoint.json``
    and a recorded expectation that only ever matched on the machine that wrote it.

    The tail's separators are then folded to ``/`` so a run recorded on Windows is
    comparable with one recorded on Linux. The tail ends at the first space or quote,
    which is where the path ends in every form the tool prints -- a quoted argument, a
    JSON string, or a bare path at the end of a line.

    Nothing else is touched. A value that needs normalising to pass is a value that
    should be watched, not smoothed away.
    """
    base = str(tmp_path)
    roots = {base, base.replace("\\", "/"), base.replace("\\", "\\\\")}
    for root in sorted(roots, key=len, reverse=True):
        # The directory, then separators, then everything up to the next space or quote.
        pattern = re.escape(root) + r"((?:/|\\)+[^\s'\"]*)"

        def fold(match: re.Match[str]) -> str:
            tail = match.group(1).replace("\\\\", "/").replace("\\", "/")
            return f"{TMP_PLACEHOLDER}{tail}"

        text = re.sub(pattern, fold, text)
    return text.replace("\r\n", "\n")


def read_golden(name: str) -> tuple[int, str, str]:
    """Read one expectation: its exit code, the stream it came from, and its bytes.

    The header line carries what a byte comparison cannot express on its own -- the exit
    code, and whether the frozen bytes are stdout, stderr, or an artefact on disk. Freezing
    all three together means the expectation describes the whole of what a user could
    observe, not just the part that happened to be printable.

    The file is read as bytes and decoded to preserve its original line endings (a
    sample artefact is CRLF) while avoiding the ``newline`` parameter of
    ``Path.read_text``, which exists only on Python 3.13+; the project supports 3.11+.
    """
    raw = (EXPECTED_DIR / name).read_bytes().decode("utf-8")
    header, _, body = raw.partition("\n")
    fields = dict(
        part.split("=", 1) for part in header.removeprefix("# ").split(" ") if "=" in part
    )
    return int(fields["exit_code"]), fields["stream"], body


def build_inputs(directory: Path) -> None:
    """Put this suite's fixed inputs into ``directory``.

    Shared with ``regenerate.py`` so the expectations are produced from the same inputs
    the tests run against -- a generator that built its own copy could drift from them
    and rewrite every expectation to match a fixture the tests never use.
    """
    fixtures = Path(__file__).resolve().parent.parent / "fixtures"
    (directory / "src.xml").write_bytes((fixtures / SOURCE_FIXTURE).read_bytes())
    (directory / "ns.xml").write_bytes((fixtures / NAMESPACED_FIXTURE).read_bytes())
    (directory / "config.yaml").write_text(CONFIG_TEXT, encoding="utf-8")
    (directory / "unmatched.yaml").write_text(UNMATCHED_CONFIG_TEXT, encoding="utf-8")


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A directory holding this test's inputs, so every path in the output is under it.

    The inputs are copied rather than referenced in place: the expectations replace the
    run directory with a placeholder, and that only works if the run directory is
    something the test controls and the repository path never appears in the output.
    """
    build_inputs(tmp_path)
    return tmp_path
