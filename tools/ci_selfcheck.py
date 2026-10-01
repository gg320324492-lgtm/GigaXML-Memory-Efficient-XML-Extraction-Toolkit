"""Prove that the Windows and macOS legs of CI are really running tests.

★ **What this is for.** A matrix row is a claim, not a fact. ``runs-on: windows-latest``
on a job whose pytest step has been narrowed to one file still types green, and the only
thing that changed is that a platform nobody checks any more looks checked. Three of this
project's eight "green here, red in CI" incidents were of exactly that shape -- a
suspicion, an environment difference, and a suite that never ran -- so this milestone
adds the machine checks that make the claim falsifiable.

Three subcommands, and each answers a different way the claim could be false:

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

None of this replaces pytest's exit code, and none of it makes a failure quieter. It adds
the two things an exit code cannot say: *how much* ran, and *on what*.
"""

from __future__ import annotations

import argparse
import locale
import os
import pathlib
import platform
import shutil
import sys
import tempfile
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
WINDOWS_ONLY_TESTS = (
    "tests/integration/test_atomic_output.py::test_a_target_held_open_is_one_clear_error_and_the_partial_survives",
    "tests/integration/test_atomic_output.py::test_the_held_open_error_names_the_partial_file",
    "tests/integration/test_gui_history.py::"
    "test_a_run_the_cli_stopped_is_shown_as_interrupted_and_can_be_continued",
    "tests/integration/test_interrupt_report.py::test_a_failed_run_still_says_failed",
    "tests/integration/test_interrupt_report.py::test_a_finished_run_still_says_ok",
    "tests/integration/test_interrupt_report.py::"
    "test_a_stopped_run_writes_a_report_that_says_it_was_interrupted",
    "tests/integration/test_interrupt_report.py::test_the_numbers_in_that_report_are_the_ones_on_disk",
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

    return problems, listing


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
    except SelfCheckError as exc:
        # ★ stdout is flushed first, and that is not tidiness. Without it the verdict --
        # the one line a reader came for -- is written to stderr ahead of the report it is
        # a verdict about, because stderr is unbuffered and stdout is not. A log that shows
        # FAILED and then a run that passed is a log nobody trusts.
        sys.stdout.flush()
        print(f"\nSELF-CHECK FAILED\n{exc}", file=sys.stderr)
        sys.stderr.flush()
        return 1
    except (OSError, ET.ParseError, yaml.YAMLError) as exc:
        sys.stdout.flush()
        print(f"\nSELF-CHECK COULD NOT RUN\n{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.stderr.flush()
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
