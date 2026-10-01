"""The public API is what ``__all__`` says it is, and what ``py.typed`` promises.

★ **M10 criterion D.** Two separate claims, and the second is the one that catches people:

* every name in ``gigaxml.__all__`` **can actually be imported**;
* every one of them is **fully annotated**, because the package ships
  :file:`py.typed` and a ``Typing :: Typed`` classifier. **A stub is not a promise.**

**``-> Any`` is refused, which is what makes the annotation rule worth having.** The brief
put it as "either annotate it or do not export it, and do not add ``-> Any`` to get past
the test" -- so :func:`_any_annotations` below walks the exported signatures looking for
``Any`` in any position, parameter or return. **A test that only checked for *missing*
annotations would be passed by a file full of ``Any``**, which is precisely the outcome the
brief forbade, so the check has to look for both.

**And the document has to agree.**
:func:`test_the_document_and_the_export_list_name_the_same_things` parses
``python-api.md`` and requires the names in its table and the names in ``__all__`` to
be the same set. A declaration nobody can check is a promise in a comment; making the
document and the export list mutually verifying is what turns "documented" into something
the suite checks on every run.
"""

from __future__ import annotations

import ast
import dataclasses
import enum
import importlib
import inspect
import pathlib
import re
import typing
from typing import Any

import pytest

import gigaxml
import gigaxml.errors

#: The document this milestone makes the contract, at the repository root -- **not** under
#: ``docs/``, which is gitignored (``.gitignore:45``). That is the M1-S-FIX disease: a
#: specification that lives only on one machine's disk is not a specification.
API_DOC = pathlib.Path(__file__).resolve().parents[2] / "python-api.md"

#: Names whose "annotation" is the standard exception constructor. An exception class has no
#: parameters of its own and cannot be given any without changing what callers catch, so
#: asking for annotations on one would be asking for a lie.
_EXCEPTION_BASE = Exception


# --- does every declared name actually exist? -------------------------------


def test_every_exported_name_can_be_imported() -> None:
    """★ **Criterion D's first half, on the real package.**

    ``__all__`` is a claim in a list. This is the claim being checked: each name resolves
    through :pep:`562`'s ``__getattr__`` or through an eager import, and is the object the
    source module says it is rather than, say, a module that happens to share its name.

    Every name goes through ``importlib`` in a fresh lookup rather than ``getattr``, so a
    name that is cached in ``globals()`` by an earlier test cannot make a broken one look
    working.
    """
    broken: list[str] = []
    for name in gigaxml.__all__:
        module_name, attribute, _ = _resolve(name)
        try:
            found = getattr(importlib.import_module(module_name), attribute)
        except (ImportError, AttributeError) as exc:
            broken.append(f"{name}: {exc}")
            continue
        assert found is not None, f"{name} resolved to None"

    assert not broken, f"__all__ names that do not import: {broken}"


def _resolve(name: str) -> tuple[str, str, str]:
    """``(module, attribute, note)`` for where a public name really lives."""
    lazy = gigaxml._LAZY_EXPORTS.get(name)
    if lazy is not None:
        module_name, _, attribute = lazy.partition(":")
        return module_name, attribute, "lazy"
    # The error classes are imported eagerly in ``gigaxml/__init__.py``; their module of
    # record is ``gigaxml.errors``, which is also where the canonical definitions live.
    # (``gigaxml.errors`` is imported at the top of this file rather than here: an import
    # inside this function would bind ``gigaxml`` as a *local* name and shadow the
    # module-level one for the whole function -- which is exactly what it did the first
    # time, and 51 tests said so.)
    if hasattr(gigaxml.errors, name):
        return "gigaxml.errors", name, "eager"
    return "gigaxml", name, "eager"


# --- is every exported name actually annotated? -----------------------------


def _annotation_of(signature: inspect.Signature, name: str) -> object:
    return signature.parameters[name].annotation


