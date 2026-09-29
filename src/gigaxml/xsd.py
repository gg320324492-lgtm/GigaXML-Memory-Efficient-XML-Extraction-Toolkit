"""Optional XSD support: field types from a schema, and validation against one.

**This is not on the parsing path, and the arrangement is the point.** The reader in
:mod:`gigaxml.parser.streaming` does not know this module exists, imports nothing from
it, and would work unchanged if this file were deleted -- which is what the test in
``tests/integration/test_xsd_optional.py`` checks by putting an import of it on the
core path and watching a plain install fail to import. A schema is a *description of
the data*, and the project's invariant is that reading a document is decided by the
record path and the ancestor stack, never by a validator. A tool whose memory or whose
record boundaries depended on a schema library would have a second, hidden way to be
wrong.

What the schema is allowed to do is tell you what the fields *are*. Given an XSD and a
record element, it can name the type of each child, and that answer can override the
``type:`` you guessed in the config. It can also check a finished extraction against
the schema, which catches a field you forgot entirely -- a mistake no config review
finds, because the mistake is an omission rather than a typo.

``xmlschema`` is an optional dependency, under the ``xsd`` extra. Everything here
raises :class:`~gigaxml.errors.GigaXMLError` with an actionable message when it is
absent, rather than an ``ImportError`` traceback from three frames down.
"""

from __future__ import annotations

import pathlib
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from gigaxml.config import ExtractionConfig
from gigaxml.errors import GigaXMLError
from gigaxml.fields import FieldConfig, FieldType

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Mapping

__all__ = [
    "XSD_FIELD_TYPES",
    "SchemaUnavailableError",
    "apply_schema_types",
    "available",
    "record_field_types",
    "require_schema",
    "validate_document",
]

#: The extra that provides the dependency, named in the error message so a user who
#: hit it knows the one command that fixes it.
EXTRA_NAME: Final = "xsd"

#: Built-in XSD type names mapped to the project's own. Deliberately conservative: a
#: type not listed here stays a string rather than being guessed at, because a wrong
#: conversion is worse than no conversion -- ``"00123"`` read as an int is 123, and the
#: leading zeros were the data.
XSD_FIELD_TYPES: Final[Mapping[str, FieldType]] = {
    "string": FieldType.STRING,
    "normalizedString": FieldType.STRING,
    "token": FieldType.STRING,
    "anyURI": FieldType.STRING,
    "int": FieldType.INT,
    "integer": FieldType.INT,
    "long": FieldType.INT,
    "nonNegativeInteger": FieldType.INT,
    "positiveInteger": FieldType.INT,
    "double": FieldType.FLOAT,
    "float": FieldType.FLOAT,
    "decimal": FieldType.DECIMAL,
    "boolean": FieldType.BOOL,
    "date": FieldType.DATE,
}


class SchemaUnavailableError(GigaXMLError):
    """Raised when a schema is asked for and ``xmlschema`` is not installed.

    A ``GigaXMLError`` rather than an ``ImportError`` so the CLI's existing handler
    turns it into one ``error:`` line and exit 1, the same as every other deliberate
    failure -- an optional dependency being absent is a configuration problem, not a
    crash.
    """


def available() -> bool:
    """Is the optional dependency importable right now?

    Checked by import rather than by reading installed metadata, so it cannot disagree
    with what would actually happen on the next line.
    """
    try:
        import xmlschema  # noqa: F401
    except ImportError:
        return False
    return True


def require_schema() -> object:
    """Import ``xmlschema`` or explain how to get it.

    Returns:
        The ``xmlschema`` module.

    Raises:
        SchemaUnavailableError: the extra is not installed.
    """
    try:
        import xmlschema
    except ImportError as exc:
        raise SchemaUnavailableError(
            "XSD support needs the optional 'xmlschema' dependency, which is not "
            f"installed. Install it with: pip install 'gigaxml[{EXTRA_NAME}]'. "
            "Everything else gigaxml does works without it -- only `schema:` in a "
            "config and the validation helpers need this."
        ) from exc
    return xmlschema


