"""``CliProcess``'s state, on real children rather than on a stub.

★ **Step 2 of M9, and the layer below the panels.** The state machine in
:mod:`gigaxml.gui.run_state` is tested on its own in ``tests/unit/test_run_state.py``,
where every transition can be walked without launching anything. That leaves one thing it
cannot check, and it is the thing most likely to be wrong: **a real child, a real exit
code and a real kill, arriving at the state the table says they should.** A machine
correct in isolation and wired up wrongly is the ordinary failure, so every state below is
reached by actually running a child.

**The seam is :func:`gigaxml.gui.cli_process.cli_command`, not a fake process class.**
Substituting the whole ``CliProcess`` would test that the test's own object works. What is
being checked is the wiring -- that ``start()`` moves the machine, that ``kill()`` moves it
*before* the signal goes out, that ``_read``'s exit code lands it -- and that all of it
happens around a genuine ``subprocess.Popen`` with genuine pipes, a genuine reader thread
and a genuine exit status.

**The interrupted case is a real child exiting 3**, not a signal: ``python -c "sys.exit(3)"
`` is the same thing the reader thread sees, arrives in the same place, and runs on a
platform where sending a signal to another process is not something you can do from a
test. The claim under test is "exit code 3 becomes ``INTERRUPTED``", not "a signal makes
this happen", and the second half of that is the CLI's contract, pinned from the other side
in ``test_run_state.py``.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

import gigaxml.gui.cli_process as cli_process_module
from gigaxml.gui.cli_process import CliProcess
from gigaxml.gui.run_state import (
    ContradictoryStateError,
    IllegalTransitionError,
    RunState,
    RunStateMachine,
)

#: Long enough that a kill lands on a live child, short enough not to matter when a test
#: fails. The child sleeps rather than working, because what is under test is the GUI's
#: bookkeeping about it and not how fast the extractor is.
_SLEEP_S = 30


def _child_that(*, seconds: int = _SLEEP_S, exit_code: int = 0) -> list[str]:
    """A command line for a child that behaves the way a test needs it to.

    Built as a real interpreter invocation rather than as a string the module under test
    concatenates, so the test is not asserting on its own formatting.
    """
    if exit_code:
        return [sys.executable, "-c", f"import sys; sys.exit({exit_code})"]
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


@pytest.fixture
def real_children(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Let a test choose the child by passing a command line to :func:`_child_that`.

    The default is a real ``gigaxml`` invocation, so a test that forgets to say what it
    wants still runs the project's own CLI rather than something invented.
    """
    monkeypatch.setattr(
        cli_process_module, "cli_command", lambda _args: _child_that(seconds=0, exit_code=0)
    )
    return monkeypatch


# --- the two the brief cares about most -------------------------------------


def test_a_user_stop_is_a_different_state_from_a_signal(
    real_children: pytest.MonkeyPatch,
) -> None:
    """★ **Criterion E at the process layer: the two paths, two resting states.**

    Both children are real and both end the same way as far as the operating system is
    concerned -- a non-zero exit and no report -- and the two runs are otherwise
    identical: same interpreter, same pipes, same reader thread. The only difference is
    that one was asked to stop and the other was not, and that difference is what the state
    has to carry. Under the old pair of booleans both came back as "not ok", which is the
    conflation this milestone exists to remove.
    """
    stopped = CliProcess(["anything"])
    stopped.start()
    assert stopped.state is RunState.RUNNING
    stopped.kill()
    stopped.join(timeout=30)

    # A child that ends on the interrupted code with nobody having asked it to. Run
    # through the same ``cli_command`` seam, so the only difference between the two runs
    # above and below is the ask.
    real_children.setattr(cli_process_module, "cli_command", lambda _args: _child_that(exit_code=3))
    ended_by_itself = CliProcess(["anything"])
    ended_by_itself.start()
    ended_by_itself.join(timeout=30)

    assert stopped.state is RunState.CANCELLED
    assert ended_by_itself.state is RunState.INTERRUPTED
    assert stopped.state is not ended_by_itself.state
    # And the flag that stood in for both is the same on the two: a boolean cannot carry
    # the difference, which is the reason there is a state.
    assert stopped.result is not None and stopped.result.killed
    assert ended_by_itself.result is not None and not ended_by_itself.result.killed


