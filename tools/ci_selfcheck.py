"""Prove that the Windows and macOS legs of CI are really running tests, and that the
release chain is pinned to code nobody can change underneath it.

★ **What this is for.** A matrix row is a claim, not a fact. ``runs-on: windows-latest``
on a job whose pytest step has been narrowed to one file still types green, and the only
thing that changed is that a platform nobody checks any more looks checked. Three of this
project's eight "green here, red in CI" incidents were of exactly that shape -- a
suspicion, an environment difference, and a suite that never ran -- so this milestone
adds the machine checks that make the claim falsifiable.

Six subcommands, and each answers a different way the claim could be false:

``shape``
    Static. Reads ``.github/workflows/*.yml`` and asserts the properties a matrix is not
    allowed to have: no ``continue-on-error``, no condition on any step that runs tests,
    no ``--ignore`` in a pytest command, one shared list of test directories across every
    job, exactly one coverage gate, and ``performance-baseline`` still a single
    ubuntu job. Nothing here needs a runner, so it runs in the ordinary pull request.

``platform --job NAME``
    Runtime, and it closes the loop the other two cannot. It reads the job's declared
    ``runs-on`` out of the workflow and fails unless *this interpreter* is that platform.
    A leg that has quietly been pointed at the wrong image says so here rather than
    quietly passing. It also prints the machine facts the report cannot see -- whether
    the filesystem folds case, whether symlinks can be created, which Qt platform
    plugins the wheel shipped -- because those are the three things that decide whether a
    platform-specific test can run at all, and on a runner they are not the same as on the
    machine that wrote the test.

``report FILE``
    Runtime. Reads pytest's own ``--junit-xml`` output and asserts the run was real: a
    floor on the number of tests, no failures or errors, a ceiling on skips, and -- the
    part that earns its keep -- that the tests which exist *only* to prove a Windows
    behaviour actually ran on Windows and actually skipped elsewhere. That last assertion
    fails in both directions on purpose: it catches a Windows leg that skips its way to
    green, and it catches a platform guard that stopped guarding.

``pins`` [--verify-remote]
    Static, plus optionally the network. M15's half. Two third-party actions sit on the
    release chain with the rights to publish, and both are referenced by a *moving* ref --
    one by a tag, one by a branch, which is worse because a branch can be force-pushed with
    no history and no notice. ``pins`` reads every ``uses`` out of every workflow and
    requires that anything not under GitHub's own ``actions/`` namespace is pinned to a
    40-character commit SHA *and* carries a comment naming the release that SHA came from.
    The comment is a YAML comment, so the parser cannot see it and this tool reads it off
    the line as well. With ``--verify-remote`` it then asks each remote, with
    ``git ls-remote``, whether that release really is that commit -- which is what turns a
    label from a comment into a fact. A network failure is reported as the check failing
    to run, exit 2, and never as a defect in the workflow.

``tracked``
    Static, and about the repository rather than the workflow. A path that ``.gitignore``
    excludes is meant to be invisible here, and ``git add -f`` is the one command that
    makes it visible anyway -- so the convention is only as strong as the next time
    somebody reaches for it. Three milestone reports reached for it, and ``.gitignore``
    says in as many words that the friction is the point. This asks ``git check-ignore``
    which of its rules each tracked path matches, and fails when the rule that matched is
    a committed ``.gitignore`` and not a machine-local exclude file. The distinction is not
    a detail: see the note in ``check_tracked`` for why the naive form of this check is red
    on a healthy repository.

``deps``
    Static, and the newest. Every job that runs a ``tools/*.py`` script must install what
    that script imports -- the defect that killed the second 2.0 release candidate, because
    the ``release`` job runs only on a tag and nothing in a pull request could see what it
    needed. It is a subset of ``shape`` (which calls the same function) with a readable
    table in front of the verdict, so a green run shows what was checked. See
    ``check_python_dependencies`` for exactly what it does and does not cover.

None of this replaces pytest's exit code, and none of it makes a failure quieter. It adds
the two things an exit code cannot say: *how much* ran, and *on what*.
"""

from __future__ import annotations

import argparse
import ast
import locale
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Any

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"

#: ``runs-on`` label -> the ``sys.platform`` value a runner on that image reports.
#:
#: The mapping is explicit rather than a prefix test because ``macos-latest`` and
#: ``darwin`` are not one another's substrings in any useful direction, and because a
#: comparison that silently fails to match would pass -- the failure mode this whole tool
#: exists to prevent.
RUNS_ON_TO_SYS_PLATFORM = {
    "windows": "win32",
    "ubuntu": "linux",
    "macos": "darwin",
}

#: Tests that exist to prove a Windows-only behaviour, and therefore can only *run* on
#: Windows. They skip everywhere else by design.
#:
#: ★ This list is the load-bearing part of the milestone and it is maintained by hand, which
#: is a cost with a purpose: renaming one of these tests now fails CI with a message
#: naming it, rather than quietly removing the only assertion that the Windows leg
#: exercises anything Windows-specific. A list that updated itself would be a list nobody
#: reads.
#:
#: ★ Which is also why an entry here is a claim to be argued, not a thing to add to make a
#: leg green. It carried seven until 2026-10-03, when the run self-check failed on every
#: non-Windows leg with five of them: those five *ran* on linux rather than skipping. Each
#: was judged individually, and each came off -- not as a way of silencing the message, but
#: because the platform claim in the entry was false:
#:
#: * ``test_interrupt_report.py::test_a_failed_run_still_says_failed`` and
#:   ``::test_a_finished_run_still_says_ok`` send no signal at all. They start the CLI, let
#:   it finish, and read the report. Nothing in either is a Windows operation, so "can only
#:   run on Windows" was never true of them; they look like they were listed by file rather
#:   than by what they do.
#: * ``test_interrupt_report.py::test_a_stopped_run_writes_a_report_that_says_it_was_
#:   interrupted`` and ``::test_the_numbers_in_that_report_are_the_ones_on_disk`` do send a
#:   signal, but through helpers that branch on the platform deliberately: ``_command``
#:   wraps the child in the ``SetConsoleCtrlHandler`` shim on win32 and leaves it alone
#:   elsewhere, ``_start`` sets ``CREATE_NEW_PROCESS_GROUP`` only on win32, and ``_stop``
#:   sends ``CTRL_C_EVENT`` on win32 and ``os.kill(pid, SIGINT)`` everywhere else. That is
#:   the signature of a test written to run on both, not of one that could not.
#: * ``test_gui_history.py::test_a_run_the_cli_stopped_is_shown_as_interrupted_and_can_be_
#:   continued`` inlines the same three branches over its own child. It is not one of the
#:   tests that go through ``_terminate``, which is the actual Windows-only helper in that
#:   file and whose ``pytest.skip("TerminateProcess is a Windows call")`` is the reason
#:   seven *other* tests skip on a linux leg.
#:
#: Two independent facts back this up, and neither is "CI was red". ``git log -p --follow``
#: over both files shows **no skip marker has ever existed on any of the five**: not in the
#: revision that introduced ``test_interrupt_report.py`` (d21ce8e) and not in any later one,
#: and the single marker in ``test_gui_history.py`` has sat on ``_terminate``'s line 104
#: since that file's first revision. So this was never a guard that went missing; the tests
#: were cross-platform from the start and the list was wrong about them. And the 2026-10-02
#: CI run measured the consequence: on the linux legs all five ran, none appeared among the
#: skips, and neither appeared among the failures -- they passed.
#:
#: Gating them would have been the other available fix and would have been worse: it deletes
#: the only end-to-end coverage of "a signal arrives and the run writes a report saying so"
#: on two of the three platforms the product ships binaries for, and it does it to satisfy a
#: list. The platform legs exist to find differences *between* platforms; a test of this
#: shape is exactly the kind that finds them.
#:
#: ★ What is left is what the description above actually names: two tests whose bodies make
#: a call POSIX does not have. Both begin with
#: ``pytest.skip("only Windows refuses os.replace while the target is open")``, and both
#: were skipped on every linux leg measured -- the list, the code and the run agree.
WINDOWS_ONLY_TESTS = (
    "tests/integration/test_atomic_output.py::test_a_target_held_open_is_one_clear_error_and_the_partial_survives",
    "tests/integration/test_atomic_output.py::test_the_held_open_error_names_the_partial_file",
)

