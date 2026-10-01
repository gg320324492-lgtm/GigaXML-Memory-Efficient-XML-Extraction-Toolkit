"""What each exit code is, and the one that must never come back.

**The exit code is the contract a script sees.** Everything else gigaxml prints is for a
person reading a terminal; the code is what a shell loop, a CI step or a wrapper reads
without reading anything. ``ERRORS.md`` documents five of them, and this file is the
executable half of that document -- if the two disagree, one of them is wrong and this
one is what the suite catches.

**Why the codes cannot simply be enumerated.** ``EXIT_OK``/``EXIT_ERROR``/``EXIT_USAGE``
are readable off the module. The two that matter are not:

* **``3`` is the whole feature.** A run stopped by Ctrl-C and a run that failed want
  opposite reactions -- after an error, retrying unchanged reproduces the error; after a
  stop, the work was going fine and resuming is safe. One code for both forces a script to
  parse stderr. The code exists because ``RunInterruptedError`` descends from
  ``GigaXMLError``, so the branch that produces it has to come **before** the one that
  catches the superclass; ``except`` matches in order and a clause below is unreachable.
  Nothing about that arrangement survives a refactor on its own, which is why the test
  below starts a real child process and stops it with a real signal.

* **``4`` is reserved, not exercised by the product.** Nothing in gigaxml raises an
  unexpected exception on purpose, so a test for ``4`` has to make one happen. It
  replaces the ``extract`` handler with one that raises, which is the same situation a
  genuine bug produces and the only honest way to reach the branch.

**Both mutations in ``docs/AGENT_BRIEF_STAGE3-M6FIX.md`` §1 C are checked here, and
the load-bearing assertions are named in the comments below** -- the interrupt one at
:func:`test_a_stopped_run_exits_3_not_1`, and the four ``1`` assertions that a
user-error code wrongly changed to ``3`` would turn red.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import gigaxml.cli as cli_module
from gigaxml.cli import (
    EXIT_ERROR,
    EXIT_INTERNAL,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    main,
)

#: Reused from the interrupt suite rather than rewritten. **A second copy of the
#: start-and-stop machinery would be a second copy of the timing it depends on**, and the
#: reason that suite times the signal by the run instead of by a sleep is a history of
#: this test being green locally and red on every CI platform. One mechanism, one
#: reason it can be flaky.
from tests.integration.test_interrupt_report import (
    MANIFEST_TIMEOUT_S,
    _config,
    _document,
    _start,
    _stop,
    _wait_for_manifest,
)

SOURCE = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<catalog><products>"
    '<product id="1"><name>Alpha</name></product>'
    '<product id="2"><name>Beta</name></product>'
    "</products></catalog>"
)

CONFIG = 'record: /catalog/products/product\nfields:\n  id:\n    path: "@id"\n'


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A document and a config, in the directory the runs happen inside.

    ``main()`` resolves relative paths against the *process* working directory, and the
    same-file refusal is only writable as a short relative name if there is somewhere
    for it to be relative to.
    """
    (tmp_path / "a.xml").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "cfg.yaml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / "nomatch.yaml").write_text(
        'record: /catalog/products/nosuchelement\nfields:\n  id:\n    path: "@id"\n',
        encoding="utf-8",
    )
    (tmp_path / "badtype.yaml").write_text(
        'record: /catalog/products/product\nfields:\n  id:\n    path: "@id"\n    type: notatype\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def extract(*args: str) -> int:
    return main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv", *args])


# --- criterion B: every code, each asserted on its own scenario ---------------


def test_a_run_that_worked_exits_0(workspace: Path) -> None:
    """The baseline every other code is a departure from.

    Asserted on a run that actually wrote its output, not on an empty invocation: a
    ``0`` from a command that did nothing would satisfy this just as well, and the
    distinction matters precisely because ``0`` is the code a wrapper trusts.
    """
    code = extract()

    assert code == EXIT_OK
    assert (workspace / "out.csv").read_text(encoding="utf-8").count("\n") == 3


def test_a_config_the_tool_cannot_use_exits_1(workspace: Path) -> None:  # noqa: ARG001
    """A field type that is not one of the supported ones.

    The first of the four ``1`` scenarios, and the plainest: the user's mistake is in
    the config, the message names the offending key, and nothing about it is worth
    retrying.
    """
    assert main(["extract", "a.xml", "-c", "badtype.yaml", "-o", "out.csv"]) == EXIT_ERROR


def test_a_record_path_that_matches_nothing_exits_1(workspace: Path) -> None:  # noqa: ARG001
    """The config is well-formed and the document is fine; they just disagree.

    Worth its own assertion rather than folding into the config case above, because it
    is the failure a user meets most often -- a renamed element, a path off by a level --
    and it arrives as a different error class. Both are the user's to fix, which is what
    ``1`` means.
    """
    assert main(["extract", "a.xml", "-c", "nomatch.yaml", "-o", "out.csv"]) == EXIT_ERROR


def test_an_input_file_that_does_not_exist_exits_1(workspace: Path) -> None:  # noqa: ARG001
    """**The environmental ``1`` -- an ``OSError``, not a ``GigaXMLError``.**

    A separate assertion because it is the one that reaches ``main`` by a different
    ``except`` clause. A change to that clause's return value moves this code and leaves
    the three above untouched, so covering only the ``GigaXMLError`` branch would let
    the environmental half of ``1`` go unverified.
    """
    assert main(["extract", "nope.xml", "-c", "cfg.yaml", "-o", "out.csv"]) == EXIT_ERROR


def test_writing_the_output_over_the_input_exits_1(workspace: Path) -> None:
    """**M5's refusal, carried on the ``1`` code.**

    The security policy in ``tests/security/test_filesystem_paths.py`` asserts on the
    file's bytes and deliberately says nothing about the exit code -- "we refuse" and
    "we refuse with a particular code" are different claims. This is the half that file
    does not make, and the half a wrapper reads: a script that must not retry has to see
    ``1``, and a script treating ``1`` as retryable must see the same code here as for a
    missing file.

    ``--format csv`` is passed for M5's reason: without it the run stops earlier, on
    format inference, and this would assert that check instead of the refusal.

    The source's bytes are checked too, not only the code. ``1`` is what a wrapper reads;
    the untouched file is what the *user* would notice, and a test asserting only the
    first would stay green if the refusal moved after the writer was created --
    ``test_filesystem_paths.py`` guards that, and the point here is that the code and the
    refusal arrive together.
    """
    before = (workspace / "a.xml").read_bytes()

    code = main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "a.xml", "--format", "csv"])

    assert code == EXIT_ERROR
    assert (workspace / "a.xml").read_bytes() == before, (
        "the run exited 1 but had already overwritten the document; a refusal that "
        "arrives after the write has destroyed the one copy of the data"
    )


