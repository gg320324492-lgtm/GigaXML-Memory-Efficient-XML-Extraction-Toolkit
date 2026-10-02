"""Execute ``packaging/gigaxml.spec`` far enough to prove the version it would build.

**Why this file exists.** The packaging workflow runs only on a tag, so a defect in a
build input is invisible to every pull request in between. On the first tag this
repository ever built, all three platforms failed with

    ValueError: invalid literal for int() with base 10: '0rc1'

raised while the spec was being *parsed* -- with the whole test suite green, because
nothing in it ever executed the spec. The reduction the spec performs is one line, and
it was also the one line nobody watched.

So this runs it. What it does **not** do is build anything: PyInstaller is stubbed out
and its four builder calls are replaced by a recorder, so what executes is the spec's
own Python -- its imports, its path arithmetic, and above all the reduction from a
version that may carry a pre-release suffix to the four integers a Windows version
number is made of.

The version is a parameter rather than whatever ``pyproject.toml`` happens to say,
because a check that only exercises today's version stops covering the case it exists
for the moment today's version stops being a pre-release. A guard that has quietly
stopped watching is worse than one that is red.

Run::

    python -m tools.spec_check
"""

from __future__ import annotations

import contextlib
import pathlib
import re
import sys
import tempfile
import types
from collections.abc import Iterator
from typing import Any
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "gigaxml.spec"

#: The grammar a Windows version number takes. The same measurement the test suite
#: records against ISCC 6, restated here for the same reason it is restated there: a
#: check that compares a tool against the tool it is checking is a tautology.
WINDOWS_VERSION_GRAMMAR = re.compile(r"^[0-9]+(\.[0-9]+){0,3}$")

#: What a version may legitimately carry that a naive "split on the dots, then int()"
#: reduction cannot survive. Checked on every run rather than only today's, because the
#: point of this file is the shape of the *next* bump rather than the shape of this one.
PRE_RELEASE_VERSIONS = ("2.0.0rc1", "3.0.0b2", "1.0.0a1", "2.0.1.dev4", "2.0.0.post1", "1.2.3")


class BuildStep:
    """Stand-in for whatever PyInstaller hands back from each of its build calls.

    ``a.pure``, ``a.zipped_data`` and the rest are consumed by the call after it in the
    spec, so every attribute answers with another instance. That keeps the chain of
    calls intact without any of it being the thing under test.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args
        self.kwargs = kwargs

    def __getattr__(self, name: str) -> BuildStep:
        # Dunder lookups are questions about the type rather than the stand-in, and
        # answering them with an instance turns every ``copy`` and ``repr`` into an
        # infinite recursion somewhere far from here.
        if name.startswith("__"):
            raise AttributeError(name)
        return BuildStep()


@contextlib.contextmanager
def _stubbed_pyinstaller() -> Iterator[None]:
    """A PyInstaller that exists to be imported, and to do nothing else.

    ``from PyInstaller.utils.hooks import collect_submodules`` is the only way the spec
    reaches PyInstaller outside its build calls. Replacing it unconditionally, rather
    than using the real package on a machine that happens to have one, is what makes
    what runs here identical on a laptop and on a test runner that has no PyInstaller
    installed at all.
    """

    def collect_submodules(_package: str) -> list[str]:
        return []

    pyinstaller = types.ModuleType("PyInstaller")
    utils = types.ModuleType("PyInstaller.utils")
    hooks = types.ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = collect_submodules
    pyinstaller.utils = utils
    utils.hooks = hooks
    with mock.patch.dict(
        sys.modules,
        {
            "PyInstaller": pyinstaller,
            "PyInstaller.utils": utils,
            "PyInstaller.utils.hooks": hooks,
        },
    ):
        yield


def _restore_sys_path(saved: list[str]) -> None:
    sys.path[:] = saved


def _temporary_project(version: str) -> str:
    """A ``pyproject.toml`` declaring ``version``, and nothing else the spec could want."""
    return f'[project]\nname = "gigaxml"\nversion = "{version}"\n'


def load_spec(version: str | None = None) -> dict[str, Any]:
    """Execute the spec and return the module-level names it built.

    ``version`` points the spec at a throwaway repository root that declares it, so the
    file on disk is executed unchanged and the version being exercised is an argument
    rather than whatever this repository happens to say today. The real repository root
    stays on ``sys.path`` so the ``tools`` package the spec imports is still the one in
    this tree; the temporary root holds no ``tools`` for the finder to stop at.
    """
    source = SPEC.read_text(encoding="utf-8")
    specpath = SPEC.parent
    namespace: dict[str, Any] = {}

    with contextlib.ExitStack() as stack:
        if version is not None:
            root = pathlib.Path(stack.enter_context(tempfile.TemporaryDirectory()))
            (root / "packaging").mkdir()
            (root / "pyproject.toml").write_text(_temporary_project(version), encoding="utf-8")
            specpath = root / "packaging"
        stack.callback(_restore_sys_path, list(sys.path))
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        stack.enter_context(_stubbed_pyinstaller())
        namespace.update(
            {
                "__file__": str(SPEC),
                "__name__": "gigaxml.spec",
                # The names PyInstaller injects into a spec before executing it.
                "SPECPATH": str(specpath),
                "Analysis": BuildStep,
                "PYZ": BuildStep,
                "EXE": BuildStep,
                "COLLECT": BuildStep,
            }
        )
        exec(compile(source, str(SPEC), "exec"), namespace)
    return namespace


def spec_quad(version: str | None = None) -> tuple[int, ...]:
    """The four integers the spec would hand the resource block for ``version``."""
    return load_spec(version)["VERSION_TUPLE"]


def _versions_to_check() -> list[str]:
    """The version this repository declares, then the shapes a future bump may carry."""
    declared = str(load_spec()["VERSION"])
    return list(dict.fromkeys([declared, *PRE_RELEASE_VERSIONS]))


def main() -> int:
    problems: list[str] = []
    try:
        versions = _versions_to_check()
    except Exception as exc:
        print(
            f"::error::packaging/gigaxml.spec does not execute against this checkout: "
            f"{type(exc).__name__}: {exc}"
        )
        return 1

    for version in versions:
        try:
            rendered = ".".join(str(part) for part in spec_quad(version))
        except Exception as exc:
            problems.append(f"{version}: the spec raised {type(exc).__name__}: {exc}")
            continue
        if WINDOWS_VERSION_GRAMMAR.fullmatch(rendered) is None:
            problems.append(
                f"{version}: the spec produced {rendered!r}, which is not a Windows version number"
            )
            continue
        print(f"{version} -> {rendered}")

    for problem in problems:
        print(f"::error::{problem}")
    if problems:
        print(f"spec_check: {len(problems)} of {len(versions)} versions could not be reduced")
        return 1
    print(f"spec_check: the spec reduces {len(versions)} versions to digits and dots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
