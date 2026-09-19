"""Bounded-memory streaming record reader built on :func:`lxml.etree.iterparse`.

Peak RSS stays flat regardless of how many records the document holds, but only
because of two things that are easy to get wrong.

**1. Every consumed element is released, not just the matched records.**

The obvious implementation is ``iterparse(events=("end",), tag=record_tag)`` plus
the usual ``elem.clear()`` / unlink-siblings dance. It looks bounded and it is
not: ``tag=`` filters out every element that is not a record, and an element you
never see is an element you can never release. A document shaped like

    <catalog><products>...N product records...</products><orders>...M orders...</orders></catalog>

keeps all ``M`` orders resident, because no ``<order>`` end event is ever
delivered. Measured on the 100MB fixture: RSS grows 9.4x from the 10MB to the
100MB file, i.e. essentially linear -- the exact failure this project exists to
avoid.

So the reader subscribes to ``("start", "end")`` without a ``tag`` filter, tracks
the element depth, and releases *everything* it has finished with -- while
leaving the currently open record subtree intact, since that is what is about to
be yielded. Tag matching still happens against the fully qualified ``{uri}local``
name produced by :func:`resolve_record_tag`; ``lxml`` matches qualified names, so
handing it a bare local name for an element in a namespace silently matches
nothing and yields zero records.

**2. Cleanup is verified, not assumed.**

``tests/performance/test_memory.py`` contains a reversed test that disables the
cleanup entirely and asserts RSS explodes, so the bounded-memory result cannot be
an artefact of a scan that quietly did nothing.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Iterator, Mapping
from contextlib import closing
from pathlib import Path
from typing import IO, Final

from lxml import etree

__all__ = ["RecordPathError", "StreamingRecordReader", "resolve_record_tag"]

#: A path segment written with an explicit namespace prefix, e.g. ``ns:product``.
_PREFIXED_SEGMENT: Final = re.compile(r"^(?P<prefix>[A-Za-z_][A-Za-z0-9_.\-]*):(?P<local>[^:]+)$")

#: A path segment written as a bare XML name, e.g. ``product``.
_BARE_SEGMENT: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")

#: Suffixes treated as gzip-compressed input.
_GZIP_SUFFIXES: Final = (".gz", ".gzip")


class RecordPathError(ValueError):
    """Raised when ``record_path`` cannot be resolved or matches nothing.

    Deliberately *not* a silent empty result: a record path that matches zero
    elements is almost always a namespace mistake, and returning zero rows would
    hide it behind a plausible-looking "extracted 0 records" report.
    """


def _split_segments(record_path: str) -> list[str]:
    """Split an absolute element path into its non-empty segments."""
    if not record_path.startswith("/"):
        raise RecordPathError(
            f"record_path must be an absolute element path starting with '/': {record_path!r}"
        )
    segments = [segment for segment in record_path.split("/") if segment]
    if not segments:
        raise RecordPathError(f"record_path contains no element segments: {record_path!r}")
    return segments


def resolve_record_tag(
    record_path: str,
    namespaces: Mapping[str, str] | None = None,
) -> str:
    """Translate the final segment of ``record_path`` into a qualified tag.

    Resolution rules for the last segment:

    * ``ns:local`` with ``namespaces={"ns": "urn:x"}`` -> ``{urn:x}local``
    * ``local`` with ``namespaces={"": "urn:x"}``     -> ``{urn:x}local``
    * ``local`` with no default namespace             -> ``local``

    Args:
        record_path: absolute element path, e.g. ``"/catalog/products/product"``.
        namespaces: prefix-to-URI map; the empty string is the default namespace.

    Returns:
        The qualified tag the reader compares element tags against.

    Raises:
        RecordPathError: the path is malformed, or a prefix used by the path is
            not present in ``namespaces``.
    """
    segment = _split_segments(record_path)[-1]
    ns_map: Mapping[str, str] = namespaces or {}

    prefixed = _PREFIXED_SEGMENT.match(segment)
    if prefixed is not None:
        prefix = prefixed.group("prefix")
        local = prefixed.group("local")
        if prefix not in ns_map:
            raise RecordPathError(
                f"record_path segment {segment!r} uses namespace prefix {prefix!r}, "
                f"which is not present in the namespace map "
                f"(known prefixes: {sorted(ns_map) or 'none'})"
            )
        uri = ns_map[prefix]
        if not uri:
            raise RecordPathError(f"namespace prefix {prefix!r} maps to an empty URI")
        return f"{{{uri}}}{local}"

    if _BARE_SEGMENT.match(segment) is None:
        raise RecordPathError(f"record_path segment is not a valid XML name: {segment!r}")

    default_uri = ns_map.get("")
    if default_uri:
        return f"{{{default_uri}}}{segment}"
    return segment


class StreamingRecordReader:
    """Iterate the records of a large XML document with bounded memory.

    Args:
        source: path to the XML file. Paths ending in ``.gz``/``.gzip`` are
            decompressed on the fly.
        record_path: absolute element path of the record node, e.g.
            ``"/catalog/products/product"``. The final segment may carry a
            namespace prefix (``"/c:products/c:product"``).

            **Only the final segment is used for matching.** The ancestor
            segments document intent rather than filter, so a path with the right
            last segment but wrong ancestors still yields records. Verifying the
            full chain belongs to the inspect phase, which walks ancestors
            anyway. Use a record path whose *last* segment does not exist to
            trigger the zero-match error.
        namespaces: prefix-to-URI map. Use the empty string as the key for a
            default namespace.
        _clean: test-only escape hatch. Leave at ``True``. It exists so the
            performance suite can prove that the cleanup is what keeps memory
            bounded; production callers must not pass it.

    Yields:
        The ``lxml`` element for each record. It is only valid until the next
        iteration step -- it is cleared and unlinked immediately after being
        yielded, so copy anything you need to keep.

    Raises:
        RecordPathError: the record path is malformed, uses an unknown namespace
            prefix, or the document contains zero matching elements.
    """

    def __init__(
        self,
        source: str | Path,
        record_path: str,
        namespaces: Mapping[str, str] | None = None,
        *,
        _clean: bool = True,
    ) -> None:
        self._source = Path(source)
        self._record_path = record_path
        self._namespaces: dict[str, str] | None = dict(namespaces) if namespaces else None
        self._clean = _clean
        self._tag = resolve_record_tag(record_path, self._namespaces)

    @property
    def record_path(self) -> str:
        """The record path this reader was configured with."""
        return self._record_path

    @property
    def record_tag(self) -> str:
        """The qualified tag elements are matched against."""
        return self._tag

    def _open(self) -> IO[bytes]:
        if self._source.suffix.lower() in _GZIP_SUFFIXES:
            return gzip.open(self._source, "rb")
        return self._source.open("rb")

    def _release(self, elem: etree._Element) -> None:
        """Drop a finished element and unlink its consumed siblings.

        ``keep_tail=True`` preserves the whitespace that follows the element, so
        the surviving tree stays well-formed.
        """
        if not self._clean:
            return
        elem.clear(keep_tail=True)
        while elem.getprevious() is not None:
            del elem.getparent()[0]

    def __iter__(self) -> Iterator[etree._Element]:
        matched = 0
        depth = 0
        record_depth = 0

        with closing(self._open()) as stream:
            context = etree.iterparse(
                stream,
                events=("start", "end"),
                # No `tag=` filter on purpose: filtering here would hide every
                # non-record element, and those are exactly the elements that
                # otherwise accumulate for the whole document. See the module
                # docstring for the measurement that forced this.
                #
                # --- Security defaults. Deliberately not configurable: ---
                # entities are never expanded, the network is never touched and
                # no DTD (internal or external) is loaded. Exposing these as
                # flags would let a config file silently turn XXE back on.
                resolve_entities=False,
                no_network=True,
                load_dtd=False,
                attribute_defaults=False,
                huge_tree=False,
            )

            for event, elem in context:
                if event == "start":
                    depth += 1
                    if record_depth == 0 and elem.tag == self._tag:
                        record_depth = depth
                    continue

                if record_depth == 0:
                    # Nothing open: safe to release immediately.
                    self._release(elem)
                elif depth == record_depth:
                    # The record itself. Yield first, release after the consumer
                    # has moved on.
                    record_depth = 0
                    matched += 1
                    yield elem
                    self._release(elem)
                # depth > record_depth means we are inside the open record;
                # releasing a descendant now would hand back an empty record.

                depth -= 1

        if matched == 0:
            raise RecordPathError(
                f"record_path {self._record_path!r} matched 0 elements in {self._source} "
                f"(resolved tag: {self._tag!r}). If the document uses a namespace, pass "
                f"`namespaces=` (use '' as the key for a default namespace)."
            )
