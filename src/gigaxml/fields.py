"""Field extraction from one record element.

This module is deliberately built around a single constraint: **a record element
is only valid inside the loop body it was yielded in.** The reader clears and
unlinks the record as soon as the consumer asks for the next one, so nothing here
may keep an ``Element`` past the call it was handed to. That is why
:func:`extract_record` returns a dict of scalars rather than a tree of elements,
and why the only element references held anywhere in this file are the ones inside
:func:`_matches_for`'s memo, which is created and dropped within a single
``extract_record`` call.

**Field paths are relative to the record element** and Phase 2 implements only
five forms:

===============  ==========================================
``@id``          an attribute of the record element itself
``.``            the record element's own text
``Name``         the text of a child element
``Manufacturer/Name``  step down through children, then text
``Price/@currency``    step down, then read an attribute
===============  ==========================================

Absolute paths (``/`` or ``//``) belong to the *record* path and are rejected
here. So are ``..``, predicates, wildcards and functions -- Phase 2 is not a
partial XPath implementation, and silently ignoring a construct a user wrote
would be worse than refusing it.

**Text is normalised the same way everywhere:**

    ``"".join(elem.itertext())`` -> collapse whitespace runs -> strip

``itertext()`` includes the indentation of every nested element, so a price
element written as ``<price>\\n  49.90\\n</price>`` would otherwise coerce with
leading and trailing whitespace. That trap cost time in Phase 1; the rule is
defined once here and pinned by tests.

**Cost.** Descent finds children with C-level ``findall``, memoized per
``(node, tag)`` for the duration of one :func:`extract_record` call. The memo is
what stops a config that reads several fields out of the same wrapper --
``manufacturer/name`` plus ``manufacturer/country`` -- from rescanning the
record's children once per field: on a 5 004-child record with 50 such fields it
is a 7.2x win, and it costs ~3% when the fields' first segments are all distinct.
Bucketing every child by tag in one Python pass was measured and rejected as
~50x more expensive per child than ``findall``; see :func:`_matches_for`.
"""

from __future__ import annotations

import datetime
import decimal
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final

from lxml import etree

from gigaxml.errors import FieldPathError, FieldTypeError, MissingRequiredFieldError
from gigaxml.paths import resolve_attribute_name, resolve_segments

__all__ = [
    "ExtractionResult",
    "FieldConfig",
    "FieldPathSpec",
    "FieldType",
    "coerce_value",
    "extract_record",
    "normalize_text",
    "parse_field_path",
]

#: A plain integer literal. Underscores are rejected deliberately: ``int("1_000")``
#: is ``1000`` in Python, which would silently accept a value no XML producer
#: meant as a number.
_INTEGER_LITERAL: Final = re.compile(r"^[+-]?\d+$")

#: A plain decimal or scientific literal. Rejects ``nan``/``inf``/hex/underscores:
#: a non-finite number in an extracted field poisons every later aggregate, and
#: the source document has no way to mean one.
_NUMERIC_LITERAL: Final = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")

#: Accepted spellings for a boolean, case-insensitively.
_BOOL_TRUE: Final = frozenset({"true", "1", "yes"})
_BOOL_FALSE: Final = frozenset({"false", "0", "no"})

#: Constructs Phase 2 refuses rather than partially supports.
#:
#: ``*``/``[``/``]``/``(``/``)`` cannot occur in a legal XML name, so a substring
#: scan is safe and gives a much better message than "not a valid XML name".
#: ``..`` is *not* in this list: ``.`` is a legal name character, so ``x..y`` is a
#: valid element name and only a whole ``..`` segment is a parent step.
_UNSUPPORTED_MARKERS: Final = (
    ("*", "a wildcard ('*')"),
    ("[", "a predicate ('[...]')"),
    ("]", "a predicate ('[...]')"),
    ("(", "a function call"),
    (")", "a function call"),
)