#: The test directories every job runs. Asserted identical everywhere, so a platform leg
#: cannot quietly end up covering less than the ubuntu leg.
TEST_DIRECTORIES = (
    "tests/unit",
    "tests/integration",
    "tests/golden",
    "tests/security",
    "tests/property",
)


class SelfCheckError(Exception):
    """A claim the workflow makes, which the machine does not support."""


class RemoteUnreachableError(Exception):
    """The version could not be resolved, because the network was not there.

    ★ Deliberately not a ``SelfCheckError``. The two mean opposite things and the log has
    to keep them apart: "this workflow is wrong" is a defect to fix, "this check could not
    reach github.com" is infrastructure, and a guard that reports the second in the voice
    of the first trains people to ignore it. It exits 2, which is the code this tool has
    always used for "could not run" -- never 1.
    """


# --- workflow reading -------------------------------------------------------------


def load_workflows() -> dict[str, dict[str, Any]]:
    """Every workflow file, keyed by path, as parsed YAML."""
    loaded: dict[str, dict[str, Any]] = {}
    for path in sorted(WORKFLOW_DIR.glob("*.yml")):
        parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise SelfCheckError(f"{path.name}: the top level is not a mapping")
        loaded[path.name] = parsed
    if not loaded:
        raise SelfCheckError(f"no workflow files under {WORKFLOW_DIR}")
    return loaded


def base_name(label: str) -> str:
    """``test [python-version=3.13]`` -> ``test``: the job name as the workflow spells it."""
    return label.split(" [")[0]


def _label(job_name: str, values: dict[str, Any]) -> str:
    """A short, readable identification of one expanded job.

    Only the matrix key that decides *where it runs* is shown. A package build's include
    entry carries six keys, and a table cell that wraps is a table nobody reads -- which
    would make this lister decorative, and a decorative job list is worse than none.
    """
    if not values:
        return job_name
    platform_key = next((k for k in ("os", "runs-on", "python-version") if k in values), None)
    if platform_key is not None:
        return f"{job_name} [{platform_key}={values[platform_key]}]"
    first = sorted(values.items())[0]
    return f"{job_name} [{first[0]}={first[1]}]"


def steps_of(job: dict[str, Any]) -> list[dict[str, Any]]:
    raw = job.get("steps") or []
    return [step for step in raw if isinstance(step, dict)]


def run_text(step: dict[str, Any]) -> str:
    return str(step.get("run") or "")


def runs_pytest(step: dict[str, Any]) -> bool:
    """Does this step invoke pytest?

    ``python -m pytest`` and a bare ``pytest`` both count; a step that only mentions the
    word in a comment does not, which is why the test looks for the invocation rather
    than the substring.
    """
    text = run_text(step)
    return "pytest" in text and (" pytest" in text or text.strip().startswith("pytest"))


def runs_selfcheck(job: dict[str, Any], subcommand: str) -> bool:
    return any(f"ci_selfcheck.py {subcommand}" in run_text(step) for step in steps_of(job))


def expand(job_name: str, job: dict[str, Any]) -> list[dict[str, Any]]:
    """Every concrete job the matrix produces, as ``{key: value}`` dicts.

    ``include`` and ``exclude`` are expanded rather than refused, because
    ``package.yml`` uses ``matrix.include`` for its three-way build and a self-check that
    cannot read the repository it checks is worse than none.

    The algorithm follows GitHub's documented order: build the cartesian product of the
    ordinary keys, drop anything an ``exclude`` matches, then apply each ``include`` in
    turn -- merged into the first combination it does not *overwrite an original* value
    of, and otherwise appended as a combination of its own.

    ★ What is not implemented: a plain-list matrix with an ``include`` that merges into
    several combinations at once, and expression-valued matrix keys. Neither shape occurs
    here, and both are refused below rather than approximated -- a lister that prints a job
    list GitHub would not agree with is the exact failure this tool exists to catch, and it
    would catch it in the wrong direction.
    """
    raw = ((job.get("strategy") or {}).get("matrix")) or {}
    if any(key in raw for key in raw if not isinstance(raw[key], (list, dict))):
        raise SelfCheckError(f"job {job_name!r}: unsupported strategy.matrix key")

    ordinary = {
        k: v for k, v in raw.items() if isinstance(v, list) and k not in ("include", "exclude")
    }
    includes = raw.get("include") or []
    excludes = raw.get("exclude") or []

    keys = sorted(ordinary)
    combinations: list[dict[str, Any]] = [{}]
    for key in keys:
        combinations = [{**base, key: value} for base in combinations for value in ordinary[key]]

    for exclusion in excludes:
        if not isinstance(exclusion, dict):
            raise SelfCheckError(f"job {job_name!r}: matrix.exclude entries must be mappings")
        combinations = [
            combination
            for combination in combinations
            if not all(combination.get(k) == v for k, v in exclusion.items())
        ]

    for addition in includes:
        if not isinstance(addition, dict):
            raise SelfCheckError(f"job {job_name!r}: matrix.include entries must be mappings")
        for combination in combinations:
            if any(key in combination for key in addition):
                continue
            combination.update(addition)
            break
        else:
            combinations.append(dict(addition))

    if not combinations:
        raise SelfCheckError(f"job {job_name!r}: its matrix expands to no jobs at all")
    return combinations


def declared_platform(runs_on: object) -> str | None:
    """The ``sys.platform`` a ``runs-on`` label promises, or None if it is not one of ours."""
    if not isinstance(runs_on, str):
        return None
    for label, value in RUNS_ON_TO_SYS_PLATFORM.items():
        if runs_on.startswith(label):
            return value
    return None


def job_listing() -> list[tuple[str, str, dict[str, Any], str]]:
    """(workflow, job name, matrix values, runs-on) for every job CI will run."""
    listing: list[tuple[str, str, dict[str, Any], str]] = []
    for workflow_name, workflow in load_workflows().items():
        jobs = workflow.get("jobs") or {}
        for job_name, job in jobs.items():
            for values in expand(job_name, job):
                runs_on = job.get("runs-on")
                if isinstance(runs_on, str) and "${{" in runs_on:
                    for key, value in values.items():
                        token = "${{ matrix." + key + " }}"
                        if token in runs_on:
                            runs_on = runs_on.replace(token, str(value))
                listing.append(
                    (
                        workflow_name,
                        _label(job_name, values),
                        values,
                        str(runs_on),
                    )
                )
    return listing


# --- shape -----------------------------------------------------------------------


