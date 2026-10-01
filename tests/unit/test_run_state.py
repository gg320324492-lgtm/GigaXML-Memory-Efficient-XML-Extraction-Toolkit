"""The run state machine, before anything is wired to it.

★ **Step 1 of M9, and the reason these tests come first.** The state model is the part of
this milestone that can be wrong in ways no window will show you: a table that permits a
contradiction, a state nothing can reach, an "outcome" that two states both claim. All of
that is decided here, in a module that imports no widget library, so it can be tested
without a display and changed without a screenshot. Wiring a panel to a wrong state machine
is a much more expensive mistake than writing the wrong one.

**The three claims that matter, and what each one is aimed at:**

* :func:`test_no_state_is_both_active_and_terminal` -- the brief's "no contradictory
  combination", on the enum itself. A run is undecided or it is decided; a state that is
  both would be a state a caller can read either way from.
* :func:`test_a_user_stop_and_a_signal_are_different_states` -- criterion E at the level
  below the GUI. The two are the same shape on disk and different events, and the milestone
  exists because one flag was standing in for both.
* :func:`test_the_interrupted_exit_code_is_the_clis_own` -- the copy in ``run_state``
  rather than an import, pinned so it cannot drift.
"""

from __future__ import annotations

import enum
import subprocess
import sys

import pytest

from gigaxml.gui.run_state import (
    ACTIVE,
    INTERRUPTED_EXIT_CODE,
    NO_RUN,
    TERMINAL,
    ContradictoryStateError,
    IllegalTransitionError,
    RunState,
    RunStateMachine,
    contradiction,
    outcome_state,
)

#: The states the brief names, spelled out here rather than derived, so that a state renamed
#: or dropped shows up as a failure instead of quietly changing what the milestone means.
#: ``CANCELLED`` is not on this list and :func:`test_cancelled_is_the_eighth_state_on_purpose`
#: says why it exists anyway.
BRIEF_SEMANTICS = {
    "IDLE": "no run has been started",
    "STARTING": "the child is being launched",
    "RUNNING": "the child is up and nothing asked it to stop",
    "CANCELLING": "the user asked it to stop and the child has not gone",
    "FINISHED": "it ran and succeeded",
    "FAILED": "it ran and did not succeed",
    "INTERRUPTED": "something other than the user ended it",
}


# --- the enum itself --------------------------------------------------------


@pytest.mark.parametrize(("name", "meaning"), sorted(BRIEF_SEMANTICS.items()))
def test_every_semantic_the_brief_names_has_a_state_of_its_own(name: str, meaning: str) -> None:
    """Seven semantics, seven states. Named one at a time so a rename cannot pass.

    The ``meaning`` is only used in the failure message, and that is the point of passing
    it: a failure then says which of the brief's requirements is unmet rather than only
    that a name is missing.
    """
    assert hasattr(RunState, name), f"RunState has no {name} -- {meaning}"


def test_cancelled_is_the_eighth_state_on_purpose() -> None:
    """★ **The one place this goes past the brief's list, and the reason is criterion E.**

    The brief names seven states, with ``CANCELLING`` described as "stop requested, waiting
    for the child to exit". Taken literally that makes ``CANCELLING`` transient, and then a
    user-stopped run has nowhere to rest that is not shared with a run a signal ended --
    because the only terminal states left are FINISHED (a lie: the output is not complete)
    and FAILED (wrong: nothing broke) and INTERRUPTED (the case the brief is complaining
    about, where one flag covers two events). So the two paths would end in the *same*
    state, and criterion E -- "one test asserting these two paths produce different
    ``RunState``" -- would fail on the resting state rather than only mid-flight.

    Two states for one user intent is not the "two sets of state coexisting" the milestone
    warns against: ``CANCELLING`` answers *when it was asked* and ``CANCELLED`` answers
    *that it has now stopped*, and both are needed because they are different instants.
    Every other decision this milestone made took the brief's seven at face value.
    """
    assert RunState.CANCELLED.value == "cancelled"
    assert RunState.CANCELLED in TERMINAL
    assert RunState.CANCELLING in ACTIVE


def test_every_state_value_is_lowercase_and_unique() -> None:
    """The value is what ends up in a report, a log line and a test's expectation.

    A duplicate would make two states indistinguishable to anything that reads ``.value``,
    which is precisely the collapse this milestone is about; a capital would make the
    strings inconsistent with every other lowercase state name in the package.
    """
    values = [state.value for state in RunState]
    assert len(set(values)) == len(values), "two states share a value"
    for state in RunState:
        assert state.value == state.value.lower()
        assert state.value.replace("_", "").isalnum()


