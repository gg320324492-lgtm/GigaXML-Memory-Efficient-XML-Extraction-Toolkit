"""The fast-forward must cost time, not memory.

``--resume`` re-parses the source and skips the records already accounted for. That is
the only way to do it -- XML cannot be re-entered mid-stream -- and it is honest as
long as skipping is cheap in memory, which is what these tests pin:

* skipping four times as many records must not cost four times the memory (criterion
  4b's additive band, reused), which is what would happen if skipped records were
  retained;
* skipping everything must not cost more than processing everything, measured with the
  same config so the comparison is fair.

Measured in subprocesses through ``tests/_mem.py``. Excluded from CI (A11).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._mem import fastforward_in_subprocess, plain_extract_in_subprocess

pytestmark = pytest.mark.performance

#: Criterion 4b's band, reused: four times the records may not add more than this.
ADDITIVE_LIMIT_MB = 8.0

#: Criterion 4a's ceiling, reused for the largest skip.
ABSOLUTE_LIMIT_MB = 32.0

#: The allocator's noise floor at this size, in MiB. Two measurements of the same
#: workload differ by around this much, so a strict ordering between two of them is
#: not a fact about the code. See the test that uses it for why the allowance is safe.
NOISE_MB = 4.0


def test_skipping_a_million_records_does_not_accumulate(s100_path: Path, s400_path: Path) -> None:
    small = fastforward_in_subprocess(s100_path, 291_200)
    large = fastforward_in_subprocess(s400_path, 1_164_800)
    print(
        f"[skip] s100 delta={small['delta_mb']} MiB  skipped={small['skipped']:,}  "
        f"{small['seconds']}s"
    )
    print(
        f"[skip] s400 delta={large['delta_mb']} MiB  skipped={large['skipped']:,}  "
        f"{large['seconds']}s"
    )

    delta_100 = float(small["delta_mb"])
    delta_400 = float(large["delta_mb"])
    difference = delta_400 - delta_100
    print(
        f"[skip] 4x the records cost {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB)  (limit +{ADDITIVE_LIMIT_MB} MiB)"
    )

    assert large["skipped"] == 4 * small["skipped"] == 1_164_800
    assert delta_400 <= ABSOLUTE_LIMIT_MB, (
        f"skipping {large['skipped']:,} records added {delta_400} MiB, over the "
        f"{ABSOLUTE_LIMIT_MB} MiB ceiling"
    )
    assert difference <= ADDITIVE_LIMIT_MB, (
        f"4x the skipped records added {difference:+.3f} MiB "
        f"({delta_100} MiB -> {delta_400} MiB); skipped records are being retained"
    )


def test_skipping_costs_no_more_memory_than_processing(s400_path: Path, tmp_path: Path) -> None:
    """The comparison Gate 2 asks for, with both sides using the same config.

    A wider config costs more per record, so measuring the fast-forward against a
    differently-configured extraction would flatter it. Both sides here extract one
    attribute.

    **On the tolerance.** At this size both measurements land on the allocator's noise
    floor -- around 2 MiB against a 28 MiB interpreter baseline -- so they are not
    reliably ordered, and asserting a strict ``<=`` would make this test pass or fail
    on scheduling. The failure this is guarding against is accumulation, and
    accumulating 1,164,800 records would cost hundreds of megabytes, not two. The
    allowance therefore costs nothing in detection power while removing the coin flip.
    The decisive evidence for "skipping does not accumulate" is the four-times test
    above, where the difference was +0.227 MiB.
    """
    skipped = fastforward_in_subprocess(s400_path, 1_164_800)
    processed = plain_extract_in_subprocess(s400_path, tmp_path / "plain")
    print(
        f"[skip] fast-forward   delta={skipped['delta_mb']} MiB  "
        f"{skipped['seconds']}s  ({skipped['input_mb']} MiB)"
    )
    print(
        f"[skip] full extraction delta={processed['delta_mb']} MiB  "
        f"{processed['seconds']}s  ({processed['input_mb']} MiB)"
    )

    delta_skip = float(skipped["delta_mb"])
    delta_process = float(processed["delta_mb"])
    print(f"[skip] difference {delta_skip - delta_process:+.3f} MiB  (allowance {NOISE_MB} MiB)")

    assert skipped["skipped"] == processed["records"] == 1_164_800
    assert delta_process <= ABSOLUTE_LIMIT_MB
    assert delta_skip <= delta_process + NOISE_MB, (
        f"skipping cost {delta_skip} MiB against {delta_process} MiB for processing the "
        f"same records; the fast-forward is accumulating something"
    )
    assert delta_skip <= ABSOLUTE_LIMIT_MB
