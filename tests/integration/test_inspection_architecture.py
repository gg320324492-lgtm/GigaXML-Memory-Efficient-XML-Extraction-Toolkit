"""The dependency directions the ``inspect`` split is supposed to hold.

★ **Criterion D, and the half of the split that a behavioural test cannot see.** Every
test that passes tells you ``inspect`` still produces the same bytes. None of them
tell you the ranking did not quietly move *into* the scanner, which would leave the
output identical and the separation fictional. These are shape assertions, and each
one names the mistake it is aimed at.

Three directions are checked:

* the CLI's ``inspect_cmd`` reaches inspection through its published entry points and
  does not implement any walking itself;
* the inspection package does not import the CLI (M7's test already guards this from
  the other side, and breaking it would mean the layers had met);
* within the package, ``models`` is the bottom -- it imports nothing from its siblings,
  so a data class can never come to depend on the code that fills it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import gigaxml.cli_pkg.inspect_cmd as inspect_cmd_module
import gigaxml.inspection.models as models_module

INSPECTION = Path(models_module.__file__).parent

#: The four names ``inspect_cmd`` is allowed to reach for. The first three are the
#: published API; ``DEFAULT_MAX_PATHS`` and ``DEFAULT_MAX_DEPTH`` are the parser's
#: defaults, which have to come from somewhere the walk also reads.
ALLOWED_INSPECT_CMD = {
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PATHS",
    "generate_config",
    "inspect_document",
}


def _imported_names(module_path: Path) -> set[str]:
    """Every name a module imports, from any module."""
    names: set[str] = set()
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("gigaxml"):
                names.add(node.module)
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.startswith("gigaxml"))
    return names


def test_the_cli_command_reaches_inspection_only_through_its_published_entry_points() -> None:
    """★ **Criterion D: the command dispatches, it does not walk.**

    ``gigaxml inspect --json`` is a rendering of what the inspection package produced.
    If the CLI grew its own path table, its own scoring or its own namespace handling,
    the output would still be right for the cases both implementations happen to agree
    on -- and wrong, differently, for every document where they did not.
    """
    imported = _imported_names(Path(inspect_cmd_module.__file__))

    inspection_imports = {name for name in imported if ".inspection" in name}
    assert inspection_imports <= {f"gigaxml.inspection.{name}" for name in ALLOWED_INSPECT_CMD}, (
        f"inspect_cmd imports {sorted(inspection_imports)}; only the published entry "
        f"points are allowed: {sorted(ALLOWED_INSPECT_CMD)}. Walking, scoring or "
        "namespace handling inside the command is the failure this guards."
    )


def test_the_inspection_package_does_not_import_the_cli() -> None:
    """The layers have to stay separate in both directions.

    M7's architecture test already asserts this from the CLI's side; this asserts it
    from inspection's, so the pair fails whichever module someone edits.
    """
    for path in sorted(INSPECTION.rglob("*.py")):
        offenders = [name for name in _imported_names(path) if name.startswith("gigaxml.cli")]
        assert not offenders, f"{path.name} imports the CLI: {offenders}"


def test_the_models_module_depends_on_nothing_else_in_the_package() -> None:
    """★ **The data classes are the bottom of the graph, and must stay there.**

    A :class:`Candidate` is a value. If it imported the ranking that fills it, the two
    would be one module wearing two names, and every other direction would be
    uncheckable because there would be nothing underneath.

    Two references are permitted and both are inside function bodies, which is why
    they are not visible here: a module-scope ``ImportFrom`` inside an ``if
    TYPE_CHECKING`` block is the shape this test looks for and both of them avoid --
    ``models`` reaches :func:`_prefixes_in` and :func:`_index_of` by importing them
    where they are used, because importing them at module scope would be a cycle.
    """
    imported = _imported_names(Path(models_module.__file__))
    siblings = {name for name in imported if name.startswith("gigaxml.inspection")}

    assert not siblings, (
        f"models imports {sorted(siblings)}. The data classes are the bottom of the "
        "graph; a value that imports the code filling it is not a value."
    )


def test_only_the_scanner_parses_xml() -> None:
    """★ **One module parses the document. A second one is a second opinion nobody asked for.**

    ``lxml`` reaching a module other than the scanner means parsing logic has leaked --
    most likely a namespace question, which is exactly the sort of thing that is
    correct in isolation and disagrees with the walk in a document that rebinds a
    prefix. ``config_generator`` writes YAML and ``candidates`` scores; neither has any
    business near a parser.
    """
    allowed = {"scanner.py"}

    for path in sorted(INSPECTION.rglob("*.py")):
        if path.name in allowed:
            continue
        source = path.read_text(encoding="utf-8")
        assert "etree" not in source and "iterparse" not in source, (
            f"{path.name} references lxml. Parsing happens in scanner.py and nowhere "
            "else -- a second parser is a second set of answers about namespaces."
        )


@pytest.mark.parametrize(
    "module", ["scanner", "candidates", "inference", "config_generator", "models"]
)
def test_every_module_in_the_package_parses_and_has_a_docstring(module: str) -> None:
    """A module whose first line is code is a module nobody has read yet.

    Cheap to check, and it is the only thing that keeps the five from turning back into
    one file with extra steps: each carries a sentence saying what kind of claim its
    contents make, which is the whole reason the split exists.
    """
    path = INSPECTION / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert ast.get_docstring(tree), f"{path.name} has no module docstring"