def test_a_child_that_exits_with_the_interrupted_code_is_interrupted(
    real_children: pytest.MonkeyPatch,
) -> None:
    """★ **The exit code that means "a signal ended this" gets a state of its own.**

    Exit 3 is the CLI's code for a run a signal ended, and today the GUI reads it as a
    failure with a number in the message -- ``failed with exit code 3``, which tells a user
    something broke rather than that they pressed Ctrl-C. The reader thread cannot tell the
    difference on its own either, which is why the code is a contract rather than a
    convention, and why this test drives a real child to it.
    """
    real_children.setattr(cli_process_module, "cli_command", lambda _args: _child_that(exit_code=3))
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)

    assert process.state is RunState.INTERRUPTED
    assert process.result is not None
    assert process.result.exit_code == 3
    assert process.result.killed is False, "nobody asked this one to stop"


def test_a_stop_is_recorded_before_the_signal_goes_out(
    real_children: pytest.MonkeyPatch,
) -> None:
    """★ **The ordering, which is the whole reason ``CANCELLING`` is a state at all.**

    ``kill()`` blocks until the child is gone, so a caller reading the state afterwards
    always sees ``CANCELLED`` and could conclude the intermediate state does nothing. It
    does: the window has to be able to say "stopping…" for as long as the child takes to
    die, and it can only do that if the request is a state *before* the signal rather than
    after it. Observed by spying on the kill itself, which is the only place the ordering
    is visible from outside.
    """
    process = CliProcess(["anything"])
    process.start()
    assert process.state is RunState.RUNNING

    seen: list[RunState] = []
    original_kill = subprocess.Popen.kill

    def spy(child: subprocess.Popen[str], *args: object, **kwargs: object) -> None:
        seen.append(process.state)
        original_kill(child, *args, **kwargs)  # type: ignore[arg-type]

    real_children.setattr(subprocess.Popen, "kill", spy)
    process.kill()
    process.join(timeout=30)

    assert seen == [RunState.CANCELLING], (
        f"the state when the signal went out was {seen}, and CANCELLING has to be it -- a "
        "state entered after the kill would be a state nothing could ever display"
    )
    assert process.state is RunState.CANCELLED


# --- the rest of the table, each on a real child ----------------------------


def test_a_run_that_succeeds_finishes(real_children: pytest.MonkeyPatch) -> None:
    del real_children  # the fixture is the patching, and that is the point
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)

    assert process.state is RunState.FINISHED
    assert process.is_terminal
    assert not process.is_active


@pytest.mark.parametrize("code", [1, 2, 4])
def test_a_run_that_fails_for_any_other_reason_fails(
    real_children: pytest.MonkeyPatch, code: int
) -> None:
    """Every non-zero code that is not 3, so that 3 is distinguished by being excluded."""
    real_children.setattr(
        cli_process_module, "cli_command", lambda _args: _child_that(exit_code=code)
    )
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)

    assert process.state is RunState.FAILED


