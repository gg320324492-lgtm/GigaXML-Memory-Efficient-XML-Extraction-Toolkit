"""The three boundaries the GUI is supposed to hold, as assertions rather than intentions.

★ **M9 criteria B, C and D. All three are green today, and that is their purpose.**

The brief is explicit that these guard the future rather than report the present, and that
the executor must not manufacture a failure to make a test look earned. So each one below
is written to be *sensitive* instead: :func:`test_the_orchestration_checker_catches_every_way`
and :func:`test_the_parser_checker_catches_every_route` feed each real way of breaking the
rule through the checker and require it to be caught, on the line that carries it. A guard
that has never fired is indistinguishable from a guard that stopped working, and the
project's existing ``test_gui_no_parsing.py`` already establishes that measuring its own
sensitivity is the house style rather than a novelty.

**AST, never a text search, and for a reason this repository has already paid for.** The
brief's own criterion B is a perfect illustration: ``gui/document_info.py`` has the word
``subprocess`` in its module docstring, in a sentence explaining that the GUI times a
child process end to end. A grep for ``subprocess`` under ``gui/panels/`` happens to be
green today, but the same search one directory up would fail on a comment, and a guard that
fires on its own explanation cannot be used to check anything else. Comments are not nodes
and strings are not names, so a tree walk has no such ambiguity.

**Criterion C is not a second copy of the no-parsing guard, and the difference is
measured rather than asserted.** ``test_gui_no_parsing.py`` bans modules and names that
appear in the source. This file bans two ways around it that it cannot see:

* a **dynamic import** -- ``importlib.import_module("gigaxml.inspect")`` names a banned
  module in a :class:`ast.Constant`, and reaches it through a call rather than an
  ``Import`` node;
* **reaching a banned module through an unbanned one.** :mod:`gigaxml.run` is imported by
  the GUI on purpose -- ``QUARANTINABLE`` is a tuple of exception classes -- and the
  existing guard documents that as an accepted boundary. :mod:`gigaxml.cli` is different
  and cannot be argued the same way: measured, importing it adds :mod:`gigaxml.inspect`
  and :mod:`gigaxml.sample` to what is already loaded, and **both are modules the existing
  guard bans outright.** That is why ``run_state.py`` writes the interrupted exit code out
  rather than importing it, and why a line saying ``from gigaxml.cli import EXIT_INTERRUPTED``
  has to fail here: it breaks a rule the other guard cannot observe.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import gigaxml.gui.panels.execution as execution_module
import gigaxml.gui.panels.structure as structure_module
import gigaxml.gui.run_state as run_state_module

#: The guarded trees, derived from the modules under test rather than from an installed
#: package path, so the check covers the checkout a change is being made to.
GUI_ROOT = Path(run_state_module.__file__).resolve().parent
PANELS_ROOT = GUI_ROOT / "panels"

#: The panels that own a child process and therefore own a run's state. The results and
#: error panels are deliberately not here: they display what a run left behind and hold no
#: child, so their own bookkeeping answers "what am I showing", not "how did the run end".
STATE_OWNING_PANELS = {
    "execution.py": execution_module,
    "structure.py": structure_module,
}

#: Every attribute name the boolean era used for "how did this run go". Banned as
#: **assignments** in the panels above, which is what makes criterion D checkable: to
#: decide a run's state from a boolean, something has to store that boolean, and this
#: refuses the store. ``_finished_generation`` is not on the list and is not matched by
#: prefix -- it is an int naming *which* run an answer belongs to, which is a different
#: question from whether one finished, and no state can answer it.
RETIRED_STATE_FLAGS = frozenset(
    {"_finished", "_finished_flag", "_running", "_cancelled", "_cancelling", "_stopping"}
)


# --- criterion B: orchestration stays in cli_process -------------------------


#: Names that spawn something when they appear as an **attribute** -- ``subprocess.run``,
#: ``os.system``, ``multiprocessing.Process``. Any receiver is accepted; see the note in
#: :func:`_spawns` for why an alias is not a way round it.
_SPAWNING_ATTRIBUTES = frozenset(
    {
        "Popen",
        "run",
        "call",
        "check_call",
        "check_output",
        "system",
        "popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "spawnl",
        "spawnv",
        "spawnle",
        "spawnve",
        "execl",
        "execv",
        "execlp",
        "execvp",
        "execlpe",
        "execvpe",
        "Process",
        "run_module",
        "run_path",
    }
)

#: The subset that is also unambiguous as a **bare name**, and so may be matched without a
#: receiver: ``from subprocess import Popen; Popen([...])``.
#:
#: ★ **This set is deliberately much smaller than the one above, and the first version of
#: this checker got it wrong in the instructive direction.** It matched every bare ``Name``
#: in the spawning list, and on the real tree that reported nineteen hits -- every one of
#: them the *parameter* called ``run`` in ``_note_finished(self, run: RunResult)``. **A
#: guard that fires on ordinary code is worse than no guard**, because the first time
#: somebody hits a false positive the next thing that happens is that it gets switched off.
#: The names left out are exactly the ones a reader would never use as a local variable:
#: ``run``, ``call``, ``system``, ``popen`` and ``Process`` are ordinary words, and all of
#: them appear as identifiers in this codebase.
_SPAWNING_BARE_NAMES = frozenset(
    {
        "Popen",
        "fork",
        "forkpty",
        "posix_spawn",
        "spawnl",
        "spawnv",
        "spawnle",
        "spawnve",
        "execl",
        "execv",
        "execlp",
        "execvp",
        "execlpe",
        "execvpe",
    }
)


def _spawns(tree: ast.AST) -> list[tuple[int, str]]:
    """Every way a module starts another process, as ``(line, reason)`` pairs.

    Walks the tree, so a module docstring naming ``subprocess`` -- and one does, in
    ``document_info.py`` -- is a :class:`ast.Constant` here and is not reported.
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in {"subprocess", "multiprocessing", "runpy"}:
                    found.append((node.lineno, f"imports {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in {"subprocess", "multiprocessing", "runpy"}:
                found.append((node.lineno, f"imports from {node.module}"))
        elif isinstance(node, ast.Attribute) and node.attr in _SPAWNING_ATTRIBUTES:
            # **The receiver is not checked, and deliberately.** ``subprocess as sp`` then
            # ``sp.Popen(...)`` is the same call, and a checker that matched on the name it
            # was imported under would have to track every alias in the package to keep up.
            # ``Popen`` is not a name anything else here plausibly has -- which is the same
            # judgement ``test_gui_no_parsing.py`` makes about ``etree.iterparse`` -- so the
            # attribute name alone is enough, and the alias is exactly what makes the
            # receiver unreliable.
            found.append((node.lineno, f"calls {ast.unparse(node.value)}.{node.attr}"))
        elif isinstance(node, ast.Name) and node.id in _SPAWNING_BARE_NAMES:
            # ``from subprocess import Popen`` -- the import line above is caught, but the
            # *call* is the thing that spawns, and it deserves naming in its own right.
            found.append((node.lineno, f"calls {node.id}()"))
    return found


def test_no_panel_under_the_gui_starts_a_process() -> None:
    """★ **Criterion B. Green today, and it is here for the day it stops being.**

    ``CliProcess`` is the only thing in this package that starts a child, and the reason is
    the project's own: a multi-gigabyte document must be processed in bounded memory, so it
    is processed by the CLI and the window reads what the CLI prints. A panel that grew its
    own ``Popen`` would put that document in the window's memory and make the claim false,
    and it would do so in exactly the place nobody reads the guard for.

    Two checks rather than one, because the brief's version is a single word and this is the
    whole family: ``Popen`` alone would be satisfied by ``os.system``, and a process started
    without the word ``subprocess`` anywhere in the file is the shape that actually happens.
    """
    assert PANELS_ROOT.is_dir(), f"no panels directory under {GUI_ROOT}"
    offenders: list[str] = []
    for path in sorted(PANELS_ROOT.rglob("*.py")):
        offenders += [
            f"{path}:{line}: {reason}"
            for line, reason in _spawns(ast.parse(path.read_text(encoding="utf-8")))
        ]

    assert not offenders, (
        "panels render and dispatch; the child process is cli_process.py's job:\n"
        + "\n".join(offenders)
    )


def test_the_only_module_that_starts_a_child_is_still_the_one_that_does() -> None:
    """The other half of criterion B: a rule nothing satisfies is a rule nothing checks.

    Pairing it with the panel assertion means the two cannot both be satisfied by deleting
    process-starting outright, and it names the file the whole design rests on -- so a
    future refactor that moved it would have to move it *somewhere* and this would say
    where.
    """
    source = (GUI_ROOT / "cli_process.py").read_text(encoding="utf-8")
    assert "Popen" in source, "cli_process.py no longer starts the child the GUI depends on"


#: Each way a panel could grow its own orchestration, and the line it would be on.
_ORCHESTRATION_MUTATIONS: list[tuple[str, str, int]] = [
    ("popen_by_name", "import subprocess\nsubprocess.Popen(['x'])\n", 2),
    ("popen_from_import", "from subprocess import Popen\nPopen(['x'])\n", 2),
    ("subprocess_run", "import subprocess\nsubprocess.run(['x'])\n", 2),
    ("os_system", "import os\nos.system('ls')\n", 2),
    ("os_popen", "import os\nos.popen('ls')\n", 2),
    (
        "inside_a_method",
        "class P:\n"
        "    def go(self):\n"
        "        import subprocess\n"
        "        return subprocess.Popen(['x'])\n",
        4,
    ),
    ("aliased", "import subprocess as sp\nsp.Popen(['x'])\n", 2),
    ("from_multiprocessing_import_line", "from multiprocessing import Process\nProcess()\n", 1),
]


@pytest.mark.parametrize(
    ("name", "source", "expected_line"),
    _ORCHESTRATION_MUTATIONS,
    ids=[item[0] for item in _ORCHESTRATION_MUTATIONS],
)
def test_the_orchestration_checker_catches_every_way(
    name: str, source: str, expected_line: int
) -> None:
    """★ **Criterion B proving it would notice, as a permanent test.**

    Eight real shapes, and each must be caught **on the line that carries it**. Pinning the
    line rather than the count is what makes this a check of *what* was found: a checker
    reporting line 1 for something on line 2 would satisfy a count-based assertion while
    having read nothing.

    The one case expected on line **1** rather than 2 is deliberate, and it documents the
    checker's contract rather than papering over it. ``from multiprocessing import
    Process`` is caught at the import; the bare ``Process()`` on the next line is not,
    because ``Process`` is an ordinary word and :data:`_SPAWNING_BARE_NAMES` leaves it out
    to keep ``_note_finished(self, run)`` from being reported nineteen times over. The
    import is enough -- you cannot reach the class without it -- and the trade is written
    down where somebody widening the bare set would find it.
    """
    del name  # only the id
    found = _spawns(ast.parse(source))
    assert found, f"the checker did not notice this at all:\n{source}"
    assert expected_line in {line for line, _ in found}, (
        f"the violation is on line {expected_line} and the checker reported "
        f"{sorted({line for line, _ in found})}:\n{source}"
    )


def test_the_orchestration_checker_ignores_the_words_that_explain_the_rule() -> None:
    """**The half a grep cannot pass, and the reason this file is AST-based at all.**

    ``gui/document_info.py`` says the word ``subprocess`` in a sentence explaining that the
    GUI times a child end to end. A text search reports that; a tree walk does not. This
    test is what stops the check being "helpfully" widened into a text search later, which
    would make it fire on its own documentation.
    """
    documented = (
        '"""Timed end to end through subprocess, one child per document."""\n'
        "# A comment naming Popen, os.system and runpy.\n"
        "SUBPROCESS = ('Popen', 'os.system')\n"
    )
    assert _spawns(ast.parse(documented)) == []


# --- criterion C: the strengthened parser guard ------------------------------

#: The modules ``test_gui_no_parsing.py`` bans. Duplicated rather than imported, because
#: importing that module from here would pull a test's fixtures into a test, and because
#: the point is that this file bans the *same* set by a *different* route -- if the two
#: drifted, this guard would silently be checking something else.
BANNED_MODULES = frozenset({"lxml", "gigaxml.parser", "gigaxml.inspect", "gigaxml.sample"})

#: And the module that is not banned but reaches them. ``gigaxml.run`` is imported by the
#: GUI on purpose and the existing guard says so; ``gigaxml.cli`` dispatches the two
#: commands that are banned by name, and measured it adds both to a loaded process.
CLI_MODULE = "gigaxml.cli"


def _root(name: str) -> str:
    """``lxml.etree.iterparse`` -> ``lxml``; ``gigaxml.parser.streaming`` -> ``gigaxml.parser``."""
    parts = name.split(".")
    return ".".join(parts[:2]) if name.startswith("gigaxml.") else parts[0]


def _dynamic_parser_reaches(tree: ast.AST) -> list[tuple[int, str]]:
    """Every route to a banned module that the existing guard's walk cannot see.

    The existing guard reads ``Import``/``ImportFrom`` nodes and attribute/name references.
    These are the ones that get past all of that: a module name in a string, resolved at run
    time by something that is not an import statement; the same name reached through a
    function object rather than a module; and code evaluated from a string.
    """
    found: list[tuple[int, str]] = []

    # ★ **One hop of indirection, because that is the shape evasion actually takes.**
    # ``module = "gigaxml.inspect"`` then ``importlib.import_module(module)`` defeats a
    # checker that only reads string literals, and it costs one extra line to write. So a
    # name bound to a string literal is resolved. This is not dataflow analysis and does
    # not pretend to be: it does not follow reassignment, concatenation, a parameter, or a
    # value built in a loop. **What it covers is the case somebody would write to get past a
    # literal-only check**, and the limit is stated here rather than left to be found.
    literals: dict[str, str] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    literals[target.id] = node.value.value

    def text_of(argument: ast.AST) -> str | None:
        """The string this argument denotes, or ``None`` when it cannot be read."""
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        if isinstance(argument, ast.Name):
            return literals.get(argument.id)
        return None

    def check_arguments(node: ast.Call, how: str) -> None:
        for argument in (*node.args, *(kw.value for kw in node.keywords)):
            value = text_of(argument)
            if value is None:
                continue
            if _root(value) in BANNED_MODULES or value.startswith(CLI_MODULE):
                found.append((node.lineno, f"{how}({value!r})"))

    for node in ast.walk(tree):
        # ★ **Calls, not attributes.** The first version of this walked ``ast.Attribute``
        # and read ``node.args`` off it -- an attribute has no such field -- so it reported
        # nothing for any route at all, and all eight self-tests failed. **The failures were
        # the checker being wrong, not the tree being wrong**, which is the shape of bug this
        # milestone exists to be about: a checker that reports nothing while looking as
        # though it is looking.
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name) and function.id in {
                "__import__",
                "import_module",
                "load_module",
            }:
                check_arguments(node, function.id)
            elif isinstance(function, ast.Attribute) and function.attr in {
                "import_module",
                "load_module",
            }:
                check_arguments(node, ast.unparse(function))
        elif isinstance(node, ast.Name) and node.id in {"eval", "exec", "compile"}:
            # **Refused whatever they are handed**, because a guard that had to understand
            # the argument would lose to whoever writes the argument. ``QApplication.exec()``
            # is an attribute call rather than a bare name, so the GUI's own ``.exec()`` is
            # untouched by this.
            found.append((node.lineno, f"evaluates code at run time ({node.id})"))
    return found


def test_no_module_under_the_gui_reaches_a_banned_module_the_other_guard_cannot_see() -> None:
    """★ **Criterion C, the strengthened version. Green today; here for the day it isn't.**

    Everything the existing ``test_gui_no_parsing.py`` catches, this does not repeat: that
    guard is thorough about what it can see, and a second copy of it would be a second
    thing to keep up to date. This is the part it structurally cannot see -- a module name
    in a string, resolved at run time by something that is not an import statement.

    The failure message names the line, so a fix is a place to go rather than a thing to
    search for.
    """
    offenders: list[str] = []
    for path in sorted(GUI_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [f"{path}:{line}: {reason}" for line, reason in _dynamic_parser_reaches(tree)]

    assert not offenders, (
        "the interface process must not reach a parser or a banned module by a route the "
        "no-parsing guard cannot see:\n" + "\n".join(offenders)
    )


def test_no_module_under_the_gui_imports_the_cli() -> None:
    """★ **The measured hole, and the reason ``run_state.py`` copies a constant.**

    ``gui/app.py`` reaches ``gigaxml.cli`` in two places already -- inside ``if __name__ ==
    "__main__"`` and inside a frozen-build branch -- and both are excluded here on purpose:
    a frozen build *is* the CLI, and the ``__main__`` guard is how a frozen binary is
    launched at all. Neither happens when the window is merely open.

    Everywhere else it would be a way to reach :mod:`gigaxml.inspect` and
    :mod:`gigaxml.sample` -- modules ``test_gui_no_parsing.py`` bans outright -- without
    spelling either name. That is why the interrupted exit code is written out in
    ``run_state.py`` and pinned there by a test instead of being imported.
    """
    for path in sorted(GUI_ROOT.rglob("*.py")):
        if path.name == "app.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == CLI_MODULE:
                offenders = [alias.name for alias in node.names]
                pytest.fail(
                    f"{path}:{node.lineno}: {path.name} imports from {CLI_MODULE} ({offenders}). "
                    f"Importing it loads gigaxml.inspect and gigaxml.sample, both banned by "
                    "test_gui_no_parsing.py. app.py is exempt because a frozen build is the "
                    "CLI; nothing else is."
                )
            if isinstance(node, ast.Import) and any(
                alias.name == CLI_MODULE for alias in node.names
            ):
                pytest.fail(f"{path}:{node.lineno}: {path.name} imports {CLI_MODULE}")


def test_the_guard_reads_every_module_including_the_new_one() -> None:
    """★ **\"Covers the new code\", as a measurement rather than a promise.**

    The brief asks for the no-parsing rule to be *strengthened to cover new code*, and the
    honest reading of that is that a new module has to be inside the guarded walk. This
    asserts the walk found ``run_state.py`` specifically -- M9's new module -- so a future
    module that the walk missed shows up as a failure here rather than as a gap nobody
    noticed.
    """
    sources = sorted(GUI_ROOT.rglob("*.py"))
    assert Path(run_state_module.__file__) in sources, "run_state.py is outside the guarded walk"
    assert Path(execution_module.__file__) in sources
    assert len(sources) == len(list(GUI_ROOT.rglob("*.py"))), "the walk saw fewer files than exist"


_PARSER_ROUTES: list[tuple[str, str, int]] = [
    ("import_module", "import importlib\nimportlib.import_module('lxml.etree')\n", 2),
    ("dunder_import", "mod = 'gigaxml.parser'\n__import__(mod)\n", 2),
    (
        "import_module_from",
        "from importlib import import_module\nimport_module('gigaxml.inspect')\n",
        2,
    ),
    (
        "submodule_route",
        "import importlib\nimportlib.import_module('gigaxml.parser.streaming')\n",
        2,
    ),
    ("through_the_cli", "import importlib\nimportlib.import_module('gigaxml.cli')\n", 2),
    ("eval_of_a_built_name", "mod = 'lxml'\neval('import ' + mod)\n", 2),
    ("exec_of_anything", "exec('from lxml import etree')\n", 1),
]


@pytest.mark.parametrize(
    ("name", "source", "expected_line"),
    _PARSER_ROUTES,
    ids=[item[0] for item in _PARSER_ROUTES],
)
def test_the_parser_checker_catches_every_route(name: str, source: str, expected_line: int) -> None:
    """★ **Criterion C proving it would notice.**

    Seven routes, each required to be caught **on its own line**. The last two are the
    blunt ones: any ``eval``/``exec``/``compile`` in a GUI module is refused whatever it is
    handed, because a guard that had to understand the argument would lose to the person
    writing the argument.
    """
    del name  # only the id
    found = _dynamic_parser_reaches(ast.parse(source))
    assert found, f"the checker did not notice this at all:\n{source}"
    assert expected_line in {line for line, _ in found}, (
        f"the route is on line {expected_line} and the checker reported "
        f"{sorted({line for line, _ in found})}:\n{source}"
    )


def test_the_parser_checker_accepts_the_imports_the_gui_actually_makes() -> None:
    """**It has to pass the real tree, or it is a rule nobody can follow.**

    Every import ``src/gigaxml/gui/`` makes today, run through the checker. If a future
    change needed one of these, this is what would say so -- and listing them here makes the
    boundary a decision rather than an accident.
    """
    from gigaxml.checkpoint import CHECKPOINT_FILENAME, Checkpoint, CheckpointError, read_checkpoint
    from gigaxml.config import ExtractionConfig, load_config, parse_config
    from gigaxml.errors import GigaXMLError
    from gigaxml.fields import FieldType
    from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME, QUARANTINABLE
    from gigaxml.writers import PARTIAL_SUFFIX

    del (
        CHECKPOINT_FILENAME,
        Checkpoint,
        CheckpointError,
        read_checkpoint,
        ExtractionConfig,
        load_config,
        parse_config,
        GigaXMLError,
        FieldType,
        DEFAULT_RUN_REPORT_FILENAME,
        QUARANTINABLE,
        PARTIAL_SUFFIX,
    )


def test_the_parser_checker_ignores_prose_that_names_the_rule() -> None:
    """**The half that cannot pass as a text search.**

    ``run_state.py``'s module docstring names ``gigaxml.cli``, ``gigaxml.inspect`` and
    ``gigaxml.sample`` in order to explain why it does not import the first. A checker that
    fired on that would be unable to check anything else, because the one place the rule is
    explained would be the one place it fails.
    """
    documented = (
        '"""Importing gigaxml.cli loads gigaxml.inspect, so this writes the number out."""\n'
        "# A comment mentioning lxml.etree.iterparse and importlib.import_module.\n"
        "ROUTES = ('gigaxml.inspect', 'lxml.etree')\n"
        "def show(name):\n"
        "    return importlib.import_module(name)\n"
    )
    assert _dynamic_parser_reaches(ast.parse(documented)) == [], (
        "nothing here names a banned module in a place the checker can read: the docstring "
        "and the comment are constants, ROUTES is never passed to an import, and `name` is "
        "a parameter this checker does not follow"
    )


# --- criterion D: panels read the state, they do not keep one ---------------


def _mentions_retired_flag(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Every ``self.<retired flag>`` a panel touches, as ``(line, name, how)`` triples.

    ★ **Reads as well as writes, and that is the second half of a lesson this milestone's
    own mutation earned.** The first version looked only at *assignments*, on the reasoning
    that a boolean decision needs somewhere to keep the boolean. That reasoning is sound for
    deciding a run's state -- but the mutation below swapped ``if self.state.is_terminal:``
    for ``if self._finished:`` **without putting the flag back**, and the check stayed
    green. It stayed green because nothing in the suite drives that line: the panel would
    raise :class:`AttributeError` on the first real run, and no test started one.

    So the check now refuses the mention in either direction, and is worded as a *mention*
    rather than as a store because a read of a name nobody defines is precisely the case the
    assignment-only version had no answer for. The failure message says which of the two it
    was, so a fix is obvious from the report.
    """
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in RETIRED_STATE_FLAGS:
            continue
        if isinstance(node.ctx, ast.Store):
            how = "stores"
        elif isinstance(node.ctx, ast.Load):
            how = "reads"
        else:
            how = "deletes"
        found.append((node.lineno, node.attr, how))
    return found


def _assigned_flags(tree: ast.AST) -> list[tuple[int, str]]:
    """The stores alone, as ``(line, name)`` pairs. Kept for the sensitivity test below."""
    return [(line, name) for line, name, how in _mentions_retired_flag(tree) if how == "stores"]


def test_no_panel_stores_a_run_state_of_its_own() -> None:
    """★ **Criterion D, and the checkable half of \"delete the old flags\"**.

    A boolean-based decision needs somewhere to keep the boolean, so refusing the store
    refuses the decision. ``is_terminal`` on the state, read from the child, is then the
    only way a panel can ask how a run ended -- and there is exactly one implementation of
    that answer for four panels rather than four.

    ``_finished_generation`` is not banned and is not matched by prefix: it is an int naming
    *which* run an answer belongs to, which is a question no run state answers, and the
    preview panel's generation check is unchanged by this milestone.
    """
    offenders: list[str] = []
    for path in sorted(PANELS_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offenders += [
            f"{path}:{line}: {how} {name}" for line, name, how in _mentions_retired_flag(tree)
        ]

    assert not offenders, (
        "a panel is rendering and dispatching; where a run is comes from its child:\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("name", sorted(STATE_OWNING_PANELS))
def test_every_state_owning_panel_exposes_the_state(name: str) -> None:
    """The positive half of criterion D: asking is possible, and it is one property.

    Naming the panels rather than walking them means a new panel that owns a child and
    forgets this fails here, which is the moment it is cheapest to fix.
    """
    module = STATE_OWNING_PANELS[name]
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

    properties = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "state"
    ]
    assert properties, f"{name} has no `state` property, so a caller cannot ask how the run is"

    decorators = {ast.unparse(d) for d in properties[0].decorator_list}
    assert "property" in decorators, f"{name}.state is a method, not something a caller reads"

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "state_of(" in source, (
        f"{name}.state should read the child through state_of(), not re-derive it -- "
        "four copies of 'no child means no run' is four places for the rule to drift"
    )


def test_the_state_flag_checker_catches_a_boolean_brought_back() -> None:
    """★ **Criterion G2, as a permanent test: the check proves it is sensitive.**

    This is the mutation the brief asks for -- swap a panel's state decision back to the
    boolean -- expressed against the checker rather than against a checkout, so it is
    reproducible from this file alone with nothing to dirty. Each shape below is a way the
    boolean comes back, and the last one is the subtlest: **assigning it inside a callback
    is not a different defect**, it is the same one.
    """
    restorations = [
        "class P:\n    def __init__(self):\n        self._finished = False\n",
        "class P:\n    def start(self):\n        self._finished_flag: bool = False\n",
        "class P:\n    def cancel(self):\n        self._cancelled = True\n",
        "class P:\n    def __init__(self):\n        self._finished = None\n",
        "class P:\n    def _note(self, run):\n        self._finished = True\n",
    ]
    for source in restorations:
        found = _mentions_retired_flag(ast.parse(source))
        assert found, f"a restored boolean was not noticed:\n{source}"

    # ★ **And the read, which the assignment-only version of this check had no answer for.**
    # It was found by a mutation rather than by thinking: swapping the drain's decision for
    # a flag nobody sets left the whole suite green, because nothing drives that line.
    reads = [
        "class P:\n    def _drain(self):\n        if self._finished:\n            pass\n",
        "class P:\n"
        "    def _finish(self):\n"
        "        if self._running or self._cancelled:\n"
        "            pass\n",
        "class P:\n    def go(self):\n        return not self._stopping\n",
    ]
    for source in reads:
        found = _mentions_retired_flag(ast.parse(source))
        assert found, f"a decision reading a flag was not noticed:\n{source}"
        assert all(how == "reads" for _, _, how in found), found

    # And the shapes that are not a run's state, so the check is not simply "any attribute".
    for source in (
        "class P:\n    def __init__(self):\n        self._finished_generation = 0\n",
        "class P:\n    def __init__(self):\n        self._user_on_error = None\n",
        "class P:\n    def _show(self):\n        self._finished_thing = True\n",
    ):
        assert _assigned_flags(ast.parse(source)) == [], f"over-matched:\n{source}"


def test_the_state_flag_checker_ignores_the_name_in_prose() -> None:
    """A docstring that explains what the flag used to be is a :class:`ast.Constant`."""
    documented = (
        '"""There is no _finished flag; the child holds the state."""\n'
        "# A comment about _finished_flag and _running.\n"
        "NAMES = ('_finished', '_running', '_cancelled')\n"
    )
    assert _assigned_flags(ast.parse(documented)) == []
