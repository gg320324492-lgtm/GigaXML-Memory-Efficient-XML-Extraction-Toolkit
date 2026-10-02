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
import re
import warnings
from dataclasses import replace
from typing import TYPE_CHECKING, Final
from urllib.parse import unquote, urlsplit

from gigaxml.config import ExtractionConfig
from gigaxml.errors import GigaXMLError, SchemaError, SecurityError
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


#: What a schema is allowed to pull in while it compiles: files from its own directory,
#: and nothing else. ``sandbox`` blocks a path that escapes the schema's directory
#: before it is opened; the default ``all`` opens it.
ALLOW: Final = "sandbox"

#: Whether XML entities are resolved while reading a schema. ``always`` refuses them,
#: which matches the streaming parser's own ``load_dtd=False`` / ``resolve_entities=False``
#: -- a schema has no business reading entities when a document never may. The default
#: is ``remote``, which permits them locally.
DEFUSE: Final = "always"

#: Explaining the policy where a user will read it, in the same shape as the refusal
#: above, so the error is actionable rather than a library name nobody can search for.
#:
#: ★ The path named here has to be a file a user can actually open. ``SECURITY.md`` sits
#: at the repository root and is tracked; a reference to ``docs/`` would point at a
#: directory this repository never commits, so anyone reading the error and following it
#: -- from a wheel, from a clone -- would find nothing.
_POLICY_NOTE: Final = (
    "A schema may include files from its own directory only; it cannot reach outside "
    "that directory, read a remote resource, or resolve an XML entity. See "
    "SECURITY.md."
)

#: ``file:///C:/x`` url-parses to the path ``/C:/x``; the leading slash is an artifact
#: of the URL grammar, not part of the drive. Matched before trimming it away.
_DRIVE_PREFIX: Final = re.compile(r"^/[A-Za-z]:")

#: The sentence :func:`_reject_escape` puts in the exception it raises, named as a
#: constant rather than repeated because **that string is the only trace left of a
#: refusal**. The library catches the exception and downgrades it to a warning, so by the
#: time it arrives the exception type is gone and the text is all that is left -- which
#: is what tells a blocked import apart from a file that simply is not there. Both ends
#: read this constant, so the two cannot drift apart.
_BLOCKED_PREFIX: Final = "block access to out of sandbox file"


def _refuse_by_policy(target: pathlib.Path, reason: object) -> SecurityError:
    """The error for a resource the schema policy refused.

    Deliberately *not* the same sentence as "not a usable XSD". A schema that is
    malformed and a schema the policy stopped are different problems with different
    fixes -- the first wants editing, the second wants knowing that the policy is what
    refused it -- and one message for both leaves a user unable to tell which they have.
    """
    # The reason can arrive as a whole sentence of the library's own -- a downgraded
    # warning that kept its own full stop -- so it is trimmed rather than doubled.
    return SecurityError(
        f"the XSD file {str(target)!r} was refused by the schema security policy: "
        f"{str(reason).rstrip('.')}. {_POLICY_NOTE}"
    )