class FieldType(Enum):
    """The declared type of a field, and how its text is converted.

    ``decimal`` exists because a price field converted with ``float`` is simply
    wrong: ``0.1 + 0.2`` is not ``0.3``, and money must not accumulate binary
    rounding error. Values of this type are :class:`decimal.Decimal` instances.
    """

    STRING = "string"
    INT = "int"
    FLOAT = "float"
    DECIMAL = "decimal"
    BOOL = "bool"
    DATE = "date"


@dataclass(frozen=True, slots=True)
class FieldPathSpec:
    """A parsed field path, relative to the record element.

    Attributes:
        segments: qualified tags to descend through, outermost first. Empty when
            the path targets the record element itself (``"."``) or only its
            attributes (``"@id"``).
        attribute: the qualified attribute name to read, or ``None`` to read text.
    """

    segments: tuple[str, ...]
    attribute: str | None

    @property
    def targets_self(self) -> bool:
        """``True`` when the path does not descend into any child element."""
        return not self.segments


@dataclass(frozen=True, slots=True)
class FieldConfig:
    """A validated field definition.

    Built by :func:`gigaxml.config.parse_config`; the dataclass itself performs no
    validation, so library code never constructs one by hand.

    Attributes:
        name: the key the extracted value is reported under.
        raw_path: the path exactly as written in the config, for error messages.
        spec: the parsed path.
        type: the declared type.
        required: when ``True``, a missing value raises
            :class:`~gigaxml.errors.MissingRequiredFieldError` instead of
            yielding ``None``.
    """

    name: str
    raw_path: str
    spec: FieldPathSpec
    type: FieldType
    required: bool


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """The outcome of extracting one record.

    Attributes:
        values: one entry per configured field; ``None`` for a missing optional
            field. Values are plain Python objects -- never elements.
        multi_matches: how many candidate elements were *discarded* because a
            descent step matched more than one child. A field is absent from this
            mapping when its path was unambiguous. Phase 5's run report uses this
            to warn about configs that silently pick the first of several
            candidates.
    """

    values: dict[str, object]
    multi_matches: dict[str, int]


def parse_field_path(
    path: str,
    namespaces: Mapping[str, str] | None = None,
) -> FieldPathSpec:
    """Parse a field path relative to the record element.

    See the module docstring for the five supported forms. Each segment resolves
    its own namespace prefix, and the default namespace applies to bare *element*
    names but never to attribute names.

    Args:
        path: e.g. ``"@id"``, ``"."``, ``"Name"``, ``"Manufacturer/Name"``,
            ``"Price/@currency"``.
        namespaces: prefix-to-URI map; the empty string is the default namespace.

    Returns:
        The parsed spec.

    Raises:
        FieldPathError: the path is empty, absolute, has an empty segment, puts an
            attribute anywhere but last, names an empty attribute, uses an
            unsupported construct (``..``, ``*``, predicates, functions), or uses
            a namespace prefix that is not in ``namespaces``.
    """
    if not isinstance(path, str) or not path.strip():
        raise FieldPathError(f"field path is empty: {path!r}")

    text = path.strip()
    if text.startswith("/"):
        raise FieldPathError(
            f"field path must be relative to the record element, not absolute: {path!r}; "
            f"a leading '/' or '//' is only meaningful for the record path"
        )
    for marker, description in _UNSUPPORTED_MARKERS:
        if marker in text:
            raise FieldPathError(
                f"field path uses {description}, which Phase 2 does not support: {path!r}"
            )

    if text == ".":
        return FieldPathSpec(segments=(), attribute=None)

    raw_segments = text.split("/")
    if any(not segment for segment in raw_segments):
        raise FieldPathError(f"field path has an empty segment: {path!r}")
    if any(segment == ".." for segment in raw_segments):
        raise FieldPathError(
            f"field path uses a parent step ('..'), which Phase 2 does not support: {path!r}"
        )

    attribute: str | None = None
    if raw_segments[-1].startswith("@"):
        attribute = raw_segments[-1][1:]
        if not attribute:
            raise FieldPathError(f"field path has an empty attribute name: {path!r}")
        raw_segments = raw_segments[:-1]

    for segment in raw_segments:
        if segment.startswith("@"):
            raise FieldPathError(
                f"field path may only read an attribute from its last segment: {path!r}"
            )
    if not raw_segments and attribute is None:
        raise FieldPathError(f"field path names neither an element nor an attribute: {path!r}")

    ns_map: Mapping[str, str] = namespaces or {}
    resolved_attribute = (
        None
        if attribute is None
        else resolve_attribute_name(
            attribute, ns_map, error_cls=FieldPathError, source="field path"
        )
    )
    return FieldPathSpec(
        segments=tuple(
            resolve_segments(raw_segments, ns_map, error_cls=FieldPathError, source="field path")
        ),
        attribute=resolved_attribute,
    )