def _missing_annotations(target: object) -> list[str]:
    """Every parameter or return of ``target`` with no annotation, as sentences.

    A function is checked whole. A class is checked through its ``__init__`` when it has
    one, because that is the signature a caller writes; a dataclass is additionally checked
    through its fields, which is where its interface actually lives.
    """
    gaps: list[str] = []
    if inspect.isfunction(target):
        signature = inspect.signature(target)
        for parameter in signature.parameters.values():
            if parameter.annotation is inspect.Parameter.empty:
                gaps.append(f"parameter {parameter.name!r}")
        if signature.return_annotation is inspect.Signature.empty:
            gaps.append("the return value")
        return gaps

    if not inspect.isclass(target):
        return gaps

    if dataclasses.is_dataclass(target):
        for field in dataclasses.fields(target):
            if field.type in (dataclasses.MISSING, "", None):
                gaps.append(f"field {field.name!r}")
    elif issubclass(target, enum.Enum):
        pass  # an enum's interface is its members; there is no constructor to annotate
    elif issubclass(target, _EXCEPTION_BASE):
        pass  # see the note on _EXCEPTION_BASE above
    else:
        initialiser = getattr(target, "__init__", None)
        if initialiser is not object.__init__:
            signature = inspect.signature(initialiser)
            for parameter in signature.parameters.values():
                if parameter.name in {"self", "cls"}:
                    continue
                if parameter.annotation is inspect.Parameter.empty:
                    gaps.append(f"{target.__name__}.__init__ parameter {parameter.name!r}")

    for method_name, method in inspect.getmembers(target, predicate=inspect.isfunction):
        if method_name.startswith("_") and method_name not in {"__iter__", "__next__", "__len__"}:
            continue
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):  # pragma: no cover - a C-level method
            continue
        gaps += [
            f"{target.__name__}.{method_name} parameter {p.name!r}"
            for p in signature.parameters.values()
            if p.name not in {"self", "cls"} and p.annotation is inspect.Parameter.empty
        ]
    return gaps


def _any_annotations(target: object) -> list[str]:
    """Every place ``target`` says ``Any``, which the contract refuses.

    ★ **This is the check that makes the annotation check mean anything.** "Has annotations"
    is satisfied by ``def f(x: Any) -> Any``, so a suite that only counted missing
    annotations would pass a public API that claims nothing about itself at all -- exactly
    the outcome the brief forbade by name.
    """
    found: list[str] = []
    if inspect.isfunction(target):
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):  # pragma: no cover
            return found
        found += [
            f"parameter {p.name!r}"
            for p in signature.parameters.values()
            if _mentions_any(p.annotation)
        ]
        if _mentions_any(signature.return_annotation):
            found.append("the return value")
        return found

    if not inspect.isclass(target):
        return found

    if dataclasses.is_dataclass(target):
        found += [
            f"field {f.name!r}: {f.type}"
            for f in dataclasses.fields(target)
            if _mentions_any(f.type)
        ]
    for method_name, method in inspect.getmembers(target, predicate=inspect.isfunction):
        try:
            signature = inspect.signature(method)
        except (TypeError, ValueError):  # pragma: no cover
            continue
        found += [
            f"{target.__name__}.{method_name} parameter {p.name!r}: {p.annotation}"
            for p in signature.parameters.values()
            if _mentions_any(p.annotation)
        ]
        if _mentions_any(signature.return_annotation):
            found.append(f"{target.__name__}.{method_name} returns {signature.return_annotation}")
    return found


def _mentions_any(annotation: object) -> bool:
    """Whether an annotation uses ``Any`` anywhere, including nested inside a generic."""
    if annotation is Any or annotation is typing.Any:
        return True
    text = str(annotation)
    # ``Any`` alone or as a subscript (``list[Any]``, ``dict[str, Any]``); ``typing.Any`` is
    # normalised away by ``str()`` on modern Python, and the word boundary keeps ``Anything``
    # -- a perfectly ordinary class name -- from being mistaken for it.
    return re.search(r"\bAny\b", text) is not None


@pytest.mark.parametrize("name", sorted(gigaxml.__all__))
def test_every_exported_name_is_fully_annotated(name: str) -> None:
    """★ **Criterion D's second half, name by name, so a failure says which one.**

    Parameterised rather than looped: a loop that collected every offender would report a
    list, and a list is one line to skim. Twenty-four names means the failure names the
    offending API, which is the only thing anybody needs from it.
    """
    module_name, attribute, _ = _resolve(name)
    target = getattr(importlib.import_module(module_name), attribute)

    gaps = _missing_annotations(target)
    assert not gaps, (
        f"gigaxml exports {name!r} and the package ships py.typed, so it must be annotated. "
        f"Missing: {gaps}. Either annotate it or leave it out of __all__ -- and do not reach "
        "for `-> Any`, which is checked separately and refused."
    )


