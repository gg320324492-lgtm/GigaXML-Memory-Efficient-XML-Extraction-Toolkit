"""What a run is doing, as one value instead of several booleans.

**The problem this replaces.** Four panels each kept their own ``_finished`` flag next to
their own ``_process`` reference, and each decided on its own what those two booleans
meant. That is four answers to one question, and a question with four answers has
combinations nobody checked: a panel could hold ``_finished=True`` with ``_process`` set to
``None`` by a ``shutdown()`` that ran while the reader was mid-delivery, and every one of
the two flags would read true while the panel displayed a state that had never happened.
The state could not contradict itself, because it was never a state -- it was a pile.

**A single value cannot hold two answers at once, and that is the whole point.**
:meth:`RunStateMachine.move_to` refuses a transition that is not in
:data:`_TRANSITIONS`, so a run that has finished cannot be re-marked as running, and a run
the user stopped cannot be re-marked as either finished or failed. The refusal is raised
rather than logged-and-ignored: a caller that reaches an illegal transition has a bug, and
swallowing it would restore exactly the ambiguity this module exists to remove.

**``CANCELLING`` and ``CANCELLED`` are two states, and that is not a nicety.** They are the
two halves of one question -- *did the user ask this run to stop, or did something else
end it?* -- and answering it needs both the moment the stop was requested and the moment
the child was actually gone, because those are different instants and only the first one is
a decision anybody made. A user-stopped run and a run a signal ended are the same shape on
disk and completely different events; collapsing them into one flag is what let
``RunResult.killed`` stand in for a user's intent.

**Nothing here imports PySide6, on purpose.** :mod:`gigaxml.gui.cli_process` uses this
module, and *that* module is Qt-free so its plumbing can be tested without a display --
the same reason :mod:`gigaxml.gui.i18n` and :mod:`gigaxml.gui.settings` avoid it. A state
machine that dragged a widget library in could not be reached from either side.

**And it does not import :mod:`gigaxml.cli` to get the interrupted exit code, which is
worth stating because the obvious way to write it is wrong -- though not for the reason it
first looks.** Importing one constant out of ``gigaxml.cli`` loads :mod:`gigaxml.inspect`
and :mod:`gigaxml.sample` into the process, and **both of those are modules the existing
AST guard bans by name.** So the guard cannot see the line that breaks its own rule: no
banned module is spelled, and no banned name appears.

The subtlety, and the reason this is stated at length: ``gigaxml.gui`` **already** imports
``gigaxml.run`` -- for ``QUARANTINABLE`` and the report file name -- and that pulls in
``lxml`` and :mod:`gigaxml.parser` with it. That is an accepted boundary decision, stated
as such in ``test_gui_no_parsing.py``, and it is not what this module is avoiding. What it
avoids is *reaching a module the guard bans*: ``gigaxml.run`` was allowed on purpose, and
the two commands ``gigaxml.cli`` dispatches on top of it were not.

So the number is written out here and pinned against the CLI's own by a test, which may
import the CLI because a test is not the interface. Renaming it to an import is not a
cleanup -- it would route a forbidden module into the window through a door the guard
cannot see, and every existing test would stay green.
"""

from __future__ import annotations

import enum
from typing import Final

__all__ = [
    "ACTIVE",
    "IDLE",
    "INTERRUPTED_EXIT_CODE",
    "NO_RUN",
    "TERMINAL",
    "ContradictoryStateError",
    "IllegalTransitionError",
    "RunState",
    "RunStateMachine",
    "contradiction",
    "outcome_state",
    "state_of",
]


#: The exit code the CLI uses for a run a signal ended. Written out rather than imported;
#: see the module docstring for the measurement that rules out the import, and
#: ``tests/unit/test_run_state.py`` for the test that pins it to the CLI's own value so the
#: copy cannot drift away from it unnoticed.
INTERRUPTED_EXIT_CODE: Final = 3


