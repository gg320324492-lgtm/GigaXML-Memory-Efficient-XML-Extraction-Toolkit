"""Namespaced schemas, ambiguous names, and unresolvable imports.

Every fixture in here is written the way a real schema is written: a target namespace,
prefixed type references, ``xs:import`` of files in the same directory, and ``xs:choice``
inside the record type. That is not decoration. ``tests/golden/test_known_defects.py``
records that M1 through M15 only ever pointed this project at schemas without a target
namespace, and that the two M16 defects were invisible until one was fetched from the
internet -- so a fixture shaped to suit the code under test proves nothing here, and the
real FHIR test at the bottom is the one that counts.

The defects these cover were repaired in M16-FIX; the pins that recorded them as open are
in ``tests/golden/test_known_defects.py`` and keep their history.
"""

from __future__ import annotations

import pathlib
from typing import Final

import pytest

REPO_ROOT: Final = pathlib.Path(__file__).resolve().parents[2]
FHIR_ALL: Final = REPO_ROOT / "data" / "fhir" / "xsd" / "fhir-all.xsd"

#: The three HL7 namespaces this file uses. ``XML`` is not arbitrary: ``xmlschema`` ships
#: a copy of it inside itself, which is what lets an import of it fail *silently* by
#: falling back to that copy rather than by stopping the compile.
XML_NS: Final = "http://www.w3.org/XML/1998/namespace"
NS_A: Final = "urn:gigaxml:xsd-namespaces:a"
NS_B: Final = "urn:gigaxml:xsd-namespaces:b"
NS_ROOT: Final = "urn:gigaxml:xsd-namespaces:root"

#: The number HL7's fhir-all.xsd declares. Pinned rather than derived, because "it still
#: compiles" is also true of a schema compiled from half its files; the count is what
#: says the whole set was read.
FHIR_ELEMENT_COUNT: Final = 146

requires_fhir = pytest.mark.skipif(
    not FHIR_ALL.is_file(),
    reason=(
        "the FHIR R4 schema set is not on this machine -- 150 files, ~3 MB, not committed "
        "to the repository. Fetch it with `python examples/fhir/fetch.py`."
    ),
)


def schema_doc(
    target_namespace: str | None, body: str, *, extra_ns: tuple[tuple[str, str], ...] = ()
) -> str:
    """A schema document declaring ``target_namespace``, containing ``body``.

    Three things have to line up for a namespaced form to be *valid* rather than merely
    different: ``elementFormDefault="qualified"``, a prefix bound to the target so a
    prefixed type reference resolves, and no prefix at all for the XML namespace, which
    XML forbids binding. ``extra_ns`` binds further prefixes, which is how a record type
    ends up with children in a namespace other than its own. A fixture that fails to
    compile for a reason of its own would go red under any policy and so distinguish
    nothing.
    """
    if target_namespace is None:
        attributes = ""
    elif target_namespace == XML_NS:
        attributes = f' targetNamespace="{target_namespace}" elementFormDefault="qualified"'
    else:
        bindings = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in extra_ns)
        attributes = (
            f' targetNamespace="{target_namespace}" xmlns:t="{target_namespace}"'
            + (f" {bindings}" if bindings else "")
            + ' elementFormDefault="qualified"'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"{attributes}>\n'
        f"{body}\n</xs:schema>\n"
    )


def write(directory: pathlib.Path, name: str, text: str) -> pathlib.Path:
    """Write one schema and hand back its path."""
    target = directory / name
    target.write_text(text, encoding="utf-8")
    return target


def record_type(name: str, field: str, field_type: str = "xs:string") -> str:
    """One named complex type holding one typed field."""
    return (
        f'  <xs:complexType name="{name}"><xs:sequence>'
        f'<xs:element name="{field}" type="{field_type}"/>'
        "</xs:sequence></xs:complexType>"
    )


def global_element(name: str, type_name: str | None = None) -> str:
    """One global element declaration -- what :func:`record_field_types` looks up.

    A complex type on its own is invisible to the lookup: only a *global element* is
    indexed, which is why a fixture built from types alone reports an empty schema and
    fails in a way that looks like the code being wrong.
    """
    return (
        f'  <xs:element name="{name}" type="t:{type_name}"/>'
        if type_name
        else f'  <xs:element name="{name}" type="xs:string"/>'
    )


def import_of(namespace: str, location: str) -> str:
    """One ``xs:import`` line."""
    return f'  <xs:import namespace="{namespace}" schemaLocation="{location}"/>\n'


