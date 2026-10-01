"""A schema may not read a file outside its own directory -- and the test must prove it.

The escape route here is a real one: ``root.xsd`` includes ``../outside/secret.xsd``,
and that file exists. Both halves matter. If the target did not exist, ``allow='all'``
would fail on it too -- an absent file opens under no policy -- so both configurations
would go red and the test would say nothing about whether the policy is on. The second
test in this file exists to keep that from becoming invisible: it compiles the same
schema **without** the policy and shows the marker element arriving, which proves the
route is real, the file is there, and only the policy stands between them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

from gigaxml.errors import GigaXMLError
from gigaxml.xsd import record_field_types
from tests.security.conftest import (
    ESCAPED_ELEMENT,
    ESCAPED_FIELD,
    declared_element,
    schema_document,
)

#: What every policy refusal says, in one sentence a user can act on.
POLICY_MESSAGE = "refused by the schema security policy"


def test_a_schema_cannot_include_a_file_outside_its_own_directory(
    escaped_tree: Path,
) -> None:
    """The escape is refused, and refused as a policy decision rather than an I/O error.

    The distinction is the point of the assertion. ``missing`` or ``permission denied``
    would mean the policy let the path through and the filesystem happened to say no --
    which is what happens today, and what stops nothing.
    """
    root = escaped_tree / "sandbox" / "root.xsd"

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_the_escape_route_real_and_the_file_is_actually_reachable(escaped_tree: Path) -> None:
    """Without the policy, the same schema reaches the same file and takes its element.

    **This is what keeps the test above honest.** It compiles the identical document
    with ``allow='all'`` -- the setting before the policy existed -- and asserts the
    marker arrives. So the escape route is real, the target is not a missing file, and
    the refusal in the other test is doing the work.

    Remove the policy from :func:`gigaxml.xsd._open_schema` and the previous test fails
    with *did not raise*, at the assertion. It does not fail because a file is absent,
    and it does not fail because of an unrelated error: it fails because something from
    outside the sandbox got in, which is the behaviour this file is here to catch.
    """
    import xmlschema

    root = escaped_tree / "sandbox" / "root.xsd"
    compiled = xmlschema.XMLSchema(str(root), allow="all", defuse="remote")

    assert ESCAPED_ELEMENT in compiled.elements
    leaked = compiled.get_element(ESCAPED_ELEMENT)
    assert [child.name.split("}")[-1] for child in leaked.type.content] == [ESCAPED_FIELD]


def test_a_policy_refusal_is_told_apart_from_a_malformed_schema(tmp_path: Path) -> None:
    """A refusal names the policy; a broken schema does not.

    Both arrive as :class:`GigaXMLError`, which is right -- one line, no traceback --
    but they mean different things to whoever reads them. A schema that is wrong wants
    editing; a resource the policy stopped wants knowing that it was stopped. One
    sentence for both is a message the user cannot act on.
    """
    broken = tmp_path / "broken.xsd"
    broken.write_text(
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '<xs:element name="x" type="xs:NoSuchBuiltInType"/>\n'
        "</xs:schema>\n",
        encoding="utf-8",
    )

    with pytest.raises(GigaXMLError) as ordinary:
        record_field_types(broken, "/x")

    message = str(ordinary.value)
    assert POLICY_MESSAGE not in message
    assert "not a usable XSD" in message


def test_the_error_is_a_gigaxml_error_so_the_cli_shows_one_line(
    escaped_tree: Path,
) -> None:
    """No library traceback can reach a user: the refusal is a domain error.

    The CLI's handler recognises ``GigaXMLError`` and prints ``error: <message>`` with
    exit 1. An ``XMLResourceBlocked`` escaping this module instead would print a
    Python traceback naming an exception class from another project.
    """
    root = escaped_tree / "sandbox" / "root.xsd"

    with pytest.raises(GigaXMLError) as refused:
        record_field_types(root, f"/{ESCAPED_ELEMENT}")

    assert isinstance(refused.value, GigaXMLError)
    # And the original is attached for a reader who wants it, not thrown away.
    assert refused.value.__cause__ is not None
    assert "XMLResource" in type(refused.value.__cause__).__name__


def test_a_config_that_asks_for_no_schema_never_reaches_the_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The extra absent, or present: a run with no ``schema:`` is untouched either way.

    The policy only exists on the path a schema compiler walks. A config without one
    must not start importing ``xmlschema`` at all -- that is what keeps a plain
    install a two-dependency install, and it is the same boundary the schema itself
    now respects. Hiding the module proves the claim rather than restating it.
    """
    import builtins

    from gigaxml.config import parse_config
    from gigaxml.xsd import apply_schema_types

    real_import = builtins.__import__

    def deny(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] == "xmlschema":
            raise ImportError("the xsd extra is deliberately absent here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)

    config = parse_config({"record": "/catalog/product", "fields": {"name": {"path": "name"}}})

    assert apply_schema_types(config) is config


def test_an_unknown_type_in_a_schema_is_an_error_not_a_silence(tmp_path: Path) -> None:
    """A schema that fails to compile for an ordinary reason still fails out loud.

    Guards the other direction: a policy that swallowed *every* exception would make
    every schema look refused, and a genuinely broken schema would look like a security
    event. The two have to stay distinguishable in both directions.
    """
    broken = tmp_path / "unknown.xsd"
    broken.write_text(
        '<?xml version="1.0"?>\n'
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'
        '<xs:element name="x" type="xs:NotAnActualType"/>\n'
        "</xs:schema>\n",
        encoding="utf-8",
    )

    with pytest.raises(GigaXMLError) as excinfo:
        record_field_types(broken, "/x")

    assert "not a usable XSD" in str(excinfo.value)


def test_a_record_path_a_schema_does_not_declare_still_says_which(tmp_path: Path) -> None:
    """The unchanged half: after the policy, an ordinary schema still answers normally.

    A change that made *all* schemas refuse would pass every rejection test here and
    break every user. This exercises the ordinary success path through the same code.
    """
    from gigaxml.fields import FieldType

    good = tmp_path / "good.xsd"
    good.write_text(schema_document(declared_element("product", "price")), encoding="utf-8")

    assert record_field_types(good, "/product") == {"price": FieldType.STRING}


def test_the_whole_core_still_runs_with_the_extra_hidden(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Criterion F: a machine with no ``xmlschema`` extracts normally.

    Hiding the module and then doing a real extraction -- parse, field extraction,
    writer -- rather than importing the core and calling it done. The claim is that
    the security policy sits *on* the optional layer and therefore disappears with it;
    if a policy import had leaked onto the parsing path, this is where it would show
    up, as an ImportError from inside a run that never asked for a schema.
    """
    import builtins

    from gigaxml.cli import main

    document = tmp_path / "src.xml"
    document.write_text(
        '<?xml version="1.0"?>\n'
        "<catalog>\n"
        '  <products><product id="1"><name>Alpha</name></product></products>\n'
        "</catalog>\n",
        encoding="utf-8",
    )
    config = tmp_path / "cfg.yaml"
    config.write_text(
        "record: /catalog/products/product\n"
        "fields:\n"
        "  product_id:\n"
        '    path: "@id"\n'
        "  name:\n"
        "    path: name\n",
        encoding="utf-8",
    )
    output = tmp_path / "out.csv"

    real_import = builtins.__import__

    def deny(name: str, *args: object, **kwargs: object) -> object:
        if name.split(".")[0] == "xmlschema":
            raise ImportError("the xsd extra is deliberately absent here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)

    assert main(["extract", str(document), "-c", str(config), "-o", str(output)]) == 0

    rows = output.read_text(encoding="utf-8").splitlines()
    assert rows[0] == "product_id,name"
    assert rows[1] == "1,Alpha"