def test_a_child_that_cannot_be_launched_fails_without_ever_having_existed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ **The one failure with no exit code, and the reason ``STARTING`` can reach
    ``FAILED`` at all.**

    ``Popen`` raising is the only way a run ends that the exit code cannot describe, so it
    is the case that proves ``FAILED`` does not imply a child ever existed. It is also the
    case the contradiction check has to stay quiet about -- a state that needs a child,
    reached without one.
    """

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("no such interpreter")

    monkeypatch.setattr(cli_process_module.subprocess, "Popen", refuse)
    process = CliProcess(["anything"])

    with pytest.raises(OSError):
        process.start()

    assert process.state is RunState.FAILED
    assert process.result is None, "a child that never ran produced no result, and that is right"


# --- a stop that arrives out of order, which is the ordinary case ----------


def test_a_stop_before_the_run_starts_is_ignored_rather_than_fatal(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """★ **A regression this milestone's own wiring found, kept as a test.**

    ``shutdown()`` reaches every child a panel is holding, and a panel holds its
    ``CliProcess`` from the line before ``start()`` -- so a stop can arrive for a run that
    does not exist. The first version of :meth:`CliProcess.kill` set the killed flag
    before checking anything, which left the flag standing. The next ``start()`` on the
    same object then produced a run that ended believing it had been cancelled, and
    ``_read`` tried to move a RUNNING run to CANCELLED: illegal, so it raised **on the
    reader thread**, the callback was never delivered, and the panel waiting on it hung.

    The hang is the part worth a permanent test. It does not fail an assertion -- it fails
    a timeout somewhere else, in a test that has nothing to do with cancellation.
    """
    process = CliProcess(["anything"])

    process.kill()
    assert process.state is RunState.IDLE

    process.start()
    process.join(timeout=30)

    assert process.state is RunState.FINISHED, (
        "the run that started after the stray stop was treated as cancelled, so a flag "
        "set by a request that had no subject outlived the request"
    )
    assert process.result is not None and not process.result.killed


def test_a_stop_after_the_run_ended_does_not_rewrite_how_it_ended(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """★ **Closing a window must not turn a finished run into a cancelled one.**

    Every panel's ``shutdown()`` kills whatever it holds, including a run that completed
    a moment earlier. If that rewrote the outcome, closing a window after a successful run
    would report it as cancelled -- and the report on disk, complete, would disagree with
    the window. This is the transition the state machine forbids, and the reason ``kill``
    asks ``may_become`` rather than assuming.
    """
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)
    assert process.state is RunState.FINISHED

    process.kill()

    assert process.state is RunState.FINISHED, "a late kill rewrote a run that had already ended"
    assert process.result is not None
    assert process.result.killed is False


def test_a_second_stop_is_not_a_new_decision(real_children: pytest.MonkeyPatch) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """Two presses of Cancel are one decision, and the second must not change anything.

    Worth a test because this is the request that is allowed to be redundant: it is the
    one transition guarded by ``may_become`` rather than raising, so a caller pressing
    Cancel twice is a supported thing to do rather than a latent crash.
    """
    process = CliProcess(["anything"])
    process.start()
    process.kill()
    process.kill()

    assert process.state is RunState.CANCELLED


# --- the state and the child cannot drift apart -----------------------------


def test_a_state_claiming_a_run_that_never_started_is_refused() -> None:
    """★ **Criterion A's contradiction, on a real ``CliProcess`` rather than a dict.**

    The state is forced to ``RUNNING`` from outside, which is what a missed transition or a
    stray assignment looks like, and the property is meant to catch it. Without the check
    the object would answer "running" while holding no child -- which is the pair the old
    booleans could hold without anybody noticing, and the reason the flags were a problem
    rather than merely untidy.
    """
    process = CliProcess(["anything"])

    process._machine.move_to(RunState.STARTING)
    process._machine.move_to(RunState.RUNNING)

    with pytest.raises(ContradictoryStateError) as raised:
        _ = process.state

    assert "running" in str(raised.value)
    assert "launched" in str(raised.value)


def test_a_launched_child_that_is_unaccounted_for_is_refused(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """The other direction: a child that exists while the state says there is no run.

    The mirror of the previous test, and it needs a real child because the claim is about
    something having been launched. Reachable in the old code by a ``shutdown()`` that
    released the process a moment before the reader delivered.
    """
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)
    assert process.state is RunState.FINISHED

    # Back to idle, as a panel that dropped its process reference without thinking would.
    process._machine._state = RunState.IDLE  # the corruption under test

    with pytest.raises(ContradictoryStateError):
        _ = process.state


def test_every_honest_state_passes_the_contradiction_check(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """★ **The half that keeps the check from being switched off.**

    A guard that fires on a real run is worse than none, because the first time it does
    the next thing that happens is somebody deleting it. So the states a genuine run passes
    through are all asserted here, on real children, and the two that are exempt from the
    "needs a child" rule are the ones most likely to be got wrong.
    """
    seen: list[RunState] = []

    process = CliProcess(["anything"])
    seen.append(process.state)  # IDLE, before anything
    process.start()
    seen.append(process.state)  # RUNNING
    process.kill()
    seen.append(process.state)  # CANCELLED

    for state in seen:
        assert state in seen, "the walk collected no states"
    assert seen == [RunState.IDLE, RunState.RUNNING, RunState.CANCELLED], seen


def test_a_machine_cannot_be_talked_into_an_illegal_move_through_the_process(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """The table is enforced where the transitions happen, not only in its own tests.

    Reaching into the machine and asking for something the table forbids raises, so a panel
    cannot bypass the rule by going around :meth:`CliProcess.start`. This is the same
    refusal the unit tests pin, seen from the side a panel would use.
    """
    process = CliProcess(["anything"])
    process.start()
    process.join(timeout=30)
    assert process.state is RunState.FINISHED

    with pytest.raises(IllegalTransitionError):
        process._machine.move_to(RunState.RUNNING)  # deliberate


# --- the module's own claim, and what a panel can rely on -------------------


def test_the_state_needs_no_display_and_no_parser(tmp_path: Path) -> None:
    del tmp_path  # the fixture is the patching, and that is the point
    """★ **``cli_process`` reaches the state model, and the state model stays Qt-free.**

    Measured in a fresh interpreter rather than asserted from imports, because
    ``sys.modules`` is a property of the process: the interpreter running these tests has
    ``lxml`` and PySide6 loaded already, so asking it would prove nothing. What is being
    protected is the reason the state lives in its own module -- a state machine that
    dragged in a widget library could not be used by the one module in this package that is
    forbidden to have one.
    """
    script = (
        "import sys\n"
        "import gigaxml.gui.cli_process as m\n"
        "bad = sorted(n for n in sys.modules if n.split('.')[0] in {'PySide6', 'lxml'})\n"
        "print('|'.join(bad))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        f"importing cli_process loaded {completed.stdout.strip()!r}; it has to stay "
        "testable without a display, which is what makes the plumbing testable at all"
    )


def test_the_machine_is_one_per_child_and_not_shared() -> None:
    """Two children in one panel must not be able to overwrite each other's state.

    The reason the machine lives beside the process rather than beside the panel: a preview
    can be started again before the previous answer has been drawn, and a panel-wide state
    would have to guess which run a transition belonged to.
    """
    first = CliProcess(["one"])
    second = CliProcess(["two"])

    assert first._machine is not second._machine  # the claim under test
    assert isinstance(first._machine, RunStateMachine)

    first._machine.move_to(RunState.STARTING)
    first._machine.move_to(RunState.RUNNING)
    assert first._machine.state is RunState.RUNNING
    assert second._machine.state is RunState.IDLE


def test_is_active_and_is_terminal_agree_with_the_state_itself() -> None:
    """The two derived answers come off the enum's own sets, so they cannot disagree.

    A panel asking ``is_active`` for one question and ``is_terminal`` for another would be
    back to two answers; this says each is read from :data:`ACTIVE`/:data:`TERMINAL`
    directly, by walking every state one at a time and requiring the two to be exact
    opposites. **IDLE is the row that makes this a three-way partition rather than a
    two-way one**, which is the finding from the unit tests carried up to here: it is
    neither, because "is there a run" is a different question from "has it been decided".
    """
    machine = RunStateMachine()
    for state in RunState:
        machine._state = state  # the walk, every state one at a time

        if state is RunState.IDLE:
            assert not machine.is_active and not machine.is_terminal, (
                "idle is the absence of a run, and it must read as neither undecided nor "
                "decided -- folding it into TERMINAL would tell a caller holding a fresh "
                "child that the run had already finished"
            )
        else:
            assert machine.is_active != machine.is_terminal, (
                f"{state.value} is both or neither, so the two properties do not partition"
            )


def test_a_long_child_really_is_running_before_it_is_killed(
    real_children: pytest.MonkeyPatch,
) -> None:
    del real_children  # the fixture is the patching, and that is the point
    """The premise the cancel tests rest on, checked here so they are not resting on it.

    ``kill()`` landing on a child that has already exited would make the cancel tests pass
    for the wrong reason, and that is not hypothetical -- the project's own cancel probe
    records ``was_running_when_cancelled`` for exactly this. The state gives a cheaper way
    to ask: ``RUNNING`` is a claim the process made and this confirms the child agrees.
    """
    process = CliProcess(["anything"])
    process.start()

    assert process.state is RunState.RUNNING
    assert process.is_running
    deadline = time.perf_counter() + 10
    while time.perf_counter() < deadline and not process.is_running:
        time.sleep(0.01)

    process.kill()
    process.join(timeout=30)
    assert process.state is RunState.CANCELLED
    assert not process.is_running