def _open_schema(schema_path: str | pathlib.Path) -> object:
    """Compile one XSD file, reporting a bad schema as a gigaxml error."""
    xmlschema = require_schema()
    target = pathlib.Path(schema_path)
    if not target.is_file():
        raise GigaXMLError(f"the XSD file {str(target)!r} does not exist")
    try:
        return xmlschema.XMLSchema(str(target))
    except Exception as exc:  # xmlschema raises a family of its own
        raise GigaXMLError(f"{str(target)!r} is not a usable XSD: {exc}") from exc


def _local(name: str) -> str:
    """A qualified name's local part: ``{ns}price`` -> ``price``, ``p:price`` -> ``price``."""
    return name.split("}")[-1].split(":")[-1]


def _child_elements(element: object) -> list[object]:
    """The direct child elements of an ``XsdElement``.

    ``element.type.content`` is an ``XsdGroup``; iterating it yields the children of
    *this* element only. The recursive ``iter_components`` is not what is wanted here:
    it walks the whole document's schema at once, so a record's fields would come back
    with every descendant's fields mixed in, and two records at different depths of the
    same schema would come back identical.
    """
    return list(element.type.content)  # type: ignore[attr-defined]


def _find_child(element: object, name: str) -> object | None:
    """The direct child element with this local name, or ``None``."""
    for candidate in _child_elements(element):
        if _local(candidate.name) == name:  # type: ignore[attr-defined]
            return candidate
    return None


def _field_types_of(element: object) -> dict[str, FieldType]:
    """Declared types of one element's direct children and attributes."""
    types: dict[str, FieldType] = {}
    for child in _child_elements(element):
        mapped = _map_type(child.type)  # type: ignore[attr-defined]
        if mapped is not None:
            types[_local(child.name)] = mapped  # type: ignore[attr-defined]
    # attributes is a mapping of name -> declaration, unlike content which is a sequence.
    for name, declaration in element.type.attributes.items():  # type: ignore[attr-defined]
        mapped = _map_type(declaration.type)
        if mapped is not None:
            types[f"@{_local(name)}"] = mapped
    return types


def record_field_types(schema_path: str | pathlib.Path, record_path: str) -> dict[str, FieldType]:
    """The declared type of each field of the element a record path names.

    The record element is usually **nested** -- ``/catalog/products/product`` under a
    schema declaring one global ``catalog`` -- so the path is walked a segment at a time
    rather than matched against the global element list. A lookup that only searched
    the globals would say "no such element" for every real document, which is worse
    than not looking: it reads as a broken schema and sends you to the wrong file.

    Args:
        schema_path: the ``.xsd`` file.
        record_path: the record's element path, e.g. ``/catalog/products/product``. A
            leading ``//`` is accepted and ignored, since a schema says where an
            element lives and an any-ancestor config path resolves to the same one.

    Returns:
        Field name to declared type, matched by local name so a namespaced schema and
        a plain one both work. A child whose type this project does not model is
        absent rather than mapped to a guess.

    Raises:
        SchemaUnavailableError: ``xmlschema`` is not installed.
        GigaXMLError: the file is missing, unparseable, or does not declare the path.
    """
    schema = _open_schema(schema_path)
    segments = [s for s in (_local(x) for x in record_path.replace("//", "/").split("/")) if s]
    if not segments:
        raise GigaXMLError(f"the record path {record_path!r} names no element")

    try:
        element = schema.get_element(segments[0])
    except KeyError:
        element = None
    if element is None:
        declared = ", ".join(sorted(_local(e) for e in schema.elements))  # type: ignore[attr-defined]
        raise GigaXMLError(
            f"the schema {str(schema_path)!r} declares no top-level element named "
            f"{segments[0]!r}; it has: {declared or '(none)'}"
        )

    for segment in segments[1:]:
        found = _find_child(element, segment)
        if found is None:
            children = ", ".join(
                sorted(_local(c.name) for c in _child_elements(element))  # type: ignore[attr-defined]
            )
            raise GigaXMLError(
                f"the schema {str(schema_path)!r} has no element {segment!r} inside "
                f"{_local(element.name)!r}; it has: {children or '(none)'}"  # type: ignore[attr-defined]
            )
        element = found
    return _field_types_of(element)