def test_no_state_is_both_active_and_terminal() -> None:
    """★ **"No contradictory combination", on the enum before anything is wired up.**

    Three categories, and the point is that they are disjoint **and complete** -- a state
    in two of them is a state a caller can read either way from, and a state in none of
    them is a state with no defined meaning at all. The third is ``NO_RUN``, and its
    existence is the finding: the first version of this module had two sets and left IDLE
    in neither, on the assumption that "not running" was the negation of "running". It is
    not -- "is there a run" and "has this run been decided" are different questions asked
    at different moments, and folding IDLE into ``TERMINAL`` would tell a caller that had
    just been given a child that the run had already finished.
    """
    overlap = ACTIVE & TERMINAL
    assert not overlap, f"{sorted(s.value for s in overlap)} is both undecided and decided"

    unclassified = set(RunState) - ACTIVE - TERMINAL - NO_RUN
    assert not unclassified, (
        f"{sorted(s.value for s in unclassified)} is neither active nor terminal nor the "
        "absence of a run, so a caller has no way to know what to do with it"
    )
    assert set(RunState) == ACTIVE | TERMINAL | NO_RUN
    assert not (ACTIVE | TERMINAL) & NO_RUN, "having no run is not the same as having a decided one"


# --- reachability and legality ----------------------------------------------


def _reachable() -> set[RunState]:
    """Every state a run can be in, walked out of the transition table itself.

    The walk rather than a hand-written list, so a state added to the table with nowhere to
    reach it from is found by this rather than by a reader noticing.
    """
    from gigaxml.gui.run_state import _TRANSITIONS

    seen = {RunState.IDLE}
    frontier = [RunState.IDLE]
    while frontier:
        for target in _TRANSITIONS[frontier.pop()]:
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


def test_every_state_is_reachable_from_idle() -> None:
    """A state nothing reaches is a state nobody will ever read, and its docstring lies.

    The brief's "from IDLE, any reachable path" is the positive half; this is the negative
    one, and it is the half that catches a rename: rename ``CANCELLED`` and forget to
    update the table, and the state becomes unreachable while every behavioural test still
    passes.
    """
    unreachable = set(RunState) - _reachable()
    assert not unreachable, f"no path from IDLE reaches {sorted(s.value for s in unreachable)}"


def test_only_starting_and_finished_can_begin_a_run() -> None:
    """Every route back into a run goes through STARTING, so there is one way in.

    Without this the table could grow an edge such as ``RUNNING -> STARTING``, which would
    let a second run begin while the first was still up -- two children, two readers, and
    whichever finished last would own the panel.
    """
    from gigaxml.gui.run_state import _TRANSITIONS

    into_starting = {src for src, targets in _TRANSITIONS.items() if RunState.STARTING in targets}
    assert into_starting == {RunState.IDLE} | TERMINAL, (
        f"{sorted(s.value for s in into_starting)} may start a run; only IDLE and the "
        "terminal states may, because a run that has not ended cannot be replaced"
    )


@pytest.mark.parametrize("target", list(RunState))
def test_every_state_but_idle_is_refused_as_a_first_move(target: RunState) -> None:
    """A new machine is IDLE, and IDLE only becomes STARTING.

    Checking every target rather than one hand-picked illegal one is what makes this a test
    of the table: adding a state to the enum and forgetting the table would be caught here
    rather than at a call site weeks later.
    """
    machine = RunStateMachine()
    if target is RunState.STARTING:
        assert machine.move_to(target) is target
    else:
        with pytest.raises(IllegalTransitionError):
            machine.move_to(target)


def test_a_finished_run_cannot_go_back_to_running() -> None:
    """★ **The brief's own example of an illegal move, asserted by name.**

    A test that says "``FINISHED -> RUNNING`` raises" is a test somebody can read and
    check against the requirement, rather than a test that discovers the same thing by
    enumerating.
    """
    machine = RunStateMachine()
    machine.move_to(RunState.STARTING)
    machine.move_to(RunState.RUNNING)
    machine.move_to(RunState.FINISHED)

    with pytest.raises(IllegalTransitionError) as raised:
        machine.move_to(RunState.RUNNING)

    assert raised.value.current is RunState.FINISHED
    assert raised.value.target is RunState.RUNNING
    assert machine.state is RunState.FINISHED, "a refused move must leave the state alone"


