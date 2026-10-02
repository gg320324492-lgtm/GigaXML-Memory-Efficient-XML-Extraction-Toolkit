"""The command line is stable from 2.0, so the surface it promises is written down here.

**Why this file exists at all.** 2.0 declares the command line -- commands, flags, exit
codes -- stable. Before this, nothing checked it: ``tests/golden/`` freezes what the
commands *print*, ``test_exit_codes.py`` pins what they *return*, and ``--help`` is only
asserted not to crash. **No test asserted that ``--checkpoint-every`` still existed.** A
promise nobody can check is a wish, and a wish written in a release note is the kind of
thing this project has spent three milestones removing.

**What is pinned, and what it means.** For every command, and for the top-level parser:
its name, each option string, whether that option takes a value, and whether it is
required. That is the *calling convention* -- enough that a script written against 2.0
still parses, and enough that silently turning a flag into a positional, or a required
option into an optional one, fails here rather than in somebody's shell.

**What is deliberately not pinned, because none of it is a break.** Help text is prose:
rewording it is not a compatibility change, and freezing it would make this file a place
where a typo has to be argued about. **Defaults** are not frozen either, and that is a
judgement rather than an oversight -- a default is what a user depends on most and
changes least, so changing one deserves more thought than this test can apply.
:doc:`/VERSIONING.md` lists neither as a stable surface for the same reason. Exit codes,
which *are* stable, are pinned where they belong, in ``test_exit_codes.py``.

**Changing this table is allowed.** It records a decision; it is not a wall. When a flag
genuinely has to go, change :data:`CLI_SURFACE` in the same commit and say in that
commit's message why a stable promise was broken and what a user has to do instead --
which is the only thing that makes it a decision rather than drift. Nothing stops a
change here; a test that could only be satisfied by never being wrong would be a test
nobody trusts.
"""

from __future__ import annotations

import argparse
from typing import Any

from gigaxml.cli import build_parser

#: ``True`` marks an option that is required. Whether an option takes a value is written
#: into its spelling -- ``"--json (no value)"`` rather than a second table -- so the
#: pinned surface reads the way a command line does, and a diff of it shows what a change
#: did rather than that something in a helper moved.
CLI_SURFACE: dict[str, Any] = {
    "options": {"--help (no value)": False, "--version (no value)": False, "-h (no value)": False},
    "commands": {
        "extract": {
            "--batch-size": False,
            "--checkpoint-every": False,
            "--config": True,
            "--format": False,
            "--help (no value)": False,
            "--output": True,
            "--progress (no value)": False,
            "--progress-every": False,
            "--report": False,
            "--resume (no value)": False,
            "-c": True,
            "-h (no value)": False,
            "-o": True,
            "source": "<source>",
        },
        "generate": {
            "--help (no value)": False,
            "--namespace": False,
            "--output": True,
            "--seed": False,
            "--size": True,
            "-h (no value)": False,
            "-o": True,
        },
        "inspect": {
            "--candidate": False,
            "--generate-config": False,
            "--help (no value)": False,
            "--infer-types (no value)": False,
            "--json (no value)": False,
            "--max-depth": False,
            "--max-paths": False,
            "-h (no value)": False,
            "source": "<source>",
        },
        "sample": {
            "--batch-size": False,
            "--checkpoint-every": False,
            "--config": True,
            "--format": False,
            "--help (no value)": False,
            "--limit": True,
            "--output": True,
            "--report": False,
            "--resume (no value)": False,
            "-c": True,
            "-h (no value)": False,
            "-n": True,
            "-o": True,
            "source": "<source>",
        },
    },
}


def _options(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """One parser's own options, as ``{spelling: required}``. Subparsers are left to the caller.

    **Every** spelling of an option is recorded, not just the first: ``--config`` and ``-c``
    are the same option, and a script is as likely to use the short one, so a rename that
    kept the long form would otherwise pass here while breaking half the callers. A
    positional is recorded under its name, with the value naming what has to be given.
    """
    found: dict[str, Any] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        if not action.option_strings:
            found[action.dest] = f"<{action.dest}>"
            continue
        takes_value = action.nargs != 0
        for option in action.option_strings:
            found[option if takes_value else f"{option} (no value)"] = bool(action.required)
    return dict(sorted(found.items()))


def _commands(parser: argparse.ArgumentParser) -> dict[str, dict[str, bool]]:
    """Each subcommand's own options. No command has a subcommand, so this stops here."""
    found: dict[str, dict[str, bool]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                found[name] = _options(sub)
    return {name: found[name] for name in sorted(found)}


def _actual(parser: argparse.ArgumentParser) -> dict[str, Any]:
    """The surface the parser describes now, in the shape :data:`CLI_SURFACE` uses."""
    return {"options": _options(parser), "commands": _commands(parser)}


def test_the_commands_and_their_options_are_what_a_script_was_written_against() -> None:
    """The whole surface at once, so a removal names itself in the failure."""
    assert _actual(build_parser()) == CLI_SURFACE


def test_the_commands_themselves_are_stable() -> None:
    """Named apart from their options, because losing a command loses everything under it.

    Separate because the two failures mean different things: a renamed flag has a
    migration and a removed command does not, and the sentence a user reads should not
    make them work out which happened.
    """
    assert sorted(_actual(build_parser())["commands"]) == [
        "extract",
        "generate",
        "inspect",
        "sample",
    ]


def test_the_options_every_command_accepts_are_present() -> None:
    """Per command, so a red test says *which* command lost a flag.

    Redundant on purpose. The table comparison above fails too, but it prints both sides
    of a large dict and the reader has to find the difference inside it; this prints the
    command's own missing and unexpected names, which is the sentence a commit message
    needs.
    """
    actual = _actual(build_parser())

    for command, pinned in CLI_SURFACE["commands"].items():
        missing = sorted(set(pinned) - set(actual["commands"][command]))
        unexpected = sorted(set(actual["commands"][command]) - set(pinned))
        assert not missing, f"{command} no longer accepts: {', '.join(missing)}"
        assert not unexpected, f"{command} now also accepts: {', '.join(unexpected)}"


def test_a_command_cannot_lose_the_option_a_script_asks_for_by_accident() -> None:
    """The option most likely to be broken by a refactor, asserted by name.

    ``--checkpoint-every`` is the one here because it is what makes ``--resume`` mean
    anything, it is the flag whose removal would break the most scripts per line of code
    removed, and it has already been rewritten twice in this repository's history. Naming
    it costs one line and makes the removal impossible to do quietly.
    """
    for command in ("extract", "sample"):
        assert "--checkpoint-every" in _actual(build_parser())["commands"][command]
    assert "--resume (no value)" in _actual(build_parser())["commands"]["extract"]