def _map_type(declared: object) -> FieldType | None:
    """One XSD type object to a project type, or ``None`` when it is not recognised."""
    name = getattr(declared, "name", None)
    if name is None:
        return None
    name = _local(name)
    # A user-defined simpleType names its own base, which is what actually says whether
    # the value is an int. Following it one level is the difference between a schema
    # that works and one that types every column as a string.
    base = getattr(declared, "base_type", None)
    while base is not None:
        base_name = getattr(base, "name", None)
        if base_name is not None:
            mapped = XSD_FIELD_TYPES.get(_local(base_name))
            if mapped is not None:
                return mapped
        base = getattr(base, "base_type", None)
    return XSD_FIELD_TYPES.get(name)


def apply_schema_types(config: ExtractionConfig) -> ExtractionConfig:
    """A copy of ``config`` whose field types come from its schema where it declares them.

    **The schema wins, and only where it says something.** A field the schema does not
    mention, or mentions with a type this project does not model, keeps whatever the
    config said. That asymmetry is deliberate: a schema is a better authority on types
    than a human's guess, and a worse authority on which fields you want -- it describes
    the document, not the extraction, and a config that pulls in every column the schema
    happens to declare would quietly change the shape of somebody's output.

    Fields are matched by the *local name* of the last path segment, and attributes by
    their ``@name`` form, which is how they are written in a config. A path that
    descends into children (``manufacturer/name``) matches on ``name``.

    Args:
        config: a config that may or may not carry a ``schema``.

    Returns:
        A new config; the one passed in is not modified, and is returned unchanged when
        it names no schema.

    Raises:
        SchemaUnavailableError: the extra is not installed.
        GigaXMLError: the schema is missing, unparseable, or does not declare the
            record element.
    """
    if not config.schema:
        return config
    declared = record_field_types(config.schema, config.record_path)

    rewritten: list[FieldConfig] = []
    for field in config.fields:
        key = (
            field.spec.attribute or field.spec.segments[-1]
            if (field.spec.attribute or field.spec.segments)
            else None
        )
        key = f"@{key}" if field.spec.attribute else key
        mapped = declared.get(key) if key else None
        if mapped is not None and mapped is not field.type:
            rewritten.append(replace(field, type=mapped))
        else:
            rewritten.append(field)

    return replace(config, fields=tuple(rewritten))


def validate_document(
    schema_path: str | pathlib.Path, document_path: str | pathlib.Path
) -> list[str]:
    """Check a whole document against a schema and return what does not fit.

    Not usable on a document gigaxml is extracting from -- validating it would mean
    parsing it twice, which is the opposite of this tool. It is here for the smaller
    job it does well: checking a *finished extraction* whose text has been reshaped
    into a table, where the field names are known and the volume is bounded by the
    output rather than the input.

    Args:
        schema_path: the ``.xsd`` file.
        document_path: the XML document to check.

    Returns:
        One message per validation error, empty when the document is valid.

    Raises:
        SchemaUnavailable: ``xmlschema`` is not installed.
        GigaXMLError: the schema or the document could not be read.
    """
    # require_schema() is called for its error, which is the whole reason a caller
    # without the extra gets a GigaXMLError here rather than an ImportError below.
    require_schema()
    schema = _open_schema(schema_path)
    target = pathlib.Path(document_path)
    if not target.is_file():
        raise GigaXMLError(f"the document {str(target)!r} does not exist")
    try:
        errors = list(schema.iter_errors(str(target)))  # type: ignore[attr-defined]
    except Exception as exc:  # xmlschema raises a family of its own
        raise GigaXMLError(f"{str(target)!r} could not be validated: {exc}") from exc
    return [f"{'.'.join(str(part) for part in error.path)}: {error.reason}" for error in errors]
