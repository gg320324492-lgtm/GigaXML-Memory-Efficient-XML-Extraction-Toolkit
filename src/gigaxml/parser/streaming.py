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
be yielded.

**2. Matching is on the whole ancestor chain, not on the leaf name.**

Dropping the ``tag=`` filter means the reader does its own comparison, and a
comparison against the record element's own tag alone would be wrong in a way
that never announces itself. Given

    <catalog><returns><product .../></returns></catalog>

a record path of ``/catalog/products/product`` would happily match the
``<product>`` elements under ``<returns>``, silently merging two different
schemas into one output stream. Nothing would fail; the row count would just be
wrong. So the reader keeps a stack of the qualified tags of the currently open
elements and compares it against the resolved tags of ``record_path`` (see
:func:`parse_record_path`). Memory cost is O(document depth), which is bounded by
the parser's own tree and negligible next to the record payload.

**3. A path is anchored at the root unless it says otherwise.**

``/catalog/products/product`` means "this chain, starting at the document root":
the open-element stack must equal the chain exactly. ``//products/product`` means
"these trailing segments, at any depth", which is what you want when you do not
care what the document root is called.

Without that distinction a *shorter* path silently became a wildcard -- ``/item``
matched every ``<item>`` anywhere in the document, which is the same class of
silent over-matching as the leaf-name bug above, just smaller. Root anchoring is
the default because the path is documented as an *absolute element path*; the
``//`` prefix is the explicit opt-in to suffix matching.

The same reasoning applies to namespaces: ``lxml`` matches qualified names, so
handing it a bare local name for an element in a namespace silently matches
nothing. Each path segment therefore resolves its own prefix and default
namespace, and a zero-match scan raises :class:`RecordPathError` instead of
returning an empty result.

**4. Cleanup is verified, not assumed.**