# --- a name the schema itself declares is never ambiguous -----------------------


def test_the_schemas_own_namespace_wins_over_an_import_that_declares_the_same_name(
    tmp_path: pathlib.Path,
) -> None:
    """Same local name in three namespaces; the one this schema declares is the answer.

    The boundary of the ambiguity rule, and the reason the rule is not "any duplicate is
    an error": a record path names a local name, and the schema's own ``targetNamespace``
    is the unqualified referent of that name. Refusing here would break every schema that
    imports a vocabulary sharing a name with its own -- and the ambiguity would be
    reported for a question that has exactly one answer.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.fields import FieldType
    from gigaxml.xsd import record_field_types

    write(
        tmp_path,
        "a.xsd",
        schema_doc(
            NS_A, record_type("AType", "fromA", "xs:int") + global_element("Gadget", "AType")
        ),
    )
    write(
        tmp_path,
        "b.xsd",
        schema_doc(
            NS_B, record_type("BType", "fromB", "xs:date") + global_element("Gadget", "BType")
        ),
    )
    root = write(
        tmp_path,
        "root.xsd",
        schema_doc(
            NS_ROOT,
            import_of(NS_A, "a.xsd")
            + import_of(NS_B, "b.xsd")
            + record_type("Gadget", "mine", "xs:boolean")
            + global_element("Gadget", "Gadget"),
        ),
    )

    # Both imports declare an element by the same name, so this fixture is *also* the
    # one that fails if the lookup stops qualifying with the schema's own namespace: the
    # answer would become three candidates, or the first of them, instead of the one the
    # schema itself declares. It is the only test here that can tell those apart, because
    # in every other fixture the fallback reaches the same element the Clark name does.
    assert record_field_types(root, "/Gadget") == {"mine": FieldType.BOOL}


# --- a name only the imports declare: ambiguous, and refused -------------------


def test_two_namespaces_declaring_one_name_is_refused_rather_than_resolved(
    tmp_path: pathlib.Path,
) -> None:
    """The rule that matters: two candidates means no answer, so it raises.

    Choosing either one would return a confident field type for whichever schema
    happened to be loaded first -- a wrong answer with nothing to indicate it is wrong,
    which is the outcome this project treats as worse than no answer at all.

    The assertion is on the message as well as the type, because an ambiguity error that
    only says "ambiguous" leaves the user with nothing to act on: the whole content of
    the message is the list of namespaces they have to choose between.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.errors import SchemaError
    from gigaxml.xsd import record_field_types

    write(
        tmp_path,
        "a.xsd",
        schema_doc(
            NS_A, record_type("AType", "fromA", "xs:int") + global_element("Gadget", "AType")
        ),
    )
    write(
        tmp_path,
        "b.xsd",
        schema_doc(
            NS_B, record_type("BType", "fromB", "xs:date") + global_element("Gadget", "BType")
        ),
    )
    root = write(
        tmp_path,
        "root.xsd",
        schema_doc(
            NS_ROOT,
            import_of(NS_A, "a.xsd") + import_of(NS_B, "b.xsd") + record_type("Keeper", "k"),
        ),
    )

    with pytest.raises(SchemaError) as caught:
        record_field_types(root, "/Gadget")

    message = str(caught.value)
    assert "'Gadget'" in message
    assert f"{{{NS_A}}}Gadget" in message and f"{{{NS_B}}}Gadget" in message, (
        "the message must name both candidates in full; a bare count tells a user nothing "
        "about which namespaces they are choosing between"
    )


