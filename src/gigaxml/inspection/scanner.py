"""The walk: what is **in** the document.

★ **This module measures and does not judge.** Every number it produces is a count or
a flag read off the document -- how many times a path occurred, how deep the tree got,
which prefixes a subtree rebound -- and none of it is a claim about which of those is
the record. That claim belongs to :mod:`gigaxml.inspection.candidates`, and the walk
makes it through exactly one call, :func:`inspect_document`'s last three lines, so the
boundary is a place in the code rather than a convention.

**The streaming discipline is the whole point, and it is unchanged.** The record path
is not known in advance, so ``iterparse(tag=...)`` is unavailable -- and it would be a
red line anyway: an element the parser never reports is an element that can never be
released, which is what made the naive reader's memory grow with the document (see
:mod:`gigaxml.parser.streaming`). So the walk subscribes to ``("start", "end",
"start-ns")`` with no tag filter, tracks the open-element stack to build each
element's path, and **releases every element the moment it ends**. Nothing needs a
subtree intact: attributes are read at the element's start and its text at its end,
both before it is cleared. Memory is O(document depth) plus the path table.

**The path table is capped, and hitting the cap is reported.** Ten thousand distinct
paths is already more than a human can read, so the table stops growing there -- but it
says so, with the number of further occurrences and a sample of the paths it did not
list. Silent truncation would be worse than no truncation: the report would look
complete and be wrong.
"""

from __future__ import annotations

import gzip
from contextlib import ExitStack, closing
from pathlib import Path
from typing import IO, Final

from lxml import etree

from gigaxml.fields import normalize_text
from gigaxml.inspection.candidates import score_candidates
from gigaxml.inspection.inference import _namespaces_for, _ValueCollector
from gigaxml.inspection.models import InspectionReport, PathEntry

__all__ = ["DEFAULT_MAX_DEPTH", "DEFAULT_MAX_PATHS", "inspect_document"]

#: Distinct paths tracked before the table stops growing. See the module docstring.
DEFAULT_MAX_PATHS: Final = 10_000

#: Nesting depth beyond which paths are counted but not tracked.
DEFAULT_MAX_DEPTH: Final = 32

#: Candidates reported. Enough to cover a document with several record kinds.
_MAX_CANDIDATES: Final = 10

#: Distinct child-tag-set shapes kept per path. A path with more shapes than this is
#: either genuinely irregular or adversarial; either way the consistency figure is
#: reported as truncated rather than silently computed from a partial picture.
_MAX_SHAPES_PER_PATH: Final = 64


#: Suffixes treated as gzip-compressed input. Mirrors
#: :data:`gigaxml.parser.streaming._GZIP_SUFFIXES`; kept local so this module does
#: not reach into the reader's internals.
_GZIP_SUFFIXES: Final = (".gz", ".gzip")


#: The namespace XML reserves for the ``xml`` prefix. That binding is implicit --
#: it never appears in an element's ``nsmap`` -- so it has to be seeded explicitly.
#: Without it ``prefix_for()`` returns ``None`` for ``xml:lang``, the segment
#: renders as a bare ``lang``, and the generated config parses, matches nothing and
#: extracts ``None``.
_XML_NAMESPACE: Final = "http://www.w3.org/XML/1998/namespace"


#: Unlisted paths quoted when the table is full.
_MAX_UNTRACKED_EXAMPLES: Final = 20