def normalize_text(elem: etree._Element) -> str:
    """Whitespace-normalised text of ``elem`` and all of its descendants.

    ``"".join(elem.itertext())`` -> collapse every whitespace run to one space ->
    strip. ``str.split()`` already splits on arbitrary whitespace runs and drops
    leading and trailing ones, so the collapse and the strip are the same step.

    Nested markup is flattened into the text: ``<a>x<b>y</b>z</a>`` normalises to
    ``"xyz"``. Element boundaries are not turned into spaces, because that would
    invent whitespace that is not in the document.
    """
    return " ".join("".join(elem.itertext()).split())


def coerce_value(text: str, type_: FieldType, field_name: str) -> object:
    """Convert normalised ``text`` to ``type_``.

    Args:
        text: the already-normalised field text.
        type_: the declared type.
        field_name: the configured name, used for the error message.

    Returns:
        A ``str``, ``int``, ``float``, :class:`decimal.Decimal`, ``bool`` or
        :class:`datetime.date`.

    Raises:
        FieldTypeError: the text is not convertible. The exception carries both
            ``field`` and ``raw`` so Phase 5 can write them to ``rejected.jsonl``
            without re-parsing the message.
    """
    if type_ is FieldType.STRING:
        return text

    if type_ is FieldType.INT:
        if not _INTEGER_LITERAL.match(text):
            raise _type_error(field_name, text, type_, "expected a whole number")
        return int(text)

    if type_ is FieldType.FLOAT:
        if not _NUMERIC_LITERAL.match(text):
            raise _type_error(field_name, text, type_, "expected a decimal number")
        return float(text)

    if type_ is FieldType.DECIMAL:
        if not _NUMERIC_LITERAL.match(text):
            raise _type_error(field_name, text, type_, "expected a decimal number")
        return decimal.Decimal(text)

    if type_ is FieldType.BOOL:
        lowered = text.lower()
        if lowered in _BOOL_TRUE:
            return True
        if lowered in _BOOL_FALSE:
            return False
        raise _type_error(
            field_name,
            text,
            type_,
            "expected one of true/false, 1/0, yes/no (case-insensitive)",
        )

    if type_ is FieldType.DATE:
        try:
            return datetime.date.fromisoformat(text)
        except ValueError as exc:
            raise _type_error(field_name, text, type_, f"expected an ISO date ({exc})") from exc

    raise AssertionError(f"unhandled field type {type_!r}")  # pragma: no cover