class RunState(enum.Enum):
    """Where a run is, in one value.

    Every member answers the same question, so a reader never has to hold two of them at
    once and ask whether they agree:

    * :attr:`IDLE` -- no run has been started, or the panel has released the one it had.
    * :attr:`STARTING` -- the child is being launched. Brief, and the only state in which
      no child exists yet.
    * :attr:`RUNNING` -- the child is up and nothing has been asked of it to stop.
    * :attr:`CANCELLING` -- **the user asked it to stop and the child has not gone yet.**
      The only state in which the run is being ended by a person.
    * :attr:`CANCELLED` -- it was asked to stop, and it has stopped.
    * :attr:`FINISHED` -- the child ran to its end and said it succeeded.
    * :attr:`FAILED` -- the child ran and said it did not succeed.
    * :attr:`INTERRUPTED` -- **something other than the user ended it**: a signal, which the
      CLI reports as :data:`INTERRUPTED_EXIT_CODE`.

    The last three are the terminal states, and :attr:`CANCELLED` is deliberately one of
    them. "The user stopped it" is a settled answer, not a wait: once the child is gone
    there is nothing left to find out, and a state that kept claiming to be in progress
    would be a second thing to keep in step with the child -- which is the pile this module
    exists to remove.
    """

    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    FINISHED = "finished"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    # ★ **The classification lives on the member, not only on the machine.** The first
    # version put ``is_active``/``is_terminal`` on :class:`RunStateMachine`, and every panel
    # that reads a state through a property therefore wrote ``self.state in TERMINAL`` --
    # which is the same question asked in a different place four times over, and the sort of
    # thing that is right in three panels and a typo in the fourth. It also failed loudly
    # the first time it was tried: ``self.state.is_terminal`` raised inside a Qt timer
    # callback, once every 50 ms, forever, because the pump that would have stopped it never
    # got to run. **A member that knows what kind of state it is is the whole reason a
    # panel can ask the question at all.**
    #
    # The bodies read the module-level sets below rather than restating the lists, so the
    # enum and the sets cannot disagree and there is still one list per category.

    @property
    def is_no_run(self) -> bool:
        """Whether this is the absence of a run, as :attr:`IDLE` is."""
        return self in NO_RUN

    @property
    def is_active(self) -> bool:
        """Whether a run in this state has not reached a verdict yet."""
        return self in ACTIVE

    @property
    def is_terminal(self) -> bool:
        """Whether a run in this state has reached one."""
        return self in TERMINAL


#: The one state that means "no run". Kept as a name of its own rather than inferred from
#: the absence of a child, because the panel and the machine have to agree about it and a
#: value both can read is worth more than a conclusion they each draw.
IDLE: Final = RunState.IDLE

#: States in which a run has not reached a verdict anybody can act on yet. Everything else
#: is in :data:`TERMINAL`, and the two sets together are every state: a run is either
#: undecided or decided, and there is no third thing for it to be.
ACTIVE: Final = frozenset({RunState.STARTING, RunState.RUNNING, RunState.CANCELLING})

#: States a run rests in. ``CANCELLED`` is here for the reason its docstring gives, and the
#: cost of that choice is paid deliberately: it is why the stopped-by-someone-else case gets
#: a state of its own rather than sharing this one.
TERMINAL: Final = frozenset(
    {RunState.CANCELLED, RunState.FINISHED, RunState.FAILED, RunState.INTERRUPTED}
)

#: **And the third category, which is neither.** The first version of this module had two
#: sets and left :attr:`RunState.IDLE` in neither, on the assumption that "not running" was
#: the negation of "running" and did not need saying. Its own test caught it: IDLE is the
#: answer to *is there a run*, while the other two answer *has this run been decided* --
#: questions a caller asks at different moments and must not be given the same reply. A
#: panel that has just been handed a child asks the first; a caller waiting for a verdict
#: asks the second, and folding IDLE into ``TERMINAL`` would have told it the run had
#: finished.
NO_RUN: Final = frozenset({RunState.IDLE})