def test_a_name_declared_by_exactly_one_import_is_found(tmp_path: pathlib.Path) -> None:
    """The non-ambiguous case on the other side of the same rule.

    Without this, "refuse on ambiguity" would pass on an implementation that simply
    refuses everything reaching past the root schema -- and ``xs:import`` would be
    unusable, which is the whole reason the fallback exists.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.fields import FieldType
    from gigaxml.xsd import record_field_types

    write(
        tmp_path,
        "a.xsd",
        schema_doc(NS_A, record_type("AType", "sku") + global_element("Widget", "AType")),
    )
    write(
        tmp_path,
        "b.xsd",
        schema_doc(NS_B, record_type("BType", "other", "xs:int") + global_element("Cog", "BType")),
    )
    root = write(
        tmp_path,
        "root.xsd",
        schema_doc(
            NS_ROOT,
            import_of(NS_A, "a.xsd") + import_of(NS_B, "b.xsd") + record_type("Keeper", "k"),
        ),
    )

    assert record_field_types(root, "/Widget") == {"sku": FieldType.STRING}


# --- two children of one record sharing a name ---------------------------------


def test_two_child_fields_of_one_name_are_refused(tmp_path: pathlib.Path) -> None:
    """The same rule one level down, for a nested path rather than a global one.

    ``record_field_types`` walks a nested path segment by segment, and the descent used
    to take the first child whose local name matched. Two children named ``leaf`` under
    different namespaces are two fields as far as XML is concerned, so a path naming one
    of them does not say which.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.errors import SchemaError
    from gigaxml.xsd import _child_elements, _local, _open_schema, record_field_types

    # A record type drawing one child from each of two vocabularies, which is how a
    # schema merges extensions without renaming anything.
    write(tmp_path, "a.xsd", schema_doc(NS_A, global_element("leaf")))
    write(tmp_path, "b.xsd", schema_doc(NS_B, global_element("leaf", None)))
    root = write(
        tmp_path,
        "root.xsd",
        schema_doc(
            NS_ROOT,
            import_of(NS_A, "a.xsd")
            + import_of(NS_B, "b.xsd")
            + '  <xs:element name="Holder"><xs:complexType><xs:sequence>'
            '<xs:element ref="a:leaf"/><xs:element ref="b:leaf"/>'
            "</xs:sequence></xs:complexType></xs:element>",
            extra_ns=(("a", NS_A), ("b", NS_B)),
        ),
    )

    # The fixture must really contain two same-named children, or the refusal below
    # proves nothing: a policy that read nothing at all would also refuse.
    holder = _open_schema(root).get_element(f"{{{NS_ROOT}}}Holder")
    names = [_local(str(child.name)) for child in _child_elements(holder)]
    assert names.count("leaf") == 2, f"fixture changed shape: {names}"

    with pytest.raises(SchemaError) as caught:
        record_field_types(root, "/Holder/leaf")
    assert f"{{{NS_A}}}leaf" in str(caught.value)


# --- a group is not a field ----------------------------------------------------


def test_a_choice_inside_the_record_type_is_not_reported_as_a_field(tmp_path: pathlib.Path) -> None:
    """``xs:choice`` is a branch point, and it has no type to read.

    FHIR writes every resource this way and nests three deep, so a lookup that stopped at
    the first group would find no fields anywhere in the real schema set -- which is what
    this test's own fixture reproduces. The fields are the element declarations *below*
    the group, and they must be found.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.fields import FieldType
    from gigaxml.xsd import record_field_types

    source = write(
        tmp_path,
        "choice.xsd",
        schema_doc(
            NS_ROOT,
            '  <xs:complexType name="Wrapper">\n'
            "    <xs:sequence>\n"
            "      <xs:choice>\n"
            '        <xs:element name="alpha" type="xs:int"/>\n'
            '        <xs:element name="beta" type="xs:date"/>\n'
            "      </xs:choice>\n"
            "    </xs:sequence>\n"
            "  </xs:complexType>\n"
            '  <xs:element name="wrapper" type="t:Wrapper"/>',
        ),
    )

    fields = record_field_types(source, "/wrapper")
    assert fields == {"alpha": FieldType.INT, "beta": FieldType.DATE}
    assert "xs:choice" not in {str(key) for key in fields}, "a group leaked into the field set"


def test_a_record_element_declared_with_an_atomic_type_has_no_fields(
    tmp_path: pathlib.Path,
) -> None:
    """``<xs:element name="id" type="xs:string"/>`` has no child sequence at all.

    Not an empty one -- no ``content`` attribute exists, so reading one is an
    ``AttributeError``. An element like that has no fields, which is the answer, and the
    reason it is asked here is that the namespaced lookup now finds such elements it
    could not reach before.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.xsd import record_field_types

    source = write(
        tmp_path,
        "atomic.xsd",
        schema_doc(NS_ROOT, '  <xs:element name="id" type="xs:string"/>'),
    )

    assert record_field_types(source, "/id") == {}


# --- an import that could not be loaded ----------------------------------------