def check_shape(workflow_name: str, job_name: str, job: dict[str, Any]) -> None:
    """Every property a test-running job is not allowed to have."""
    pytest_steps = [step for step in steps_of(job) if runs_pytest(step)]

    # 1. A condition on a step that runs tests is how a platform leg gets switched off
    #    without anybody deciding to switch it off.
    for step in pytest_steps:
        if "if" in step:
            raise SelfCheckError(
                f"{workflow_name}:{job_name}: the pytest step has an `if: {step['if']!r}`. "
                f"A platform leg that can be skipped is a platform leg nobody checks; if "
                f"this one is genuinely optional, delete the job instead."
            )

    # 2. A narrowed test selection is a claim about coverage that nothing verifies.
    for step in pytest_steps:
        text = run_text(step)
        narrowed = (
            "--ignore",
            "--deselect",
            "--ignore-glob",
            "-k ",
            "--lf",
            "--ff",
            "--last-failed",
        )
        for flag in narrowed:
            if flag in text:
                raise SelfCheckError(
                    f"{workflow_name}:{job_name}: the pytest step uses {flag!r}. A suite "
                    f"that runs less than it claims has the appearance of coverage and "
                    f"none of it, and nothing downstream can tell the difference."
                )

    # 3. The same directories everywhere. A platform leg covering less than ubuntu is
    #    the exact asymmetry this milestone exists to remove.
    if pytest_steps:
        for step in pytest_steps:
            missing = [d for d in TEST_DIRECTORIES if d not in run_text(step)]
            if missing:
                raise SelfCheckError(
                    f"{workflow_name}:{job_name}: the pytest step does not name {missing}. "
                    f"Every job runs {list(TEST_DIRECTORIES)}; a suite not written down "
                    f"here does not run."
                )

    # 4. And it leaves evidence that it ran.
    if pytest_steps and not runs_selfcheck(job, "report"):
        raise SelfCheckError(
            f"{workflow_name}:{job_name}: runs pytest but no step runs "
            f"`ci_selfcheck.py report`, so nothing records how much of the suite ran."
        )
    if pytest_steps and "--junit-xml" not in " ".join(run_text(s) for s in pytest_steps):
        raise SelfCheckError(
            f"{workflow_name}:{job_name}: runs pytest without `--junit-xml`, so the "
            f"report check has nothing to read."
        )

    # 5. A job that runs tests proves which machine it ran them on, and reports even
    #    when pytest failed. Scoped to pytest jobs on purpose: `package.yml` runs no tests,
    #    and a rule that reached into it would be a rule about a workflow this milestone
    #    was not asked to change.
    if pytest_steps and not runs_selfcheck(job, "platform"):
        raise SelfCheckError(
            f"{workflow_name}:{job_name}: runs on {job.get('runs-on')!r} but no step runs "
            f"`ci_selfcheck.py platform --job {job_name}`, so the job never checks that "
            f"the runner is the platform it claims."
        )
    report_steps = [s for s in steps_of(job) if "ci_selfcheck.py report" in run_text(s)]
    if report_steps and not any("always()" in str(s.get("if", "")) for s in report_steps):
        raise SelfCheckError(
            f"{workflow_name}:{job_name}: the report step is not `if: always()`, so it is "
            f"skipped exactly when it is needed -- which is the run that failed. A "
            f"diagnosis that only prints on success diagnoses nothing."
        )


def check_workflow_shape() -> tuple[list[str], list[tuple[str, str, dict[str, Any], str]]]:
    """All workflows at once. Returns the problems found and the expanded job list."""
    problems: list[str] = []
    listing = job_listing()
    coverage_jobs: list[str] = []
    perf_jobs: list[str] = []

    for workflow_name, workflow in load_workflows().items():
        jobs = workflow.get("jobs") or {}
        for job_name, job in jobs.items():
            # continue-on-error anywhere, at job or step level, including inside a
            # strategy block: it turns a red into a "known failure" that nobody reads.
            if job.get("continue-on-error"):
                problems.append(
                    f"{workflow_name}:{job_name}: `continue-on-error` on the job. A "
                    f"platform leg that may fail is a platform leg that will."
                )
            for step in steps_of(job):
                if step.get("continue-on-error"):
                    problems.append(
                        f"{workflow_name}:{job_name}: `continue-on-error` on the step "
                        f"{step.get('name', step.get('run', '')[:40])!r}."
                    )
            try:
                check_shape(workflow_name, job_name, job)
            except SelfCheckError as exc:
                problems.append(str(exc))

            if any("--cov-fail-under" in run_text(step) for step in steps_of(job)):
                coverage_jobs.append(job_name)
            if any("perf_baseline.py" in run_text(step) for step in steps_of(job)):
                perf_jobs.append(job_name)

    # Criterion E, made mechanical: the performance job stays single and single-platform.
    for workflow_name, workflow in load_workflows().items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            if job_name in perf_jobs:
                runs_on = str(job.get("runs-on"))
                if declared_platform(runs_on) != "linux":
                    problems.append(
                        f"{workflow_name}:{job_name}: the performance job runs on "
                        f"{runs_on!r}. It measures throughput, and a matrix across "
                        f"platforms or interpreters measures the machine instead."
                    )
                # A matrix under a fixed `runs-on` reads as harmless -- the label does not
                # change -- while quietly producing N jobs that each measure a different
                # machine and are compared against one baseline. The label alone will not
                # catch it, so the matrix itself is refused here.
                if len(expand(job_name, job)) != 1:
                    problems.append(
                        f"{workflow_name}:{job_name}: the performance job has a matrix, so "
                        f"it expands to {len(expand(job_name, job))} jobs against one "
                        f"baseline. Criterion E is that it stays a single job."
                    )
    if len(perf_jobs) != 1:
        problems.append(
            f"expected exactly one job running benchmarks/perf_baseline.py, found "
            f"{perf_jobs or 'none'}"
        )
    if len(coverage_jobs) != 1:
        problems.append(
            f"expected exactly one job with a --cov-fail-under gate, found "
            f"{coverage_jobs or 'none'}. Coverage is a whole-tree claim, so exactly one "
            f"job may assert it."
        )

    windows = [n for _w, n, _v, ro in listing if declared_platform(ro) == "win32"]
    macos = [n for _w, n, _v, ro in listing if declared_platform(ro) == "darwin"]
    if not windows:
        problems.append("no job runs on windows-latest: the platform is claimed nowhere")
    if not macos:
        problems.append("no job runs on macos-latest: the platform is claimed nowhere")

    problems.extend(check_release_artifacts())
    problems.extend(check_python_dependencies())

    return problems, listing


# --- third-party imports, per job --------------------------------------------------

#: ``pip install`` requirement name -> the module name it puts on ``sys.path``.
#:
#: ★ This table exists because the two names are genuinely different and nothing derives
#: one from the other: ``pip install pyyaml`` gives ``import yaml``, ``pip install pillow``
#: gives ``import PIL``, and a check that compared requirement names to import names
#: without knowing this would call every one of them missing. It is written out rather
#: than read from installed metadata (``top_level.txt``) because this check runs in the
#: ``ci-shape`` job, which installs almost nothing on purpose -- asking the machine what a
#: package is called would make the check depend on the very thing it is checking for.
#:
#: Only distributions this repository installs appear here. A requirement that is not in
#: the table is not silently assumed to provide nothing: it contributes its own normalised
#: name as a candidate module name, so ``pip install foo`` covers ``import foo`` and the
#: check stays useful for a dependency added tomorrow without a table edit. What it cannot
#: do is know that ``foo`` also provides ``bar`` -- and that is reported, not guessed at.
DISTRIBUTION_MODULES: dict[str, tuple[str, ...]] = {
    "pyyaml": ("yaml",),
    "pillow": ("PIL",),
    "pyside6": ("PySide6",),
    "pytest-qt": ("pytestqt",),
    "pytest-cov": ("pytest_cov",),
    "pyinstaller": ("PyInstaller",),
    "pyarrow": ("pyarrow",),
    "cyclonedx-bom": ("cyclonedx", "cyclonedx_py"),
}


