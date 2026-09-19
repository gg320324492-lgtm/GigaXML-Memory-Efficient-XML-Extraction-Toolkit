"""Path-segment resolution shared by record paths and field paths.

A record path (``/catalog/products/product``) and a field path
(``Manufacturer/Name``) look different but do the same thing to every one of
their segments: turn a name a human wrote into the *qualified* ``{uri}local``
form that lxml actually compares against. That step cannot be skipped or guessed
at -- lxml matches on qualified names, so handing it a bare local name for an
element that lives in a namespace silently matches nothing at all.

Keeping one implementation here means the two callers cannot drift apart on the
subtle parts: which namespace map key is the default (``""``), that a prefix must
exist in the map, that a prefix may not map to an empty URI, and that a segment
must be a syntactically valid XML name. What each caller keeps for itself is the
part that genuinely differs -- how a path is *split* and what an empty or
relative path means.

The errors raised are chosen by the caller through ``error_cls``, so a bad record
path raises :class:`~gigaxml.errors.RecordPathError` and a bad field path raises
:class:`~gigaxml.errors.FieldPathError` while sharing this code. ``source`` is the
label that opens the message, which is what makes the wording read naturally for
either kind of path.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Final

from gigaxml.errors import GigaXMLError

__all__ = [
    "ANY_ANCESTOR_PREFIX",
    "BARE_SEGMENT",
    "PREFIXED_SEGMENT",
    "resolve_attribute_name",
    "resolve_segment",
    "resolve_segments",
    "split_segments",
]

#: A path segment written with an explicit namespace prefix, e.g. ``ns:product``.
PREFIXED_SEGMENT: Final = re.compile(r"^(?P<prefix>[A-Za-z_][A-Za-z0-9_.\-]*):(?P<local>[^:]+)$")

#: A path segment written as a bare XML name, e.g. ``product``.
BARE_SEGMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")

#: Prefix that switches a record path from root-anchored to any-ancestor matching.
ANY_ANCESTOR_PREFIX: Final = "//"


def split_segments(path: str) -> list[str]:
    """Split a slash-separated path into its non-empty segments.

    The leading ``//`` marker is not a segment, so it disappears here. What that
    marker *means* -- anchoring at the document root versus matching any ancestor
    -- is the caller's decision, not this function's.
    """
    return [segment for segment in path.split("/") if segment]


def resolve_segment(
    segment: str,
    ns_map: Mapping[str, str],
    *,
    error_cls: type[GigaXMLError],
    source: str,
    use_default_namespace: bool = True,
) -> str:
    """Resolve one path segment into its qualified ``{uri}local`` tag.

    Three forms are accepted, and the middle one is the easy one to get wrong:

    * ``ns:local`` with ``ns_map={"ns": "urn:x"}`` -> ``{urn:x}local``
    * ``local`` with a default namespace ``ns_map={"": "urn:x"}`` -> ``{urn:x}local``
    * ``local`` with no default namespace -> ``local``, unchanged

    Args:
        segment: one path segment, e.g. ``"c:product"`` or ``"product"``.
        ns_map: prefix-to-URI map; the empty string key is the default namespace.
        error_cls: the error type to raise on a malformed or unresolvable segment.
        source: label opening the error message, e.g. ``"record_path"``.
        use_default_namespace: apply the default namespace to a bare name. Pass
            ``False`` for **attributes**, where the default namespace does not
            apply: in XML an unprefixed attribute has no namespace at all, so
            expanding ``currency`` to ``{urn:x}currency`` would make every
            attribute lookup miss.

    Returns:
        The qualified tag lxml will compare against.

    Raises:
        GigaXMLError: ``error_cls`` is raised for an unknown prefix, a prefix
            mapped to an empty URI, or a segment that is not a valid XML name.
    """
    prefixed = PREFIXED_SEGMENT.match(segment)
    if prefixed is not None:
        prefix = prefixed.group("prefix")
        local = prefixed.group("local")
        if prefix not in ns_map:
            raise error_cls(
                f"{source} segment {segment!r} uses namespace prefix {prefix!r}, "
                f"which is not present in the namespace map "
                f"(known prefixes: {sorted(ns_map) or 'none'})"
            )
        uri = ns_map[prefix]
        if not uri:
            raise error_cls(f"namespace prefix {prefix!r} maps to an empty URI")
        return f"{{{uri}}}{local}"

    if BARE_SEGMENT.match(segment) is None:
        raise error_cls(f"{source} segment is not a valid XML name: {segment!r}")

    default_uri = ns_map.get("") if use_default_namespace else None
    return f"{{{default_uri}}}{segment}" if default_uri else segment


def resolve_segments(
    segments: Iterable[str],
    ns_map: Mapping[str, str],
    *,
    error_cls: type[GigaXMLError],
    source: str,
) -> list[str]:
    """Resolve every segment of a path, outermost first.

    See :func:`resolve_segment` for the per-segment rules. Each segment resolves
    independently, so a path may freely mix prefixed and bare names.
    """
    return [resolve_segment(s, ns_map, error_cls=error_cls, source=source) for s in segments]


def resolve_attribute_name(
    name: str,
    ns_map: Mapping[str, str],
    *,
    error_cls: type[GigaXMLError],
    source: str,
) -> str:
    """Resolve an attribute name, which the default namespace does **not** apply to.

    ``ns:local`` resolves through the prefix map exactly as an element would;
    a bare ``local`` stays bare, because an unprefixed XML attribute is in no
    namespace. Getting this wrong is silent: every attribute lookup would simply
    return ``None``.
    """
    return resolve_segment(
        name,
        ns_map,
        error_cls=error_cls,
        source=source,
        use_default_namespace=False,
    )
