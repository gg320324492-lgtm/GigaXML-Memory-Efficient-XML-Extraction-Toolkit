"""The three defects this milestone confirmed: two now pinned as repairs, one still open.

The file has changed shape once already, and the history is worth keeping.

**At M0** every test here asserted the *wrong* behaviour, on purpose -- "reading this
manifest accepts ``"complete": "false"``" -- so that a fix would have something to point
at. A fix proved only by its own new test proves nothing about whether the defect was
real.

**At M2** the manifest checks landed. The tests that pinned a coercion now assert the
refusal instead, keeping their names: ``test_a_known_defect_...`` records where each one
came from, and the docstring of each says what used to happen and what happens now. What
they protect has not changed -- only the direction of the assertion, from "it was
accepted" to "it is refused".

**The third defect is still open**, and its test still asserts today's behaviour. It is
marked with the milestone that owns it, because nothing here is a licence: pinning a
defect is what makes "fixed" mean something, and a defect nobody pinned can be removed
without anyone noticing.

**Why these call the library rather than the CLI.** Each defect lives in a data contract:
what a manifest is allowed to contain, what a part name may point at, and what a run's
identity covers. The CLI is where those contracts are *used*, but a test that reached
them through a subprocess would have to arrange a whole interrupted run to observe one
coercion, and would then be asserting on a report full of the very values it is trying to
probe. The command-line surface is frozen separately, in
:mod:`tests.golden.test_cli_golden`, and the one place where a manifest defect reaches
the user's screen -- a manifest that says "incomplete" being believed -- is tested there.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gigaxml.checkpoint import (
    CHECKPOINT_VERSION,
    config_identity,
    read_checkpoint,
)
from gigaxml.config import parse_config
from gigaxml.errors import CheckpointError
from tests.golden.conftest import run_cli

#: A manifest that is valid in every way the reader checks, so each test can break
#: exactly one thing and attribute the outcome to that thing.
VALID_MANIFEST: dict[str, Any] = {
    "version": CHECKPOINT_VERSION,
    "source": {"path": "source.xml", "sha256": "a" * 64, "size": 4096},
    "config": "c" * 64,
    "records_consumed": 2909,
    "rejected": 3,
    "parts": [{"name": "part-00000.csv", "rows": 2906}],
    "complete": True,
}


def write_manifest(directory: Path, **overrides: object) -> Path:
    """Write a manifest built from the valid one, with the given keys replaced."""
    raw = json.loads(json.dumps(VALID_MANIFEST))
    raw.update(overrides)
    path = directory / "checkpoint.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


# --- defect 1: a manifest was coerced rather than validated -------------------
#
# read_checkpoint applied str() / int() / bool() to each field. Every one of those
# conversions is total: it raised on nothing a JSON document can contain, so a manifest
# could say almost anything and still be believed. The field checks replace them with
# exact type tests, and these tests now record the refusal each one produces.


def test_a_known_defect_string_false_is_read_as_complete(tmp_path: Path) -> None:
    """``"complete": "false"`` is refused instead of being read as ``True``.

    **What it used to do**: ``bool("false")`` is ``True`` because the string is
    non-empty, so a manifest spelling out that the run did *not* finish was accepted and
    believed -- and a resume skipped the work nobody had done. That was the worst of the
    coercions, because it failed toward "we are finished" rather than toward an error.

    **Now**: the field must be a JSON boolean, and a string is not one. The refusal
    names the field and says why a string is dangerous here, because the mistake it
    prevents -- believing a finished run that is not -- is not obvious from the value
    alone.
    """
    path = write_manifest(tmp_path, complete="false")

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_numeric_strings_are_coerced_to_ints(tmp_path: Path) -> None:
    """Numeric text is refused for both a count and a row count.

    **What it used to do**: ``"records_consumed": "100"`` and ``"rows": "5"`` became
    numbers, so a manifest written by something else -- or edited by hand, or produced by
    a serialiser that quotes everything -- resumed as though this tool had written it.

    **Now**: both must be JSON integers. Tested as two separate manifests so that a fix
    covering one key but not the other still fails here.
    """
    for broken in (
        {"records_consumed": "100"},
        {"parts": [{"name": "part-00000.csv", "rows": "5"}]},
    ):
        path = write_manifest(tmp_path, **broken)

        with pytest.raises(CheckpointError, match="cannot be trusted"):
            read_checkpoint(path)


def test_a_known_defect_rejected_is_never_compared_with_records_consumed(
    tmp_path: Path,
) -> None:
    """``rejected`` above ``records_consumed`` is refused.

    **What it used to do**: nothing checked the two against each other, so a manifest
    claiming 999,999 rejections against 2,909 records was believed and the report's
    rejection total exceeded the document it described.

    **Now**: a run cannot have quarantined more records than it read, and a manifest
    that says otherwise describes a run that cannot have happened.

    Note what is *not* asserted: this is a relation between two fields, not a type check,
    so a manifest with both fields as the right types but the wrong relationship has to
    be caught by comparing them -- which is the part this pins.
    """
    path = write_manifest(tmp_path, rejected=999999)

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_a_config_hash_of_any_type_is_accepted(tmp_path: Path) -> None:
    """A config hash that is not a hash is refused.

    **What it used to do**: ``"config": 123`` became ``"123"`` through ``str()``, which
    will stringify anything. That hash is what a resume compares against to decide
    whether this would be the same run, so a manifest carrying something that was never a
    hash was compared as though it were one.

    **Now**: it must be 64 lowercase hexadecimal characters -- exactly what
    :func:`gigaxml.checkpoint.config_identity` writes. The case is checked too: this
    tool's digests are lowercase, so an uppercased one is a file somebody edited.
    """
    path = write_manifest(tmp_path, config=123)

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(path)


def test_a_known_defect_an_incomplete_manifest_tells_resume_there_is_nothing_to_do(
    workdir: Path,
) -> None:
    """The refusal reaches the command line: the run stops instead of exiting 0.

    **What it used to do**: a manifest whose ``complete`` was the string ``"false"``
    ended a resumed run that had work left to do with exit code 0 and the words
    ``already complete; nothing to do`` -- which is why the coercion above mattered to
    anyone who was not reading the code.

    **Now**: the manifest is refused before the run decides anything, the exit code is
    non-zero, and the output the run was supposed to produce stays incomplete rather
    than being declared finished. The assertion is on all three, because any one of them
    alone would still let a partially-written output look done.
    """
    parts = workdir / "parts"
    first = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-o",
        str(parts),
        "--checkpoint-every",
        "2",
        "--format",
        "csv",
        cwd=workdir,
    )
    assert first.returncode == 0

    manifest_path = parts / "checkpoint.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Half the run: one part committed, the record after it never read.
    manifest["complete"] = "false"
    manifest["records_consumed"] = 1
    manifest["parts"] = manifest["parts"][:1]
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = run_cli(
        "extract",
        str(workdir / "src.xml"),
        "-c",
        str(workdir / "config.yaml"),
        "-o",
        str(parts),
        "--checkpoint-every",
        "2",
        "--resume",
        "--format",
        "csv",
        cwd=workdir,
    )

    assert result.returncode != 0
    assert "already complete" not in result.stderr
    assert "cannot be trusted" in result.stderr
    # And the record that was never extracted is still not in the output -- nothing
    # about the refusal may have advanced the run.
    rows = (parts / "part-00000.csv").read_text(encoding="utf-8")
    assert "Gamma Valve" not in rows


# --- defect 2: a part name was used as a path, unexamined ---------------------
#
# verify_parts resolved each manifest part name against the parts directory with no check
# on what the name was, so a name containing separators or a drive letter reached a file
# outside it. The check now happens when the manifest is read, before any path is built.


def test_a_known_defect_a_part_name_can_reach_a_file_outside_the_parts_directory(
    tmp_path: Path,
) -> None:
    """A part name that is not a part filename is refused when the manifest is read.

    **What it used to do**: ``"../outside.csv"`` was joined onto the parts directory and
    opened. The file one level above was a real CSV, so it was counted, and the only
    complaint was that its row count disagreed -- the tool had read a file the manifest
    pointed at, from outside the directory it was supposed to stay in.

    **This was a read, not a write.** Part names are generated by ``part_name(index,
    extension)`` on the writing side and never come from a manifest, so no run could be
    made to write outside the directory. The exposure was a resume opening an arbitrary
    file.

    **Now**: the name must be one this tool would have produced -- ``part-00000.csv``,
    ``.jsonl`` or ``.parquet`` -- and the refusal happens while the manifest is being
    read, so no path is built from it at all. The outside file is still there and still
    readable; the assertion that matters is that reaching for it never starts.
    """
    parts_dir = tmp_path / "parts"
    parts_dir.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id,name\n1,not this run's data\n", encoding="utf-8")

    manifest = write_manifest(parts_dir, parts=[{"name": "../outside.csv", "rows": 2906}])

    with pytest.raises(CheckpointError, match="cannot be trusted"):
        read_checkpoint(manifest)


# --- defect 3: the schema was not part of what a run is ------------------------
#
# config_identity hashed what the run depended on -- record path, namespaces, error
# policy, fields -- and did not include the XSD, which decides how those fields are
# typed. So two runs that would produce different data shared one identity.
#
# **Repaired in M4.** The test now asserts the opposite, as the M0 docstring said it
# would have to: the two identities must differ. It keeps its name because
# ``test_a_known_defect_...`` records where each assertion came from, and the body below
# says what used to happen -- a test rewritten without that history reads as though the
# property had always held.


def test_a_known_defect_the_xsd_is_absent_from_the_run_identity(tmp_path: Path) -> None:
    """Two schemas of different content no longer share an identity.

    **What it used to do**: an XSD's contents decide how every field is typed and
    therefore what the extraction writes, yet the run identity did not mention the
    schema at all. Changing the schema in place left the identity untouched,
    ``validate_resume`` compared that identity and nothing else about the schema, and
    ``--resume`` appended rows of a different shape to parts written under the old one.

    **Now**: the schema's content hash is part of the identity, so the two configs below
    are two runs and a resume between them is refused. The files are written here rather
    than named, because the assertion is about their *contents* -- two paths that do not
    exist could not tell a content-keyed identity from a path-keyed one.

    Note also what is deliberately not asserted: the same schema under a different name
    still produces the same identity, which is the property that keeps an identity about
    what a run *is* rather than where its files sit.
    """
    source = tmp_path / "catalog.xsd"
    base = {"record": "/catalog/products/product", "fields": {"name": {"path": "name"}}}

    # Each identity is taken while its own content is on disk. The path is the same both
    # times, which is the point: computing both after the second write would compare an
    # identity against itself and pass for the wrong reason.
    source.write_text(_XSD_DECLARING_STRING, encoding="utf-8")
    with_string_schema = config_identity(parse_config({**base, "schema": str(source)}))

    source.write_text(_XSD_DECLARING_DECIMAL, encoding="utf-8")
    with_decimal_schema = config_identity(parse_config({**base, "schema": str(source)}))

    assert with_string_schema != with_decimal_schema


#: Two schemas that declare the same element with different types. Both valid, and the
#: same length, so the identity can only be told apart by the bytes -- a size difference
#: would let a fingerprint that hashed only the length pass this.
_XSD_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?><xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">'
)
_XSD_DECLARING_STRING = f'{_XSD_HEAD}<xs:element name="name" type="xs:string"/></xs:schema>'
_XSD_DECLARING_DECIMAL = f'{_XSD_HEAD}<xs:element name="name" type="xs:decimal"/></xs:schema>'


# --- defects 4 and 5: found by M16, pointing this project at a real XSD for the first time
#
# Everything above in this file was found by reading. These two were found by downloading
# one: HL7's FHIR R4 schema set, which ``examples/fhir/fetch.py`` crawls and this file
# pins the consequences of. Neither is fixed, per M16 criterion E -- so each test asserts
# what happens today, and says in its docstring what a fix would have to change.
#
# **Why none of M1 through M15 found them.** Every XSD this project had been pointed at
# was written for these tests: no target namespace, and every ``xs:import`` naming a
# namespace that ``xmlschema`` does not also ship. Both assumptions are load-bearing and
# both are false of real schemas, so the guards measured the fixture rather than the
# world. That is the same shape as the M14 ``report`` self-check and the M15
# ``#``-inside-a-block-scalar findings: a guard proved against something shaped to suit it.


def _schema_doc(target_namespace: str | None, body: str) -> str:
    """A schema document declaring ``target_namespace``, containing ``body``.

    Two things have to line up for a namespaced form to be *valid* rather than merely
    different: ``elementFormDefault="qualified"``, and a prefix bound to the target so a
    prefixed type reference resolves. Without both, the schema fails to compile for a
    reason of its own, and a test built on that would go red under any policy and so
    distinguish nothing -- the trap ``tests/security/conftest.py`` is built around.

    The prefix is skipped for the XML namespace, which XML forbids binding to a prefix at
    all: binding one fails the document before any policy is consulted, and the resulting
    error names neither the element nor the sandbox.
    """
    if target_namespace is None:
        attributes = ""
    elif target_namespace == _XML_NAMESPACE:
        attributes = f' targetNamespace="{target_namespace}" elementFormDefault="qualified"'
    else:
        attributes = (
            f' targetNamespace="{target_namespace}" xmlns:t="{target_namespace}"'
            ' elementFormDefault="qualified"'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"{attributes}>\n'
        f"{body}\n</xs:schema>\n"
    )


_PRODUCT_TYPE = (
    '  <xs:complexType name="productType"><xs:sequence>'
    '<xs:element name="sku" type="xs:string"/></xs:sequence></xs:complexType>'
)
_PROBE_NS = "urn:gigaxml:known-defect:probe"

#: The namespace of the W3C XML schema. Not an arbitrary choice: ``xmlschema`` ships this
#: one inside itself, which is what makes defect 5 below reproducible at this size.
_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"

_MARKER_ELEMENT = (
    '  <xs:element name="leakMarker"><xs:complexType><xs:sequence>'
    '<xs:element name="leakField" type="xs:string"/></xs:sequence></xs:complexType>'
    "</xs:element>"
)


def test_a_known_defect_a_namespaced_schema_reports_declaring_the_element_it_could_not_find(
    tmp_path: Path,
) -> None:
    """**Found in M16, by ``examples/fhir/fetch.py``. Not fixed.**

    **What it does today**: ``record_field_types`` asks ``xmlschema`` for a top-level
    element by its bare local name. ``xmlschema`` keys a schema with a target namespace by
    the Clark-notation name ``{namespace}local``, so the lookup returns ``None`` and the
    caller reports that the schema declares no such element -- naming the element in the
    list of what it does declare::

        the schema 'fhir-all.xsd' declares no top-level element named 'Patient'; it has:
        Account, ActivityDefinition, ..., Patient, ...

    On the official HL7 FHIR R4 schema all 146 global elements are refused this way, so
    the error is not merely wrong but wrong about every element the schema has.

    **Why the two schemas below differ in exactly one thing.** Both compile; both declare
    one global element; the bodies are byte-identical apart from the prefix on the type
    reference, which the namespace requires. That is what makes the second half of this
    test the control that gives the first half its meaning. Without it, a fixture whose
    namespaced form was invalid for some other reason would report the same failure under
    any policy.

    **What a fix would have to change**: qualify the lookup, then keep matching *fields*
    by local name so a config written without prefixes still works. The error message is
    the part that hides this -- a user reads "declares no such element", checks the name,
    finds it in the same sentence, and has no reason left to suspect the namespace.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")
    import xmlschema

    from gigaxml.errors import SchemaError
    from gigaxml.fields import FieldType
    from gigaxml.xsd import record_field_types

    namespaced = tmp_path / "namespaced.xsd"
    plain = tmp_path / "plain.xsd"
    namespaced.write_text(
        _schema_doc(
            _PROBE_NS, _PRODUCT_TYPE + '  <xs:element name="product" type="t:productType"/>'
        ),
        encoding="utf-8",
    )
    plain.write_text(
        _schema_doc(None, _PRODUCT_TYPE + '  <xs:element name="product" type="productType"/>'),
        encoding="utf-8",
    )

    # Both must be valid XSDs, or the failure below could be either one being broken.
    for source in (namespaced, plain):
        assert xmlschema.XMLSchema(str(source)) is not None

    # Control: the identical schema without a namespace is read normally. If this failed,
    # the fixture would be wrong rather than the code, and the failure below meaningless.
    assert record_field_types(plain, "/product") == {"sku": FieldType.STRING}

    with pytest.raises(SchemaError) as caught:
        record_field_types(namespaced, "/product")

    message = str(caught.value)
    assert "declares no top-level element named 'product'" in message
    # The part that makes this a defect rather than a refusal: the message lists as
    # present the very element it just said was absent.
    assert "it has: product" in message, (
        "the error is expected to list the element it denies; if that stops happening the "
        "cause has changed and this pin needs rewriting rather than deleting"
    )