def normalise_requirement(requirement: str) -> str:
    """``PyYAML>=6.0`` -> ``pyyaml``; the comparison key for a requirement name.

    PEP 503, the same reduction ``check_sbom.normalise`` makes. Written out for the reason
    that one gives: a check whose correctness depends on a shared helper breaks when the
    helper moves, and this is four characters of ``re``.
    """
    name = re.split(r"[<>=!~\[;]", requirement.strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def third_party_imports(path: pathlib.Path) -> tuple[set[str], set[str]]:
    """The modules ``path`` imports that are neither the standard library nor this project.

    Returns ``(required, optional)``. A module is *optional* when every import of it sits
    inside a ``try:`` whose handler recovers rather than giving up -- the PySide6 import in
    ``qt_facts``, which ``ci-shape`` must not be required to install. See
    ``_guarded_import_lines`` for why "guarded" alone is not enough.

    ★ Parse, not grep. A string search for ``import`` finds the word in a docstring, which
    is how ``check_comments.py``'s prose about ``import`` would be read as a dependency on
    a module called ``(``. ``ast`` sees imports and only imports, and it sees an import
    written inside a function as well as one at the top -- which matters here, because
    ``ci_selfcheck.py`` imports PySide6 inside ``qt_facts`` and a top-level-only scan would
    miss exactly the optional dependency most likely to be dropped by accident.

    What it does *not* see: ``importlib.import_module("x")`` and anything else computed at
    run time. That gap is stated in ``check_python_dependencies`` rather than closed,
    because closing it would need a name this check cannot resolve statically.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stdlib = sys.stdlib_module_names
    guarded = _guarded_import_lines(tree)
    required: set[str] = set()
    optional: set[str] = set()
    for node in ast.walk(tree):
        names: set[str] = set()
        if isinstance(node, ast.Import):
            names = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            # ``from . import x`` and ``from .foo import x`` are this package's own;
            # ``node.level`` is how many dots, and a relative import has no module name to
            # look up.
            names = {node.module.split(".")[0]}
        ignore = {"__future__", "tools", "tests", "gigaxml"} | set(stdlib)
        for name in names:
            if name in ignore:
                continue
            (optional if node.lineno in guarded else required).add(name)
    return required, optional


def _guarded_import_lines(tree: ast.AST) -> set[int]:
    """The line numbers of imports a script has agreed it can run *without*.

    ★ Without this the check is red on a healthy repository, and a guard that is always red
    is a guard that gets deleted. ``ci_selfcheck.py`` imports PySide6 inside a
    ``try: ... except Exception:`` -- deliberately, because ``qt_facts`` runs on the
    ``ci-shape`` job, which installs no Qt and must not need to. An import the script has
    already agreed may fail is not a dependency the job has to satisfy; reporting it as one
    would make the check demand a 200 MB wheel for a static YAML check.

    ★ **But "guarded" is not the same as "optional", and conflating the two is how this
    check would have gone blind.** ``make_icon.py`` guards its Pillow import the same way --
    ``except ImportError:`` -- and then *exits 2*. It is a hard requirement wearing a
    guard's clothes, and a rule that read any guard as "optional" would stop watching the
    one build input whose absence the build job is set up to catch. So the two cases are
    told apart by what the handler does: a handler that raises, or calls ``sys.exit``, has
    decided the absence is fatal and the import is required; a handler that does anything
    else has decided to carry on.

    The test is deliberately narrow. It does not try to decide whether the *script* later
    fails for want of the module, only whether the handler itself gives up. Where it cannot
    tell, it errs towards required -- a false positive a reader can see and argue with,
    never a false negative that lets the release job die on a tag.
    """
    import_failure = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if not (
                handler.type is None
                or (isinstance(handler.type, ast.Name) and handler.type.id in import_failure)
                or (
                    isinstance(handler.type, ast.Tuple)
                    and any(
                        isinstance(elt, ast.Name) and elt.id in import_failure
                        for elt in handler.type.elts
                    )
                )
            ):
                continue
            if _gives_up(handler):
                continue  # the absence is fatal: this import is a requirement
            for child in ast.walk(node):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    lines.add(child.lineno)
    return lines


def _gives_up(handler: ast.ExceptHandler) -> bool:
    """Does this ``except`` body end the script rather than recover from it?

    ``raise ...`` (including ``raise SystemExit``) and ``sys.exit(...)`` both count. A bare
    ``return`` does not, because ``ci_selfcheck.qt_facts`` returns a facts dict with an
    ``error`` key and its caller carries on -- and because a ``return`` that happens to be
    the last statement of a required import is still a recoverable-looking shape, which is
    the direction this errs.
    """
    for child in ast.walk(handler):
        if isinstance(child, ast.Raise):
            return True
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == "exit"
            and isinstance(child.func.value, ast.Name)
            and child.func.value.id == "sys"
        ):
            return True
    return False


def python_scripts_in(step: dict[str, Any]) -> set[pathlib.Path]:
    """Every repository Python file a step's ``run:`` names, resolved to a path.

    Three shapes are recognised, and each is one this workflow really uses:
    ``python -m tools.make_icon``, ``python tools/release_checksums.py`` and a bare
    ``python -m tools.spec_check``. ``python -c "..."`` and a heredoc are not followed --
    an inline program is not a file whose imports can be read, and pretending to check it
    would be the vacuous check this one is written to avoid.

    Returns ``tools/`` and ``src/`` files that exist. A name that does not resolve is
    reported by the caller rather than dropped: a step that runs a script which is not
    there is a failure this check should not be able to see past.
    """
    text = run_text(step)
    names: set[str] = set()
    for match in re.finditer(r"-m\s+tools\.([A-Za-z_][\w.]*)", text):
        names.add("tools/" + match.group(1).replace(".", "/") + ".py")
    for match in re.finditer(r"(?<![\w./-])(tools/[\w./-]+\.py)", text):
        names.add(match.group(1))
    return {REPO_ROOT / name for name in sorted(names)}


def _installs_into_this_interpreter(line: str) -> bool:
    """Is this ``pip install`` line aimed at the job's own interpreter?

    ★ This is the whole of the environment model, and it is one rule rather than none. The
    invocations that reach the job's interpreter are ``pip install ...`` and
    ``python -m pip install ...``. An interpreter spelled with a directory in front of it
    -- ``"$RUNNER_TEMP/sbomenv/bin/python" -m pip``, ``"$RUNNER_TEMP/auditenv/bin/python"``
    -- is a venv's, and what it installs is not on the path of the step that runs
    ``tools/release_checksums.py`` afterwards. A bare word ending in ``pip`` after a slash
    (``.../bin/pip``) is the same situation.

    Conservative in the direction that keeps the check awake: a line this cannot classify is
    treated as *not* the job's environment, so an install it fails to recognise shows up as a
    missing dependency rather than silently satisfying one.
    """
    prefix = line.split("-m pip", 1)[0].split("pip install", 1)[0]
    tokens = [t.strip("\"'") for t in prefix.split() if t not in {"-", "|", "&&", ";"}]
    # Drop the `-m`/`pip`-leading tokens if the prefix is `pip install` itself.
    if not tokens:
        return True
    return all(token in {"python", "python3", "pip", "pip3", "-m"} for token in tokens)


def installed_requirements(job: dict[str, Any]) -> set[str]:
    """The normalised requirements a job installs **into the interpreter its steps run**.

    ★ The environment matters, and getting it wrong made this check blind to the exact
    defect it was written for. The release job has an ``Install`` step that runs
    ``python -m pip install pyyaml``, and an SBOM step that creates a throwaway venv and
    runs ``"$RUNNER_TEMP/sbomenv/bin/python" -m pip install .``. Both are ``pip install``
    lines in the same job, and they install into *different interpreters* -- so merging
    them let the venv's ``pip install .`` (which pulls PyYAML from ``pyproject.toml`` as a
    runtime dependency) stand in for the job having PyYAML. Measured: with the ``install``
    line deleted, a version of this function that merged everything still reported
    ``release_checksums.py`` as satisfied. A check that answers "did anything in this job
    install this" instead of "did *this interpreter* get this" passes on the broken tree.

    So an install counts only when its pip is the job's own: ``pip``, ``python -m pip``, or
    a bare ``python`` -- the last two being how every step in this repository invokes it.
    A path-qualified interpreter (``$RUNNER_TEMP/...``, ``../venv/bin/python``) is a
    different environment and is skipped, as is a ``venv``-relative ``bin/pip``.
    """
    requirements: set[str] = set()
    for step in steps_of(job):
        for line in run_text(step).splitlines():
            # ★ Strip the shell comment first. A `run: |` block is full of prose -- the
            # release job has a paragraph inside its SBOM step -- and reading it as pip
            # arguments produced a requirement set containing the words "it", "is" and
            # "user". A check whose evidence is nonsense cannot be argued with, whether or
            # not its verdict happens to be right.
            stripped = line.split("#", 1)[0].strip()
            if "pip install" not in stripped and "pip3 install" not in stripped:
                continue
            if not _installs_into_this_interpreter(stripped):
                continue
            payload = stripped.split("pip install", 1)[1].split("pip3 install", 1)[-1]
            # `-e` takes the next token as its argument; splitting it off keeps the `-e`
            # flag from being read as a requirement named "-e".
            payload = re.sub(r"(?<!\S)-e(?=\s)", "", payload)
            for token in payload.split():
                token = token.strip("\"'")  # `.[dev,gui]` is usually written quoted
                if not token or token.startswith("-"):
                    continue  # flags like --upgrade, --quiet, and a `grep` after a pipe
                if token in {"|", ">", ">>", "&&", ";"}:
                    continue
                if token in {".", "./"}:
                    # A plain local install: the runtime dependencies, no extras.
                    requirements |= runtime_requirements()
                    continue
                if token.startswith("."):
                    # `.[dev,gui]` -- the project itself with extras. Only the extras name
                    # requirements; the leading `.` is the path to install and normalises
                    # to `-`, which is not a package and must not be added as one.
                    for extra in re.findall(r"\[([^\]]*)\]", token):
                        requirements |= extras_of(extra)
                    continue
                for extra in re.findall(r"\[([^\]]*)\]", token):
                    requirements |= extras_of(extra)
                requirements.add(normalise_requirement(token))
    return requirements


def project_extras() -> dict[str, set[str]]:
    """``pyproject.toml``'s ``[project.optional-dependencies]``, normalised.

    Read rather than written down, for the reason the release globs are: a second copy of
    what ``dev`` contains is a copy that is correct until somebody adds a dependency to it,
    and the day it is wrong the check reports a package the job does install.
    """
    raw = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras: dict[str, set[str]] = {}
    for name, requirements in (raw.get("project", {}).get("optional-dependencies") or {}).items():
        extras[name] = {normalise_requirement(r) for r in requirements}
    return extras


def extras_of(extra_name: str) -> set[str]:
    """``dev,gui`` -> every requirement those extras pull in, plus the base install.

    ``.`` alone means the runtime dependencies, so ``-e ".[dev]"`` is the runtime set plus
    ``dev``. The base set is added by the caller through the literal ``.`` token, which
    normalises to ``.`` -- so it is spelled out here instead: an extras request always
    brings the runtime dependencies with it, and leaving that implicit is how a check comes
    to say a package is missing from a job that installs it.
    """
    extras = project_extras()
    wanted: set[str] = set()
    for name in (part.strip() for part in extra_name.split(",")):
        if name:
            wanted |= extras.get(name, set())
    wanted |= runtime_requirements()
    return wanted


def runtime_requirements() -> set[str]:
    """``pyproject.toml``'s ``[project.dependencies]``, normalised."""
    raw = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {normalise_requirement(r) for r in raw.get("project", {}).get("dependencies") or []}