def test_a_malformed_command_line_exits_2(
    workspace: Path,  # noqa: ARG001 -- needed for the chdir, the parser never reads it
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**argparse's code, and argparse raises rather than returns it.**

    Written as ``SystemExit`` on purpose. ``main()`` calls ``parse_args`` *outside* its
    own ``try``, so a usage error never reaches the ``except`` chain that produces every
    other code here -- it propagates as ``SystemExit(2)``, which the console script
    turns into exit status 2. Asserting ``main(...) == 2`` would not merely fail; it would
    fail for the wrong reason, on an exception rather than on the value, and the test
    would need rewriting the moment someone wrapped the parse.

    So this asserts the two things that are actually true: the exit status a caller sees
    is 2, and argparse is the one that decided it -- the message is its wording, not
    gigaxml's.

    The required arguments are all supplied, so argparse fails on the unknown option
    rather than on the first missing one. Left out, it reports "the following arguments
    are required: source, -c/--config, -o/--output" and never mentions the option this
    test is about -- still exit 2, but a test that passes without exercising the branch
    it names is the failure mode this project has been bitten by before.
    """
    with pytest.raises(SystemExit) as raised:
        main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv", "--no-such-option"])

    assert raised.value.code == EXIT_USAGE
    assert "unrecognized arguments: --no-such-option" in capsys.readouterr().err


def test_a_stopped_run_exits_3_not_1(tmp_path: Path) -> None:
    """★ **The load-bearing test for the ``except`` ordering, and the reason this file
    exists.**

    Starts a real child running a real extraction, waits until it has committed a part --
    which is the run being genuinely under way -- then stops it the way a user would.
    Asserts ``3`` where the sibling suite in ``test_interrupt_report.py`` asserts "not
    zero".

    **Why this cannot be an in-process test.** ``main`` installs the signal handler and
    uninstalls it on the way out, so nothing inside one process can deliver a real signal
    to a run that has not started one. Raising the exception by hand would test the
    test's own construction of the situation: it would prove the ``except`` chain works
    *given* an interrupt, which is not the claim. The claim is that a Ctrl-C produces
    ``3``, and only a child process can be Ctrl-C'd.

    **Why ``3`` rather than "non-zero", and why this is the one that can catch a
    regression.** ``RunInterruptedError`` descends from ``GigaXMLError``, and ``except``
    clauses match in order -- so moving the ``RunInterruptedError`` clause *below* the
    ``GigaXMLError`` one compiles, passes every existing test, and silently returns ``1``
    for every interrupt forever after. A "not zero" assertion is green in exactly the
    state that reintroduces the bug. This is the assertion that goes red.
    """
    document = _document(tmp_path)
    config = _config(tmp_path)
    parts = tmp_path / "parts"

    proc = _start(document, config, parts)
    try:
        _wait_for_manifest(proc, parts, timeout=MANIFEST_TIMEOUT_S)
        code, err = _stop(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an assertion failure
            proc.kill()
            proc.communicate()

    assert code == EXIT_INTERRUPTED, (
        f"a stopped run exited {code}, not {EXIT_INTERRUPTED}. If this is 1, the "
        "except RunInterruptedError clause has moved below except GigaXMLError -- "
        f"unreachable, because it matches in order. stderr: {err.strip()[-300:]}"
    )


def test_an_unexpected_failure_exits_4_without_a_traceback(
    workspace: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """★ **An unexpected exception is ``4``, one line, and no traceback by default.**

    **The handler is replaced rather than a bug being waited for**, because no input
    makes gigaxml fail unexpectedly -- that is the point of the code. Swapping in a
    handler that raises is the same situation a real defect produces, and it is the only
    way to reach the branch honestly.

    ``GIGAXML_DEBUG`` is cleared explicitly: the variable is read from the *process*
    environment, so a developer's shell carrying it would turn the default path red and
    the failure would say nothing about the code.
    """
    monkeypatch.delenv("GIGAXML_DEBUG", raising=False)
    monkeypatch.setattr(cli_module, "_handle_extract", _raise_unexpected, raising=True)

    code = main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv"])
    err = capsys.readouterr().err

    assert code == EXIT_INTERNAL
    # One line naming the failure, and -- the half that matters for who reads this --
    # not a wall of Python frames pasted into a bug report nobody can read.
    assert "RuntimeError" in err
    assert "Traceback (most recent call last)" not in err
    # And it says whose fault it is, because the person reading it needs to know whether
    # to go looking at their config.
    assert "bug in gigaxml" in err
    # The traceback is *hidden*, not absent: a tool whose failures cannot be diagnosed
    # is worse than one that prints too much, so the message must point at the switch.
    assert "GIGAXML_DEBUG=1" in err


def test_the_traceback_appears_when_the_debug_variable_is_set(
    workspace: Path,  # noqa: ARG001
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The other half of the pair above, and it is a separate test on purpose.

    "Hidden by default" and "available on request" are two claims about two settings. One
    test asserting both would pass if the switch were honoured but the default were also
    printing, because the assertions would be looking at the same run.
    """
    monkeypatch.setenv("GIGAXML_DEBUG", "1")
    monkeypatch.setattr(cli_module, "_handle_extract", _raise_unexpected, raising=True)

    code = main(["extract", "a.xml", "-c", "cfg.yaml", "-o", "out.csv"])
    err = capsys.readouterr().err

    assert code == EXIT_INTERNAL, "the debug switch must not change the code, only the output"
    assert "Traceback (most recent call last)" in err
    # Both parts: the frames, and the same one-line summary, so a developer reading the
    # output gets the diagnosis rather than only the stack.
    assert "raise_unexpected" in err or "_raise_unexpected" in err
    assert "bug in gigaxml" in err


def _raise_unexpected(args: object) -> int:  # noqa: ARG001 -- the handler's own signature
    """The handler :func:`main` is handed instead of the real one.

    A ``RuntimeError`` rather than a ``GigaXMLError`` on purpose: every class gigaxml
    raises deliberately is caught by a clause named for it, and the whole point of the
    ``except Exception`` branch is what reaches it.
    """
    raise RuntimeError("a defect nobody planned for")


# --- criterion C: the ordering is the contract, so assert the contract --------


def test_the_five_codes_are_the_five_the_documentation_promises() -> None:
    """``ERRORS.md``'s table and this module, checked against each other.

    Not a tautology -- the table is prose in a file nothing else reads, and this is the
    one place a number in it is compared with a number in the code. It fails on the day
    someone adds ``EXIT_SOMETHING_ELSE`` and updates one of the two.
    """
    documented = Path("ERRORS.md").read_text(encoding="utf-8")

    for code in (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPTED, EXIT_INTERNAL):
        assert f"| `{code}` |" in documented, f"exit code {code} is not in ERRORS.md's table"

    assert (EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_INTERRUPTED, EXIT_INTERNAL) == (
        0,
        1,
        2,
        3,
        4,
    ), "the codes moved; every script written against them has to be reconsidered"


def test_run_interrupted_is_caught_before_the_class_it_descends_from() -> None:
    """★ **The ``except`` ordering inside ``main``, asserted on the source.**

    The behavioural test above (``test_a_stopped_run_exits_3_not_1``) is what proves the
    feature works. This one says *where the guarantee lives*, in a form that fails with a
    sentence naming the cause instead of leaving it to a two-minute subprocess test.

    **Scoped to ``main`` on purpose.** cli.py has a second ``except RunInterruptedError``
    -- in :func:`_extract_checkpointed`, the clause that writes the interrupted report --
    and a whole-file search finds that one instead. Measured, not assumed: the first
    version of this test did exactly that, and it stayed green while
    ``test_a_stopped_run_exits_3_not_1`` was red on the same mutation. A guard that does
    not move when the thing it guards breaks is not a guard.

    Read off the module source rather than hard-coded, so it cannot drift from the file
    it describes.
    """
    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    body = source[source.index("def main(argv") :]

    interrupted = body.index("except RunInterruptedError")
    base = body.index("except GigaXMLError")

    assert interrupted < base, (
        "in main(), except RunInterruptedError now comes after except GigaXMLError. "
        "RunInterruptedError descends from GigaXMLError and except matches in order, so "
        "that clause is unreachable and every interrupted run exits 1 -- which is the bug "
        "this milestone was opened to close"
    )


def test_the_interrupted_class_still_descends_from_the_one_everything_catches() -> None:
    """The premise the ordering relies on, asserted so it cannot be edited away.

    If ``RunInterruptedError`` ever stopped descending from ``GigaXMLError``, the
    ordering above would no longer matter -- and the ``except`` chain would stop catching
    interrupts at all, which is a different failure with the same visible symptom. This
    test says the class hierarchy is intact, so a future refactor that breaks the order
    is breaking something.
    """
    from gigaxml.errors import GigaXMLError, InternalError, RunInterruptedError

    assert issubclass(RunInterruptedError, GigaXMLError)
    # And the two classes that are deliberately not ValueErrors, because no value was
    # handed over that could have been rejected. `except ValueError` over promises
    # something false about them.
    assert not issubclass(RunInterruptedError, ValueError)
    assert not issubclass(InternalError, ValueError)


# --- the module compiles under the platforms CI runs --------------------------


def test_the_child_process_is_reachable_for_a_test_that_needs_a_real_signal() -> None:
    """A guard on the guard.

    The interrupt test above starts the CLI as ``sys.executable -m gigaxml.cli`` (or the
    Windows wrapper). If an install ever made that entry point unavailable -- a packaging
    change, an extra moved -- the interrupt test would fail with a message about a
    missing module, which reads as a broken product rather than a broken test harness.
    """
    result = subprocess.run(
        [sys.executable, "-c", "import gigaxml.cli"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, f"the child cannot import gigaxml: {result.stderr[-300:]}"