#: Every state a run may be in, and what it may become. The table is the state machine --
#: there is no other rule anywhere in this module, so "which transitions exist" is a thing
#: a test can read off one dict rather than a thing it has to infer from call sites.
#:
#: Three rows carry a decision rather than a mechanical consequence:
#:
#: * ``CANCELLING -> {CANCELLED}`` and **nothing else.** A run that has been asked to stop
#:   has exactly one ending, and this is the row that says so: it cannot then turn into a
#:   success, a failure or an interruption, whatever number the dying child reports.
#: * ``STARTING -> FAILED`` with no ``CANCELLING`` alongside it. A child that never launched
#:   cannot be stopped, so a run that fails at ``Popen`` has one outcome rather than two.
#: * ``CANCELLED -> STARTING`` with no ``CANCELLED -> CANCELLING``. Once a stopped run is
#:   gone it cannot be asked to stop again, and letting it be would mean a second press of
#:   Cancel looked like the first.
#:
#: ★ **A row that is missing on purpose: ``CANCELLING -> FINISHED``.** A stop landing in
#: the same instant the run completes on its own is a real race, not a hypothetical, and
#: reporting it as "cancelled" throws away a run that finished. It is not here because
#: today's behaviour is that ``killed`` wins: :func:`outcome_state` answers
#: :attr:`~RunState.CANCELLED` for a child that exited 0 *and* was killed, which is what
#: the panel has always shown and what ``test_gui_process.py`` pins when it asserts that
#: ``RunResult(exit_code=0, killed=True).ok`` is false. Adding the row would be a
#: behaviour change dressed as a table entry, and behaviour changes are not this
#: milestone's business. **It is a decision left for review, not one taken here.**
_TRANSITIONS: Final[dict[RunState, frozenset[RunState]]] = {
    RunState.IDLE: frozenset({RunState.STARTING}),
    RunState.STARTING: frozenset({RunState.RUNNING, RunState.FAILED}),
    RunState.RUNNING: frozenset(
        {
            RunState.CANCELLING,
            RunState.FINISHED,
            RunState.FAILED,
            RunState.INTERRUPTED,
        }
    ),
    RunState.CANCELLING: frozenset({RunState.CANCELLED}),
    RunState.CANCELLED: frozenset({RunState.STARTING}),
    RunState.FINISHED: frozenset({RunState.STARTING}),
    RunState.FAILED: frozenset({RunState.STARTING}),
    RunState.INTERRUPTED: frozenset({RunState.STARTING}),
}

#: The initial value, named so that a machine and the table cannot disagree about it. If
#: :data:`_TRANSITIONS` ever lists a state with nowhere to go *from*, that state is
#: unreachable and a test says so.
INITIAL: Final = RunState.IDLE


class IllegalTransitionError(RuntimeError):
    """A move the state machine does not have.

    A :class:`RuntimeError` rather than :class:`ValueError` because this is a bug in the
    caller, not a bad value handed in from outside: nothing in this project lets a user
    type a state.
    """

    def __init__(self, current: RunState, target: RunState) -> None:
        self.current = current
        self.target = target
        allowed = ", ".join(sorted(state.value for state in _TRANSITIONS[current]))
        super().__init__(
            f"a run that is {current.value} cannot become {target.value}; "
            f"from {current.value} it may become: {allowed or 'nothing'}"
        )


class ContradictoryStateError(AssertionError):
    """Two facts about a run that cannot both be true.

    Named for what it is rather than what it does: an ``AssertionError`` would be caught by
    ``python -O``, and the whole point is that a run's state stops agreeing with itself
    whether or not anyone is checking.
    """

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)


#: The states a run can only be in if a child was actually launched. ``STARTING`` is absent
#: because it is the state *before* the child exists, and ``FAILED`` is absent because
#: refusing to launch is a perfectly ordinary way for a run to fail -- ``Popen`` raising is
#: the one failure with no exit code at all, and it is the reason ``STARTING`` has an edge
#: to :attr:`~RunState.FAILED` rather than the state machine assuming every run has a child.
_REQUIRES_A_CHILD: Final = frozenset(
    {
        RunState.RUNNING,
        RunState.CANCELLING,
        RunState.CANCELLED,
        RunState.FINISHED,
        RunState.INTERRUPTED,
    }
)


def contradiction(*, state: RunState, launched: bool) -> str | None:
    """The sentence describing how these two facts disagree, or ``None``.

    **The two ways this can happen are the two the old booleans made easy.** A run
    reporting itself as under way with no child behind it, and a child that was launched
    while the run still claims there is none. Both are one line of cleanup in the wrong
    order -- a ``shutdown()`` that released ``_process`` a moment before or after the reader
    delivered -- and under the old flags neither was visible, because ``_process is None``
    and ``_finished is False`` are each an ordinary value on their own.

    **``launched`` and not ``alive``, and the difference is the subtlety.** A child can
    exit between the moment it is reaped and the moment the reader thread delivers the
    outcome, so "alive" is not an invariant: ``RUNNING`` is the correct answer throughout
    that window, and a guard that flagged it would be wrong about real runs. A guard that
    is wrong about real runs gets switched off, which is worse than not having one. What
    *is* impossible is the state reaching :data:`_REQUIRES_A_CHILD` with no child behind
    it, and a launched child meeting :attr:`~RunState.IDLE`.

    ``STARTING`` and ``FAILED`` are exempt from the first, deliberately: a run that cannot
    have launched a child may still be starting one, and may have failed trying.
    """
    if state in _REQUIRES_A_CHILD and not launched:
        return (
            f"the state is {state.value} but no child was ever launched, so there is no "
            "run for it to be the state of"
        )
    if launched and state is RunState.IDLE:
        return "a child was launched but the state is idle, so the run is unaccounted for"
    return None