def test_a_refused_move_names_where_it_came_from_and_where_it_wanted_to_go() -> None:
    """The message is the diagnosis, and a caller reading only the message should be able
    to fix the call without opening this file."""
    machine = RunStateMachine()
    machine.move_to(RunState.STARTING)
    machine.move_to(RunState.FAILED)

    with pytest.raises(IllegalTransitionError) as raised:
        machine.move_to(RunState.CANCELLED)

    message = str(raised.value)
    assert "failed" in message
    assert "cancelled" in message
    assert "starting" in message, "the message should say what the run may become instead"


# --- the two kinds of ending, which is the point of the milestone ----------


def test_a_user_stop_and_a_signal_are_different_states() -> None:
    """★ **Criterion E, at the layer below the GUI: the two paths never share a state.**

    Two runs, identical on disk -- a partial output, no report, a non-zero exit -- and the
    only thing that separates them is who ended them. A user pressed Cancel; a signal
    arrived. One of those is a decision somebody made and the other is not, so they are
    given different states all the way down, and this asserts the *resting* state rather
    than a moment on the way there.
    """
    stopped_by_user = RunStateMachine()
    stopped_by_user.move_to(RunState.STARTING)
    stopped_by_user.move_to(RunState.RUNNING)
    stopped_by_user.move_to(RunState.CANCELLING)
    stopped_by_user.move_to(RunState.CANCELLED)

    ended_by_signal = RunStateMachine()
    ended_by_signal.move_to(RunState.STARTING)
    ended_by_signal.move_to(RunState.RUNNING)
    ended_by_signal.move_to(RunState.INTERRUPTED)

    assert stopped_by_user.state is not ended_by_signal.state
    assert stopped_by_user.state is RunState.CANCELLED
    assert ended_by_signal.state is RunState.INTERRUPTED


def test_the_waiting_state_and_the_waited_state_are_different() -> None:
    """The user's decision and the child's death are different instants, so they are
    different states, and the first is not lost when the second arrives.

    Collapsing them would mean the window could not say "stopping…" while it is still
    stopping -- it would have to say either "running" (a lie) or "cancelled" (early).
    """
    machine = RunStateMachine()
    machine.move_to(RunState.STARTING)
    machine.move_to(RunState.RUNNING)
    machine.move_to(RunState.CANCELLING)

    assert machine.is_active, "a run being stopped is still undecided"
    assert not machine.is_terminal

    machine.move_to(RunState.CANCELLED)
    assert machine.is_terminal
    assert not machine.is_active


@pytest.mark.parametrize(
    ("exit_code", "killed", "expected"),
    [
        (0, False, RunState.FINISHED),
        (INTERRUPTED_EXIT_CODE, False, RunState.INTERRUPTED),
        (1, False, RunState.FAILED),
        (2, False, RunState.FAILED),
        (4, False, RunState.FAILED),
        # **And the three that used to be misread.** A child we asked to stop exits with
        # whatever the signal gave it. Reading that as a failure is how a user who pressed
        # Cancel gets told their run broke, so ``killed`` is consulted first and wins.
        (0, True, RunState.CANCELLED),
        (1, True, RunState.CANCELLED),
        (-9, True, RunState.CANCELLED),
        (INTERRUPTED_EXIT_CODE, True, RunState.CANCELLED),
    ],
)
def test_a_finished_child_lands_in_the_state_its_exit_code_names(
    exit_code: int, killed: bool, expected: RunState
) -> None:
    """Every combination the CLI can actually produce, mapped to what it means.

    Eight rows rather than three because the interesting ones are the ones that used to be
    wrong. ``(0, True)`` in particular: a child that had already finished when the kill
    landed reports success *and* that it was killed, and ``RunResult.ok`` has always said
    ``False`` for it. That is correct for ``ok`` -- the caller asked for something and did
    not get a clean answer -- but it must not make the run a failure either.
    """
    assert outcome_state(exit_code=exit_code, killed=killed) is expected


def test_a_stopped_run_cannot_turn_into_a_finished_one() -> None:
    """★ **A transition that is missing on purpose, named so nobody adds it by accident.**

    A stop landing in the same instant the run completes is a real race, and
    ``CANCELLING -> FINISHED`` is the row that would express it. It is deliberately
    absent: today's behaviour is that ``killed`` wins, so a child that exited 0 *and* was
    killed is reported as cancelled, which is what the panel has always shown and what
    ``test_gui_process.py`` pins when it asserts ``RunResult(exit_code=0,
    killed=True).ok`` is false. Adding the row would be a behaviour change wearing a table
    entry's clothes.

    So the absence is asserted from both sides -- the machine refuses the move, and the
    mapping still answers ``CANCELLED`` -- and whoever rules that the race should be
    honoured has to change a test on purpose rather than add a dict key and watch the
    suite stay green.
    """
    machine = RunStateMachine()
    machine.move_to(RunState.STARTING)
    machine.move_to(RunState.RUNNING)
    machine.move_to(RunState.CANCELLING)

    with pytest.raises(IllegalTransitionError):
        machine.move_to(RunState.FINISHED)

    assert outcome_state(exit_code=0, killed=True) is RunState.CANCELLED


