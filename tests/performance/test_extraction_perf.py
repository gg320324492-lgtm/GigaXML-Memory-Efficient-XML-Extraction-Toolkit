"""Extraction-cost regression tests.

``_descend`` used to rescan a node's children once per field, making extraction
O(fields x children). That is **invisible on the synthetic datasets** -- every
generated record has 6 children -- so Phase 7's benchmark would never have exposed
it, and the only chance to catch it is a test that builds deliberately wide
records.

The case that matters is a config reading several fields out of the same wrapper
(``manufacturer/name`` + ``manufacturer/country``), because that is where the
repeat scan is pure waste. ``tests/unit/test_fields.py`` pins the memo itself
deterministically; the thresholds here are a second line of defence.

Excluded from CI (roadmap A11). Thresholds are calibrated on the development
machine and are meant to catch a change of *kind* -- a factor of several -- not a
few percent of noise.
"""

from __future__ import annotations

import time

import pytest
from lxml import etree

from gigaxml.config import parse_config
from gigaxml.fields import extract_record

pytestmark = pytest.mark.performance

#: Fields in the wide-record config, all sharing one first path segment.
FIELD_COUNT = 50

#: Children on the wide record. The generated data has 6, so this is ~800x wider.
WIDE_CHILDREN = 5004

#: Narrow record, for the scaling comparison.
NARROW_CHILDREN = 504

#: Ceiling on nanoseconds per field for the shared-segment case.
#:
#: Measured on the development machine: ~2 900 ns/field with the memo, ~21 000
#: ns/field without it. The ceiling sits between the two with ~3.4x headroom for
#: the shipped code and ~2.1x for the regression it is meant to catch.
NS_PER_FIELD_LIMIT = 10_000


def build_wide_record(children: int) -> etree._Element:
    """A ``<product>`` with one ``<w>`` wrapper plus filler up to ``children``."""
    inner = "".join(f"<f{i}>value-{i}</f{i}>" for i in range(FIELD_COUNT))
    filler = "".join(f"<x{i}/>" for i in range(max(0, children - 1)))
    return etree.fromstring(f'<product id="1"><w>{inner}</w>{filler}</product>')


def shared_segment_fields() -> dict[str, dict[str, str]]:
    """Every field descends through the same ``w`` element."""
    return {f"f{i}": {"path": f"w/f{i}"} for i in range(FIELD_COUNT)}


def _best_ns_per_field(record: etree._Element, fields: list, *, batches: int = 3) -> float:
    """Fastest of ``batches`` timed runs, reported per field.

    The minimum is used rather than the mean: the fastest run is the one least
    disturbed by the scheduler, which is what a regression test wants to compare.
    """
    extract = extract_record
    for _ in range(5):  # warm up
        extract(record, fields)

    best = float("inf")
    for _ in range(batches):
        started = time.perf_counter()
        extract(record, fields)
        best = min(best, time.perf_counter() - started)
    return best * 1e9 / len(fields)


def test_shared_first_segment_on_a_wide_record_is_bounded() -> None:
    """The regression guard: 50 fields out of one wrapper on a 5 004-child record.

    Without the per-(node, tag) memo every field rescans all 5 004 children; with
    it the record is scanned once and each field only scans the 50-child wrapper.
    """
    config = parse_config({"record": "/r", "fields": shared_segment_fields()})
    record = build_wide_record(WIDE_CHILDREN)

    per_field = _best_ns_per_field(record, config.fields)

    print(
        f"[perf] shared segment, {WIDE_CHILDREN} children, "
        f"{FIELD_COUNT} fields: {per_field:.0f} ns/field"
    )
    assert per_field <= NS_PER_FIELD_LIMIT, (
        f"{per_field:.0f} ns/field on a {WIDE_CHILDREN}-child record, over the "
        f"{NS_PER_FIELD_LIMIT} ns ceiling; the children are being rescanned per field"
    )


def test_cost_per_field_grows_with_width_not_with_width_squared() -> None:
    """A guard against accidental quadratic behaviour.

    10x the children may cost ~10x per field (each field must look at every child
    once), but not 100x. The bound is loose on purpose: it is there to catch a
    change of kind, and the two widths differ by a factor of ten.
    """
    config = parse_config({"record": "/r", "fields": shared_segment_fields()})

    narrow = _best_ns_per_field(build_wide_record(NARROW_CHILDREN), config.fields)
    wide = _best_ns_per_field(build_wide_record(WIDE_CHILDREN), config.fields)
    ratio = wide / narrow

    print(
        f"[perf] {NARROW_CHILDREN} -> {WIDE_CHILDREN} children "
        f"({WIDE_CHILDREN / NARROW_CHILDREN:.1f}x the data): {ratio:.2f}x the cost per field"
    )
    assert ratio <= 4 * (WIDE_CHILDREN / NARROW_CHILDREN), (
        f"{WIDE_CHILDREN / NARROW_CHILDREN:.1f}x the children cost {ratio:.2f}x per field; "
        f"that is worse than linear"
    )


def test_a_wide_record_still_extracts_the_right_values() -> None:
    """The cheap sanity check: the wide-record path is not just fast, it is correct."""
    config = parse_config({"record": "/r", "fields": shared_segment_fields()})
    record = build_wide_record(WIDE_CHILDREN)

    result = extract_record(record, config.fields)

    assert result.values["f0"] == "value-0"
    assert result.values[f"f{FIELD_COUNT - 1}"] == f"value-{FIELD_COUNT - 1}"
    assert len(result.values) == FIELD_COUNT
    assert result.multi_matches == {}
