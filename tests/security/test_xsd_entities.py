"""XML entities are refused while reading a schema, and that is a behaviour change.

The last file is where an input that used to work stops working, so it is written as
two claims rather than one. First, that a schema carrying an internal entity is refused
and that the refusal *names the entity rule* -- a message saying "schema syntax error"
would send the user to edit a file that is perfectly well-formed. Second, that this is
a change: the same file compiles under the library's own default, which is what it
compiles under before this policy. The second test is what makes the first one
readable as a decision rather than an accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

from gigaxml.errors import GigaXMLError
from gigaxml.xsd import record_field_types, validate_document
from tests.security.conftest import (
    ESCAPED_ELEMENT,
    declared_element,
    schema_document,
    xsd_with_doctype,
)

POLICY_MESSAGE = "refused by the schema security policy"


def test_a_plain_schema_still_compiles_under_the_policy(tmp_path: Path) -> None:
    """The permitted half, again, for this setting: nothing about ``defuse='always'``
    touches a schema with no entities in it."""
    from gigaxml.fields import FieldType

    plain = tmp_path / "plain.xsd"
    plain.write_text(schema_document(declared_element("product", "price")), encoding="utf-8")

    assert record_field_types(plain, "/product") == {"price": FieldType.STRING}


def test_a_schema_with_an_internal_entity_is_refused(tmp_path: Path) -> None:
    """A DOCTYPE entity declaration stops the compile, as a policy decision.

    The file is well-formed -- the ``DOCTYPE`` sits before the root element, where XML
    requires it -- so a parse error here would be a misreading of the test rather than
    a refusal. The message is asserted on in full for that reason: it has to carry the
    entity rule and the policy together, not a library complaint about syntax.
    """
    with_doctype = tmp_path / "with_entity.xsd"
    with_doctype.write_text(xsd_with_doctype(), encoding="utf-8")

    with pytest.raises(GigaXMLError) as refused:
        record_field_types(with_doctype, f"/{ESCAPED_ELEMENT}")

    message = str(refused.value)
    assert POLICY_MESSAGE in message
    assert "Entities are forbidden" in message
    assert "not a usable XSD" not in message


def test_the_same_file_would_have_compiled_before_this_policy(tmp_path: Path) -> None:
    """The behaviour change, named: this input worked, and now it does not.

    Compiled here through the library's own default for ``defuse`` -- the setting the
    policy replaces -- to show the refusal comes from the policy and not from the file.
    Without this, the previous test would be indistinguishable from a test asserting a
    schema that was always broken.
    """
    import xmlschema

    with_doctype = tmp_path / "with_entity.xsd"
    with_doctype.write_text(xsd_with_doctype(), encoding="utf-8")

    compiled = xmlschema.XMLSchema(str(with_doctype), allow="sandbox", defuse="remote")

    # It compiled: the DOCTYPE is legal, the file is well-formed, only the setting moved.
    assert ESCAPED_ELEMENT in compiled.elements


def test_validation_refuses_the_same_way_as_type_inference(tmp_path: Path) -> None:
    """The second entry point to the compiler is covered by the same policy.

    ``validate_document`` calls ``_open_schema`` too, so a policy stated only in the
    types path would leave validation open. Both callers are asserted because the
    security claim is about the module, not about one function of it.
    """
    with_doctype = tmp_path / "with_entity.xsd"
    with_doctype.write_text(xsd_with_doctype(), encoding="utf-8")
    document = tmp_path / "doc.xml"
    document.write_text("<catalog/>", encoding="utf-8")

    with pytest.raises(GigaXMLError) as refused:
        validate_document(with_doctype, document)

    assert POLICY_MESSAGE in str(refused.value)
    assert "Entities are forbidden" in str(refused.value)