def modules_provided(requirement: str) -> set[str]:
    """The module names a normalised requirement could put on ``sys.path``.

    The table where one is known, and the requirement's own name otherwise. The fallback is
    what keeps this from being a list of two packages that only knows about today: a script
    that imports something whose distribution matches the module name -- the common case --
    is covered without an edit here.
    """
    known = DISTRIBUTION_MODULES.get(requirement)
    if known is not None:
        return set(known)
    return {requirement.replace("-", "_"), requirement}


def check_python_dependencies() -> list[str]:
    """Every third-party import on a job's Python path must be installed by that job.

    ★ **The defect this exists for.** ``package.yml``'s ``release`` job ran only on a tag,
    so nothing in an ordinary pull request could notice what it needed. It declared an
    interpreter and no dependencies, and the second release candidate of 2.0 died on
    ``ModuleNotFoundError: No module named 'yaml'`` -- from a script that had parsed fine in
    every pull request, on every developer's machine, because every one of them had PyYAML
    installed. The first milestone that added a Python step to that job declared the
    interpreter and trusted the rest to be there. This is the check that would have been red.

    ★ **What it covers.** Every job in every ``.github/workflows/*.yml``; every ``run:``
    step that invokes a ``tools/*.py`` script by ``-m tools.x`` or by path; the third-party
    modules that script imports, read with ``ast``; and whether the job's own ``run:`` steps
    install a requirement that provides each of them -- resolving ``.[extra]`` against
    ``pyproject.toml``.

    ★ **What it does not cover, stated rather than left to be discovered.** It follows
    imports in ``tools/`` and not in ``src/``: a job that runs ``gigaxml`` itself -- the CLI
    or the GUI -- has its imports in the package, and checking those means deciding which
    optional imports are required, which is the ``[project.optional-dependencies]`` question
    and not this one. It does not see a dependency reached through ``importlib``, a
    ``python -c`` program, or a script under a path other than ``tools/``. It is static: it
    compares what the job says it installs against what the tree says the script imports,
    so an install that fails at run time still passes here. Each of these is a gap in a
    check that answers one real question, not a way the check passes without answering it.

    A **guarded** import -- one inside a ``try:`` that catches ``ImportError`` -- is not
    required, because the script has already agreed it can run without it. That is the
    ``ci_selfcheck.py`` PySide6 import, and without this rule the check would fail the
    ``ci-shape`` job for not installing Qt, which is a false positive on a healthy
    repository. See ``_guarded_import_lines``.
    """
    problems: list[str] = []
    for workflow_name, workflow in load_workflows().items():
        for job_name, job in (workflow.get("jobs") or {}).items():
            steps = steps_of(job)
            if not any(python_scripts_in(step) for step in steps):
                continue
            installed = installed_requirements(job)
            provided: set[str] = set()
            for requirement in installed:
                provided |= modules_provided(requirement)
            for step in steps:
                for script in sorted(python_scripts_in(step)):
                    if not script.is_file():
                        problems.append(
                            f"{workflow_name}:{job_name}: step {step.get('name', '')!r} runs "
                            f"{script.relative_to(REPO_ROOT).as_posix()}, which is not in the "
                            f"tree. A check that cannot find the script cannot check what it "
                            f"imports."
                        )
                        continue
                    required, _optional = third_party_imports(script)
                    missing = sorted(required - provided)
                    if missing:
                        problems.append(
                            f"{workflow_name}:{job_name}: step {step.get('name', '')!r} runs "
                            f"{script.relative_to(REPO_ROOT).as_posix()}, which imports "
                            f"{', '.join(missing)}, and this job installs nothing that "
                            f"provides them (it installs: "
                            f"{', '.join(sorted(installed)) or 'nothing'}). This job would "
                            f"fail on the runner with ModuleNotFoundError -- and if it runs "
                            f"only on a tag, not until then."
                        )
    return problems


#: The two files M15 adds to every release, and the script that makes each.
RELEASE_ARTIFACTS = (
    ("SHA256SUMS.txt", "release_checksums.py"),
    ("gigaxml-sbom.json", "check_sbom.py"),
)


def check_release_artifacts() -> list[str]:
    """The release must produce the checksums and the SBOM, and must attach both.

    ★ Three separate claims, and each can be true while the other two are false:
    the file is generated, the file is attached, and the file is checked. Removing the
    generation step leaves a release page with no checksums and a green job; removing the
    line from ``files:`` leaves a checksum file generated, verified, and never shipped.
    Neither failure announces itself, which is the whole reason this check exists -- and
    the shape of it is the one this project has paid for repeatedly, including the four
    blank release pages that were generated correctly and attached to nothing.
    """
    problems: list[str] = []
    workflow = load_workflows().get("package.yml")
    if workflow is None:
        return ["package.yml is missing, so there is no release to check"]

    release = (workflow.get("jobs") or {}).get("release")
    if release is None:
        return ["package.yml has no `release` job"]

    steps = steps_of(release)
    create = next((s for s in steps if "action-gh-release" in str(s.get("uses", ""))), None)
    if create is None:
        return ["package.yml's release job has no step using action-gh-release"]
    attached = str((create.get("with") or {}).get("files", ""))
    everything = "\n".join(run_text(step) for step in steps)

    for filename, script in RELEASE_ARTIFACTS:
        if script not in everything:
            problems.append(
                f"package.yml:release: nothing runs tools/{script}, so {filename} is "
                f"either never produced or produced without anything checking it. A "
                f"release with no checksums and no SBOM still goes out green, and an "
                f"unchecked one looks exactly like a checked one from the log."
            )
        if filename not in attached:
            problems.append(
                f"package.yml:release: {filename} is not in the release step's `files:`, so "
                f"it is produced and never attached. `fail_on_unmatched_files` cannot catch "
                f"this -- it guards the other direction."
            )
    return problems