``tests/performance/test_memory.py`` contains a reversed test that disables the
cleanup entirely and asserts RSS explodes, so the bounded-memory result cannot be
an artefact of a scan that quietly did nothing.
"""

from __future__ import annotations

import gzip
from collections.abc import Iterator, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Final

from lxml import etree

from gigaxml.errors import RecordPathError
from gigaxml.paths import ANY_ANCESTOR_PREFIX, resolve_segments, split_segments

__all__ = [
    "RecordPathError",
    "RecordPathSpec",
    "StreamingRecordReader",
    "parse_record_path",
    "resolve_record_tags",
]

#: Suffixes treated as gzip-compressed input.
_GZIP_SUFFIXES: Final = (".gz", ".gzip")


@dataclass(frozen=True, slots=True)
class RecordPathSpec:
    """A parsed record path: the resolved chain plus how it should be matched.

    Attributes:
        chain: one qualified ``{uri}local`` tag per path segment, outermost first.
        anchored: ``True`` for ``/a/b/c`` -- the open-element stack must equal
            ``chain`` exactly, i.e. the path names the document root. ``False``
            for ``//a/b/c`` -- only the trailing ``len(chain)`` stack entries are
            compared, so any ancestors are accepted.
    """

    chain: tuple[str, ...]
    anchored: bool

    @property
    def leaf_tag(self) -> str:
        """The qualified tag of the record element itself (the final segment)."""
        return self.chain[-1]


def _split_segments(record_path: str) -> list[str]:
    """Split an absolute element path into its non-empty segments.

    The leading ``//`` marker is not a segment, so it disappears here; the
    anchoring decision is made by :func:`parse_record_path`.
    """
    if not record_path.startswith("/"):
        raise RecordPathError(
            f"record_path must be an absolute element path starting with '/': {record_path!r}"
        )
    segments = split_segments(record_path)
    if not segments:
        raise RecordPathError(f"record_path contains no element segments: {record_path!r}")
    return segments


def parse_record_path(
    record_path: str,
    namespaces: Mapping[str, str] | None = None,
) -> RecordPathSpec:
    """Parse ``record_path`` into a resolved chain plus its matching mode.

    Each segment is resolved independently, so a path may mix prefixed and bare
    segments, and the default namespace (the ``""`` key) applies to every bare
    segment. Resolution rules per segment:

    * ``ns:local`` with ``namespaces={"ns": "urn:x"}`` -> ``{urn:x}local``
    * ``local`` with ``namespaces={"": "urn:x"}``     -> ``{urn:x}local``
    * ``local`` with no default namespace             -> ``local``

    Matching mode:

    * a single leading ``/`` -> ``anchored=True``: the open-element stack must
      equal the chain exactly, so the path has to name the document root
    * a leading ``//``       -> ``anchored=False``: only the trailing segments are
      compared, so any ancestors are accepted (XPath-flavoured)

    Args:
        record_path: element path, e.g. ``"/catalog/products/product"`` or
            ``"//products/product"``.
        namespaces: prefix-to-URI map; the empty string is the default namespace.

    Returns:
        The resolved chain and whether it is anchored at the root.

    Raises:
        RecordPathError: the path is relative, has no segments (``"/"``, ``"//"``),
            contains an invalid XML name, or uses a prefix that is not present in
            ``namespaces``.
    """
    anchored = not record_path.startswith(ANY_ANCESTOR_PREFIX)
    return RecordPathSpec(
        chain=tuple(resolve_record_tags(record_path, namespaces)),
        anchored=anchored,
    )


def resolve_record_tags(
    record_path: str,
    namespaces: Mapping[str, str] | None = None,
) -> list[str]:
    """Resolve every segment of ``record_path`` into a qualified tag.

    The anchoring marker is not part of the chain, so ``"/a/b"`` and ``"//a/b"``
    resolve to the same list. Use :func:`parse_record_path` when you also need to
    know which matching mode the path asks for -- that is what it calls.

    The per-segment work is shared with field paths; see :mod:`gigaxml.paths`.

    Args:
        record_path: element path, with or without the ``//`` prefix.
        namespaces: prefix-to-URI map; the empty string is the default namespace.

    Returns:
        One qualified tag per segment, outermost first.

    Raises:
        RecordPathError: the path is malformed, a segment is not a valid XML
            name, or a prefix used by the path is not present in ``namespaces``.
    """
    return resolve_segments(
        _split_segments(record_path),
        namespaces or {},
        error_cls=RecordPathError,
        source="record_path",
    )


class StreamingRecordReader:
    """Iterate the records of a large XML document with bounded memory.

    Args:
        source: path to the XML file. Paths ending in ``.gz``/``.gzip`` are
            decompressed on the fly.
        record_path: element path of the record node. Any segment may carry a
            namespace prefix (``"/c:products/c:product"``).

            **The full chain is matched, not just the final segment.** The reader
            compares the currently open element stack against every segment of
            this path, so a path with the right last segment but wrong ancestors
            matches nothing and raises ``RecordPathError``. Two branches that end
            in the same element name are therefore kept apart instead of being
            silently merged.

            **A single leading ``/`` anchors the path at the document root:** the
            stack must equal the chain exactly, so ``"/catalog/products/product"``
            matches ``product`` only at depth 3 with exactly those ancestors.
            **A leading ``//`` matches the trailing segments at any depth:**
            ``"//products/product"`` matches whatever the root happens to be
            called. ``//`` still needs at least one segment.
        namespaces: prefix-to-URI map. Use the empty string as the key for a
            default namespace. Each segment resolves its own prefix.
        _clean: test-only escape hatch. Leave at ``True``. It exists so the
            performance suite can prove that the cleanup is what keeps memory
            bounded; production callers must not pass it.

    Yields:
        The ``lxml`` element for each record. It is only valid until the next
        iteration step -- it is cleared and unlinked immediately after being
        yielded, so copy anything you need to keep.

    Raises:
        RecordPathError: the record path is malformed, uses an unknown namespace
            prefix, or the document contains zero elements matching the path.
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
        self._spec: RecordPathSpec = parse_record_path(record_path, self._namespaces)

    @property
    def record_path(self) -> str:
        """The record path this reader was configured with."""
        return self._record_path

    @property
    def record_spec(self) -> RecordPathSpec:
        """The parsed record path: resolved chain plus matching mode."""
        return self._spec

    @property
    def record_chain(self) -> tuple[str, ...]:
        """Every segment of the record path, resolved to a qualified tag."""
        return self._spec.chain

    @property
    def record_tag(self) -> str:
        """The qualified tag of the record element itself (the final segment)."""
        return self._spec.chain[-1]

    @property
    def anchored(self) -> bool:
        """``True`` when the path must match from the document root."""
        return self._spec.anchored

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

    def _matches(self, stack: Sequence[str]) -> bool:
        """True when the open-element stack satisfies the configured record path.

        ``stack`` holds the qualified tags of the elements currently open,
        outermost first. An anchored path demands an exact match from the root;
        an any-ancestor path only compares the trailing segments.
        """
        chain = self._spec.chain
        depth = len(stack)
        if self._spec.anchored:
            return depth == len(chain) and tuple(stack) == chain
        if depth < len(chain):
            return False
        return tuple(stack[depth - len(chain) :]) == chain

    def __iter__(self) -> Iterator[etree._Element]:
        matched = 0
        record_depth = 0
        # Qualified tags of the elements currently open, outermost first. lxml's
        # iterparse emits no events for comments or processing instructions, so
        # every event we see corresponds to exactly one element and the stack
        # stays aligned with the document structure. Memory: O(document depth).
        stack: list[str] = []

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
                    stack.append(elem.tag)
                    if record_depth == 0 and self._matches(stack):
                        record_depth = len(stack)
                    continue

                # The element that is ending was pushed at its own start, and
                # every descendant has already ended and popped, so the stack
                # height *is* this element's depth.
                depth = len(stack)
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

                stack.pop()

        if matched == 0:
            mode = "root-anchored" if self._spec.anchored else "any-ancestor"
            raise RecordPathError(
                f"record_path {self._record_path!r} matched 0 elements in {self._source} "
                f"(resolved chain: {list(self._spec.chain)!r}, mode: {mode}). The full "
                f"ancestor chain must match, not just the final segment; a path starting "
                f"with '/' must match from the document root, and '//' means any "
                f"ancestors. If the document uses a namespace, pass `namespaces=` "
                f"(use '' as the key for a default namespace)."
            )