class _NamespaceScope:
    """Prefix bindings in effect, with O(1) push and pop for nested declarations.

    A single flat prefix-to-URI dict would be wrong for a document that rebinds a
    prefix inside a subtree: the path rendered for an earlier element would use the
    later binding, and the generated config would silently fail to match. Bindings
    are therefore scoped the way the document scopes them.

    The ``xml`` prefix starts bound to the XML namespace because XML binds it
    implicitly. A document may also declare it explicitly, and that is legal as long
    as it names the same URI -- which is why the pre-seeded binding is not treated
    as shadowing when the declaration repeats it.
    """

    __slots__ = ("_bindings", "_history", "_seen", "_shadowed")

    def __init__(self) -> None:
        self._bindings: dict[str, str] = {"xml": _XML_NAMESPACE}
        self._history: list[tuple[str, str | None]] = []
        self._seen: dict[str, set[str]] = {}
        self._shadowed: set[str] = set()

    def mark(self) -> int:
        """The current declaration depth, to be handed back to :meth:`release`."""
        return len(self._history)

    def declare(self, prefix: str, uri: str) -> None:
        """Bind ``prefix`` to ``uri`` for the element that is about to open."""
        previous = self._bindings.get(prefix)
        if previous is not None and previous != uri:
            self._shadowed.add(prefix)
        self._history.append((prefix, previous))
        self._bindings[prefix] = uri
        self._seen.setdefault(prefix, set()).add(uri)

    def release(self, mark: int) -> None:
        """Undo every declaration made since ``mark``."""
        while len(self._history) > mark:
            prefix, previous = self._history.pop()
            if previous is None:
                self._bindings.pop(prefix, None)
            else:
                self._bindings[prefix] = previous

    def prefix_for(self, uri: str) -> str | None:
        """The prefix currently bound to ``uri``, preferring the default namespace."""
        if self._bindings.get("") == uri:
            return ""
        return next(
            (p for p in sorted(self._bindings) if p and self._bindings[p] == uri),
            None,
        )

    @property
    def shadowed(self) -> frozenset[str]:
        """Prefixes that were rebound to a different URI somewhere in the document."""
        return frozenset(self._shadowed)

    def uris_seen(self, prefix: str) -> frozenset[str]:
        """Every URI ``prefix`` was bound to in this document."""
        return frozenset(self._seen.get(prefix, ()))


_MB: Final = 1024 * 1024


class _PathAccumulator:
    """Mutable per-path counters, frozen into a :class:`PathEntry` at the end."""

    __slots__ = (
        "attributes",
        "children",
        "count",
        "depth",
        "path",
        "prefix_uris",
        "shapes",
        "shapes_truncated",
    )

    def __init__(self, path: str, depth: int) -> None:
        self.path = path
        self.depth = depth
        self.count = 0
        self.attributes: set[str] = set()
        self.children: set[str] = set()
        self.shapes: dict[frozenset[str], int] = {}
        self.shapes_truncated = False
        #: prefix -> every URI it stood for at this path (own segment and children).
        self.prefix_uris: dict[str, set[str]] = {}

    def note_namespace(self, prefix: str | None, uri: str | None) -> None:
        """Record that ``prefix`` stood for ``uri`` at this path."""
        if prefix is None or uri is None:
            return
        self.prefix_uris.setdefault(prefix, set()).add(uri)

    def add_shape(self, shape: frozenset[str]) -> None:
        """Record one instance's direct-child tag set."""
        if shape in self.shapes:
            self.shapes[shape] += 1
            return
        if len(self.shapes) >= _MAX_SHAPES_PER_PATH:
            self.shapes_truncated = True
            return
        self.shapes[shape] = 1

    def freeze(self) -> PathEntry:
        """Turn the counters into an immutable entry."""
        dominant = max(self.shapes.values(), default=0)
        return PathEntry(
            path=self.path,
            depth=self.depth,
            count=self.count,
            attribute_names=tuple(sorted(self.attributes)),
            child_tags=tuple(sorted(self.children)),
            dominant_shape_count=dominant,
            distinct_shapes=len(self.shapes),
            shapes_truncated=self.shapes_truncated,
        )


def _open_binary(source: str | Path | IO[bytes]) -> IO[bytes]:
    """A readable byte stream for ``source``, transparently decompressing gzip.

    A stream is returned untouched -- it belongs to whoever opened it, and ``inspect``
    does not close what it did not open. The walk below is identical either way: lxml
    pulls blocks from a file-like whether or not it can seek, so ``inspect -`` costs
    the same bounded memory as a file on disk.
    """
    if hasattr(source, "read"):
        return source  # type: ignore[return-value]
    path = Path(source)
    if path.suffix.lower() in _GZIP_SUFFIXES:
        return gzip.open(path, "rb")
    return path.open("rb")