# --- platform --------------------------------------------------------------------


def filesystem_facts(directory: pathlib.Path) -> dict[str, object]:
    """The three filesystem properties that decide whether a platform test can run.

    Each is probed rather than inferred from ``os.name``, because the operating system and
    the filesystem it was installed on are two different claims -- a case-sensitive NTFS
    volume on Windows would fail the first and pass the second, and only a probe tells
    them apart.
    """
    probe = directory / "caseprobe.txt"
    probe.write_text("probe", encoding="utf-8")
    folds_case = (directory / "CASEPROBE.TXT").exists()
    probe.unlink()

    link = directory / "symprobe.txt"
    link.write_text("probe", encoding="utf-8")
    target = directory / "symprobe-target.txt"
    try:
        link.unlink()
        link.symlink_to(target.name)
        symlinks = True
    except (OSError, NotImplementedError, AttributeError):
        symlinks = False
    target.unlink(missing_ok=True)
    link.unlink(missing_ok=True)

    hard_target = directory / "hardprobe.txt"
    hard_target.write_text("probe", encoding="utf-8")
    hard_link = directory / "hardprobe-link.txt"
    try:
        os.link(hard_target, hard_link)
        hardlinks = True
    except (OSError, NotImplementedError, AttributeError):
        hardlinks = False
    hard_link.unlink(missing_ok=True)
    hard_target.unlink(missing_ok=True)

    return {
        "folds_case": folds_case,
        "can_symlink": symlinks,
        "can_hardlink": hardlinks,
        "executable_bit": os.access(directory / "nothing", os.X_OK) is not False,
        "path_separator": os.sep,
        "filesystem_encoding": sys.getfilesystemencoding(),
        "preferred_encoding": locale.getpreferredencoding(False),
    }


def qt_facts() -> dict[str, object]:
    """What the installed Qt can actually open a window with.

    ``pip install`` succeeding says the wheel unpacked. It does not say a platform plugin
    is in it, and a missing one turns every GUI test into a collection error rather than a
    visible failure -- which is the "a stage whose tests always skip has the appearance
    of coverage and none of it" failure, wearing a different hat.
    """
    facts: dict[str, object] = {"importable": False, "plugins": [], "offscreen": False}
    try:
        import PySide6
    except Exception as exc:
        facts["error"] = f"{type(exc).__name__}: {exc}"
        return facts
    facts["importable"] = True
    root = pathlib.Path(PySide6.__file__).parent
    # PySide6 6.8+ moved the plugin tree from Qt/plugins to plugins; both spellings are
    # checked because which one applies depends on the installed version and a check that
    # only knew one would report "no plugins" on a perfectly good install.
    for candidate in (root / "plugins" / "platforms", root / "Qt" / "plugins" / "platforms"):
        if candidate.is_dir():
            names = sorted(p.name for p in candidate.iterdir())
            facts["plugins"] = names
            facts["offscreen"] = any("offscreen" in name for name in names)
            break
    facts["version"] = getattr(PySide6, "__version__", "unknown")
    return facts


def check_platform(job_name: str) -> None:
    """Fail unless this interpreter is on the platform the named job declares."""
    wanted: str | None = None
    declared: str | None = None
    for _workflow, name, _values, runs_on in job_listing():
        base = base_name(name)
        if base == job_name:
            declared = declared_platform(runs_on)
            wanted = declared
    if wanted is None:
        raise SelfCheckError(
            f"no job named {job_name!r} in {WORKFLOW_DIR}. Job names: "
            f"{[base_name(n) for _w, n, _v, _r in job_listing()]}"
        )

    actual = sys.platform
    print("--- what this runner actually is -------------------------------")
    print(f"  sys.platform            {actual}")
    print(f"  platform.system()       {platform.system()}")
    print(f"  platform.machine()      {platform.machine()}")
    print(f"  python                  {platform.python_version()}  ({sys.executable})")
    print(f"  job {job_name!r} declares runs-on for  {wanted}")
    print(f"  os.linesep              {os.linesep!r}")
    print(f"  free disk               {shutil.disk_usage(pathlib.Path.cwd()).free / 2**30:.1f} GiB")

    print("--- filesystem -----------------------------------------------")
    with tempfile.TemporaryDirectory(prefix="ci-selfcheck-") as raw:
        facts = filesystem_facts(pathlib.Path(raw))
    for key, value in facts.items():
        print(f"  {key:<26}{value}")

    print("--- qt ---------------------------------------------------------")
    qt = qt_facts()
    for key, value in qt.items():
        print(f"  {key:<26}{value}")

    print("----------------------------------------------------------------")
    if actual != wanted:
        raise SelfCheckError(
            f"job {job_name!r} is declared for {wanted} but this runner is {actual}. "
            f"The job would run and report green while testing the wrong platform."
        )
    if not qt.get("offscreen"):
        raise SelfCheckError(
            "the installed PySide6 ships no offscreen platform plugin, so every GUI test "
            "would fail to start rather than fail visibly. CI would go red with a Qt "
            "error in a hundred places instead of one line here."
        )


# --- report ----------------------------------------------------------------------


def parse_junit(path: pathlib.Path) -> list[dict[str, Any]]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    cases: list[dict[str, Any]] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            children = [child.tag for child in case]
            reason = None
            for child in case:
                if child.tag == "skipped" and child.get("message"):
                    reason = child.get("message")
            cases.append(
                {
                    "id": f"{case.get('classname', '')}::{case.get('name', '')}",
                    "file": case.get("classname", "").replace(".", "/"),
                    "name": case.get("name", ""),
                    "outcome": (
                        "skipped"
                        if "skipped" in children
                        else "failed"
                        if ("failure" in children or "error" in children)
                        else "passed"
                    ),
                    "reason": reason,
                }
            )
    return cases


def dotted_to_id(path_and_name: str) -> str:
    """``tests/integration/test_x.py::test_y`` -> ``tests.integration.test_x::test_y``.

    The form pytest's own junit writer uses for ``classname``, which is the dotted module
    path without the ``.py``. Written out rather than guessed at the comparison site: a
    lookup keyed on the wrong shape fails open, and a check that cannot find its own
    targets reports "absent from the report" for every one of them -- which is what a
    first version of this did, and why the failure said nothing true about the run it had
    just described as perfect.
    """
    path, _, name = path_and_name.partition("::")
    module = path.replace("/", ".").removesuffix(".py")
    return f"{module}::{name}"


