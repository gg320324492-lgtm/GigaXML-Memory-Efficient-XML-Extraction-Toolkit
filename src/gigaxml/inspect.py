"""Streaming structure inspection: what is in this document, without reading it all.

The motivating case is a multi-gigabyte XML file nobody has a schema for. ``inspect``
answers "what is in here, and which element repeats often enough to be a record?"
by walking the document once and keeping **path-level counters**, never elements.

**Why the walk looks the way it does.** The record path is not known yet, so the
``iterparse(tag=...)`` shortcut is unavailable -- and it is a red line anyway: an
element the parser never reports is an element that can never be released, which is
what made the naive reader's memory grow with the document (see
:mod:`gigaxml.parser.streaming`). So the walk subscribes to ``("start", "end",
"start-ns")`` with no tag filter, tracks the open-element stack to build each
element's path, and **releases every element the moment it ends**. Nothing here
needs a subtree intact: an element's attributes are read at its start and its text
at its end, both before it is cleared. Memory is O(document depth) plus the path
table.

**The path table is capped, and hitting the cap is reported.** Ten thousand distinct
paths is already more than a human can read, so the table stops growing there --
but it says so, with the number of further occurrences and a sample of the paths it
did not list. Silent truncation would be worse than no truncation: the report would
look complete and be wrong.

**Candidate scoring is explainable by construction.** A score comes from exactly two
deterministic statistics -- how often the element repeats under its parent, and how
similar its instances' sibling structures are -- and every candidate carries both
numbers as evidence. A candidate must also have structure (at least one child tag or
attribute); a bare leaf repeats exactly as often as its parent record and would tie
with it, so it is not a candidate. A document with several plausible records reports
several, ranked, rather than silently picking one.
"""

from __future__ import annotations

import gzip
import math
import sys
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Final

import yaml
from lxml import etree

from gigaxml.errors import FieldTypeError, InspectionError
from gigaxml.fields import FieldType, coerce_value, normalize_text

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PATHS",
    "Candidate",
    "InspectionReport",
    "PathEntry",
    "ValueSample",
    "generate_config",
    "infer_field_type",
    "inspect_document",
]

#: Distinct paths tracked before the table stops growing. See the module docstring.
DEFAULT_MAX_PATHS: Final = 10_000

#: Nesting depth beyond which paths are counted but not tracked.
DEFAULT_MAX_DEPTH: Final = 32

#: Distinct child-tag-set shapes kept per path. A path with more shapes than this is
#: either genuinely irregular or adversarial; either way the consistency figure is
#: reported as truncated rather than silently computed from a partial picture.
_MAX_SHAPES_PER_PATH: Final = 64

#: (path, slot) pairs for which text/attribute values are sampled.
_MAX_VALUE_SLOTS: Final = 4_096

#: Distinct values kept per slot.
_MAX_VALUES_PER_SLOT: Final = 32

#: Unlisted paths quoted when the table is full.
_MAX_UNTRACKED_EXAMPLES: Final = 20

#: Candidates reported. Enough to cover a document with several record kinds.
_MAX_CANDIDATES: Final = 10

#: Weights of the two -- and only two -- scoring inputs.
_REPEAT_WEIGHT: Final = 0.6
_CONSISTENCY_WEIGHT: Final = 0.4

#: An element must repeat at least this often to be a record candidate.
_MIN_CANDIDATE_COUNT: Final = 2

#: The namespace XML reserves for the ``xml`` prefix. That binding is implicit --
#: it never appears in an element's ``nsmap`` -- so it has to be seeded explicitly.
#: Without it ``prefix_for()`` returns ``None`` for ``xml:lang``, the segment
#: renders as a bare ``lang``, and the generated config parses, matches nothing and
#: extracts ``None``.
_XML_NAMESPACE: Final = "http://www.w3.org/XML/1998/namespace"

#: Suffixes treated as gzip-compressed input. Mirrors
#: :data:`gigaxml.parser.streaming._GZIP_SUFFIXES`; kept local so this module does
#: not reach into the reader's internals.
_GZIP_SUFFIXES: Final = (".gz", ".gzip")

