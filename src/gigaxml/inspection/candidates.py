"""The ranking: which repeating path is *probably* the record.

**Everything here is an inference, and nothing here counts anything.** A
:class:`~gigaxml.inspection.models.Candidate` says "this path repeats 2,000,000
times" and "this is therefore probably the record, score 0.98" in one object, and
this module is the half that produces the second half. The counts come from
:mod:`gigaxml.inspection.scanner`, which measures; this module decides what the
measurement means, and it can be wrong about a document it has never seen.

That is why the split put them apart rather than merely apart in the file listing. A
caller that wants a fact -- how often does this path occur -- should not have to know
whether the number arrived by counting or by scoring, and a caller reading a score
should not have to wonder how many times the path really repeated. The two questions
have different failure modes: a count is wrong only if the scan was wrong, a score is
wrong whenever the heuristic does not fit the document.

**The formula is unchanged and is not this milestone's business.** Two weights, a
minimum repeat count, and a tie-break towards the shallower path -- all fixed in
:data:`_REPEAT_WEIGHT`, :data:`_CONSISTENCY_WEIGHT` and
:data:`_MIN_CANDIDATE_COUNT`. What changed is which module they live in, so that the
sentence "a score is only meaningful within one report" sits next to the code that
computes it rather than 400 lines away in a file that also parses XML.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import TYPE_CHECKING, Final

from gigaxml.inspection.models import Candidate, PathEntry

if TYPE_CHECKING:  # pragma: no cover - typing only
    # The namespace accumulator is the *scanner's* object, and reaching it here would
    # make the inference layer depend on the fact layer's internals -- the direction
    # this split exists to make impossible. It appears in one annotation and is never
    # called, so a TYPE_CHECKING import keeps the runtime graph acyclic and the
    # annotation honest.
    from gigaxml.inspection.scanner import _PathAccumulator

__all__ = ["score_candidates"]

#: Weights of the two -- and only two -- scoring inputs.
_REPEAT_WEIGHT: Final = 0.6
_CONSISTENCY_WEIGHT: Final = 0.4

#: An element must repeat at least this often to be a record candidate.
_MIN_CANDIDATE_COUNT: Final = 2


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


def score_candidates(
    entries: Sequence[PathEntry],
    *,
    limit: int,
    accumulators: Mapping[str, _PathAccumulator],
) -> tuple[Candidate, ...]:
    """Rank the scanner's measurements, and give each candidate its namespace map.

    ★ **This is the only place the two halves of ``inspect`` meet**, and it is a named
    function rather than two calls the scanner makes itself so that the seam is
    checkable. A caller holding measurements cannot reach a judgement without going
    through here, and the scanner cannot produce a judgement without going through
    here -- which is what makes "the fact module does no scoring" a property of the
    code rather than a claim about it.

    Args:
        entries: the frozen path table from the scan.
        limit: how many ranked candidates to keep.
        accumulators: the scanner's live accumulators, read for prefix bindings only.
            A ``TYPE_CHECKING``-typed collaborator, never a computed one -- see the
            import at the top of this module for why that matters.
    """
    return _with_namespaces(_score_candidates(entries, limit=limit), accumulators)


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
