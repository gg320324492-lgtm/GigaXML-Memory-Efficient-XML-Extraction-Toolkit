"""Type narrowing from sampled values, and the prefix helpers that go with it.

Two things live here, and they are here together because each is a reading of the
document rather than a judgement about it.

**The value collector** samples a bounded number of text and attribute values per
field slot, so ``--infer-types`` has something to narrow from. The bounds are what
keep the walk's memory flat: sampling is off by default, and even on, the sample is
capped per slot and per document.

**The prefix helpers** read a rendered path or a candidate's own fields and report
which namespace prefixes they use. That is a fact about strings, not a judgement --
but it belongs with the collector rather than with the scanner, because it exists to
serve inference and config generation, and putting it in the scanner would make the
fact layer depend on the modules that consume its output.

**The narrowing itself is an inference and is labelled as one.** ``infer_field_type``
guesses from a sample, and a sample of three values is not a census: it can say
``int`` for a column whose 400th row holds a date. What it returns is therefore a
*proposal*, carried in the generated config as an explicit ``type:``, which the user
edits or does not. Nothing here decides on its own.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

from gigaxml.errors import FieldTypeError
from gigaxml.fields import FieldType, coerce_value
from gigaxml.inspection.models import Candidate, ValueSample

__all__ = ["infer_field_type"]

#: (path, slot) pairs for which text/attribute values are sampled.
_MAX_VALUE_SLOTS: Final = 4_096

#: Distinct values kept per slot.
_MAX_VALUES_PER_SLOT: Final = 32


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


def _converts(value: str, field_type: FieldType) -> bool:
    """Whether ``value`` converts under the extractor's own rules."""
    try:
        coerce_value(value, field_type, "<inspect>")
    except FieldTypeError:
        return False
    return True


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