def _release(elem: etree._Element) -> None:
    """Drop a finished element and unlink its consumed siblings.

    ``keep_tail=True`` preserves the whitespace that follows the element, so the
    surviving tree stays well-formed. Identical to the reader's rule, for the same
    reason: this is what keeps the walk's memory flat.

    **The parent check is load-bearing.** The root element has no parent, and it only
    has a preceding sibling when the document opens with a comment or a processing
    instruction -- which is exactly what a licence header looks like. Without this the
    walk raised ``TypeError`` on such documents, and ``inspect`` is the command a user
    reaches for precisely when they do not recognise a document.
    """
    elem.clear(keep_tail=True)
    parent = elem.getparent()
    if parent is None:
        return
    while elem.getprevious() is not None:
        del parent[0]


def _segment(tag: str, scope: _NamespaceScope) -> tuple[str, str | None, str | None]:
    """Render a qualified tag as a path segment.

    Returns:
        ``(segment, prefix, uri)``. ``prefix`` and ``uri`` are ``None`` for a tag in
        no namespace, and are both reported otherwise -- **including the empty
        prefix**, which is how a bare segment in a default namespace is spelled. A
        segment rendered bare because of a default namespace still needs that
        namespace in the generated config, so the caller has to know it was used.

        ``prefix`` is ``None`` while ``uri`` is set when a tag is in a namespace
        with no declared prefix: lxml should not produce that, but it is reported
        rather than guessed at.
    """
    if not tag.startswith("{"):
        return tag, None, None
    uri, _, local = tag[1:].partition("}")
    prefix = scope.prefix_for(uri)
    if prefix is None:
        return local, None, uri
    return (f"{prefix}:{local}" if prefix else local), prefix, uri


