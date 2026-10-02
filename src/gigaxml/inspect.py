"""Streaming structure inspection: what is in this document, without reading it all.

The motivating case is a multi-gigabyte XML file nobody has a schema for. ``inspect``
answers "what is in here, and which element repeats often enough to be a record?" by
walking the document once and keeping **path-level counters**, never elements.

**Why the walk looks the way it does.** The record path is not known yet, so the
``iterparse(tag=...)`` shortcut is unavailable -- and it is a red line anyway: an element
the parser never reports is one that can never be released, which is what made the naive
reader's memory grow with the document (see :mod:`gigaxml.parser.streaming`). So the walk
subscribes to ``("start", "end", "start-ns")`` with no tag filter, tracks the open-element
stack to build each element's path, and **releases every element the moment it ends**.

**The path table is capped, and hitting the cap is reported.** Ten thousand distinct paths
is more than a human can read, so the table stops growing there -- but it says so, with the
number of further occurrences and a sample of what it did not list. Silent truncation would
be worse than none: the report would look complete and be wrong.

**Candidate scoring is explainable by construction.** A score comes from exactly two
deterministic statistics -- how often the element repeats under its parent, and how similar
its instances' sibling structures are -- and every candidate carries both as evidence. A
candidate must also have structure; a bare leaf repeats exactly as often as its parent
record and would tie with it, so it is not a candidate.

**Where the code is.** The walk is in :mod:`gigaxml.inspection.scanner` and the ranking in
:mod:`gigaxml.inspection.candidates`; this module is the entry point that stays put, because
``from gigaxml.inspect import inspect_document`` is a published import path -- the CLI's
``inspect_cmd``, the desktop panels and the benchmarks all use it. The full module map, and
why the split fell where it did, are in the engineering notes.

That split is the separation this module exists for. "/catalog/product occurred 2,000,000
times" is a fact about the document, measured by counting. "This is probably the record
path, score 0.98" is a heuristic over that fact, and can be wrong about a document it has
never seen. :data:`gigaxml.inspection.models.FIELD_KINDS` says which is which in the JSON,
and the weights a reader might argue with live next to the formula that applies them.
"""

from __future__ import annotations

from typing import Final

from gigaxml.inspection.candidates import (
    _CONSISTENCY_WEIGHT,
    _MIN_CANDIDATE_COUNT,
    _REPEAT_WEIGHT,
)
from gigaxml.inspection.config_generator import generate_config
from gigaxml.inspection.inference import infer_field_type
from gigaxml.inspection.models import (
    FIELD_KINDS,
    Candidate,
    InspectionReport,
    PathEntry,
    ValueSample,
)
from gigaxml.inspection.scanner import DEFAULT_MAX_DEPTH, DEFAULT_MAX_PATHS, inspect_document

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_PATHS",
    "FIELD_KINDS",
    "Candidate",
    "InspectionReport",
    "PathEntry",
    "ValueSample",
    "generate_config",
    "infer_field_type",
    "inspect_document",
]

#: The two scoring weights, and the minimum repeat count. **Re-exported rather than
#: moved**, because the scoring formula is the one thing in ``inspect`` a caller may
#: want to reproduce or argue with, and a constant nobody can reach is not an argument
#: waiting to happen. The formula itself is in :mod:`gigaxml.inspection.candidates`
#: and did not change by a character.
_REPEAT_WEIGHT: Final = _REPEAT_WEIGHT
_CONSISTENCY_WEIGHT: Final = _CONSISTENCY_WEIGHT
_MIN_CANDIDATE_COUNT: Final = _MIN_CANDIDATE_COUNT