def test_no_two_states_describe_the_same_ending() -> None:
    """★ **"Cannot be both cancelled and finished", as a property of the mapping.**

    The brief's example of a contradictory combination is a run marked finished *and*
    cancelled. With one value per run that is unrepresentable, and this is the test that
    would notice if it became representable again: for each way a run can end, exactly one
    state may claim it. Two states claiming one ending is the same defect wearing a
    different hat -- a caller could not tell which to believe.
    """
    endings: dict[str, set[RunState]] = {}
    for exit_code in (0, 1, 2, INTERRUPTED_EXIT_CODE, 4):
        for killed in (False, True):
            state = outcome_state(exit_code=exit_code, killed=killed)
            endings.setdefault(f"exit {exit_code}{' killed' if killed else ''}", set()).add(state)

    for ending, states in endings.items():
        assert len(states) == 1, f"{ending} is described by {sorted(s.value for s in states)}"


# --- the copy that must not drift -------------------------------------------


def test_the_interrupted_exit_code_is_the_clis_own() -> None:
    """★ **The one number in this module that is written out rather than imported.**

    :data:`~gigaxml.gui.run_state.INTERRUPTED_EXIT_CODE` is a copy of the CLI's
    ``EXIT_INTERRUPTED``, and it has to be a copy. Measured: importing that constant from
    :mod:`gigaxml.cli` loads :mod:`gigaxml.inspect` and :mod:`gigaxml.sample`, and **both
    are modules ``test_gui_no_parsing.py`` bans by name** -- so the guard cannot see the
    line that breaks its own rule. Nothing banned is spelled, and no banned name appears.

    Worth being precise about what this is *not*, because the first version of this
    docstring got it wrong: ``gigaxml.gui`` **already** imports ``gigaxml.run`` -- for
    ``QUARANTINABLE`` and the report file name -- and that pulls in ``lxml`` and
    :mod:`gigaxml.parser` with it. That is an accepted boundary decision, documented as
    such by the existing guard's own test. What is forbidden is reaching a module the guard
    bans, and ``gigaxml.run`` was allowed on purpose while the two commands
    ``gigaxml.cli`` dispatches on top of it were not.

    A test may import the CLI; a test is not the interface. This is where the copy is
    pinned, so that changing the CLI's code without changing this one is a failure here
    rather than a window that quietly stops recognising a signal.
    """
    from gigaxml.cli import EXIT_INTERRUPTED

    assert INTERRUPTED_EXIT_CODE == EXIT_INTERRUPTED


