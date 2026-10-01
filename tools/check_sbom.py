"""Check that the release's SBOM describes this project and not something else.

★ **Why this needs to exist when ``cyclonedx-py`` already validates.** Two different
questions, and passing one says nothing about the other. ``--validate`` answers "is this
legal CycloneDX?" -- it validates the shape and knows nothing about what is in it. A file
that validates perfectly can describe a Python 2 application, or an environment where the
install failed and nothing was found, or a project by a different name. The second
question is the one a release page makes a promise about: *this* release ships *these*
packages.

Three things are asserted, and they are chosen so that each one fails for a different
reason rather than all three failing together on a bad file:

* the file is CycloneDX at all, and parses;
* the root component is this project, at this project's version, read from
  ``pyproject.toml`` rather than from a constant -- so a release built from a tree whose
  version moved cannot ship an SBOM naming the old one;
* every runtime dependency declared in ``pyproject.toml`` appears among the components.

Anything in the SBOM that is *not* a declared dependency is printed rather than asserted
away. ``pip`` is in there -- it really is installed in the environment the SBOM was taken
from -- and deciding to hide that would make the SBOM a tidied-up document instead of a
record of a machine. The report says so instead.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import tomllib

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Components that are expected to be in the SBOM without being declared dependencies,
#: because the environment they were collected from contains them. Checked only so the
#: printed list is complete, never so the run goes red: a venv's own installer appearing
#: in a record of a venv is a fact, not a defect.
EXPECTED_UNDECLARED = {"pip", "setuptools", "wheel"}


def normalise(name: str) -> str:
    """PEP 503 normalisation: runs of ``-``, ``_`` and ``.`` collapse to one ``-``.

    ★ The first version of this kept ``-`` and dropped ``_`` and ``.`` entirely, and its
    docstring said PEP 503. It was not PEP 503, and the test that says so is the only
    reason that was found before a dependency with a dot in its name made a declared
    package look absent from an SBOM that lists it. `lxml` and `pyyaml` have no
    separators, so the two versions agree on everything this project depends on today --
    which is exactly why a wrong implementation here would have survived until the day it
    mattered. Written out rather than imported: four lines, and a check whose correctness
    depends on a dependency is a check that can break for reasons unrelated to what it
    checks.
    """
    return re.sub(r"[-_.]+", "-", name).lower()


def declared_dependencies() -> tuple[str, str, list[str]]:
    """The project name, its version, and its runtime dependency names, from pyproject."""
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    return (
        project["name"],
        project["version"],
        [
            normalise(requirement.split(">")[0].split("=")[0].split("<")[0].split("!")[0].strip())
            for requirement in project["dependencies"]
        ],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sbom", type=pathlib.Path)
    args = parser.parse_args(argv)

    name, version, dependencies = declared_dependencies()
    print(
        f"pyproject declares {name} {version} with runtime dependencies: {', '.join(dependencies)}"
    )

    problems: list[str] = []
    try:
        bom = json.loads(args.sbom.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"\nSBOM COULD NOT BE READ\n{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if bom.get("bomFormat") != "CycloneDX":
        problems.append(f"bomFormat is {bom.get('bomFormat')!r}, not CycloneDX")
    if not str(bom.get("specVersion", "")).startswith("1."):
        problems.append(f"specVersion is {bom.get('specVersion')!r}, which is not CycloneDX 1.x")

    root = (bom.get("metadata") or {}).get("component") or {}
    root_name = normalise(str(root.get("name", "")))
    if root_name != normalise(name):
        problems.append(
            f"the root component is {root.get('name')!r}, but this project is {name!r}. The "
            f"SBOM describes something else."
        )
    if str(root.get("version")) != version:
        problems.append(
            f"the root component says version {root.get('version')!r}, but pyproject says "
            f"{version!r}. A release built from a tree whose version moved must not ship an "
            f"SBOM naming the old one."
        )

    components = {
        normalise(str(component.get("name", ""))): str(component.get("version", ""))
        for component in bom.get("components") or []
    }
    for dependency in dependencies:
        if dependency not in components:
            problems.append(
                f"{dependency} is a declared runtime dependency and is absent from the "
                f"SBOM's components. Either the install did not happen, or the SBOM was "
                f"taken from somewhere else."
            )

    undeclared = sorted(set(components) - set(dependencies) - {normalise(name)})
    print(f"\ncomponents ({len(components)}):")
    for component in sorted(components):
        mark = "" if component in dependencies else "   <- not a declared dependency"
        print(f"  {component:<14}{components[component]}{mark}")
    unexpected = [c for c in undeclared if c not in EXPECTED_UNDECLARED]
    if undeclared:
        print(f"\nalso present and not declared in pyproject: {', '.join(undeclared)}")
        print(
            "  these are printed rather than rejected: they are real entries in the "
            "environment\n  the SBOM was taken from, and a record of a machine is not "
            "tidied up."
        )
    if unexpected:
        print(f"  of those, not expected from a plain venv: {', '.join(unexpected)}")

    if problems:
        print(f"\n{len(problems)} problem(s):", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print(f"\nSBOM describes {name} {version} and lists every declared runtime dependency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