def inspect_document(
    source: str | Path | IO[bytes],
    *,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    collect_values: bool = False,
    max_candidates: int = _MAX_CANDIDATES,
) -> InspectionReport:
    """Walk ``source`` once and report its structure.

    Args:
        source: XML file (optionally gzipped), an open binary stream, or ``"-"`` to
            read standard input. A stream is walked exactly as a file is, at the same
            bounded memory; the report records its size as unknown, because a pipe has
            no length and this walk never needed one.
        max_paths: distinct paths tracked before the table stops growing.
        max_depth: nesting depth beyond which paths are counted but not tracked.
        collect_values: sample field values, for ``--infer-types``. Off by default:
            it costs a text read per leaf, and nothing else needs it.
        max_candidates: how many ranked candidates to keep.

    Returns:
        The report.

    Raises:
        OSError: the file cannot be read.
        lxml.etree.XMLSyntaxError: the document is not well-formed XML.
    """
    is_stream = hasattr(source, "read")
    location = Path("<stream>") if is_stream else Path(source)
    # A stream has no length to stat, and the walk is streaming precisely so that it
    # never had to know one. Reporting 0.0 would say "empty document", which is a claim
    # this module has no way to support.
    input_mb = None if is_stream else location.stat().st_size / _MB

    scope = _NamespaceScope()
    pending_ns: list[tuple[str, str]] = []
    accumulators: dict[str, _PathAccumulator] = {}
    untracked_occurrences = 0
    untracked_examples: list[str] = []
    elements_seen = 0
    max_depth_seen = 0
    depth_truncated = False
    unmapped: set[str] = set()
    collector = _ValueCollector()

    stack_tags: list[str] = []
    stack_paths: list[str] = []
    stack_segments: list[str] = []
    open_children: list[set[str]] = []
    scope_marks: list[int] = []

    # Closed only if opened here: a caller's stream belongs to the caller, and the
    # walk below is identical either way because lxml pulls blocks from a file-like
    # whether or not it can seek. Same shape as StreamingRecordReader.__iter__.
    with ExitStack() as owned:
        stream = _open_binary(source)
        if not is_stream:
            owned.enter_context(closing(stream))
        context = etree.iterparse(
            stream,
            events=("start", "end", "start-ns"),
            # Same security defaults as the reader, for the same reason: no entity
            # expansion, no network, no DTD, no huge_tree.
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            attribute_defaults=False,
            huge_tree=False,
        )

        for event, payload in context:
            if event == "start-ns":
                prefix, uri = payload
                pending_ns.append((str(prefix), str(uri)))
                continue

            if event == "start":
                elements_seen += 1
                mark = scope.mark()
                for prefix, uri in pending_ns:
                    scope.declare(prefix, uri)
                pending_ns.clear()
                scope_marks.append(mark)

                tag = payload.tag
                segment, prefix, uri = _segment(tag, scope)
                if prefix is None and uri is not None:
                    unmapped.add(uri)
                parent_path = stack_paths[-1] if stack_paths else ""
                full_path = f"{parent_path}/{segment}"

                stack_tags.append(tag)
                stack_paths.append(full_path)
                stack_segments.append(segment)
                open_children.append(set())
                if len(open_children) >= 2:
                    open_children[-2].add(segment)
                    # The parent's generated fields include this child, so the
                    # parent must know which namespace the child's segment used.
                    parent_accumulator = accumulators.get(parent_path)
                    if parent_accumulator is not None:
                        parent_accumulator.note_namespace(prefix, uri)

                depth = len(stack_tags)
                max_depth_seen = max(max_depth_seen, depth)
                if depth > max_depth:
                    depth_truncated = True
                    continue

                accumulator = accumulators.get(full_path)
                if accumulator is None:
                    if len(accumulators) >= max_paths:
                        untracked_occurrences += 1
                        if len(untracked_examples) < _MAX_UNTRACKED_EXAMPLES:
                            untracked_examples.append(full_path)
                        continue
                    accumulator = _PathAccumulator(full_path, depth)
                    accumulators[full_path] = accumulator
                accumulator.count += 1
                accumulator.note_namespace(prefix, uri)
                for raw_name in payload.attrib:
                    name, attribute_prefix, attribute_uri = _segment(raw_name, scope)
                    accumulator.attributes.add(f"@{name}")
                    # An attribute's own prefix has to be registered exactly as an
                    # element's does. `@dc:creator` is only usable in a generated
                    # config if `dc` reaches the namespaces block, and the attribute
                    # is the only place that prefix ever appears.
                    accumulator.note_namespace(attribute_prefix, attribute_uri)
                continue

            # --- end ---
            children = open_children.pop()
            full_path = stack_paths.pop()
            segment = stack_segments.pop()
            stack_tags.pop()
            mark = scope_marks.pop()

            accumulator = accumulators.get(full_path)
            if accumulator is not None:
                accumulator.add_shape(frozenset(children))
                accumulator.children.update(children)
                if collect_values:
                    if not children and stack_paths:
                        collector.add(stack_paths[-1], segment, normalize_text(payload))
                    for raw_name, value in payload.attrib.items():
                        name, _, _ = _segment(raw_name, scope)
                        collector.add(full_path, f"@{name}", " ".join(value.split()))

            # The element's own namespace declarations are in scope for its own
            # attributes, so the scope is released only once they have been read.
            scope.release(mark)
            _release(payload)

    entries = tuple(sorted(accumulators.values(), key=lambda acc: (-acc.count, acc.path)))
    frozen = tuple(entry.freeze() for entry in entries)
    # ★ The seam. Everything above this line is measurement; everything it returns is
    # judgement. One named call, so that "the scanner does no scoring" is something a
    # test can read off this file rather than something this file's docstring claims.
    candidates = score_candidates(frozen, limit=max_candidates, accumulators=accumulators)

    return InspectionReport(
        source=str(source),
        input_mb=None if input_mb is None else round(input_mb, 3),
        elements_seen=elements_seen,
        max_depth_seen=max_depth_seen,
        max_depth_limit=max_depth,
        depth_truncated=depth_truncated,
        max_paths=max_paths,
        paths_truncated=untracked_occurrences > 0,
        untracked_occurrences=untracked_occurrences,
        untracked_examples=tuple(untracked_examples),
        namespaces=_namespaces_for(candidates),
        shadowed_prefixes=tuple(sorted(scope.shadowed)),
        unmapped_namespaces=tuple(sorted(unmapped)),
        paths=frozen,
        candidates=candidates,
        value_samples=collector.freeze() if collect_values else {},
        values_truncated=collector.truncated,
    )