def test_importing_the_state_machine_brings_no_parser_into_the_process() -> None:
    """★ **The claim the module docstring makes, measured rather than asserted.**

    A separate interpreter, because the one running the tests has lxml loaded already by
    the time any GUI test has run, and ``sys.modules`` is a property of the process rather
    than of the import. What is being protected is the reason the exit code is copied
    instead of imported, so the test has to stand on its own rather than borrow the claim
    from a neighbouring test's imports.
    """
    script = (
        "import sys\n"
        "import gigaxml.gui.run_state\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name.split('.')[0] in {'lxml', 'gigaxml'}\n"
        "    and name.split('.')[:2] in (['lxml'], ['gigaxml', 'parser'],\n"
        "                                 ['gigaxml', 'inspect'], ['gigaxml', 'sample'])\n"
        ")\n"
        "print(','.join(leaked))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert completed.returncode == 0, completed.stderr

    leaked = completed.stdout.strip()
    assert leaked == "", (
        f"importing the state machine loaded {leaked} -- the state model has to be "
        "reachable from cli_process.py, which is Qt-free, and from a display-free test"
    )


# --- the contradiction check, and whether it would notice -------------------


@pytest.mark.parametrize(
    ("state", "launched", "why"),
    [
        (RunState.RUNNING, False, "a run under way with no child to be running"),
        (RunState.CANCELLING, False, "a run being stopped that never had a child"),
        (RunState.CANCELLED, False, "a stopped run whose child was never launched"),
        (RunState.FINISHED, False, "a finished run with nothing that could have finished"),
        (RunState.INTERRUPTED, False, "a signal that ended a run that never started"),
        (RunState.IDLE, True, "a child that was launched and then unaccounted for"),
    ],
)
def test_a_contradiction_is_named_rather_than_survived(
    state: RunState, launched: bool, why: str
) -> None:
    """★ **The two ways the old booleans could disagree, and both are now refusable.**

    ``_process is None`` alongside ``_finished = True`` was not an error under the old
    model -- both were ordinary values -- and it is the shape a run takes when a
    ``shutdown()`` releases the child a moment before or after the reader delivers. Naming
    it here means the caller can be told, rather than the panel carrying two answers and
    displaying a state that never happened.

    Six rows rather than two because the states are listed one at a time: a state added to
    :data:`_REQUIRES_A_CHILD` later has to be added here, and a row that is missing shows
    up as an untested state rather than as a passing suite.
    """
    sentence = contradiction(state=state, launched=launched)

    assert sentence is not None, f"no contradiction reported for {why}"
    assert state.value in sentence
    assert ContradictoryStateError(sentence)  # the type exists and takes the sentence


@pytest.mark.parametrize(
    ("state", "launched"),
    [
        (RunState.IDLE, False),
        # **Both launch states, and the second is the one that would have been a false
        # alarm.** A run that cannot start -- ``Popen`` raising -- is FAILED with no child
        # behind it, and a rule written as "any non-idle state needs a child" calls that a
        # contradiction. It is not: refusing to launch is how a run fails when there is no
        # exit code to read. STARTING is the same argument one step earlier.
        (RunState.STARTING, False),
        (RunState.STARTING, True),
        (RunState.RUNNING, True),
        (RunState.CANCELLING, True),
        (RunState.CANCELLED, True),
        (RunState.FINISHED, True),
        (RunState.FAILED, True),
        (RunState.FAILED, False),
        (RunState.INTERRUPTED, True),
    ],
)
def test_every_honest_pairing_is_not_a_contradiction(state: RunState, launched: bool) -> None:
    """★ **The half that keeps the check from being switched off.**

    A guard that fires on real states is worse than no guard, because the first time it
    does the next thing that happens is somebody deleting it. So every pairing that
    actually occurs has to be silent, and these are all of them -- ten rows, enumerated
    rather than sampled, so a state added later has somewhere obvious to be added too.
    """
    assert contradiction(state=state, launched=launched) is None


def test_the_checker_would_notice_if_it_stopped_checking() -> None:
    """★ **Mutation, as a permanent test: the check proves it is sensitive.**

    A checker that has never reported anything is indistinguishable from a checker that
    stopped working, so this feeds it the four contradictions above and requires a report
    for each. The project's existing no-parsing guard does the same thing for the same
    reason, and this is the house style rather than a novelty.
    """
    import gigaxml.gui.run_state as module

    real = module.contradiction
    try:
        module.contradiction = lambda **_: None  # the mutation: a checker that never fires
        for state in RunState:
            for launched in (True, False):
                assert module.contradiction(state=state, launched=launched) is None, (
                    "the mutant was supposed to say nothing for every pairing; if this "
                    "fails the test is not testing the mutation"
                )
        # And the real one still fires, which is the property the mutant removed.
        assert real(state=RunState.RUNNING, launched=False) is not None
        assert real(state=RunState.IDLE, launched=True) is not None
    finally:
        module.contradiction = real


def test_the_machine_starts_idle_and_reports_itself_readably() -> None:
    """Construction, and the repr a failure message will show.

    Not decoration: a state machine that printed ``<object at 0x...>`` would put an
    address in the middle of every assertion message about it, which is the one line a
    reader most needs to be legible.
    """
    machine = RunStateMachine()
    assert machine.state is RunState.IDLE
    assert machine.is_active is False
    assert machine.is_terminal is False
    assert repr(machine) == "RunStateMachine(idle)"


def test_may_become_answers_without_moving() -> None:
    """A caller that has to branch on legality needs to ask first, and asking must not
    change the state -- otherwise the question is the answer."""
    machine = RunStateMachine()
    assert machine.may_become(RunState.STARTING)
    assert not machine.may_become(RunState.FINISHED)
    assert machine.state is RunState.IDLE


def test_run_state_is_an_enum_and_not_a_string_alias() -> None:
    """``RunState.RUNNING != "running"`` is the property that stops a comparison from
    quietly succeeding against the wrong thing.

    An enum whose members compared equal to their values would pass every test written
    against the value and fail against the member -- so this pins the difference while it
    is cheap to change, rather than after something has relied on it.
    """
    assert issubclass(RunState, enum.Enum)
    assert RunState.RUNNING != "running"
    assert RunState("running") is RunState.RUNNING
