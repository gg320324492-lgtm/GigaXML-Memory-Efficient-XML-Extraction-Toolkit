"""Bounded-memory proof for the streaming reader.

This is the test that gives the project its reason to exist, so it is written to
be able to *fail*.

**Which number is asserted, and why.** Two quantities come out of every
measurement:

``peak_mb``
    The process's peak RSS. This is what ``peak_rss_mb()`` returns (B3), and the
    ratio gate ``peak_rss(s100) <= 1.30 * peak_rss(s10)`` is asserted on it.
``delta_mb``
    ``peak_mb`` minus the RSS measured immediately before the scan, i.e. the
    marginal cost of the scan. The 100MB ceiling is asserted on the increment
    over an empty-process baseline, which is the same quantity.

The *ratio* is deliberately not asserted on ``delta_mb``. At these sizes the
marginal cost is around 1 MiB, which is the same order as CPython's allocator
warm-up, so a delta ratio measures arena noise rather than the algorithm:
``tests`` measured 0.211 MiB at 10MB, 1.195 MiB at 100MB and 1.504 MiB at 400MB.
Ten times the data looked like 5.7x the memory, forty times the data looked like
7.1x -- while the absolute peak moved from 28.74 to 29.57 MiB. The delta ratio is
printed for transparency but not asserted; ``test_memory_plateaus`` is what
actually pins down boundedness.

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

#: 4x the data may not cost more than this multiple of the marginal memory.
PLATEAU_RATIO_LIMIT = 2.0

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
    print(f"[mem] marginal delta ratio       = {delta_ratio:.3f}  (informational)")
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
    """4x more data must not cost 4x more marginal memory.

    This is the assertion that actually establishes boundedness. The 10MB -> 100MB
    ratio alone cannot: 10MB sits below the allocator's warm-up plateau, so its
    marginal cost is artificially low. Once past the plateau the curve is flat.
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
    print(
        f"[mem] {size_ratio:.2f}x the data cost {delta_ratio:.3f}x the marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB)"
    )

    assert profiles["s400"]["records"] == 4 * profiles["s100"]["records"]
    assert delta_ratio <= PLATEAU_RATIO_LIMIT, (
        f"{size_ratio:.2f}x the input grew marginal memory {delta_ratio:.3f}x "
        f"({delta_100} MiB -> {delta_400} MiB); that is not a plateau"
    )


def test_throughput_is_recorded(s10_path: Path, s100_path: Path) -> None:
    """Log throughput for the benchmark report. Intentionally assertion-free."""
    for label, path in (("s10", s10_path), ("s100", s100_path)):
        profile = _measure(path, clean=True)
        _report(f"{label} (throughput)", profile)
        assert profile["records"] > 0
