"""The data classes, which decide nothing.

A :class:`PathEntry` is a measurement and a :class:`Candidate` is a judgement, and
keeping them in one module is deliberate rather than convenient: a reader holding a
:class:`Candidate` can see, in one place, exactly which of its fields came from
counting the document and which came from scoring it. ``count``, ``depth``,
``attribute_names`` and ``child_tags`` are facts -- they are what the document
contains. ``score``, ``repeat_score`` and ``shape_consistency`` are inferences over
those facts, produced by :mod:`gigaxml.inspection.candidates`.

:meth:`InspectionReport.to_dict` says so in the JSON as well, through
:data:`FIELD_KINDS`, so a program reading ``--json`` output can tell the two apart
without knowing this paragraph exists.

This module imports nothing from the rest of the package: the classes are the bottom
of the dependency graph, and the two places below that reach back up do it inside a
function body, at call time, to break what would otherwise be a cycle.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: Which of a candidate's JSON fields are measurements and which are judgements.
#:
#: **This is the machine-readable half of the fact/inference separation.** The human
#: output has always said "score" and "occurrences" in different words, so a person
#: reading it is not misled; a program reading ``--json`` had no way to tell, because
#: ``count`` and ``score`` sat in the same object with nothing saying which was which.
#: A consumer that ranks candidates by ``count`` and one that trusts ``score`` are
#: making different claims about the document, and only one of them is a measurement.
#:
#: The mapping is exhaustive over the candidate fields and total: every key of
#: :meth:`Candidate.to_dict` appears here exactly once, so a field added later without
#: a classification fails the test that checks this rather than shipping unlabelled.
FIELD_KINDS: dict[str, str] = {
    # --- measured: the document says so ---
    "path": "fact",
    "depth": "fact",
    "count": "fact",
    "attribute_names": "fact",
    "child_tags": "fact",
    "namespaces": "fact",
    "missing_namespaces": "fact",
    "nested_inside": "fact",
    "evidence": "fact",
    # --- inferred: a heuristic over those measurements ---
    "score": "inference",
    "repeat_score": "inference",
    "shape_consistency": "inference",
}

#: The two kinds, named, so a consumer can iterate rather than hard-code the strings.
FACT: str = "fact"
INFERENCE: str = "inference"

__all__ = [
    "FACT",
    "FIELD_KINDS",
    "INFERENCE",
    "Candidate",
    "InspectionReport",
    "PathEntry",
    "ValueSample",
]


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
        # `_prefixes_in` is defined below, in this module: it is string splitting, it
        # knows nothing about inference, and putting it in `inference` made this
        # module depend on the module that fills it -- a cycle that a function-body
        # import papered over rather than avoided.
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
    #: Size of the input in MiB, or ``None`` when the source is a stream. A pipe has no
    #: length to stat, and ``0.0`` would read as an empty document rather than as one
    #: whose size nobody measured -- the same rule the run report follows.
    input_mb: float | None
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
        """A JSON-serialisable view of the whole report.

        ★ **``field_kinds`` is new, and it is the point of this split reaching the
        output.** Every candidate below carries both measurements (``count``) and
        judgements (``score``) in one object, which is fine for a person reading
        ``to_text`` -- the wording there has always separated them -- and useless for
        a program: nothing in the JSON said which was which, so a caller ranking by
        ``count`` and a caller trusting ``score`` were indistinguishable from the
        bytes. ``field_kinds`` names every candidate field as a measurement or a
        heuristic, so a consumer can ask rather than guess.

        It is exhaustive over the candidate fields on purpose. A field added later
        without a classification fails
        :func:`tests.integration.test_inspection_architecture.test_every_candidate_field_is_classified`
        rather than shipping unlabelled, which is the only way a label stays true.
        """
        return {
            "field_kinds": dict(FIELD_KINDS),
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
            "  input            "
            + (f"{self.input_mb:,.2f} MiB" if self.input_mb is not None else "unknown (stream)"),
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


def _prefixes_in(rendered: str) -> set[str]:
    """Every ``prefix:`` used by a rendered path segment or attribute slot.

    A local XML name cannot contain ``:``, so splitting on the first colon is
    unambiguous. A leading ``@`` marks an attribute slot (``@dc:creator``) and has to
    come off first, or the "prefix" reads as ``@dc`` and never matches the map.

    **Here rather than beside the inference code** because it is string splitting and
    nothing else -- no sample, no type, no judgement. It lives here so that
    :class:`Candidate` can ask it a question without importing a sibling, which is what
    keeps this module the bottom of the graph.
    """
    prefixes: set[str] = set()
    for segment in rendered.split("/"):
        name = segment.removeprefix("@")
        if ":" in name:
            prefixes.add(name.partition(":")[0])
    return prefixes


def _index_of(candidates: Sequence[Candidate], path: str | None) -> int:
    """1-based rank of ``path`` among ``candidates``, or 0 when it is absent.

    Belongs with the type it indexes for the same reason as :func:`_prefixes_in`: it is
    a question about candidates and their order, answered by walking a list. Importing
    the ranking module to ask it would make this module depend on the code that fills
    it.
    """
    if path is None:
        return 0
    return next((i for i, c in enumerate(candidates, start=1) if c.path == path), 0)
