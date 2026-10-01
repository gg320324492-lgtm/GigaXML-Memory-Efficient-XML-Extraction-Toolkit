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

**Where the code is.** The walk lives in :mod:`gigaxml.inspection.scanner`; this
module is the entry point that stays put. ``from gigaxml.inspect import
inspect_document`` is a published import path -- the CLI's ``inspect_cmd``, the
desktop application's panels and the benchmarks all use it -- so it is re-exported
here rather than moved. What moved is the code, and it moved by responsibility:

=========================================  ==========================================
:mod:`gigaxml.inspection.models`            the data classes, which decide nothing
:mod:`gigaxml.inspection.scanner`           the walk: what is **in** the document
:mod:`gigaxml.inspection.candidates`        the ranking: what a record **probably is**
:mod:`gigaxml.inspection.inference`         type narrowing from sampled values
:mod:`gigaxml.inspection.config_generator`  a candidate rendered as a YAML config
=========================================  ==========================================

That scanner/candidates line is the separation this split exists for. "/catalog/
product occurred 2,000,000 times" is a fact about the document, measured by counting.
"This is probably the record path, score 0.98" is a heuristic over that fact, and can
be wrong about a document it has never seen. They were computed in one function body
before, so a reader could not tell which was which. Now the module a value comes from
says, :data:`gigaxml.inspection.models.FIELD_KINDS` says so in the JSON, and the
weights a reader might want to argue with live next to the formula that applies them
rather than 400 lines away in a file that also parses XML.

Nothing about either changed: the same weights, the same wording, the same algorithm.
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
