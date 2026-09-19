"""The rejection log must be flat in the number of rejections.

This is the one place in Phase 5A with a memory obligation attached, and it is the
easy one to get wrong: collecting rejected records and writing them once at the end is
the obvious implementation, and it makes memory proportional to the number of bad
records -- which is the one quantity a quarantine run is most likely to have a lot of.

The measurement makes every record fail, so there is exactly one rejection per record:
about 291,000 for the 100MB dataset and 1.16 million for the 400MB one. If the log
buffered, the 400MB delta would be roughly four hundred times the 100MB delta. The
additive band from criterion 4b catches that without needing to know the constant.

Measured in a subprocess through ``tests/_mem.py --reject``. Excluded from CI (A11).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._mem import rejections_in_subprocess

pytestmark = pytest.mark.performance

#: Gate 4b reused: 4x the rejections may not add more than this much marginal memory.
PLATEAU_ADDITIVE_LIMIT_MB = 8.0

#: Gate 4a reused: an absolute ceiling for the 400MB run.
ABSOLUTE_LIMIT_MB = 32.0


def _report(label: str, profile: dict[str, float | int]) -> None:
    print(
        f"[reject] {label:<8} baseline={profile['baseline_mb']:>8} MiB  "
        f"peak={profile['peak_mb']:>8} MiB  delta={profile['delta_mb']:>7} MiB  "
        f"records={profile['records']:>10,}  rejected={profile['rejected']:>10,}  "
        f"log={profile['rejected_log_mb']:>8} MiB  {profile['seconds']:>8}s"
    )


def test_a_million_rejections_cost_no_extra_memory(
    s100_path: Path, s400_path: Path, tmp_path: Path
) -> None:
    small = rejections_in_subprocess(s100_path, tmp_path / "s100")
    large = rejections_in_subprocess(s400_path, tmp_path / "s400")
    _report("s100", small)
    _report("s400", large)

    delta_100 = float(small["delta_mb"])
    delta_400 = float(large["delta_mb"])
    difference = delta_400 - delta_100

    print(
        f"[reject] 4x the rejections cost {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB)  (limit +{PLATEAU_ADDITIVE_LIMIT_MB} MiB)"
    )
    print(f"[reject] 400MB delta = {delta_400} MiB (limit {ABSOLUTE_LIMIT_MB})")

    # The run really did reject everything, rather than bailing out early.
    assert large["records"] == 4 * small["records"] == 1_164_800
    assert large["rejected"] == large["records"]
    assert small["rows"] == large["rows"] == 0, "nothing survives an all-bad document"
    assert large["rejected_log_mb"] > 200, "the log is real, not a stub"

    assert delta_400 <= ABSOLUTE_LIMIT_MB, (
        f"rejecting {large['rejected']:,} records added {delta_400} MiB, over the "
        f"{ABSOLUTE_LIMIT_MB} MiB ceiling"
    )
    assert difference <= PLATEAU_ADDITIVE_LIMIT_MB, (
        f"4x the rejections added {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB), over the +{PLATEAU_ADDITIVE_LIMIT_MB} MiB "
        f"band; the rejection log is accumulating instead of streaming"
    )
