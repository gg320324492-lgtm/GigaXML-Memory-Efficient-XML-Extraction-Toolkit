"""Security tests: where a schema is stopped from reaching things it should not.

Everything in this directory is about one boundary -- a schema is a file the *user*
chose, but the includes inside it are not, and neither is anything those includes
point at. A schema compiles before a run starts, in the same process, with no
supervision of its own, so what it is allowed to read is decided entirely by the policy
this module's tests exist to pin.

Two facts are built once, here, because getting either wrong turns every test that
depends on it into a test of nothing.

**The escaped file must really exist.** This is the trap the milestone was built
around: an out-of-sandbox path pointing at a file that is not there fails under *any*
setting -- ``allow='all'`` cannot open a file that is absent either -- so both
configurations go red and the test distinguishes nothing. It looks like it passes
because it fails, which is the worst shape a passing test can have. Every escaped case
here therefore names a **real** file carrying a marker element, and asserts on whether
*that element* could be reached. The two configurations then genuinely differ: with the
policy off, the marker lands in the schema; with it on, the compile is stopped first.

**The marker.** :data:`ESCAPED_ELEMENT` is declared only in the file outside the
sandbox. Reaching it means a file was read that should not have been.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Declared only inside the file outside the sandbox. Reaching it is the failure.
ESCAPED_ELEMENT = "outsideSecretMarker"

#: The field *inside* :data:`ESCAPED_ELEMENT`, which is what proves it was reached.
#: Asserted on rather than merely compiling, because compiling says a file was opened
#: and this says something from it ended up in the schema.
ESCAPED_FIELD = "leakField"

#: Declared only in a file beside the schema that includes it. Reaching it is success.
INCLUDED_ELEMENT = "insideMarker"
INCLUDED_FIELD = "insideField"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
_SCHEMA_OPEN = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'


def declared_element(name: str, field: str) -> str:
    """An element declaration carrying one child, as a schema body.

    **The child is not decoration.** ``record_field_types`` reads a record element's
    *content*, and a plain ``type="xs:string"`` element has none -- it is an atomic
    builtin, and asking it for ``.content`` raises ``AttributeError`` before the test
    ever reaches the assertion. A test written that way would fail for an unrelated
    reason, which is the same "red on the wrong thing" that makes a broken test
    worthless: with the policy removed it would still raise, so it would pass while
    protecting nothing.
    """
    return (
        f'<xs:element name="{name}">\n'
        "  <xs:complexType>\n"
        "    <xs:sequence>\n"
        f'      <xs:element name="{field}" type="xs:string"/>\n'
        "    </xs:sequence>\n"
        "  </xs:complexType>\n"
        "</xs:element>"
    )


def schema_document(body: str) -> str:
    """A complete XSD whose content is ``body``, inside the root element.

    The ``DOCTYPE`` case is why this helper exists rather than each test writing its
    own string: an internal entity declaration belongs **before** ``<xs:schema>``, so
    it needs a path of its own (:func:`xsd_with_doctype`). Getting either order wrong
    yields a document that is simply not well-formed, and a non-well-formed document is
    rejected under every policy setting -- a test built on one would report a refusal
    for the wrong reason and keep passing with the policy removed.
    """
    return f"{_XML_DECL}{_SCHEMA_OPEN}{body}</xs:schema>\n"


def xsd_with_doctype() -> str:
    """A well-formed schema carrying an internal entity -- legal to write, refused to read.

    The element is complex for the reason :func:`declared_element` gives: with a plain
    ``type="xs:string"`` element the *unchanged* code path after a successful compile
    raises ``AttributeError``, so a test using this would fail for that reason and not
    for the refusal when the policy setting moved. Complex, the element reads normally
    and the test fails the way it is meant to: it did not raise.
    """
    return (
        _XML_DECL
        + "<!DOCTYPE xs:schema [\n"
        + '  <!ENTITY never "resolved">\n'
        + "]>\n"
        + _SCHEMA_OPEN
        + declared_element(ESCAPED_ELEMENT, ESCAPED_FIELD)
        + "\n</xs:schema>\n"
    )


@pytest.fixture
def escaped_tree(tmp_path: Path) -> Path:
    """A schema whose include escapes its directory, with a real file to escape into.

    Returns the directory containing both trees, so a test can name either side::

        escaped_tree/
          outside/secret.xsd   real, declares ESCAPED_ELEMENT
          sandbox/root.xsd     includes ../outside/secret.xsd
    """
    outside = tmp_path / "outside"
    sandbox = tmp_path / "sandbox"
    outside.mkdir()
    sandbox.mkdir()

    (outside / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, ESCAPED_FIELD)),
        encoding="utf-8",
    )
    (sandbox / "root.xsd").write_text(
        schema_document('<xs:include schemaLocation="../outside/secret.xsd"/>'),
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture
def include_tree(tmp_path: Path) -> Path:
    """A schema with a legitimate include beside it -- the case that must keep working.

    A policy that refuses every include passes every rejection test and breaks every
    user, so the harmless case is built alongside the hostile one rather than after it.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "inner.xsd").write_text(
        schema_document(declared_element(INCLUDED_ELEMENT, INCLUDED_FIELD)),
        encoding="utf-8",
    )
    (sandbox / "root.xsd").write_text(
        schema_document('<xs:include schemaLocation="inner.xsd"/>'),
        encoding="utf-8",
    )
    return sandbox


def write_schema(directory: Path, name: str, body: str) -> Path:
    """Write one schema into ``directory`` and return its path."""
    target = directory / name
    target.write_text(schema_document(body), encoding="utf-8")
    return target