@pytest.mark.parametrize("name", sorted(gigaxml.__all__))
def test_no_exported_name_uses_any(name: str) -> None:
    """★ **The brief's \"do not add ``-> Any`` to get past the test\", made executable.**

    ``Any`` is a hole in the type system shaped like a promise. A public API that uses it
    tells a type checker \"nothing is known here\", and the caller is right not to trust it --
    which means the ``py.typed`` marker is advertising a guarantee the API does not keep.
    """
    module_name, attribute, _ = _resolve(name)
    target = getattr(importlib.import_module(module_name), attribute)

    offenders = _any_annotations(target)
    assert not offenders, f"gigaxml.{name} uses Any at: {offenders}"


# --- the export list, the type-checker block and the document all agree -----


def test_the_lazy_table_and_the_all_list_name_the_same_pipeline() -> None:
    """Three declarations of the same set: ``__all__``, ``_LAZY_EXPORTS`` and ``TYPE_CHECKING``.

    ★ **They are one fact written three times, and the price of PEP 562 is that price.** The
    run-time half is :data:`gigaxml._LAZY_EXPORTS`; the static half is the
    ``TYPE_CHECKING`` block, which a type checker reads and nobody at run time sees. Add a
    name to one and forget the other and you get either a public name with no type (the
    checker silently sees nothing) or a typed name nobody can import.

    The eager error classes are checked separately below, because they live in the import
    block rather than in either of these two.
    """
    # ★ Parsed with :mod:`ast`, not a regex. A regex over the block finds single-line
    # ``from x import a, b`` but cannot follow a parenthesised multi-line one -- which is
    # how ``extract_record`` and the three field names went missing the first time, and the
    # test that was written to catch exactly that kind of miss was the thing missing it.
    typed_names: set[str] = set()
    module_ast = ast.parse(pathlib.Path(gigaxml.__file__).read_text(encoding="utf-8"))
    for statement in module_ast.body:
        if not isinstance(statement, ast.If):
            continue
        test = statement.test
        if not (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING"):
            continue
        for inner in ast.walk(ast.Module(body=statement.body, type_ignores=[])):
            if isinstance(inner, ast.ImportFrom) and (inner.module or "").startswith("gigaxml"):
                typed_names.update(alias.name for alias in inner.names)

    lazy = set(gigaxml._LAZY_EXPORTS)
    assert lazy <= set(gigaxml.__all__), (
        f"{sorted(lazy - set(gigaxml.__all__))} are resolvable at run time but not declared "
        "in __all__, so they are internal by accident"
    )
    assert lazy <= typed_names, (
        f"{sorted(lazy - typed_names)} resolve at run time but are missing from the "
        "TYPE_CHECKING block, so a type checker cannot see them"
    )
    pipeline = {n for n in gigaxml.__all__ if not n.endswith("Error") and n != "__version__"}
    assert pipeline == lazy, (
        f"the pipeline names and the lazy table disagree: only in __all__ "
        f"{sorted(pipeline - lazy)}, only lazy {sorted(lazy - pipeline)}"
    )


def test_the_eager_error_classes_are_all_of_them_and_all_declared() -> None:
    """Every public error class is a real, importable exception -- and the list is complete.

    Completeness matters because of what a base class without subclasses is for: a caller
    catches :class:`GigaXMLError` in order to *branch*, and a branch whose case labels
    cannot be imported is a branch on message text. This project says in ``errors.py`` that
    matching on wording "is the one thing worse than not classifying -- it would break the
    first time a message is reworded", and an unexported subclass makes that the only
    option.
    """
    declared = {name for name in gigaxml.__all__ if name.endswith("Error")}
    assert declared == set(gigaxml.errors.__all__), (
        f"only in gigaxml.__all__: {sorted(declared - set(gigaxml.errors.__all__))}; "
        f"only in errors.__all__: {sorted(set(gigaxml.errors.__all__) - declared)}"
    )
    for name in sorted(declared):
        error = getattr(gigaxml.errors, name)
        assert issubclass(error, Exception), f"{name} is exported as an error and is not one"
    assert issubclass(gigaxml.GigaXMLError, Exception)


# --- the document ----------------------------------------------------------


_ROW = re.compile(r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|\s*(stable|beta)\s*\|", re.M)


def _documented() -> dict[str, str]:
    """``name -> tier`` for every row of the document's export table."""
    text = API_DOC.read_text(encoding="utf-8")
    return dict(_ROW.findall(text))


def test_the_document_exists_at_the_repository_root_and_is_tracked() -> None:
    """★ **Criterion B's premise, and M1-S-FIX's lesson.**

    ``docs/`` is gitignored, which is exactly how a specification ends up living on one
    machine. So the document is at the root beside ``CONFIG-FORMAT.md`` and
    ``RUN-REPORT-FORMAT.md``, the other two contracts this project keeps, and this asserts
    it is where it is supposed to be before anything reads it.
    """
    assert API_DOC.is_file(), f"{API_DOC} is missing; the contract has to be in the repository"
    assert API_DOC.parent.name != "docs", "docs/ is gitignored; the contract cannot live there"


def test_the_document_and_the_export_list_name_the_same_things() -> None:
    """★ **Criterion B, made checkable: the document and the code cannot drift.**

    A public API described in a file and a public API exported from a package are two
    statements about the same set, and two statements drift. So the table's names are
    compared against ``__all__`` -- both directions, because a name exported but
    undocumented is a promise nobody was told, and a name documented but not exported is a
    promise the code does not keep.
    """
    documented = set(_documented())
    exported = set(gigaxml.__all__) - {"__version__"}

    assert documented == exported, (
        f"documented but not exported: {sorted(documented - exported)}; "
        f"exported but not documented: {sorted(exported - documented)}"
    )


def test_the_document_says_the_internal_layout_is_not_a_contract() -> None:
    """★ **Criterion E, and the sentence that licensed M7--M9, asserted rather than hoped.**

    "Anything not listed may change in a minor release" is the rule; this is the test that
    it is still written down.

    ★ **The claim and the assertion have to be about the same thing, and the first version
    of this test was not.** It checked that the word "internal" appeared somewhere, that
    the three package names appeared somewhere, and that some sentence containing "not a
    contract" appeared somewhere -- three independent conditions, none of which required
    them to be *about each other*. Deleting every statement that the internal layouts are
    outside the contract left all three true, because §3.2 says the GUI's layout "are not
    a contract" and that is a sentence about widgets rather than about
    ``gigaxml.cli``. Both mutations were caught once this demanded one **paragraph** naming
    a package and saying its layout is not promised.
    """
    text = API_DOC.read_text(encoding="utf-8")
    # Whitespace collapsed per paragraph before anything is matched. The licence sentence is
    # wrapped across two source lines ("...is not part of the\ncontract..."), so a literal
    # substring search misses it -- **the first version of this check failed on the
    # unmodified document for exactly that reason.** A reader does not see a line break
    # inside a sentence, and neither should a check that exists to say what a reader sees.
    paragraphs = [" ".join(block.split()) for block in text.split("\n\n") if block.strip()]
    packages = ("gigaxml.cli", "gigaxml.inspect", "gigaxml.gui")

    for package in packages:
        assert any(package in block for block in paragraphs), (
            f"{package} is never named in the document, so a reader is not told it exists "
            "and therefore not told that its layout may move"
        )

    outside = ("not part of the contract", "not in the contract", "not a contract")
    together = [
        block
        for block in paragraphs
        if any(phrase in block.lower() for phrase in outside)
        and any(package in block for package in packages)
    ]
    assert together, (
        "no single passage both names one of "
        f"{', '.join(packages)} and says its internal layout is outside the contract. "
        "That sentence is what made the M7-M9 splits shippable, and a reader has to be "
        "able to find it -- a sentence about the GUI's widgets does not make it."
    )


def test_every_example_in_the_document_runs() -> None:
    """★ **Criterion B's second half: "working" is the load-bearing word.**

    Criterion B asks for the minimum working example for each stable API, and pseudo-code
    tells a reader nothing about whether a call is real -- which is the thing they are
    trying to find out. So the examples are **extracted and executed**, each in its own
    namespace so a name in one block cannot satisfy a missing definition in another.

    Each block writes its own document and config, which is also what makes them worth
    copying: the reader pastes one block and it runs, rather than assembling the project
    layout first.
    """
    text = API_DOC.read_text(encoding="utf-8")
    fence = "```python" + chr(10)
    blocks = [block.split("```")[0] for block in text.split(fence)[1:]]
    assert len(blocks) >= 3, f"expected at least three python examples, found {len(blocks)}"

    for index, block in enumerate(blocks, start=1):
        namespace: dict[str, object] = {"__name__": f"python_api_example_{index}"}
        # Running the document's own examples *is* the assertion.
        exec(compile(block, f"python-api.md#example-{index}", "exec"), namespace)