def check_report(report: pathlib.Path, min_tests: int, max_skips: int) -> None:
    cases = parse_junit(report)
    counts = Counter(case["outcome"] for case in cases)
    passed = counts["passed"]
    skipped = counts["skipped"]
    failed = counts["failed"]

    print("--- what the run actually did ---------------------------------")
    print(f"  report file              {report}")
    print(f"  total test cases         {len(cases)}")
    print(f"  passed                   {passed}")
    print(f"  skipped                  {skipped}")
    print(f"  failed or errored        {failed}")

    if skipped:
        print("  skip reasons:")
        reasons = Counter(
            (case["reason"] or "(no message)").splitlines()[0][:110]
            for case in cases
            if case["outcome"] == "skipped"
        )
        for reason, count in reasons.most_common():
            print(f"    {count:>4}  {reason}")
    print("----------------------------------------------------------------")

    problems: list[str] = []
    if len(cases) < min_tests:
        problems.append(
            f"only {len(cases)} test cases ran, floor is {min_tests}. A suite that runs "
            f"less than it claims still reports success."
        )
    if failed:
        problems.append(f"{failed} test cases failed; this check does not hide that")
    if skipped > max_skips:
        problems.append(
            f"{skipped} tests skipped, ceiling is {max_skips}. Every skip is a place the "
            f"platform is not being checked; the reasons are printed above."
        )

    by_id = {case["id"]: case["outcome"] for case in cases}

    on_windows = sys.platform == "win32"
    for wanted in WINDOWS_ONLY_TESTS:
        state = by_id.get(dotted_to_id(wanted))
        if state is None:
            problems.append(
                f"{wanted} is in WINDOWS_ONLY_TESTS but is absent from the report. Either "
                f"it was renamed, or the leg stopped running the file it is in."
            )
        elif on_windows and state == "skipped":
            problems.append(
                f"{wanted} skipped on Windows. It exists to prove a Windows-only "
                f"behaviour, so a skip here means this leg is not testing anything "
                f"Windows-specific -- and it is the failure mode that looks like success."
            )
        elif not on_windows and state != "skipped":
            problems.append(
                f"{wanted} ran on {sys.platform} rather than skipping. The test asserts a "
                f"Windows behaviour, so either the guard is gone or the platform claim in "
                f"WINDOWS_ONLY_TESTS is out of date."
            )

    if problems:
        raise SelfCheckError("\n  - ".join(["", *problems]))


# --- pins -------------------------------------------------------------------------

#: ``actions/*`` is GitHub's own namespace. Criterion C leaves those on tags: they are
#: reviewed in the same repository as the workflow, published by the same vendor, and
#: pinned by the runner image owner. Everything else is a third party whose tag someone
#: else can move, and the rule below is scoped to them by *owner*, not by a list of two
#: names -- a list would have to be edited every time a release step is added, and the
#: moment it is forgotten is the moment the guard stops guarding the thing it was written
#: for.
FIRST_PARTY_OWNER = "actions"

SHA_REF = re.compile(r"\A[0-9a-f]{40}\Z")


def trailing_comments(text: str) -> dict[str, str]:
    """``uses:`` value -> the comment written beside it, read from the raw text.

    ★ YAML drops comments, so a parsed workflow cannot see the version number that a pin is
    required to carry -- and the version number is the entire point of the pin. The
    comment therefore has to be read off the line, which means this tool has two views of
    every file and has to be careful they agree: ``check_pins`` requires that every
    ``uses`` the parser found also appears on a line of its own. A ``uses`` written in a
    form this cannot see on one line is reported, not skipped -- a value the version check
    cannot reach is a value the version check cannot vouch for.
    """
    found: dict[str, str] = {}
    pattern = re.compile(r"\A\s*(?:-\s+)?uses:\s*(\S+)\s*(?:#\s*(.*?))?\s*\Z")
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            found.setdefault(match.group(1), (match.group(2) or "").strip())
    return found


def parsed_uses(workflow: dict[str, Any]) -> list[str]:
    """Every ``uses`` in the workflow, from the parsed structure rather than the text."""
    values: list[str] = []
    for job in (workflow.get("jobs") or {}).values():
        for step in steps_of(job):
            value = step.get("uses")
            if isinstance(value, str):
                values.append(value)
    return values


def split_use(value: str) -> tuple[str, str]:
    """``owner/repo/path@ref`` -> (``owner/repo``, ``ref``)."""
    slug, _, ref = value.rpartition("@")
    if not slug or not ref:
        raise SelfCheckError(
            f"\n  - {value!r} is not a usable action reference: expected owner/repo@ref"
        )
    return slug, ref


