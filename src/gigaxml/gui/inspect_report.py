"""Reading ``inspect --json``. Pure functions, no Qt, no I/O.

The JSON is a contract; the wording of the warnings on stderr is not. This module reads
the former and never the latter -- a panel that scraped "shadowed" out of a warning would
break the first time somebody improved the sentence.

Like :mod:`gigaxml.gui.progress`, this exists separately from the window because the
interesting failures are here: a key that moved, a value that arrived as the wrong type, a
document that produced no candidates at all. All of that is testable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _as_str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _as_str_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str)}


@dataclass(frozen=True)
class Candidate:
    """One repeating structure the tool thinks might be the records."""

    path: str
    score: float
    count: int
    shape_consistency: float
    repeat_score: float
    #: The path this candidate sits *inside*, or ``None`` if it is a top-level one.
    #: Read from the JSON rather than inferred from the text, which is the whole point of
    #: the field existing.
    nested_inside: str | None
    depth: int
    child_tags: tuple[str, ...] = ()
    attribute_names: tuple[str, ...] = ()
    namespaces: dict[str, str] = field(default_factory=dict)
    missing_namespaces: tuple[str, ...] = ()
    evidence: str = ""

    @property
    def is_nested(self) -> bool:
        return self.nested_inside is not None


@dataclass(frozen=True)
class PathEntry:
    """One distinct path in the document."""

    path: str
    count: int
    depth: int
    has_children: bool
    shape_consistency: float
    distinct_shapes: int
    dominant_shape_count: int
    child_tags: tuple[str, ...] = ()
    attribute_names: tuple[str, ...] = ()
    shapes_truncated: bool = False


@dataclass(frozen=True)
class InspectReport:
    """Everything ``inspect --json`` said about one document."""

    source: str
    input_mb: float
    elements_seen: int
    namespaces: dict[str, str]
    shadowed_prefixes: tuple[str, ...]
    unmapped_namespaces: tuple[str, ...]
    candidates: tuple[Candidate, ...]
    paths: tuple[PathEntry, ...]
    paths_truncated: bool
    paths_limit: int
    paths_tracked: int
    untracked_occurrences: int
    depth_seen: int
    depth_limit: int
    depth_truncated: bool
    values_truncated: bool

    @property
    def has_warnings(self) -> bool:
        """Whether anything about this report deserves to be shown prominently.

        Kept together so a panel cannot accidentally show one warning and forget another:
        each of these means the picture is incomplete or ambiguous, and a user reading the
        candidate list without knowing that would draw the wrong conclusion.
        """
        return bool(
            self.shadowed_prefixes
            or self.unmapped_namespaces
            or self.paths_truncated
            or self.depth_truncated
            or self.values_truncated
        )

    def warnings(self) -> list[str]:
        """One line per thing the user should know, in plain language."""
        lines: list[str] = []
        if self.shadowed_prefixes:
            lines.append(
                "These prefixes mean more than one thing in this document, so paths "
                "using them cannot be read as a single namespace: "
                + ", ".join(self.shadowed_prefixes)
            )
        if self.unmapped_namespaces:
            lines.append(
                "These namespaces are used in the document but have no prefix: "
                + ", ".join(self.unmapped_namespaces)
            )
        if self.paths_truncated:
            lines.append(
                f"The path table stopped at {self.paths_limit} distinct paths. "
                f"{self.untracked_occurrences:,} later occurrences were not tracked, so "
                f"the list below is incomplete."
            )
        if self.depth_truncated:
            lines.append(
                f"The document nests deeper than {self.depth_limit} levels and was not "
                f"followed past that."
            )
        if self.values_truncated:
            lines.append(
                "Only a sample of values was collected; the examples shown are not exhaustive."
            )
        return lines

    def candidate_for(self, path: str) -> Candidate | None:
        for candidate in self.candidates:
            if candidate.path == path:
                return candidate
        return None


def parse_report(payload: object) -> InspectReport | None:
    """Turn a decoded ``--json`` payload into a report, or ``None`` if it is not one.

    ``None`` rather than an exception, for the same reason the progress parser returns
    ``None``: the child may print something unexpected, and a window must not fall over
    because of it. A payload missing its top-level shape is not a report.
    """
    data = _mapping(payload)
    if not data or "candidates" not in data:
        return None

    depth = _mapping(data.get("depth"))
    table = _mapping(data.get("path_table"))

    return InspectReport(
        source=_as_str(data.get("source")),
        input_mb=_as_float(data.get("input_mb")) or 0.0,
        elements_seen=_as_int(data.get("elements_seen")) or 0,
        namespaces={
            key: value
            for key, value in _mapping(data.get("namespaces")).items()
            if isinstance(value, str)
        },
        shadowed_prefixes=_as_str_tuple(data.get("shadowed_prefixes")),
        unmapped_namespaces=_as_str_tuple(data.get("unmapped_namespaces")),
        candidates=tuple(_candidate(item) for item in _list(data.get("candidates"))),
        paths=tuple(_path_entry(item) for item in _list(data.get("paths"))),
        paths_truncated=bool(table.get("truncated")),
        paths_limit=_as_int(table.get("limit")) or 0,
        paths_tracked=_as_int(table.get("tracked")) or 0,
        untracked_occurrences=_as_int(table.get("untracked_occurrences")) or 0,
        depth_seen=_as_int(depth.get("seen")) or 0,
        depth_limit=_as_int(depth.get("limit")) or 0,
        depth_truncated=bool(depth.get("truncated")),
        values_truncated=bool(_mapping(data.get("value_sampling")).get("truncated")),
    )


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _candidate(item: object) -> Candidate:
    data = _mapping(item)
    nested = data.get("nested_inside")
    return Candidate(
        path=_as_str(data.get("path")),
        score=_as_float(data.get("score")) or 0.0,
        count=_as_int(data.get("count")) or 0,
        shape_consistency=_as_float(data.get("shape_consistency")) or 0.0,
        repeat_score=_as_float(data.get("repeat_score")) or 0.0,
        nested_inside=nested if isinstance(nested, str) else None,
        depth=_as_int(data.get("depth")) or 0,
        child_tags=_as_str_tuple(data.get("child_tags")),
        attribute_names=_as_str_tuple(data.get("attribute_names")),
        namespaces={
            key: value
            for key, value in _mapping(data.get("namespaces")).items()
            if isinstance(value, str)
        },
        missing_namespaces=_as_str_tuple(data.get("missing_namespaces")),
        evidence=_as_str(data.get("evidence")),
    )


def _path_entry(item: object) -> PathEntry:
    data = _mapping(item)
    return PathEntry(
        path=_as_str(data.get("path")),
        count=_as_int(data.get("count")) or 0,
        depth=_as_int(data.get("depth")) or 0,
        has_children=bool(data.get("has_children")),
        shape_consistency=_as_float(data.get("shape_consistency")) or 0.0,
        distinct_shapes=_as_int(data.get("distinct_shapes")) or 0,
        dominant_shape_count=_as_int(data.get("dominant_shape_count")) or 0,
        child_tags=_as_str_tuple(data.get("child_tags")),
        attribute_names=_as_str_tuple(data.get("attribute_names")),
        shapes_truncated=bool(data.get("shapes_truncated")),
    )


def paths_to_csv(report: InspectReport) -> str:
    """The path table as CSV, for the export the brief asks for.

    Written here rather than in the panel so the escaping is testable: a path containing a
    comma or a quote is entirely possible in XML, and a naive join would produce a file
    that a spreadsheet reads as the wrong number of columns.
    """
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        ["path", "count", "depth", "has_children", "shape_consistency", "distinct_shapes"]
    )
    for entry in report.paths:
        writer.writerow(
            [
                entry.path,
                entry.count,
                entry.depth,
                entry.has_children,
                entry.shape_consistency,
                entry.distinct_shapes,
            ]
        )
    return buffer.getvalue()
