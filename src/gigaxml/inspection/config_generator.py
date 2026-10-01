"""A ranked candidate rendered as a runnable YAML config.

**The last step of ``inspect``, and the only one that produces something you can run.**
It turns "this path probably holds your records, and these are the fields its instances
expose" into the config :mod:`gigaxml.config` reads -- which means it is where an
inference from the ranking stage becomes a thing the extractor will believe.

That is worth stating plainly, because it is why the module is separate rather than
folded into the ranking. A wrong score costs a re-run of ``inspect``, which is cheap. A
wrong config costs a silent extraction against the wrong record path, which is not --
and the failure looks like empty output rather than like an error.

**Field types in here are proposals, and they are marked as such.** Each field's type
comes from :func:`~gigaxml.inspection.inference.infer_field_type`, which reads a
bounded sample. A sample is evidence, not a census, so the type written is the one the
sample supports and the user is expected to correct it -- which is why ``decimal`` is
never inferred, and why the config it produces is a starting point rather than an
answer.
"""

from __future__ import annotations

import sys

import yaml

from gigaxml.errors import InspectionError
from gigaxml.fields import FieldType
from gigaxml.inspection.inference import infer_field_type
from gigaxml.inspection.models import InspectionReport, _index_of

__all__ = ["generate_config"]


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