def test_a_known_defect_a_blocked_import_still_compiles_and_only_warns(tmp_path: Path) -> None:
    """**Found in M16. Not fixed.** The refusal holds; the *report* of it does not.

    An ``xs:import`` pointing out of the sandbox is stopped -- ``XMLResourceBlocked`` is
    raised inside ``_reject_escape``, and the outside file is genuinely not read. But
    ``xmlschema`` catches that, downgrades it to ``XMLSchemaImportWarning``, and satisfies
    the namespace from its own bundled copy, so the schema **compiles and the caller gets
    a schema back**. A user who deleted a file, or whose schema references something the
    policy refuses, gets a successful run.

    ``SECURITY.md`` records the pre-M1 behaviour as *"not merely 'it goes out to the
    network', but 'the failed fetch is silently downgraded to a Warning and the schema
    still compiles'"*, and the repaired behaviour as ``XMLResourceBlocked``. On the
    ``xs:import`` path the first half is still true: **nothing was read and nothing was
    fetched.** The second half -- telling the user -- is what came back.

    **The condition is a bundled fallback, not the escape.** Every fixture M1 wrote
    imported a namespace ``xmlschema`` does not ship, so there was nothing to fall back to
    and the block ended the compile. The namespace here is the one this fixture borrows
    from the FHIR set for exactly that reason.

    **Both halves are asserted, and neither alone would do.** ``leakMarker`` absent proves
    the file was not read; the absence of an exception proves the refusal was not
    reported. A test asserting only the first would pass on the old behaviour too.
    """
    pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional")
    import warnings

    from gigaxml.xsd import _open_schema

    outside = tmp_path / "outside"
    sandbox = tmp_path / "sandbox"
    outside.mkdir()
    sandbox.mkdir()
    (outside / "secret.xsd").write_text(
        _schema_doc(_XML_NAMESPACE, _MARKER_ELEMENT), encoding="utf-8"
    )
    (sandbox / "inside.xsd").write_text(
        _schema_doc(_XML_NAMESPACE, _MARKER_ELEMENT), encoding="utf-8"
    )

    def root_naming(location: str) -> Path:
        root = sandbox / "root.xsd"
        root.write_text(
            _schema_doc(
                None,
                f'  <xs:import namespace="{_XML_NAMESPACE}" schemaLocation="{location}"/>\n'
                + _MARKER_ELEMENT,
            ),
            encoding="utf-8",
        )
        return root

    # Control: the same import naming a file inside the sandbox. This one must both
    # compile and bring the declaration across, or the assertions below prove nothing --
    # a policy that read nothing at all would satisfy "leakMarker absent".
    control = _open_schema(root_naming("inside.xsd"))
    assert f"{{{_XML_NAMESPACE}}}leakMarker" in control.maps.elements

    root = root_naming("../outside/secret.xsd")
    assert (outside / "secret.xsd").is_file(), "the escaped file must really exist"

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        blocked = _open_schema(root)  # does NOT raise, and that is the defect

    assert f"{{{_XML_NAMESPACE}}}leakMarker" not in blocked.maps.elements, (
        "the file outside the sandbox was read -- the reach boundary is not holding"
    )
    assert caught_warnings, (
        "no warning was emitted, so the block was not even reported; if xmlschema has "
        "stopped downgrading this, a fix has landed and this pin should be rewritten"
    )
