"""The includes that must keep working, and the files that must not be able to loop.

Rejection is the easy half. A policy that refused every include would pass every
refusal test and break every user who has ever split a schema across two files, so the
permitted case is asserted first and given equal weight.

The last two tests here are about limits rather than permission: a schema is compiled
before any run begins, with no supervising thread, so a document that could keep the
compiler busy forever would hang the CLI. It does not -- ``xmlschema`` de-duplicates
includes by location, so a cycle resolves -- and the test pins that rather than
assuming it.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

from gigaxml.errors import GigaXMLError
from gigaxml.xsd import record_field_types
from tests.security.conftest import (
    ESCAPED_ELEMENT,
    INCLUDED_ELEMENT,
    INCLUDED_FIELD,
    declared_element,
    schema_document,
)

#: A ceiling rather than a tuning knob. The measured behaviour is ~0.06s for both
#: cycles below; this is 100x that, chosen so the test catches a regression to
#: unbounded recursion without ever being the reason a slow machine goes red. It is a
#: test's timeout, not a limit imposed on the product -- the product gets no such
#: setting, because a resource limit would be a second, weaker version of the policy.
CYCLE_CEILING_SECONDS = 6.0


def test_a_schema_still_includes_a_file_from_its_own_directory(include_tree: Path) -> None:
    """A harmless include resolves, and its element is usable from the including schema.

    Asserted as a value, not as "did not raise": the point is that the *other* file's
    declaration arrived intact, so the two files still compose into one schema.
    """
    from gigaxml.fields import FieldType

    root = include_tree / "root.xsd"

    assert record_field_types(root, f"/{INCLUDED_ELEMENT}") == {INCLUDED_FIELD: FieldType.STRING}


def test_an_absolute_path_inside_the_sandbox_is_not_refused(include_tree: Path) -> None:
    """``sandbox`` means "inside the schema's directory", not "written as a bare name".

    An absolute path that resolves *within* the sandbox is exactly the same file as
    the relative spelling. Refusing it would be a policy that rejects harmless input,
    which fails the stated ordering: the permissibility half is the harder one.
    """
    from gigaxml.fields import FieldType

    inner = include_tree / "inner.xsd"
    root = include_tree / "root.xsd"
    # Point at the very same file by its full path instead of its name.
    root.write_text(
        schema_document(f'<xs:include schemaLocation="{inner.as_posix()}"/>'),
        encoding="utf-8",
    )

    assert record_field_types(root, f"/{INCLUDED_ELEMENT}") == {INCLUDED_FIELD: FieldType.STRING}


def test_a_nested_include_that_escapes_is_refused(tmp_path: Path) -> None:
    """The escape does not have to be one hop; two directories down is still an escape.

    Built here rather than from the shared fixture because a nested escape needs a
    directory the schema lives *under*: the schema sits in ``sandbox/deep/``, and
    ``../../outside/secret.xsd`` climbs two levels to reach the file.
    """
    (tmp_path / "outside").mkdir()
    (tmp_path / "sandbox" / "deep").mkdir(parents=True)
    (tmp_path / "outside" / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, "leakField")),
        encoding="utf-8",
    )
    nested = tmp_path / "sandbox" / "deep" / "root.xsd"
    nested.write_text(
        schema_document('<xs:include schemaLocation="../../outside/secret.xsd"/>'),
        encoding="utf-8",
    )

    with pytest.raises(GigaXMLError, match="refused by the schema security policy"):
        record_field_types(nested, f"/{ESCAPED_ELEMENT}")


def test_a_schema_that_includes_itself_compiles_quickly(tmp_path: Path) -> None:
    """A self-referential schema terminates instead of recursing until it is killed.

    There is no depth limit to set here, and deliberately no setting at all: the
    compiler already resolves a location it has seen, so the cycle closes on its own.
    This test pins that so it cannot quietly become unbounded -- an unbounded compile
    would hang the CLI at startup with nothing to interrupt it but a kill signal.
    """
    path = tmp_path / "self.xsd"
    path.write_text(
        schema_document('<xs:include schemaLocation="self.xsd"/>'),
        encoding="utf-8",
    )

    started = time.perf_counter()
    _compile(path)
    elapsed = time.perf_counter() - started

    assert elapsed < CYCLE_CEILING_SECONDS


def test_two_schemas_that_include_each_other_terminate(tmp_path: Path) -> None:
    """The mutual case: ``a`` includes ``b``, which includes ``a``."""
    document = schema_document('<xs:include schemaLocation="{}"/>')
    (tmp_path / "a.xsd").write_text(document.format("b.xsd"), encoding="utf-8")
    (tmp_path / "b.xsd").write_text(document.format("a.xsd"), encoding="utf-8")

    started = time.perf_counter()
    _compile(tmp_path / "a.xsd")
    elapsed = time.perf_counter() - started

    assert elapsed < CYCLE_CEILING_SECONDS


def test_a_malformed_schema_is_an_error_not_a_traceback(tmp_path: Path) -> None:
    """A schema that is not well-formed XML is refused as a domain error, not a crash.

    Covers the ordinary-bad-file direction of criterion G: every input the compiler
    might choke on has to leave as a message a user can read, rather than as a
    library's traceback reaching the terminal.
    """
    malformed = tmp_path / "malformed.xsd"
    malformed.write_text("<xs:schema><unclosed", encoding="utf-8")

    with pytest.raises(GigaXMLError, match="not a usable XSD"):
        record_field_types(malformed, "/anything")


def _compile(path: Path) -> object:
    """Compile one schema through the module under test, returning the result."""
    from gigaxml.xsd import _open_schema

    return _open_schema(path)