def remote_shas(slug: str, version: str) -> dict[str, str]:
    """The SHAs a remote actually gives for ``version``, as a tag or as a branch.

    Read with ``git ls-remote`` rather than from the GitHub API because this is the same
    command a person would run to check by hand, and the point of the check is that its
    answer can be reproduced without trusting anything this tool does.
    """
    completed = subprocess.run(
        [
            "git",
            "ls-remote",
            f"https://github.com/{slug}",
            f"refs/tags/{version}",
            f"refs/tags/{version}^{{}}",
            f"refs/heads/{version}",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise RemoteUnreachableError(
            f"git ls-remote https://github.com/{slug} failed with "
            f"{completed.returncode}: {completed.stderr.strip()[:200]}"
        )
    shas: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if sha and name:
            shas[name] = sha
    return shas


def check_pins(verify_remote: bool) -> None:
    """Every third-party action pinned to an immutable SHA, and labelled with the release
    that SHA came from."""
    problems: list[str] = []
    rows: list[tuple[str, str, str, str]] = []
    first_party: set[str] = set()

    for filename in sorted(WORKFLOW_DIR.glob("*.yml")):
        text = filename.read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        comments = trailing_comments(text)

        for value in parsed_uses(workflow):
            slug, ref = split_use(value)
            owner = slug.split("/")[0]
            comment = comments.get(value)
            if comment is None:
                problems.append(
                    f"{filename.name}: {value} is on one line nowhere in the file, so the "
                    f"version comment beside it cannot be read. Write the uses on its own "
                    f"line with the version after a '#'."
                )
                continue
            if owner == FIRST_PARTY_OWNER:
                first_party.add(slug)
                continue
            if not SHA_REF.match(ref):
                problems.append(
                    f"{filename.name}: {value} names a MOVING ref. A tag or a branch can be "
                    f"repointed by whoever owns it, so what runs today is not what runs "
                    f"tomorrow. Pin the commit SHA and write the version beside it."
                )
                continue
            if not comment:
                problems.append(
                    f"{filename.name}: {slug} is pinned to {ref[:12]} with no version "
                    f"comment. A bare SHA is unreadable three months from now, which is "
                    f"the same as not saying which release was reviewed."
                )
                continue

            matched = ""
            if verify_remote:
                remote = remote_shas(slug, comment)
                tag_object = remote.get(f"refs/tags/{comment}")
                peeled = remote.get(f"refs/tags/{comment}^{{}}")
                head = remote.get(f"refs/heads/{comment}")
                if not remote:
                    problems.append(
                        f"{filename.name}: {slug} is pinned to {ref[:12]} and commented "
                        f"{comment!r}, but {comment!r} is not a tag or a branch on that "
                        f"repository. The version comment is a claim; this one is false."
                    )
                    continue
                if ref in {tag_object, peeled, head}:
                    which = "annotated tag" if ref == tag_object and peeled else "release"
                    matched = f"{comment} ({which})"
                else:
                    problems.append(
                        f"{filename.name}: {slug} is pinned to {ref[:12]}, but its comment "
                        f"says {comment!r} and that release is "
                        f"{'the tag object ' + tag_object[:12] if tag_object else ''}"
                        f"{'and the commit ' + peeled[:12] if peeled else ''}"
                        f"{'and the branch head ' + head[:12] if head else ''}. "
                        f"One of those two numbers is wrong."
                    )
                    continue
            rows.append((slug, ref, comment, matched or "not verified"))

    print(f"{'third-party action':<40}{'pinned to':<14}{'version':<14}resolved to")
    print("-" * 96)
    for slug, ref, comment, matched in rows:
        print(f"{slug:<40}{ref[:12]:<14}{comment:<14}{matched}")
    print(
        f"\n{len(rows)} third-party action(s) pinned; "
        f"{len(first_party)} first-party action(s) left on tags: "
        f"{', '.join(sorted(first_party))}"
    )
    if not verify_remote:
        print("(run with --verify-remote to resolve each version comment against its remote)")

    if problems:
        raise SelfCheckError("\n  - ".join(["", *problems]))


def _tracked_paths() -> list[str]:
    """Every path in the index, as git writes them."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"], cwd=REPO_ROOT, capture_output=True, timeout=120
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()[:200]
        raise SelfCheckError(f"git ls-files failed with {completed.returncode}: {detail}")
    return [p.decode("utf-8", "replace") for p in completed.stdout.split(b"\0") if p]


def check_tracked() -> tuple[int, list[str]]:
    """No tracked path may be excluded by a committed ``.gitignore``. Returns how many
    paths were asked about, and which ones a machine-local rule excludes instead.

    ★ **The question is *which* rule matched, not whether one did.** ``git check-ignore``
    reads three sources -- the ``.gitignore`` files in the tree, ``$GIT_DIR/info/exclude``,
    and ``core.excludesFile`` -- and only the first is in this repository. On this checkout
    the second carries ``/.??*``, which matches every dot-path at the root,
    ``.gitignore`` and ``.github/`` among them, both tracked deliberately. A check that
    asked only "is this path ignored" is therefore red on a healthy repository, and a guard
    that is always red is a guard that gets deleted.

    ``-v`` reports which rule matched, and gitignore(5)'s ladder puts the ``.gitignore``
    files above ``$GIT_DIR/info/exclude`` above ``core.excludesFile``, first level that
    matches decides. That a ``.gitignore`` match is never hidden behind a local one is
    measured rather than assumed: ``.venv/_probe.py`` force-added is matched by both this
    repository's ``.venv/`` and the local ``/.??*``, and git attributes it to
    ``.gitignore``. The paths only the local rule matches are returned and printed rather
    than counted silently -- there are five here, and they are the whole reason the naive
    form of this check does not work.
    """
    tracked = _tracked_paths()
    if not tracked:
        raise SelfCheckError("git ls-files returned no paths; this is not a populated checkout.")

    answered = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin", "-z", "-v"],
        cwd=REPO_ROOT,
        input=("\0".join(tracked) + "\0").encode("utf-8"),
        capture_output=True,
        timeout=120,
    )
    if answered.returncode not in (0, 1):
        detail = answered.stderr.decode("utf-8", "replace").strip()[:200]
        raise SelfCheckError(f"git check-ignore failed with {answered.returncode}: {detail}")

    # ★ -v emits four NUL-separated fields per match -- source, line, pattern, path -- and
    # prints a re-included path as well, named by the "!..." line that allowed it back in.
    # Read in groups of four rather than split on a tab: a pattern may contain one.
    fields = [f.decode("utf-8", "replace") for f in answered.stdout.split(b"\0") if f]
    if len(fields) % 4:
        raise SelfCheckError(
            f"git check-ignore -v produced {len(fields)} fields, which is not a multiple of "
            "four; this tool reads them as (source, line, pattern, path)"
        )

    problems: list[str] = []
    local: list[str] = []
    for index in range(0, len(fields), 4):
        source, line, pattern, path = fields[index : index + 4]
        if pattern.startswith("!"):
            continue
        if pathlib.PurePosixPath(source).name != ".gitignore":
            local.append(f"{path} ({source}:{line} {pattern})")
            continue
        problems.append(
            f"{path} is tracked, and {source}:{line} ({pattern}) excludes it. Either the "
            "path should not be tracked, or .gitignore should not exclude it -- and the "
            "friction of `git add -f` is the mechanism that makes the first true quietly."
        )

    if problems:
        raise SelfCheckError("\n  - ".join(["", *problems]))
    return len(tracked), sorted(local)


# --- entry point -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    shape = sub.add_parser("shape", help="static checks over .github/workflows/*.yml")
    shape.add_argument("--verbose", action="store_true", help="print the expanded job list")

    plat = sub.add_parser("platform", help="prove this runner is the platform its job claims")
    plat.add_argument("--job", required=True, help="the workflow job to check against")

    rep = sub.add_parser("report", help="check a pytest --junit-xml report")
    rep.add_argument("report", type=pathlib.Path, help="path to the junit xml")
    rep.add_argument("--min-tests", type=int, required=True)
    rep.add_argument("--max-skips", type=int, required=True)

    pins = sub.add_parser(
        "pins", help="every third-party action pinned to an immutable SHA, version-labelled"
    )
    pins.add_argument(
        "--verify-remote",
        action="store_true",
        help="resolve each version comment against github with git ls-remote",
    )

    sub.add_parser("tracked", help="no tracked path may be excluded by a committed .gitignore")

    sub.add_parser(
        "deps",
        help="every job installs what its tools/*.py steps import (a subset of `shape`)",
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "shape":
            problems, listing = check_workflow_shape()
            print("=" * 96)
            print("every job CI will run, expanded from the matrices")
            print("=" * 96)
            print(f"{'workflow':<16}{'job':<44}runs-on")
            print("-" * 96)
            for workflow, name, _values, runs_on in listing:
                print(f"{workflow:<16}{name:<44}{runs_on}")
            print()
            if args.verbose:
                for workflow, name, _values, runs_on in listing:
                    print(f"  {workflow} {name} -> {runs_on}")
            if problems:
                print(f"{len(problems)} problem(s) with the workflow:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(
                f"shape: clean. {len(listing)} jobs, "
                f"{len(load_workflows())} workflow files, "
                f"{len(TEST_DIRECTORIES)} shared test directories."
            )
            return 0
        if args.command == "platform":
            check_platform(args.job)
            print("platform: this runner is the platform its job declares.")
            return 0
        if args.command == "report":
            check_report(args.report, args.min_tests, args.max_skips)
            print("report: the run was real, and it ran the platform-specific tests.")
            return 0
        if args.command == "pins":
            print("=" * 96)
            print("the release chain's third-party actions")
            print("=" * 96)
            check_pins(args.verify_remote)
            print(
                "pins: every third-party action is pinned to a commit SHA and labelled "
                "with the release it came from."
            )
            return 0
        if args.command == "tracked":
            checked, local = check_tracked()
            print(
                f"tracked: {checked} paths in the index, and not one of them is excluded "
                "by a committed .gitignore."
            )
            if local:
                print(
                    f"  {len(local)} are excluded only by a machine-local rule, which is "
                    f"this machine's business and not the repository's: {', '.join(local)}"
                )
            return 0
        if args.command == "deps":
            # Prints the whole table before the verdict, so a reader can see *what* was
            # checked and not only that nothing was wrong. Same shape as `pins`: the
            # evidence and the conclusion, in that order, in one log.
            print("=" * 96)
            print("what each job's tools/*.py steps import, and what that job installs")
            print("=" * 96)
            for workflow_name, workflow in load_workflows().items():
                for job_name, job in (workflow.get("jobs") or {}).items():
                    scripts: set[pathlib.Path] = set()
                    for step in steps_of(job):
                        scripts |= python_scripts_in(step)
                    if not scripts:
                        continue
                    installed = ", ".join(sorted(installed_requirements(job))) or "nothing"
                    print(f"{workflow_name}:{job_name}")
                    print(f"    installs: {installed}")
                    for script in sorted(scripts):
                        name = script.relative_to(REPO_ROOT).as_posix()
                        if not script.is_file():
                            print(f"    {name}: NOT IN THE TREE")
                            continue
                        required, optional = third_party_imports(script)
                        line = f"    {name}: requires {', '.join(sorted(required)) or 'none'}"
                        if optional:
                            line += f" (optional: {', '.join(sorted(optional))})"
                        print(line)
            problems = check_python_dependencies()
            if problems:
                print(f"\n{len(problems)} problem(s):")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print("\ndeps: every job's tools/*.py steps import only what the job installs.")
            return 0
    except SelfCheckError as exc:
        # ★ stdout is flushed first, and that is not tidiness. Without it the verdict --
        # the one line a reader came for -- is written to stderr ahead of the report it is
        # a verdict about, because stderr is unbuffered and stdout is not. A log that shows
        # FAILED and then a run that passed is a log nobody trusts.
        sys.stdout.flush()
        print(f"\nSELF-CHECK FAILED\n{exc}", file=sys.stderr)
        sys.stderr.flush()
        return 1
    except RemoteUnreachableError as exc:
        sys.stdout.flush()
        print(
            f"\nPINS COULD NOT BE VERIFIED\n{exc}\n"
            f"This is not a claim about the workflow. It is the check that could not run.",
            file=sys.stderr,
        )
        sys.stderr.flush()
        return 2
    except (OSError, ET.ParseError, yaml.YAMLError) as exc:
        sys.stdout.flush()
        print(f"\nSELF-CHECK COULD NOT RUN\n{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.stderr.flush()
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