def _path_from_url(url: str) -> pathlib.Path | None:
    """The filesystem path a ``file://`` URL names, or ``None`` if it does not name one.

    Written here rather than borrowed: :meth:`pathlib.Path.from_uri` is Python 3.13 and
    this package supports 3.11, while the library's own URL helpers normalize *to* a URL
    but never come back. The only fiddly case is Windows, where ``file:///C:/x`` parses
    to the path ``/C:/x`` -- hence the drive-letter guard.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("file", "") or not parsed.path:
        return None
    raw = unquote(parsed.path)
    if _DRIVE_PREFIX.match(raw):
        raw = raw[1:]
    return pathlib.Path(raw)


def _within(path: pathlib.Path, boundary: pathlib.Path) -> bool:
    """Is ``path`` inside ``boundary``? Both must already be resolved."""
    try:
        path.relative_to(boundary)
    except ValueError:
        return False
    return True


def _reject_escape(source: object, base_url: str | None, boundary: pathlib.Path) -> None:
    """Refuse a resource that resolves outside the sandbox, before it is opened.

    **This runs before the loader touches the file**, which is what makes the refusal a
    refusal rather than an I/O error that happens to appear afterwards. The path is
    resolved -- symlinks followed -- and only then compared, so a link whose literal
    name sits inside the sandbox while its target sits outside does not slip through.
    The question answered here is "which file", never "which name".

    Anything this cannot answer locally is left to the library's own ``allow`` check,
    which stays in force throughout: sources that are not strings (a stream, an element),
    remote URLs, paths that do not resolve. Declining to answer is the safe direction --
    a path this function cannot understand is still caught downstream rather than waved
    through.
    """
    from xmlschema.exceptions import XMLResourceBlocked
    from xmlschema.loaders import normalize_url
    from xmlschema.utils.urls import is_local_url

    if not isinstance(source, str):
        return
    url = normalize_url(source, base_url)
    if not is_local_url(url):
        return
    resolved_path = _path_from_url(url)
    if resolved_path is None:
        return
    try:
        resolved = resolved_path.resolve()
    except OSError:  # pragma: no cover - lets the library report the real problem
        return
    if not _within(resolved, boundary):
        raise XMLResourceBlocked(f"{_BLOCKED_PREFIX} {url}")


def _sandbox_loader_class(boundary: pathlib.Path) -> type:
    """Build the loader that measures every resource it is handed against ``boundary``.

    A class is *built* rather than declared, because the library constructs it as
    ``loader_class(maps=..., locations=..., use_fallback=...)`` -- three fixed arguments,
    with no channel for a boundary. Closing over one here is the only place it can ride
    along.

    ``load_schema`` is the method overridden, not ``include_schema`` or ``import_schema``:
    both of those end in ``load_schema``, and so does every other route to a resource
    (redefines, overrides, namespace lookups), so intercepting the one method covers all
    of them. The check happens **before** delegating, so by the time anything could
    refuse, nothing has been opened yet.
    """
    from xmlschema.loaders import SchemaLoader

    class _SandboxLoader(SchemaLoader):
        def load_schema(
            self,
            source: object,
            namespace: str | None = None,
            base_url: str | None = None,
            build: bool = False,
            partial: bool = False,
        ) -> object:
            _reject_escape(source, base_url, boundary)
            return super().load_schema(
                source,
                namespace,
                base_url,
                build,
                partial,  # type: ignore[arg-type]
            )

    return _SandboxLoader


def _open_schema(schema_path: str | pathlib.Path) -> object:
    """Compile one XSD file, reporting a bad schema as a gigaxml error.

    **This is the only place a schema is compiled**, so it is also the only place the
    policy in :data:`ALLOW` and :data:`DEFUSE` has to be stated. Both defaults are
    permissive -- ``all`` and ``remote`` -- which means a caller that passes nothing
    gets a schema compiler that reads any file the path points at and will open a
    remote resource over the network. This module passes both explicitly, with no way
    for a caller to relax them: an escape hatch for schema resources would contradict
    the reader's security defaults, which likewise take no options.

    **The sandbox boundary is this file's resolved parent directory**, and it is set
    here rather than inherited from the library. The caller named this path, so a symlink
    *at* the root points where the user meant it to; everything reached *from* the root is
    measured against what that name actually resolves to. A resource inside is fine, a
    resource outside is refused, and a link is no different from the file it leads to --
    which is the whole point of resolving before comparing.

    The rejection is classified rather than rewrapped. ``XMLResourceBlocked`` and
    ``XMLResourceForbidden`` descend from ``XMLResourceError`` and mean "the policy said
    no"; they are caught first and given the message above. Everything else -- an
    unparseable file, an undeclared namespace, a schema that references nothing that
    exists -- falls through to "not a usable XSD", which is the honest description of a
    file that is simply wrong.

    **An import that could not be loaded is not one of the tolerated cases.** The library
    satisfies a namespace it failed to import from its own bundled copy, reports that as a
    warning, and returns a schema whose declarations are silently missing. The
    boundaries still hold under that -- nothing was read, nothing was fetched -- but the
    user is handed an incomplete answer and told nothing, which is the failure this
    function exists to prevent. So ``XMLSchemaWarning`` is escalated to an exception here
    and classified by the same two buckets: one carrying :data:`_BLOCKED_PREFIX` was the
    policy speaking, the rest are a file that is wrong or absent. Raising it at the point
    it is raised is deliberate -- the library's own handler runs *inside* the compile, so
    that is the one a downgraded warning cannot escape.

    The escalation is scoped to this call and costs nothing when there is nothing to
    escalate. It does temporarily replace the process-wide warning filters, which is not
    thread-safe; that is acceptable because a schema is compiled before a run begins, in
    one call, and this package never compiles two at once.
    """
    xmlschema = require_schema()
    target = pathlib.Path(schema_path)
    if not target.is_file():
        raise SchemaError(f"the XSD file {str(target)!r} does not exist")
    # Imported here, after require_schema(), so a machine without the extra never
    # reaches it, and because these three live under xmlschema.exceptions rather than
    # on the module's top level -- reaching them from a module-level import would be
    # an ImportError at load time for anyone who installed a plain gigaxml. Only the
    # base class is named: the concrete subclasses (``XMLSchemaImportWarning`` among them)
    # are built on demand, and importing one by name raises ImportError.
    from xmlschema.exceptions import (
        XMLResourceBlocked,
        XMLResourceForbidden,
        XMLSchemaWarning,
    )

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("error", category=XMLSchemaWarning)
            return xmlschema.XMLSchema(
                str(target),
                allow=ALLOW,
                defuse=DEFUSE,
                loader_class=_sandbox_loader_class(target.resolve().parent),
            )
    except (XMLResourceBlocked, XMLResourceForbidden) as exc:
        raise _refuse_by_policy(target, exc) from exc
    except Exception as exc:  # xmlschema raises a family of its own
        # The same refusal, one layer down: by the time the library has downgraded it,
        # all that is left of the exception is the sentence _reject_escape wrote.
        if isinstance(exc, XMLSchemaWarning) and _BLOCKED_PREFIX in str(exc):
            raise _refuse_by_policy(target, exc) from exc
        raise SchemaError(f"{str(target)!r} is not a usable XSD: {exc}") from exc


def _local(name: str) -> str:
    """A qualified name's local part: ``{ns}price`` -> ``price``, ``p:price`` -> ``price``."""
    return name.split("}")[-1].split(":")[-1]


