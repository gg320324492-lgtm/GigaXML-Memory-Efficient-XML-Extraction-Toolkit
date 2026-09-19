"""``inspect`` must stay flat on a document nobody has a schema for.

This is the hardest of the memory cases. The reader can lean on knowing the record
path; ``inspect`` cannot, so it has no ``tag=`` filter to hide behind and must
release every element itself. If it did not, memory would grow with the document and
the whole feature would be unusable on the files it exists for.

Measured in a subprocess through ``tests/_mem.py --inspect``: in-process numbers
would include pytest's heap and the garbage earlier tests left behind, which is more
than enough to swamp a sub-MiB result.

Excluded from CI (roadmap A11); the 400MB walk takes about 27 seconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._mem import inspect_in_subprocess

pytestmark = pytest.mark.performance

#: Gate 4b reused: 4x the input may not add more than this much marginal memory.
PLATEAU_ADDITIVE_LIMIT_MB = 8.0

#: Gate 4a reused: an absolute ceiling for the 400MB walk.
ABSOLUTE_LIMIT_MB = 32.0


def _report(label: str, profile: dict[str, float | int]) -> None:
    print(
        f"[inspect] {label:<10} baseline={profile['baseline_mb']:>8} MiB  "
        f"peak={profile['peak_mb']:>8} MiB  delta={profile['delta_mb']:>7} MiB  "
        f"elements={profile['elements_seen']:>11,}  paths={profile['paths']:>5}  "
        f"{profile['seconds']:>7}s  {profile['mb_per_sec']:>6} MiB/s"
    )


def test_inspecting_four_times_the_data_costs_no_extra_memory(
    s100_path: Path, s400_path: Path
) -> None:
    """Gate 2: ``delta(400) <= delta(100) + 8 MiB``, the additive band from 4b."""
    small = inspect_in_subprocess(s100_path)
    large = inspect_in_subprocess(s400_path)
    _report("s100", small)
    _report("s400", large)

    delta_100 = float(small["delta_mb"])
    delta_400 = float(large["delta_mb"])
    difference = delta_400 - delta_100

    print(
        f"[inspect] 4.01x the input cost {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB)  (limit +{PLATEAU_ADDITIVE_LIMIT_MB} MiB)"
    )
    print(f"[inspect] 400MB delta = {delta_400} MiB (limit {ABSOLUTE_LIMIT_MB})")

    # Records scale exactly 4x; total elements do not, because the document
    # header does not. The point of this assertion is only that the walk really
    # did the work rather than bailing out early.
    ratio = large["elements_seen"] / small["elements_seen"]
    assert 3.99 <= ratio <= 4.01, ratio
    assert delta_400 <= ABSOLUTE_LIMIT_MB, (
        f"the 400MB walk added {delta_400} MiB, over the {ABSOLUTE_LIMIT_MB} MiB ceiling"
    )
    assert difference <= PLATEAU_ADDITIVE_LIMIT_MB, (
        f"4x the input added {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB), over the +{PLATEAU_ADDITIVE_LIMIT_MB} MiB "
        f"band; elements are not being released"
    )


def test_the_path_table_does_not_grow_with_the_document(s100_path: Path, s400_path: Path) -> None:
    """The document is 4x bigger and has 4x the elements but the same *schema*.

    A path table that grew with the data would mean paths were being tracked per
    element rather than per distinct path, which is the whole memory story here.
    """
    small = inspect_in_subprocess(s100_path)
    large = inspect_in_subprocess(s400_path)

    assert small["elements_seen"] > 3_000_000
    assert large["elements_seen"] > 14_000_000
    assert small["paths"] == large["paths"], "same schema, same number of distinct paths"
    assert small["paths"] < 100, "a schema has tens of paths, not millions"


def test_the_candidate_is_the_same_record_at_both_sizes(s100_path: Path, s400_path: Path) -> None:
    """Ranking must not depend on document size.

    An absolute saturating repeat term made the top candidate flip between sizes
    during development, because two record kinds both reached the ceiling and the
    tie-break decided. The term is now relative to the document.
    """
    small = inspect_in_subprocess(s100_path)
    large = inspect_in_subprocess(s400_path)

    assert small["top_candidate"] == large["top_candidate"] == "/catalog/products/product"
    assert large["top_count"] == 4 * small["top_count"] == 1_164_800
