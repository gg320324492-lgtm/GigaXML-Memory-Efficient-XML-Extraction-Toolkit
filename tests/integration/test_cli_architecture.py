"""Two architecture tests, and the reason they are here rather than in the golden suite.

**These do not check that the CLI works.** The golden suite (six cases, byte for byte)
and the M6 exit-code suite (five codes) already do that, and they did not weaken by a
single byte when ``cli.py`` was split. What those cannot see is the *shape*: a module
that has quietly accumulated four jobs again passes every behavioural test, because
behaviour is unchanged -- that is the whole difficulty with a file that has too many
responsibilities.

So these two assert the shape, and each one is written so that it fails on a specific
mistake rather than in general:

* :func:`test_main_does_no_work_of_its_own` reads ``main``'s source and fails if an
  implementation-layer name appears in it. The mutation in the brief -- pushing a
  ``_write_report_safely(...)`` call into ``main`` -- turns it red.
* :func:`test_nothing_outside_the_gui_imports_the_cli` walks the package and fails if a
  module other than the desktop application imports ``gigaxml.cli``. **It is green
  today**, and that is the point: it is a ratchet against a future change, not a
  description of a current fault.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import gigaxml.cli as cli_module

#: The package root, walked by the dependency-direction test.
PACKAGE = Path(cli_module.__file__).parent

#: Implementation-layer names ``main`` must not mention. Each is something ``main``
#: could plausibly reach for and get a plausible-looking result -- which is what makes
#: them worth naming:
#:
#: * ``source_identity`` / ``config_identity`` -- hashing, which is a report field's job;
#: * ``write_checkpoint`` / ``read_checkpoint`` -- the checkpoint's own bookkeeping;
#: * ``RowWriter`` / ``create_writer`` -- opening an output;
#: * ``consume_records`` / ``StreamingRecordReader`` -- reading a document;
#: * ``write_report_safely`` / ``run_report_path`` -- where a report goes;
#: * ``load_config`` -- reading the config;
#: * ``open_source`` / ``open`` -- opening the input.
#:
#: Parsing the command line is deliberately *not* on this list. Deciding what a flag
#: means is one of the four jobs ``main`` keeps.
FORBIDDEN_IN_MAIN = (
    "source_identity",
    "config_identity",
    "write_checkpoint",
    "read_checkpoint",
    "require_intact_parts",
    "validate_resume",
    "RowWriter",
    "create_writer",
    "consume_records",
    "StreamingRecordReader",
    "write_report_safely",
    "write_run_report",
    "run_report_path",
    "rejection_log",
    "load_config",
    "open_source",
    "reject_output_that_is_the_input",
    "sample_records",
    "inspect_document",
    "generate_dataset",
)


def _function_source(name: str) -> str:
    """The source of a top-level function in :mod:`gigaxml.cli`.

    Read off the module's file rather than imported, because the question is about the
    text of the function -- what a reader sees when they open it.
    """
    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            lines = source.splitlines()
            return "\n".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"{name} is not a top-level function of {cli_module.__file__}")


def test_main_does_no_work_of_its_own() -> None:
    """★ **判据 C: ``main`` parses, dispatches, and turns an exception into a number.**

    It must not open a file, hash a document, or write a row. Asserting on the source
    rather than on behaviour is the point: every one of those operations would be
    *correct* here, and the golden suite would stay green if it appeared -- the failure
    this guards is a module slowly becoming the place everything happens, which no
    output assertion can see coming.
    """
    body = _function_source("main")

    found = [name for name in FORBIDDEN_IN_MAIN if name in body]
    assert not found, (
        f"main() now mentions {found}. It builds the parser, dispatches, and maps the "
        "exception onto an exit code; work belongs in the command handler it dispatches "
        "to (gigaxml.cli_pkg.extract_cmd and friends) or in common.py when two commands "
        "share it. A call added here would still be correct, and every behavioural test "
        "would still pass -- which is exactly why this is asserted here."
    )


def test_main_only_dispatches_to_a_handler() -> None:
    """The other half of the same claim: ``main`` calls exactly one thing on the way out.

    ``main`` is allowed to do three things -- parse, install the signal handlers, call
    the handler -- and nothing else. Asserting the *call list* rather than a list of
    banned names catches a new call that no list thought of, which is the way a
    name-based test goes stale.
    """
    tree = ast.parse(_function_source("main"))
    function = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main"
    )
    called = sorted(
        {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
    )

    assert called == [
        "_install_interrupt_handlers",
        "_report_unexpected",
        "build_parser",
        "handler",
        "print",
        "restore_signals",
    ], (
        f"main() calls {called}. The allowed set is build_parser, "
        "_install_interrupt_handlers, handler, restore_signals, print for the one line "
        "each outcome writes, and _report_unexpected -- turning an exception nobody "
        "predicted into exit code 4 is the top-level boundary's own job, which is why "
        "it is here and not in a command."
    )


def test_nothing_outside_the_gui_imports_the_cli() -> None:
    """★ **判据 D: the dependency only points one way. Green today, on purpose.**

    ``gigaxml.cli`` is the outermost layer -- it is what the console script and the
    desktop application call. Nothing below it may import it, because a module that
    knows about the command line cannot be used without it, and the dependency that
    follows is the one that turns a CLI into a library that can only be run.

    The desktop application is the one exception and always was: it launches the CLI.
    Everything else -- config, fields, parser, writers, checkpoint, inspect, xsd, run,
    sample, generate -- must be usable without the CLI existing.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE).as_posix()
        if relative.startswith("gui/") or relative == "cli.py" or relative.startswith("cli_pkg/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [f"{node.module or ''}.{alias.name}".lstrip(".") for alias in node.names]
            if any(name == "gigaxml.cli" or name.startswith("gigaxml.cli.") for name in names):
                offenders.setdefault(relative, []).extend(
                    name for name in names if name.startswith("gigaxml.cli")
                )

    assert not offenders, (
        f"these modules import gigaxml.cli: {offenders}. The CLI is the outermost layer; "
        "a dependency pointing back at it means the code below can only be reached by "
        "running a command. The desktop application is exempt because launching the CLI "
        "is what it does."
    )


def test_the_cli_package_does_not_import_the_gui() -> None:
    """The same rule from the other side: the CLI must never need the window.

    This one is not hypothetical. ``gui/app.py`` imports ``gigaxml.cli``, so a reverse
    import would be a cycle -- and a cycle between them would surface as an
    ``ImportError`` only at startup, on whichever platform imports first.
    """
    offenders: list[str] = []
    for path in sorted((PACKAGE / "cli_pkg").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                offenders += [a.name for a in node.names if a.name.startswith("gigaxml.gui")]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.startswith("gigaxml.gui"):
                    offenders.append(module)

    assert not offenders, (
        f"the CLI imports the GUI: {offenders}. A command-line tool that cannot start "
        "without a GUI toolkit installed is a tool nobody can install on a server, and "
        "gui/app.py imports the CLI -- so this would also be a cycle."
    )


@pytest.mark.parametrize("name", ["main", "build_parser"])
def test_the_public_import_path_still_works(name: str) -> None:
    """判据 B: ``from gigaxml.cli import main`` is what the desktop application does.

    Worth a test of its own because the split is exactly the kind of change that breaks
    it quietly: ``main`` moved to a module that imports it, the console script and
    ``gui/app.py`` keep working, and nothing red until an install.
    """
    import importlib

    imported = importlib.import_module("gigaxml.cli")
    assert callable(getattr(imported, name, None)), (
        f"gigaxml.cli no longer exports {name}, which is a published import path: "
        "gui/app.py imports it by name."
    )


def test_the_console_script_entry_point_names_a_real_callable() -> None:
    """``pyproject.toml`` says ``gigaxml = "gigaxml.cli:main"`` -- check that resolves.

    ★ **This is the check that makes "we did not change pyproject.toml" a measured
    claim rather than an assumption.** The split left the entry point alone precisely
    so it could not break, and this is what verifies that reasoning rather than trusting
    it: if the split had moved ``main`` without re-exporting it, this goes red and the
    console script is broken on every machine that installs the package.
    """
    import importlib

    pyproject = PACKAGE.parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    entry = next(
        line.split("=", 1)[1].strip().strip('"')
        for line in text.splitlines()
        if line.strip().startswith("gigaxml = ")
    )
    module_name, _, attribute = entry.partition(":")

    imported = importlib.import_module(module_name)
    assert callable(getattr(imported, attribute, None)), (
        f"pyproject.toml points the console script at {entry}, which does not resolve to a callable"
    )