#: The XML Schema language's own namespace. Its globals sit in ``maps.elements`` of
#: *every* compiled schema, because the compiler builds the meta-schema into the maps --
#: they are not declarations from the schema under test, so a lookup that treated them as
#: such would report a ``{...XMLSchema}element`` as a record element of somebody's data.
_XSD_NAMESPACE: Final = "http://www.w3.org/2001/XMLSchema"


def _global_element(schema: object, name: str, schema_path: object) -> object | None:
    """The one global element whose local name is ``name``, or ``None`` if there is none.

    **The library indexes globals by Clark name.** A schema with a ``targetNamespace``
    registers ``product`` under ``{urn:example}product``, and a lookup by the bare name
    returns ``None`` -- which used to be reported to the user as "this schema declares no
    such element", while the very next clause of the same sentence listed it. The fix is
    not to soften that message but to make the question answerable: qualify the name with
    the schema's own namespace first, which is where a record path means "the element
    this schema declares".

    Failing that, the name is looked for across every namespace the schema loaded, because
    ``xs:import`` exists precisely so a schema can use elements it does not declare itself.

    **More than one hit is an error.** A record path names a local name and nothing else,
    so two schemas declaring ``Gadget`` leave the question genuinely unanswered, and
    picking one would return a confident type for the wrong field. That is the silent
    wrong answer this project treats as worse than no answer, so it raises.

    Returns:
        The element, or ``None`` when no loaded schema declares the name at all -- which
        is the only case in which the caller's "no such element" message can be true.
    """
    namespace = getattr(schema, "target_namespace", "") or ""
    qualified = f"{{{namespace}}}{name}" if namespace else name
    try:
        found = schema.get_element(qualified)  # type: ignore[attr-defined]
    except KeyError:  # pragma: no cover - the library returns None for an absent name
        found = None
    if found is not None:
        return found

    candidates = sorted(
        key
        for key in schema.maps.elements  # type: ignore[attr-defined]
        if _local(key) == name and not key.startswith(f"{{{_XSD_NAMESPACE}}}")
    )
    if len(candidates) > 1:
        raise SchemaError(
            f"the schema {str(schema_path)!r} has {len(candidates)} global elements "
            f"named {name!r}, one per namespace: {', '.join(candidates)}. A record path "
            "names a local name only, so which of them it means is not decided here."
        )
    if candidates:
        return schema.get_element(candidates[0])  # type: ignore[attr-defined]
    return None


def _field_declarations(content: object) -> list[object]:
    """The element declarations reachable from one ``content`` sequence.

    A ``<xs:choice>`` or ``<xs:all>`` written inside a record's type is not a field. It
    is a branch point, and it arrives as an ``XsdGroup`` carrying no ``.type`` at all --
    asking one for its type is an ``AttributeError``, not an empty field. FHIR writes
    every resource that way and nests three deep, so a lookup that stopped at the first
    group would report no fields anywhere in the real-world schema set this project has
    been pointed at. Groups are descended; only element declarations come back.

    ``XsdElement`` has a ``.content`` of its own -- the child sequence of *that* element --
    so ``.content`` cannot tell the two apart and ``.type`` is the test. An element is
    collected, never descended into, which is what keeps a record's fields free of its
    descendants'.
    """
    found: list[object] = []
    for item in content:  # type: ignore[union-attr]
        if hasattr(item, "type"):
            found.append(item)
        elif hasattr(item, "content"):
            found.extend(_field_declarations(item.content))
    return found