def test_a_missing_import_and_a_refused_import_are_reported_differently(
    tmp_path: pathlib.Path,
) -> None:
    """Two failures, two sentences, because the user's next step differs.

    A file that is not there is a file the user has to supply. A file the policy stopped
    is a boundary the user cannot argue with by creating the file. Both are
    ``GigaXMLError`` and neither is silent -- the M16 defect was that *both* were silent,
    and it would be easy to fix that by reporting them identically, which would trade one
    silent failure for one that misleads.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.errors import GigaXMLError, SchemaError, SecurityError
    from gigaxml.xsd import _open_schema

    outside = tmp_path / "outside"
    sandbox = tmp_path / "sandbox"
    outside.mkdir()
    sandbox.mkdir()
    write(outside, "secret.xsd", schema_doc(XML_NS, '<xs:element name="leak" type="xs:string"/>'))
    write(sandbox, "inside.xsd", schema_doc(XML_NS, '<xs:element name="inside" type="xs:string"/>'))

    def root_naming(location: str) -> pathlib.Path:
        return write(
            sandbox,
            "root.xsd",
            schema_doc(None, import_of(XML_NS, location)),
        )

    control = _open_schema(root_naming("inside.xsd"))
    assert f"{{{XML_NS}}}inside" in control.maps.elements, (
        "a legitimate same-directory import must keep working -- this is the no-false-"
        "positive half of the change, and it is what makes the refusals below meaningful"
    )

    with pytest.raises(SecurityError) as refused:
        _open_schema(root_naming("../outside/secret.xsd"))
    with pytest.raises(SchemaError) as absent:
        _open_schema(root_naming("nowhere.xsd"))

    assert isinstance(refused.value, GigaXMLError)
    assert isinstance(absent.value, GigaXMLError)
    assert type(refused.value) is not type(absent.value), (
        "the two failures must not collapse into one error type"
    )
    assert "refused by the schema security policy" in str(refused.value)
    assert "is not a usable XSD" in str(absent.value)
    assert "refused by the schema security policy" not in str(absent.value)


# --- the real schema set -------------------------------------------------------


@requires_fhir
def test_the_real_fhir_r4_schema_set_still_compiles() -> None:
    """The no-false-positive half, on 150 files and 296 include/import edges.

    Turning a downgraded import warning into an error is only safe if legitimate schemas
    do not produce one. FHIR is the largest public XSD set likely to be pointed at this
    tool, and it is the set that found the defects in the first place; if it raised here,
    the change would be a regression wearing a fix's clothes.
    """
    from gigaxml.xsd import _open_schema

    schema = _open_schema(FHIR_ALL)

    assert len(schema.elements) == FHIR_ELEMENT_COUNT, (
        f"expected {FHIR_ELEMENT_COUNT} global elements, got {len(schema.elements)}; a "
        "smaller number means part of the set did not compile, which a bare 'it "
        "compiled' would not have shown"
    )
    assert schema.target_namespace == "http://hl7.org/fhir"


@requires_fhir
def test_the_real_fhir_r4_schema_yields_field_types() -> None:
    """What the M16 defect actually cost: 146 elements, none of them reachable.

    Compiling is the weaker half. Before the repair a record path on this schema could
    not be resolved at all, so ``record_field_types`` -- the reason this module exists --
    had never been run against a real XSD. The fields asserted here are the ones whose
    types this project models; the complex ones (``identifier``, ``name``, ``telecom``)
    are absent by design rather than by failure, which is the same rule that keeps an
    unrecognised type from being guessed at.
    """
    from gigaxml.fields import FieldType
    from gigaxml.xsd import record_field_types

    fields = record_field_types(FHIR_ALL, "/Patient")

    assert fields["active"] is FieldType.BOOL
    assert fields["birthDate"] is FieldType.DATE
    assert fields["multipleBirthInteger"] is FieldType.INT
    assert "identifier" not in fields, "a complexType has no project type and must be absent"


# --- what a namespaced schema does to the message ------------------------------


def test_a_namespaceless_schema_error_is_word_for_word_what_it_was(tmp_path: pathlib.Path) -> None:
    """Criterion A's second half, as text: a plain schema changes nothing at all.

    "Namespaced schemas now work" is easy; the risk is the other direction, a repair that
    quietly reworded the message every existing user of a plain schema reads. The lookup
    for a schema with no target namespace is the same call with the same argument as
    before, and this pins the sentence it produces so that stays true.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")

    from gigaxml.errors import SchemaError
    from gigaxml.xsd import record_field_types

    source = write(tmp_path, "plain.xsd", schema_doc(None, record_type("PType", "sku")))

    with pytest.raises(SchemaError) as caught:
        record_field_types(source, "/nonesuch")

    message = str(caught.value)
    assert message == (
        f"the schema {str(source)!r} declares no top-level element named 'nonesuch'; it has: (none)"
    ), "the plain-schema message changed; the namespace clause must not leak into it"