_MB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ValueSample:
    """Distinct values observed for one field slot, plus how many were seen.

    Attributes:
        values: distinct normalised values, in first-seen order, capped.
        observed: total observations, including repeats.
        truncated: more distinct values existed than were kept.
    """

    values: tuple[str, ...]
    observed: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class PathEntry:
    """What was seen at one element path."""

    path: str
    depth: int
    count: int
    attribute_names: tuple[str, ...]
    child_tags: tuple[str, ...]
    dominant_shape_count: int
    distinct_shapes: int
    shapes_truncated: bool

    @property
    def has_children(self) -> bool:
        """``True`` when at least one instance had a child element."""
        return bool(self.child_tags)

    @property
    def shape_consistency(self) -> float:
        """Share of instances whose child-tag set matched the most common one."""
        return self.dominant_shape_count / self.count if self.count else 0.0

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view."""
        return {
            "path": self.path,
            "depth": self.depth,
            "count": self.count,
            "attribute_names": list(self.attribute_names),
            "child_tags": list(self.child_tags),
            "has_children": self.has_children,
            "dominant_shape_count": self.dominant_shape_count,
            "distinct_shapes": self.distinct_shapes,
            "shapes_truncated": self.shapes_truncated,
            "shape_consistency": round(self.shape_consistency, 6),
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    """A path proposed as the record path, with the evidence behind the score."""

    path: str
    depth: int
    score: float
    count: int
    repeat_score: float
    shape_consistency: float
    attribute_names: tuple[str, ...]
    child_tags: tuple[str, ...]
    namespaces: dict[str, str]

    @property
    def missing_namespaces(self) -> tuple[str, ...]:
        """Prefixes this candidate uses that its own namespace map cannot express.

        Non-empty when a prefix stood for different URIs at different instances of
        this very path -- one flat ``namespaces:`` block cannot describe that, so a
        config for this candidate cannot be generated at all.

        Attribute slots count as uses. Leaving them out made this guard blind to the
        one case that mattered most: a candidate whose *path* needs no namespace but
        whose attribute does reported ``missing_namespaces: []`` while the config it
        produced could not be loaded at all.
        """
        used = _prefixes_in(self.path)
        for child in self.child_tags:
            used |= _prefixes_in(child)
        for attribute in self.attribute_names:
            used |= _prefixes_in(attribute)
        return tuple(sorted(prefix for prefix in used if prefix not in self.namespaces))

    @property
    def evidence(self) -> str:
        """One line naming the two statistics the score came from."""
        return (
            f"{self.count:,} occurrences; sibling structure consistency "
            f"{self.shape_consistency:.1%}; repeat term {self.repeat_score:.3f} "
            f"(relative to the most frequent path in this document)"
        )

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view."""
        return {
            "path": self.path,
            "depth": self.depth,
            "score": round(self.score, 6),
            "count": self.count,
            "repeat_score": round(self.repeat_score, 6),
            "shape_consistency": round(self.shape_consistency, 6),
            "attribute_names": list(self.attribute_names),
            "child_tags": list(self.child_tags),
            "namespaces": dict(self.namespaces),
            "missing_namespaces": list(self.missing_namespaces),
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class InspectionReport:
    """The result of one :func:`inspect_document` walk."""

    source: str
    input_mb: float
    elements_seen: int
    max_depth_seen: int
    max_depth_limit: int
    depth_truncated: bool
    max_paths: int
    paths_truncated: bool
    untracked_occurrences: int
    untracked_examples: tuple[str, ...]
    namespaces: dict[str, str]
    shadowed_prefixes: tuple[str, ...]
    unmapped_namespaces: tuple[str, ...]
    paths: tuple[PathEntry, ...]
    candidates: tuple[Candidate, ...]
    value_samples: dict[str, dict[str, ValueSample]]
    values_truncated: bool

    @property
    def tracked_paths(self) -> int:
        """How many distinct paths the table holds."""
        return len(self.paths)

    def entry_for(self, path: str) -> PathEntry | None:
        """The tracked entry for ``path``, or ``None``."""
        return next((entry for entry in self.paths if entry.path == path), None)

    def nested_inside(self, candidate: Candidate) -> str | None:
        """The path of a higher-ranked candidate that contains ``candidate``.

        A record's own repeating sub-structures -- ``.../product/tags`` inside
        ``.../product`` -- repeat exactly as often as the record and score the same.
        They are genuine candidates and are not hidden, but they are not competing
        records either, so the report says which is which. Candidates are ranked, so
        the first containing one is the highest-ranked.

        The scan covers **every** candidate, not only those ranked above. A nested
        path usually repeats more often than the record containing it, so it usually
        ranks *above* it -- and that is precisely the case where the annotation
        matters. Returning early on ``candidate`` itself made the loop blind to it and
        reported no nesting at all.
        """
        for other in self.candidates:
            if other is candidate:
                continue
            if candidate.path.startswith(f"{other.path}/"):
                return other.path
        return None

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view of the whole report."""
        return {
            "source": self.source,
            "input_mb": self.input_mb,
            "elements_seen": self.elements_seen,
            "depth": {
                "seen": self.max_depth_seen,
                "limit": self.max_depth_limit,
                "truncated": self.depth_truncated,
            },
            "path_table": {
                "tracked": self.tracked_paths,
                "limit": self.max_paths,
                "truncated": self.paths_truncated,
                "untracked_occurrences": self.untracked_occurrences,
                "untracked_examples": list(self.untracked_examples),
            },
            "namespaces": dict(self.namespaces),
            "shadowed_prefixes": list(self.shadowed_prefixes),
            "unmapped_namespaces": list(self.unmapped_namespaces),
            "value_sampling": {"truncated": self.values_truncated},
            "candidates": [
                {**candidate.to_dict(), "nested_inside": self.nested_inside(candidate)}
                for candidate in self.candidates
            ],
            "paths": [entry.to_dict() for entry in self.paths],
        }

    def to_text(self) -> str:
        """A human-readable rendering."""
        lines: list[str] = [
            f"gigaxml inspect: {self.source}",
            f"  input            {self.input_mb:,.2f} MiB",
            f"  elements seen    {self.elements_seen:,}",
            f"  max depth        {self.max_depth_seen} (limit {self.max_depth_limit})"
            + ("  [TRUNCATED]" if self.depth_truncated else ""),
            f"  distinct paths   {self.tracked_paths:,} (limit {self.max_paths:,})",
        ]
        if self.namespaces:
            rendered = ", ".join(
                f"{prefix or '<default>'}={uri}" for prefix, uri in sorted(self.namespaces.items())
            )
            lines.append(f"  namespaces       {rendered}")
        if self.paths_truncated:
            lines.append(
                f"  WARNING          path table is full ({self.max_paths:,} paths); a further "
                f"{self.untracked_occurrences:,} element occurrences are at paths not listed"
            )
            for example in self.untracked_examples[:5]:
                lines.append(f"                     e.g. {example}")
            lines.append(
                "                     (distinct unlisted paths are not counted -- that would "
                "need unbounded memory)"
            )
        if self.shadowed_prefixes:
            lines.append(
                f"  WARNING          namespace prefix(es) rebound while an outer binding was "
                f"still in scope: {', '.join(self.shadowed_prefixes)}"
            )
        unresolvable = [c for c in self.candidates if c.missing_namespaces]
        if unresolvable:
            lines.append(
                f"  WARNING          {len(unresolvable)} candidate(s) use a prefix that means "
                f"more than one URI along their own path, so no single config can express "
                f"them: {', '.join(c.path for c in unresolvable[:3])}"
            )
        if self.unmapped_namespaces:
            lines.append(
                f"  WARNING          namespace(s) with no declared prefix: "
                f"{', '.join(self.unmapped_namespaces)}"
            )

        lines.append("")
        if self.candidates:
            lines.append(f"record candidates ({len(self.candidates)})")
            for index, candidate in enumerate(self.candidates, start=1):
                nested = self.nested_inside(candidate)
                note = f"   [nested inside #{_index_of(self.candidates, nested)}]" if nested else ""
                lines.append(
                    f"  {index}. {candidate.path}{note}\n"
                    f"     score {candidate.score:.3f}  {candidate.evidence}"
                )
            if any(self.nested_inside(c) for c in self.candidates):
                lines.append(
                    "  candidates marked [nested inside] are repeating structures *within* a "
                    "higher-ranked candidate, not competing records"
                )
        else:
            lines.append("record candidates (none)")
            lines.append("  no path repeats at least twice. A candidate needs to occur more than")
            lines.append(
                "  once; a path with structure qualifies, and so does a bare leaf unless a"
            )
            lines.append(
                "  repeating element with structure sits above it. Read the path table below"
            )
            lines.append("  and write the record path by hand.")

        lines.append("")
        lines.append(f"paths ({self.tracked_paths:,}), most frequent first")
        lines.append(f"  {'count':>10}  {'depth':>5}  {'kids':>4}  path")
        for entry in self.paths[:40]:
            lines.append(
                f"  {entry.count:>10,}  {entry.depth:>5}  {len(entry.child_tags):>4}  {entry.path}"
            )
        if len(self.paths) > 40:
            lines.append(f"  ... {len(self.paths) - 40:,} more (use --json for all of them)")
        return "\n".join(lines)


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


def _open_binary(path: Path) -> IO[bytes]:
    """Open ``path`` for reading, transparently decompressing gzip."""
    if path.suffix.lower() in _GZIP_SUFFIXES:
        return gzip.open(path, "rb")
    return path.open("rb")


def _release(elem: etree._Element) -> None:
    """Drop a finished element and unlink its consumed siblings.

    ``keep_tail=True`` preserves the whitespace that follows the element, so the
    surviving tree stays well-formed. Identical to the reader's rule, for the same
    reason: this is what keeps the walk's memory flat.
    """
    elem.clear(keep_tail=True)
    while elem.getprevious() is not None:
        del elem.getparent()[0]


def _index_of(candidates: Sequence[Candidate], path: str | None) -> int:
    """1-based rank of ``path`` among ``candidates``, or 0 when it is absent."""
    if path is None:
        return 0
    return next((i for i, c in enumerate(candidates, start=1) if c.path == path), 0)


def _repeat_score(count: int, max_count: int) -> float:
    """Map a repeat count onto ``0..1`` relative to the most frequent path seen.

    **Relative, not absolute, and deliberately so.** An absolute saturating curve
    gives every path past the saturation point the same repeat term, and the ranking
    between two genuinely different record kinds then falls through to the
    tie-break. That is not hypothetical: during development the top candidate flipped
    between documents of different sizes -- ``products/product`` won on a 10MB file
    because ``orders/order`` had not saturated yet, and once it had, ``orders/order``
    won on the alphabetical tie-break. Normalising by the document's own maximum
    keeps the term monotonic in the count at every size.

    The consequence is that a score is only meaningful *within one report*; it is
    not comparable across documents. It is used for nothing else.
    """
    if max_count <= 1:
        return 1.0
    return math.log10(count) / math.log10(max_count)


def _score_candidates(entries: Sequence[PathEntry], *, limit: int) -> tuple[Candidate, ...]:
    """Rank paths as record candidates.

    The score uses two inputs and nothing else: the repeat count, and the share of
    instances whose sibling structure matched the most common one. Ties are broken
    towards the *shallower* path, because a record is the outermost repeating
    structure -- ``/catalog/products/product`` and its child ``.../product/tags``
    repeat equally often, and the shallower one is the record.

    Eligibility, which is a gate rather than a score term, keeps two kinds of noise
    out of the ranking:

    * A path must repeat at least twice. A single occurrence is not a record.
    * A path must have structure -- a child or an attribute -- **or** be a bare leaf
      with no repeating structured ancestor. See :func:`_is_eligible` for why both
      halves of that are needed.
    """
    by_path = {entry.path: entry for entry in entries}
    max_count = max((entry.count for entry in entries), default=1)
    scored: list[Candidate] = []
    for entry in entries:
        if entry.count < _MIN_CANDIDATE_COUNT:
            continue
        if not _is_eligible(entry, by_path):
            continue
        repeat = _repeat_score(entry.count, max_count)
        consistency = entry.shape_consistency
        scored.append(
            Candidate(
                path=entry.path,
                depth=entry.depth,
                score=_REPEAT_WEIGHT * repeat + _CONSISTENCY_WEIGHT * consistency,
                count=entry.count,
                repeat_score=repeat,
                shape_consistency=consistency,
                attribute_names=entry.attribute_names,
                child_tags=entry.child_tags,
                namespaces={},
            )
        )
    # Depth before raw count: two paths that score the same because one is nested
    # inside the other repeat equally often, and the shallower one is the record.
    scored.sort(
        key=lambda candidate: (-candidate.score, candidate.depth, -candidate.count, candidate.path)
    )
    return tuple(scored[:limit])


def _with_namespaces(
    candidates: Sequence[Candidate],
    accumulators: Mapping[str, _PathAccumulator],
) -> tuple[Candidate, ...]:
    """Give each candidate the namespace map its *own* path and fields need.

    Per candidate, not per document: two candidates can disagree about a prefix --
    one may sit entirely inside a subtree that rebinds it -- and a document-wide
    union would then make both unusable when only one actually is.
    """
    enriched: list[Candidate] = []
    for candidate in candidates:
        accumulator = accumulators.get(candidate.path)
        mapping: dict[str, str] = {}
        if accumulator is not None:
            for prefix, uris in sorted(accumulator.prefix_uris.items()):
                if len(uris) == 1:
                    mapping[prefix] = next(iter(uris))
        enriched.append(replace(candidate, namespaces=mapping))
    return tuple(enriched)


def _is_eligible(entry: PathEntry, table: Mapping[str, PathEntry]) -> bool:
    """Whether a path may be ranked as a record candidate.

    Args:
        entry: the path being considered.
        table: every tracked path, keyed by path. Needed because the rule for a bare
            leaf is about its *ancestors*, which a single entry cannot see.

    A path with structure -- a child tag or an attribute name -- is always eligible.

    A **bare leaf** is eligible unless it has an ancestor that is itself a repeating
    record, meaning an ancestor with structure and at least two occurrences. The two
    shapes that distinction separates:

    * ``<root><line>text</line> x500</root>``: nothing above ``line`` is a record, so
      ``line`` *is* the record. Excluding bare leaves outright -- which an earlier
      version did -- meant such a document got no candidate at all, and that is
      exactly the log-file shape this tool gets pointed at.
    * ``<product><tags><tag>a</tag></tags></product>``: ``.../product`` repeats and
      has structure, so ``.../product/tags/tag`` is one of its fields. Admitting it
      would put a field above the record it belongs to, because a leaf has a
      trivially perfect sibling consistency (every leaf has no children) and usually
      repeats more often -- on the project's own 10MB dataset ``.../tags/tag`` occurs
      101,691 times against ``.../product``'s 29,120.

    Both halves of the ancestor test are needed: requiring only "has an ancestor"
    would let a single-occurrence container such as ``<root>`` disqualify everything
    beneath it.
    """
    if entry.child_tags or entry.attribute_names:
        return True
    return not _has_repeating_structured_ancestor(entry.path, table)


def _has_repeating_structured_ancestor(path: str, table: Mapping[str, PathEntry]) -> bool:
    """Whether any proper ancestor of ``path`` repeats at least twice and has structure."""
    segments = path.split("/")[1:]
    for end in range(1, len(segments)):
        ancestor = table.get("/" + "/".join(segments[:end]))
        if ancestor is None or ancestor.count < _MIN_CANDIDATE_COUNT:
            continue
        if ancestor.child_tags or ancestor.attribute_names:
            return True
    return False


class _SlotSample:
    """Mutable accumulator for one field slot's values."""

    __slots__ = ("observed", "truncated", "values")

    def __init__(self) -> None:
        self.values: list[str] = []
        self.observed = 0
        self.truncated = False

    def observe(self, value: str) -> None:
        """Count one observation, keeping the value if it is new and there is room."""
        self.observed += 1
        if value in self.values:
            return
        if len(self.values) >= _MAX_VALUES_PER_SLOT:
            self.truncated = True
            return
        self.values.append(value)


class _ValueCollector:
    """Bounded sampler of field values, used only for ``--infer-types``."""

    __slots__ = ("_samples", "truncated")

    def __init__(self) -> None:
        self._samples: dict[str, dict[str, _SlotSample]] = {}
        self.truncated = False

    def add(self, path: str, slot: str, value: str) -> None:
        """Record one observed value for ``(path, slot)``."""
        by_slot = self._samples.get(path)
        if by_slot is None:
            if len(self._samples) >= _MAX_VALUE_SLOTS:
                self.truncated = True
                return
            by_slot = {}
            self._samples[path] = by_slot
        sample = by_slot.get(slot)
        if sample is None:
            if len(by_slot) >= _MAX_VALUE_SLOTS:
                self.truncated = True
                return
            sample = _SlotSample()
            by_slot[slot] = sample
        sample.observe(value)

    def freeze(self) -> dict[str, dict[str, ValueSample]]:
        """Turn the accumulators into immutable samples."""
        return {
            path: {
                slot: ValueSample(tuple(sample.values), sample.observed, sample.truncated)
                for slot, sample in by_slot.items()
            }
            for path, by_slot in self._samples.items()
        }


def inspect_document(
    source: str | Path,
    *,
    max_paths: int = DEFAULT_MAX_PATHS,
    max_depth: int = DEFAULT_MAX_DEPTH,
    collect_values: bool = False,
    max_candidates: int = _MAX_CANDIDATES,
) -> InspectionReport:
    """Walk ``source`` once and report its structure.

    Args:
        source: XML file, optionally gzipped.
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
    location = Path(source)
    input_mb = location.stat().st_size / _MB

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

    with closing(_open_binary(location)) as stream:
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
    candidates = _with_namespaces(_score_candidates(frozen, limit=max_candidates), accumulators)

    return InspectionReport(
        source=str(source),
        input_mb=round(input_mb, 3),
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


def _prefixes_in(rendered: str) -> set[str]:
    """Every ``prefix:`` used by a rendered path segment or attribute slot.

    A local XML name cannot contain ``:``, so splitting on the first colon is
    unambiguous. A leading ``@`` marks an attribute slot (``@dc:creator``) and has to
    come off first, or the "prefix" reads as ``@dc`` and never matches the map.
    """
    prefixes: set[str] = set()
    for segment in rendered.split("/"):
        name = segment.removeprefix("@")
        if ":" in name:
            prefixes.add(name.partition(":")[0])
    return prefixes


def _namespaces_for(candidates: Sequence[Candidate]) -> dict[str, str]:
    """The union of the candidates' own namespace maps, for display in the report.

    A prefix two candidates disagree about is left out rather than resolved to one
    of the two bindings: showing a coin flip as a fact is worse than showing
    nothing. ``generate_config`` does not use this map -- it uses the chosen
    candidate's own, which is the only one that has to be right.
    """
    seen: dict[str, set[str]] = {}
    for candidate in candidates:
        for prefix, uri in candidate.namespaces.items():
            seen.setdefault(prefix, set()).add(uri)
    return {prefix: next(iter(uris)) for prefix, uris in sorted(seen.items()) if len(uris) == 1}


def infer_field_type(values: Sequence[str]) -> tuple[FieldType, str]:
    """Infer the narrowest safe type for a field from observed values.

    Returns ``(type, evidence)``. Only ``int``, ``float``, ``bool`` and ``date``
    are ever returned; **``decimal`` never is**. Deciding that a sampled ``49.90``
    means a decimal rather than a string is exactly the kind of lossy guess that
    turns ``49.9`` and ``49.90`` into the same value, and the sample cannot tell
    which one the document meant. A human opts into ``decimal`` deliberately.

    Convertibility is tested with the extractor's own
    :func:`gigaxml.fields.coerce_value` rather than a second set of rules, so an
    inferred type is guaranteed to coerce and cannot drift away from what
    extraction actually does. Numeric checks come before the boolean one so that
    ``0``/``1`` infer as ``int``: that keeps the value exactly, whereas ``bool``
    would be a guess between two readings of the same text.
    """
    if not values:
        return FieldType.STRING, "no values sampled"

    shown = ", ".join(repr(value) for value in values[:8])
    if len(values) > 8:
        shown += ", ..."
    sample = f"{len(values)} distinct value(s): {shown}"

    for field_type, why in (
        (FieldType.INT, "every sampled value is a whole number"),
        (FieldType.FLOAT, "every sampled value is numeric but not integral"),
        (FieldType.BOOL, "every sampled value is a boolean spelling"),
        (FieldType.DATE, "every sampled value parses as an ISO date"),
    ):
        if all(_converts(value, field_type) for value in values):
            return field_type, f"{why}; {sample}"

    return FieldType.STRING, f"values do not share a narrower type; {sample}"


def _converts(value: str, field_type: FieldType) -> bool:
    """Whether ``value`` converts under the extractor's own rules."""
    try:
        coerce_value(value, field_type, "<inspect>")
    except FieldTypeError:
        return False
    return True


def generate_config(
    report: InspectionReport,
    *,
    candidate_index: int = 1,
    infer_types: bool = False,
) -> str:
    """Render a runnable YAML config for one candidate.

    Args:
        report: the report to draw from.
        candidate_index: 1-based index into ``report.candidates``.
        infer_types: sample-based type inference. Off by default: the default
            product is all ``string``, which is lossless.

    Returns:
        YAML text that :func:`gigaxml.config.load_config` accepts.

    Raises:
        InspectionError: there are no candidates, the index is out of range, or the
            candidate's path uses a prefix that means more than one URI.

    Note:
        The return value is the YAML text and **nothing else**. When the chosen
        candidate sits inside another one, a warning goes to ``stderr`` and a line is
        added to the YAML header -- neither is part of the return value, so a library
        caller cannot detect the case by looking at what comes back. Read
        :meth:`InspectionReport.nested_inside` for that; it is the structured signal,
        and the warning is a convenience for someone at a terminal.
    """
    if not report.candidates:
        raise InspectionError(
            f"no record candidate was found in {report.source!r}: no path repeats at "
            f"least twice with a child or an attribute; inspect the path table and "
            f"write the record path by hand"
        )
    if not 1 <= candidate_index <= len(report.candidates):
        raise InspectionError(
            f"candidate {candidate_index} does not exist; {report.source!r} has "
            f"{len(report.candidates)} candidate(s)"
        )

    candidate = report.candidates[candidate_index - 1]
    missing = candidate.missing_namespaces
    if missing:
        raise InspectionError(
            f"candidate {candidate.path!r} uses namespace prefix(es) {list(missing)} that are "
            f"bound to more than one URI along its own path; a single config cannot "
            f"express that, so the record path has to be chosen by hand"
        )

    container = report.nested_inside(candidate)
    container_index = _index_of(report.candidates, container)
    if container is not None:
        # The candidate is a repeating structure *inside* another candidate, which
        # means the ranking put the inner one first -- it repeats more often. That is
        # often not what the user wants, and nothing else in the output says so.
        # Only this direction warns: a candidate that *contains* sub-structures
        # (``.../product`` over ``.../product/tags``) is the common, correct case.
        print(
            f"warning: candidate {candidate_index} ({candidate.path}) is a repeating "
            f"structure INSIDE candidate {container_index} ({container}). If you meant "
            f"the record that contains it, pass --candidate {container_index}.",
            file=sys.stderr,
        )

    fields: dict[str, dict[str, object]] = {}
    taken: set[str] = set()
    for slot in (*candidate.attribute_names, *candidate.child_tags):
        fields[_unique_field_name(slot, taken)] = {"path": slot}

    evidence: list[str] = []
    if infer_types:
        by_slot = report.value_samples.get(candidate.path, {})
        for name, definition in fields.items():
            sample = by_slot.get(str(definition["path"]))
            if sample is None:
                evidence.append(f"#   {name}: no values sampled, left as string")
                continue
            field_type, why = infer_field_type(sample.values)
            definition["type"] = field_type.value
            evidence.append(f"#   {name}: {field_type.value} -- {why}")
        if report.values_truncated:
            evidence.append("#   value sampling was truncated; some fields may be under-sampled")

    header = [
        "# Generated by `gigaxml inspect` -- a STARTING POINT, not a conclusion.",
        "#",
        f"# Source: {report.source}",
        f"# Candidate {candidate_index} of {len(report.candidates)}: {candidate.path}",
        f"#   {candidate.evidence}",
    ]
    if container is not None:
        header.append(
            f"# WARNING: this candidate is a repeating structure INSIDE candidate "
            f"{container_index} ({container}). It ranks first because it repeats more "
            f"often, which is not always what you want; use `--candidate "
            f"{container_index}` for the record that contains it."
        )
    others = [
        (index, other)
        for index, other in enumerate(report.candidates, start=1)
        if index != candidate_index and report.nested_inside(other) is None
    ]
    nested = [
        other
        for other in report.candidates
        if other is not candidate and report.nested_inside(other) is not None
    ]
    if others:
        header.append("# Other candidates this document offers, in score order:")
        for index, other in others:
            header.append(
                f"#   {index}. {other.path}  (score {other.score:.3f}, {other.count:,} occurrences)"
            )
    if nested:
        header.append(
            f"# {len(nested)} further repeating path(s) were found *inside* this candidate "
            f"(e.g. {nested[0].path}); run `gigaxml inspect --json` to see them."
        )
    header.append("#")
    header.append("# Field paths are direct children and attributes of the record element.")
    header.append("# Nested fields (a/b) and required flags have to be added by hand.")
    if not infer_types:
        header.append("# Every type is `string`, which is lossless. Re-run with --infer-types to")
        header.append(
            "# narrow int/float/bool/date -- never decimal, which cannot be inferred safely."
        )
    else:
        header.append("# Types below were INFERRED FROM A SAMPLE. Check each one against the")
        header.append("# evidence; a sample can be unrepresentative. `decimal` is never inferred.")
        header.extend(evidence)
        if any(definition.get("type") == FieldType.FLOAT.value for definition in fields.values()):
            header.append(
                "# NOTE: `float` was inferred somewhere. If that field is money, change it to"
            )
            header.append("# `decimal` by hand -- float cannot represent 49.90 exactly.")
    if report.shadowed_prefixes:
        header.append(
            f"# WARNING: prefix(es) {list(report.shadowed_prefixes)} are rebound to different "
            f"URIs in this document; check that the mapping below is the one you want."
        )
    header.append("")

    payload: dict[str, object] = {"record": candidate.path}
    if candidate.namespaces:
        payload["namespaces"] = dict(candidate.namespaces)
    payload["fields"] = fields

    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return "\n".join(header) + body


def _field_name(slot: str) -> str:
    """Turn a path slot into a usable field name.

    ``@currency`` becomes ``currency`` and ``c:name`` becomes ``name``: a YAML key
    containing ``@`` or ``:`` is legal but awkward to refer to, and the namespace
    is already carried by the path itself.
    """
    name = slot.removeprefix("@")
    _, _, local = name.partition(":")
    return local or name


def _unique_field_name(slot: str, taken: set[str]) -> str:
    """A field name that does not collide with one already chosen.

    An attribute ``@name`` and a child element ``name`` are different fields, and
    both reduce to the name ``name``. Without this the second would silently
    overwrite the first and one of them would simply not be extracted.
    """
    base = _field_name(slot)
    name = base
    suffix = 2
    while name in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    taken.add(name)
    return name