def _child_elements(element: object) -> list[object]:
    """The direct child elements of an ``XsdElement``.

    ``element.type.content`` is an ``XsdGroup``; iterating it yields the children of
    *this* element only. The recursive ``iter_components`` is not what is wanted here:
    it walks the whole document's schema at once, so a record's fields would come back
    with every descendant's fields mixed in, and two records at different depths of the
    same schema would come back identical.

    A record element declared with an atomic type -- ``<xs:element name="id"
    type="xs:string"/>`` -- has no ``content`` attribute at all rather than an empty
    sequence, so it has no children by this route and by any other.
    """
    return _field_declarations(getattr(element.type, "content", ()))  # type: ignore[attr-defined]


def _find_child(element: object, name: str) -> object | None:
    """The direct child element with this local name, or ``None``.

    More than one match is an error, not the first one. Two children sharing a local name
    under different namespaces are two distinct fields as far as XML is concerned, and a
    record path carries a local name only, so returning either of them is a coin toss
    dressed up as an answer.
    """
    matches = [
        candidate
        for candidate in _child_elements(element)
        if _local(candidate.name) == name  # type: ignore[attr-defined]
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise SchemaError(
            f"{_local(element.name)!r} has {len(matches)} child elements named "  # type: ignore[attr-defined]
            f"{name!r}, one per namespace: "
            f"{', '.join(sorted(str(c.name) for c in matches))}. "
            "A field path names a local name only, so which of them it means is not "
            "decided here."
        )
    return matches[0]


def _field_types_of(element: object) -> dict[str, FieldType]:
    """Declared types of one element's direct children and attributes."""
    types: dict[str, FieldType] = {}
    for child in _child_elements(element):
        mapped = _map_type(child.type)  # type: ignore[attr-defined]
        if mapped is not None:
            types[_local(child.name)] = mapped  # type: ignore[attr-defined]
    # attributes is a mapping of name -> declaration, unlike content which is a sequence.
    for name, declaration in getattr(element.type, "attributes", {}).items():  # type: ignore[attr-defined]
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
        a plain one both work -- the global element is found under its Clark name and
        everything below it by local name, which is how a config writes a path. A child
        whose type this project does not model is absent rather than mapped to a guess.

    Raises:
        SchemaUnavailableError: ``xmlschema`` is not installed.
        GigaXMLError: the file is missing, unparseable, or does not declare the path --
            including the case where the name is declared in more than one namespace, in
            which the schema says so rather than choosing one.
    """
    schema = _open_schema(schema_path)
    segments = [s for s in (_local(x) for x in record_path.replace("//", "/").split("/")) if s]
    if not segments:
        raise SchemaError(f"the record path {record_path!r} names no element")

    element = _global_element(schema, segments[0], schema_path)
    if element is None:
        declared = ", ".join(sorted(_local(e) for e in schema.elements))  # type: ignore[attr-defined]
        namespace = getattr(schema, "target_namespace", "") or ""
        scope = f" in namespace {namespace!r} or in any namespace it imports" if namespace else ""
        # The old wording contradicted itself -- "declares no element named 'product'"
        # followed by "it has: product" -- because it listed what the schema declares
        # while asking a question the Clark-notation index could not answer. That cannot
        # happen now: the list is a subset of what was just searched, and the search
        # covered every loaded namespace, so anything in the list would have answered
        # the question. The clause names what was searched, which is what was hidden.
        raise SchemaError(
            f"the schema {str(schema_path)!r} declares no top-level element named "
            f"{segments[0]!r}{scope}; it has: {declared or '(none)'}"
        )

    for segment in segments[1:]:
        found = _find_child(element, segment)
        if found is None:
            children = ", ".join(
                sorted(_local(c.name) for c in _child_elements(element))  # type: ignore[attr-defined]
            )
            raise SchemaError(
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
        raise SchemaError(f"the document {str(target)!r} does not exist")
    try:
        errors = list(schema.iter_errors(str(target)))  # type: ignore[attr-defined]
    except Exception as exc:  # xmlschema raises a family of its own
        raise SchemaError(f"{str(target)!r} could not be validated: {exc}") from exc
    return [f"{'.'.join(str(part) for part in error.path)}: {error.reason}" for error in errors]
