"""Bounded-memory proof for the streaming reader.

This is the test that gives the project its reason to exist, so it is written to
be able to *fail*.

**Which number is asserted, and why.** Two quantities come out of every
measurement:

``peak_mb``
    The process's peak RSS. This is what ``peak_rss_mb()`` returns (B3), and the
    gate ``peak_rss(s100) <= 1.30 * peak_rss(s10)`` is asserted on it.
``delta_mb``
    ``peak_mb`` minus the RSS measured immediately before the scan, i.e. the
    marginal cost of the scan. The 100MB ceiling is asserted on the increment
    over an empty-process baseline, which is the same quantity.

**Ratios of ``delta_mb`` are never asserted, anywhere in this file.** At these
sizes the marginal cost is around 1 MiB -- the same order as CPython's allocator
warm-up -- so a ratio of two such numbers measures arena noise rather than the
algorithm. Across the 49-pair campaign on record (35 pairs from the Phase 1.6
probe plus the auditor's independent 14, all in ``docs/PHASE-1.6-REPORT.md``)
``delta_mb`` for the same 100MB file ranged over **0.152 .. 2.227 MiB**, and for
the same 400MB file over **0.383 .. 2.184 MiB**.

Dividing one noisy number by another therefore fails on noise alone, and it did:
the retired ``delta(400) / delta(100) <= 2.0`` gate failed on runs where the
reader was behaving perfectly -- 4 times out of 35 in the probe, plus 1 more in
the auditor's batch (``3.360x`` on a difference of only +0.590 MiB). The worst
was 0.152 MiB -> 1.316 MiB, a ratio of 8.658x that reads as an explosion, sitting
on a difference of +1.164 MiB.

**Boundedness on ``delta_mb`` is therefore asserted as an additive band**, which
is what the noise actually is::

    delta(400MB) <= delta(100MB) + 8 MiB

**Why 8 MiB and not 4 MiB -- do not tighten this back without new data.** The
band was first set to 4 MiB against an observed worst difference of +1.164 MiB
(2.2x margin at the time, from 30 pairs). Widening the sample widened the worst
case: at 35 pairs it was +1.840 MiB, i.e. the tail had not been sampled out, and
4.0 / 1.840 is only 2.17x -- too thin to survive another batch. Across all 49
pairs the difference never left -1.070 .. +1.840 MiB, so 8 MiB carries a **4.35x
margin**. That is still overwhelming against the counterfactual: a reader that
kept the document resident would differ by 300 MiB at the very least -- **37.5x
the band** -- and nearer 950x if the measured no-cleanup cost (~1265 MiB for the
100MB file alone) is extrapolated linearly. The heavy lifting is done by
``test_peak_rss_stays_bounded`` (absolute 32 MiB ceiling),
``test_uncleaned_reader_blows_up`` (50x separation) and
``test_non_record_subtrees_are_released`` (5 MiB); this band only has to answer
"does memory grow with input size at all", so it can afford to be loose.

Ratios are still *printed* for transparency, and labelled informational so nobody
mistakes them for gates.

**How small a leak this suite can see -- do not overstate it.** Both gates bound an
absolute number of MiB, so they can only detect a leak once it is large enough to
move that number. Against the two dataset sizes on record:

===========  ==========================  ============================
gate         budget                      smallest detectable leak
===========  ==========================  ============================
4b           ``8 MiB / 873,600``         **~10 bytes per record**
4a           ``32 MiB / 1,164,800``      ~28 bytes per record
===========  ==========================  ============================

Below roughly 10 bytes per record this suite is blind: catching 1 byte per record
would need a record-count difference of ~8.4 million, which is far past what a
routine test run can afford. The backstop is Phase 7's real 4 GB benchmark, where
1.16 million extra records at 1 byte each is 11.6 MiB against a ~1.5 MiB baseline
and therefore visible. Until that benchmark exists, "bounded memory" here means
"bounded to within ~10 bytes per record", not "no leak at all".

Throughput is reported but never asserted -- see roadmap revision A11.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._mem import empty_baseline_mb, scan_in_subprocess

pytestmark = pytest.mark.performance

#: Peak RSS of the 100MB scan may not exceed this multiple of the 10MB scan.
RATIO_LIMIT = 1.30

#: Absolute ceiling for the 100MB scan, over the empty-process baseline, in MiB.
ABSOLUTE_LIMIT_MB = 100.0

#: Ceiling for a scan whose bulk sits in a subtree that is not the record type.
NON_RECORD_LIMIT_MB = 5.0

#: 4x the data may not add more than this many MiB of marginal memory.
#:
#: An additive band, not a ratio. The measurement noise at the ~1 MiB scale is
#: additive, so a ratio of two noisy numbers can fail on noise alone -- measured
#: here, the retired ratio gate failed 4 times out of 35 pairs, worst of all
#: 8.658x on a difference of only +1.164 MiB. A constant *offset* on a ratio does
#: not fix that either: with delta(100) = 0.66 MiB a linear reader would land at
#: delta(400) ~ 2.64 MiB, which ``2.0 * 0.66 + 2 = 3.32`` would wave through.
#:
#: **8.0, not 4.0 -- do not tighten this without new measurements.** 4 MiB was
#: chosen when the worst observed difference was +1.164 MiB (30 pairs). A larger
#: sample moved the worst case to +1.840 MiB (35 pairs) and the auditor's
#: independent 14 pairs did not beat it, so the tail is still not sampled out;
#: 4.0 / 1.840 = 2.17x was too thin. Across all 49 pairs the difference never left
#: -1.070 .. +1.840 MiB, giving 8 MiB a 4.35x margin. A linear reader would differ
#: by 300 MiB at minimum -- 37.5x the band -- so the discrimination lost by
#: widening is negligible, and the sharp gates live elsewhere (see the module
#: docstring).
PLATEAU_ADDITIVE_LIMIT_MB = 8.0

_MEASUREMENTS: dict[tuple[str, bool], dict[str, float | int]] = {}


def _measure(path: Path, *, clean: bool) -> dict[str, float | int]:
    """Run the subprocess measurement once per (file, mode) and reuse it."""
    key = (str(path), clean)
    if key not in _MEASUREMENTS:
        _MEASUREMENTS[key] = scan_in_subprocess(path, clean=clean)
    return _MEASUREMENTS[key]


def _report(label: str, profile: dict[str, float | int]) -> None:
    print(
        f"[mem] {label:<20} baseline={profile['baseline_mb']:>8} MiB  "
        f"peak={profile['peak_mb']:>8} MiB  delta={profile['delta_mb']:>8} MiB  "
        f"records={profile['records']:>9}  {profile['seconds']:>7}s  "
        f"{profile['records_per_sec']:>11} rec/s  {profile['mb_per_sec']:>7} MiB/s"
    )


def test_peak_rss_stays_bounded(s10_path: Path, s100_path: Path) -> None:
    """10x the data must not cost 10x the memory -- nor more than 100MB."""
    small = _measure(s10_path, clean=True)
    large = _measure(s100_path, clean=True)
    _report("s10 (clean)", small)
    _report("s100 (clean)", large)

    peak_small = float(small["peak_mb"])
    peak_large = float(large["peak_mb"])
    delta_small = float(small["delta_mb"])
    delta_large = float(large["delta_mb"])

    ratio = peak_large / peak_small
    delta_ratio = delta_large / delta_small if delta_small > 0 else float("inf")
    increment = peak_large - empty_baseline_mb()

    print(f"[mem] peak ratio 100MB/10MB      = {ratio:.3f}  (limit {RATIO_LIMIT})")
    print(f"[mem] marginal delta ratio       = {delta_ratio:.3f}  (informational, not a gate)")
    print(f"[mem] 100MB increment over empty = {increment:.3f} MiB (limit {ABSOLUTE_LIMIT_MB})")

    assert increment <= ABSOLUTE_LIMIT_MB, (
        f"100MB scan added {increment} MiB over the empty-process baseline, "
        f"over the {ABSOLUTE_LIMIT_MB} MiB ceiling"
    )
    assert ratio <= RATIO_LIMIT, (
        f"peak RSS went from {peak_small} MiB to {peak_large} MiB ({ratio:.3f}x) "
        f"for 10x the data, over the {RATIO_LIMIT} limit"
    )


def test_uncleaned_reader_blows_up(s100_path: Path) -> None:
    """Reversed test: prove the cleanup is load-bearing.

    Same reader, same file, one flag different. If this ever passes with a ratio
    near 1.0, the cleanup has stopped doing anything and the bounded-memory claim
    above is vacuous.
    """
    cleaned = _measure(s100_path, clean=True)
    uncleaned = _measure(s100_path, clean=False)
    _report("s100 (clean)", cleaned)
    _report("s100 (uncleaned)", uncleaned)

    delta_cleaned = float(cleaned["delta_mb"])
    delta_uncleaned = float(uncleaned["delta_mb"])
    ratio = delta_uncleaned / delta_cleaned if delta_cleaned > 0 else float("inf")
    print(f"[mem] uncleaned / cleaned marginal memory = {ratio:.2f}x")

    assert uncleaned["records"] == cleaned["records"], "both passes must see the same records"
    assert delta_uncleaned > 2.0 * delta_cleaned, (
        f"uncleaned reader used only {ratio:.2f}x the memory of the cleaned one; "
        f"the cleanup is not doing its job"
    )


def _write_orders_heavy_document(path: Path, orders: int = 400_000) -> None:
    """Write a document whose bulk is a subtree that is *not* the record type."""
    with path.open("w", encoding="utf-8") as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n<catalog>\n  <products>\n')
        for index in range(1, 51):
            handle.write(f'    <product id="{index}"><name>p{index}</name></product>\n')
        handle.write("  </products>\n  <orders>\n")
        for index in range(1, orders + 1):
            handle.write(
                f'    <order id="{index}"><product-ref>{index}</product-ref><qty>1</qty></order>\n'
            )
        handle.write("  </orders>\n</catalog>\n")


def test_non_record_subtrees_are_released(tmp_path: Path) -> None:
    """Regression test for the ``iterparse(tag=...)`` trap.

    A reader that filters on the record tag never receives the end events of any
    other element, so a document whose bulk sits outside the record type stays
    resident for the whole scan. The first version of this reader did exactly
    that: 100MB grew 9.4x over 10MB, and RSS peaked 94 MiB above baseline.
    """
    document = tmp_path / "orders-heavy.xml"
    _write_orders_heavy_document(document)
    profile = _measure(document, clean=True)
    _report("orders-heavy", profile)

    assert profile["records"] == 50
    assert float(profile["delta_mb"]) < NON_RECORD_LIMIT_MB, (
        f"a scan of {profile['input_mb']} MiB dominated by a non-record subtree "
        f"used {profile['delta_mb']} MiB; that subtree is not being released"
    )


def test_memory_plateaus(s10_path: Path, s100_path: Path, s400_path: Path) -> None:
    """4x more data must not add 4x more marginal memory.

    This is the assertion that actually establishes boundedness. The 10MB -> 100MB
    ratio alone cannot: 10MB sits below the allocator's warm-up plateau, so its
    marginal cost is artificially low. Once past the plateau the curve is flat.

    The bound is an **additive band** on the difference, not a ratio. At this
    scale ``delta_mb`` is dominated by CPython's allocator warm-up: across the
    49-pair campaign the same 100MB file ranged over 0.152 .. 2.227 MiB, so the
    ratio ``delta(400) / delta(100)`` fails on noise alone -- it did, 4 times out
    of 35, worst of all 8.658x on a difference of only +1.164 MiB. The difference
    is the stable quantity: it stayed inside -1.070 .. +1.840 MiB over all 49
    pairs, so ``PLATEAU_ADDITIVE_LIMIT_MB`` carries a 4.35x margin. See the
    constant's comment for why the band is 8 MiB rather than 4.
    """
    profiles = {
        "s10": _measure(s10_path, clean=True),
        "s100": _measure(s100_path, clean=True),
        "s400": _measure(s400_path, clean=True),
    }
    for label, profile in profiles.items():
        _report(f"{label} (plateau)", profile)

    delta_100 = float(profiles["s100"]["delta_mb"])
    delta_400 = float(profiles["s400"]["delta_mb"])
    size_ratio = float(profiles["s400"]["input_mb"]) / float(profiles["s100"]["input_mb"])
    delta_ratio = delta_400 / delta_100 if delta_100 > 0 else float("inf")
    difference = delta_400 - delta_100
    print(
        f"[mem] {size_ratio:.2f}x the data cost {delta_ratio:.3f}x the marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB, difference {difference:+.3f} MiB)"
        f"  (ratio informational, not a gate)"
    )
    print(
        f"[mem] plateau difference gate    = {difference:+.3f} MiB "
        f"(limit +{PLATEAU_ADDITIVE_LIMIT_MB} MiB)"
    )

    assert profiles["s400"]["records"] == 4 * profiles["s100"]["records"]
    assert difference <= PLATEAU_ADDITIVE_LIMIT_MB, (
        f"{size_ratio:.2f}x the input added {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB), over the +{PLATEAU_ADDITIVE_LIMIT_MB} MiB "
        f"band; that is not a plateau"
    )


def test_throughput_is_recorded(s10_path: Path, s100_path: Path) -> None:
    """Log throughput for the benchmark report. Intentionally assertion-free."""
    for label, path in (("s10", s10_path), ("s100", s100_path)):
        profile = _measure(path, clean=True)
        _report(f"{label} (throughput)", profile)
        assert profile["records"] > 0