def outcome_state(*, exit_code: int, killed: bool) -> RunState:
    """The state a run that has ended lands in.

    **``killed`` first, and it wins over everything.** A child we asked to stop reports
    whatever exit code the signal gave it -- ``1``, ``-9``, or ``3`` depending on the
    platform and the timing -- and none of those numbers is evidence about the run. Reading
    one as "failed" is how a user who pressed Cancel ends up being told their run broke.
    The ``killed`` flag is not evidence about the run either, only about who ended it, which
    is why it selects :attr:`~RunState.CANCELLED` and nothing further: the caller that
    pressed the button is what makes it a cancellation rather than an interruption, and
    :meth:`RunStateMachine.move_to` records that at the moment the request was made.

    With no kill in play the exit code is the CLI's own verdict, and it is a contract:
    :data:`INTERRUPTED_EXIT_CODE` is the one code that means "a signal ended this", and
    giving it a state of its own is what lets the window say so instead of reporting an
    interrupted run as a failure with a number in the message.
    """
    if killed:
        return RunState.CANCELLED
    if exit_code == 0:
        return RunState.FINISHED
    if exit_code == INTERRUPTED_EXIT_CODE:
        return RunState.INTERRUPTED
    return RunState.FAILED


class RunStateMachine:
    """One run's state, and the only way it may change.

    **Deliberately one machine per child, not one per panel.** A panel can start a second
    run before the first one's widgets have settled, and a panel-wide machine would have to
    guess which run a transition belonged to. Keeping it beside the process makes the
    pairing structural: a state can outlive its child only by being held by the same object
    that held the child.

    Not thread-safe, and does not need to be. Every transition is made either on the thread
    that starts the run or on the reader thread that ends it, and the two never overlap --
    the reader cannot deliver before :meth:`CliProcess.start` has returned. Readers on other
    threads see a single attribute, which is what a read of an enum member is.
    """

    __slots__ = ("_state",)

    def __init__(self) -> None:
        self._state: RunState = INITIAL

    @property
    def state(self) -> RunState:
        """Where the run is."""
        return self._state

    @property
    def is_active(self) -> bool:
        """Whether the run has not reached a verdict yet.

        Delegated to the state rather than answered here, so a caller holding
        ``panel.state`` and a caller holding ``panel.state_machine`` are given the same
        answer by the same line of code.
        """
        return self._state.is_active

    @property
    def is_terminal(self) -> bool:
        """Whether the run has reached one. Delegated, as :attr:`is_active` is."""
        return self._state.is_terminal

    def may_become(self, target: RunState) -> bool:
        """Whether ``move_to`` would accept ``target`` from here, without doing it."""
        return target in _TRANSITIONS[self._state]

    def move_to(self, target: RunState) -> RunState:
        """Make ``target`` the state, and return it.

        Raises:
            IllegalTransitionError: the move is not in :data:`_TRANSITIONS`. **Raised, never
                ignored.** A caller that reaches an illegal transition has a bug, and
                quietly carrying on is how a state machine becomes the pile of flags it
                replaced -- the caller would go on believing it had said something, and the
                next read would report a state that no run was ever in.
        """
        if target not in _TRANSITIONS[self._state]:
            raise IllegalTransitionError(self._state, target)
        self._state = target
        return target

    def __repr__(self) -> str:
        return f"RunStateMachine({self._state.value})"


def state_of(process: object) -> RunState:
    """The state of ``process``, or :attr:`~RunState.IDLE` when there is no process.

    **One definition of "a panel with no child has no run", in one place.** Four panels
    need this answer and none of them should restate it: a copy in each is four places for
    the rule to be read slightly differently, which is the failure mode this whole module
    was written to remove.

    The argument is typed ``object`` rather than the process class on purpose -- this
    module must not import :mod:`gigaxml.gui.cli_process`, which imports it. What is
    required of the argument is written in the sentence below rather than in a signature.

    **A panel that has released its child reports IDLE, and that is deliberate.** Every
    panel's ``shutdown()`` drops its process reference on the way out, and a panel whose
    child is gone genuinely has no run in hand. Reading anything else would mean keeping
    a second copy of the state beside the child purely so that a widget being destroyed
    could still be asked about it -- one more thing to keep in step, which is what this
    milestone is removing. ``test_the_window_reports_idle_after_it_closes`` pins the
    behaviour so it stays a decision rather than becoming an accident.
    """
    if process is None:
        return RunState.IDLE
    return process.state  # type: ignore[attr-defined] # the contract: a CliProcess, or a stand-in