def extract_record(
    record: etree._Element,
    fields: Sequence[FieldConfig],
) -> ExtractionResult:
    """Extract every configured field from one record element.

    The record and everything under it are read **during this call** and reduced
    to plain Python values; no element reference survives it. See the module
    docstring for why that matters.

    **Cost.** Descent asks the tree for the children matching one tag per path
    step. ``findall`` is C-level and cheap (measured ~3.4 ns per child scanned),
    so the thing worth avoiding is scanning the *same* tag twice: a config that
    reads ``manufacturer/name`` and ``manufacturer/country`` used to walk the
    record's children once per field. Matches are therefore memoized per
    ``(node, tag)`` for the duration of this call, which drops the repeated
    factor without giving up C-level scanning.

    Args:
        record: the element yielded by :class:`~gigaxml.parser.streaming.StreamingRecordReader`.
        fields: the validated field definitions, normally ``config.fields``.

    Returns:
        The extracted values plus per-field multi-match counts.

    Raises:
        MissingRequiredFieldError: a ``required`` field was absent.
        FieldTypeError: a present field's text did not convert.
    """
    values: dict[str, object] = {}
    multi_matches: dict[str, int] = {}
    # Shared for the whole call. See `_matches_for` for why this is keyed by
    # (node, tag) rather than holding a per-node bucket of all children.
    cache: dict[tuple[etree._Element, str], list[etree._Element]] = {}

    for field in fields:
        node, discarded = _descend(record, field.spec, cache)
        if discarded:
            multi_matches[field.name] = discarded

        if node is None:
            _record_missing(field, values)
            continue

        if field.spec.attribute is not None:
            raw = node.get(field.spec.attribute)
            if raw is None:
                _record_missing(field, values)
                continue
            text = " ".join(raw.split())
        else:
            text = normalize_text(node)

        values[field.name] = coerce_value(text, field.type, field.name)

    return ExtractionResult(values=values, multi_matches=multi_matches)


def _matches_for(
    node: etree._Element,
    tag: str,
    cache: dict[tuple[etree._Element, str], list[etree._Element]],
) -> list[etree._Element]:
    """``node.findall(tag)``, memoized per ``(node, tag)`` for one extraction call.

    **Why not bucket every child by tag in one Python pass?** Because that was
    measured and it is slower. On a 5 004-child record, one C-level ``findall``
    costs ~17 us (~3.4 ns per child), while a Python pass building
    ``{tag: [children]}`` costs ~900 us (~180 ns per child) -- about 50x more per
    child. Bucketing only wins past roughly 50 *distinct* tags at a single node,
    which no plausible config reaches, and it made the measured end-to-end case
    6.5x slower. An XPath union (``./a|./b``) was also measured: 172 us against
    120 us for seven separate ``findall`` calls, and lxml's XPath rejects Clark
    notation, so it cannot even express a namespaced tag.

    What *is* worth removing is rescanning the same tag. With the memo, a config
    reading ``manufacturer/name`` and ``manufacturer/country`` scans the record's
    children once for ``manufacturer`` instead of once per field.

    The returned list is the live list from ``findall``: callers must not mutate
    it, and must not keep it beyond this call. Children stay in document order,
    so "first match wins" keeps meaning the same thing.
    """
    key = (node, tag)
    found = cache.get(key)
    if found is None:
        found = node.findall(tag)
        cache[key] = found
    return found


def _descend(
    record: etree._Element,
    spec: FieldPathSpec,
    cache: dict[tuple[etree._Element, str], list[etree._Element]],
) -> tuple[etree._Element | None, int]:
    """Walk ``spec.segments`` down from ``record``.

    Returns:
        ``(element or None, discarded)``. ``discarded`` counts the candidates
        passed over because a step matched more than one child -- the first match
        wins, but the ambiguity is reported rather than hidden.
    """
    node = record
    discarded = 0
    for tag in spec.segments:
        matches = _matches_for(node, tag, cache)
        if not matches:
            return None, discarded
        discarded += len(matches) - 1
        node = matches[0]
    return node, discarded


def _record_missing(field: FieldConfig, values: dict[str, object]) -> None:
    """Store ``None`` for an absent optional field, or fail loudly if required."""
    if field.required:
        raise MissingRequiredFieldError(
            f"required field {field.name!r} is missing from a record (path {field.raw_path!r})",
            field=field.name,
        )
    values[field.name] = None


def _type_error(field_name: str, raw: str, type_: FieldType, detail: str) -> FieldTypeError:
    """Build a :class:`FieldTypeError` that carries the field name and raw value."""
    shown = raw if raw else "<empty>"
    return FieldTypeError(
        f"field {field_name!r} has type {type_.value!r} but its text is not convertible: "
        f"{detail}; raw value {shown!r}",
        field=field_name,
        raw=raw,
    )
