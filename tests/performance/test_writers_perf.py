"""Output-side memory bound: the whole pipeline must stay flat.

Phase 1 proved the reader keeps memory flat on the way in. This proves the writer
keeps it flat on the way out -- which is not automatic, because the obvious way to
write Parquet is to collect a table and write it once at the end, and that puts
the entire output in memory.

Measured in a subprocess through ``tests/_mem.py --pipeline``: the numbers taken
inside pytest would include pytest's own heap and the garbage left by earlier
tests, which is more than enough noise to make a few-MiB bound meaningless.

Excluded from CI (roadmap A11); the 400MB scan takes about 40 seconds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests._mem import empty_baseline_mb, pipeline_in_subprocess

pytestmark = pytest.mark.performance

#: Gate 4a: the 400MB pipeline may not add more than this over an empty process.
ABSOLUTE_LIMIT_MB = 32.0

#: Gate 4b, reused here: 4x the input may not add more than this much marginal
#: memory. A writer that accumulated rows would fail this by a factor of ~300.
PLATEAU_ADDITIVE_LIMIT_MB = 8.0


def _report(label: str, profile: dict[str, float | int]) -> None:
    print(
        f"[write] {label:<14} baseline={profile['baseline_mb']:>8} MiB  "
        f"peak={profile['peak_mb']:>8} MiB  delta={profile['delta_mb']:>8} MiB  "
        f"records={profile['records']:>9}  {profile['seconds']:>8}s  "
        f"in={profile['input_mb']:>8} MiB  out={profile['output_mb']:>8} MiB"
    )


def test_the_whole_pipeline_stays_bounded_from_100mb_to_400mb(
    s100_path: Path, s400_path: Path, tmp_path: Path
) -> None:
    """XML -> Parquet end to end: read, extract, batch-write."""
    small = pipeline_in_subprocess(s100_path, tmp_path / "s100.parquet")
    large = pipeline_in_subprocess(s400_path, tmp_path / "s400.parquet")
    _report("s100 -> parquet", small)
    _report("s400 -> parquet", large)

    delta_100 = float(small["delta_mb"])
    delta_400 = float(large["delta_mb"])
    difference = delta_400 - delta_100
    empty = empty_baseline_mb()
    increment = float(large["peak_mb"]) - empty

    print(f"[write] 100MB increment over empty = {float(small['peak_mb']) - empty:.3f} MiB")
    print(f"[write] 400MB increment over empty = {increment:.3f} MiB (limit {ABSOLUTE_LIMIT_MB})")
    print(
        f"[write] 4.01x the input cost {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB)  (limit +{PLATEAU_ADDITIVE_LIMIT_MB} MiB)"
    )

    assert large["records"] == 4 * small["records"]
    assert increment <= ABSOLUTE_LIMIT_MB, (
        f"the 400MB pipeline added {increment:.3f} MiB over the empty-process baseline, "
        f"over the {ABSOLUTE_LIMIT_MB} MiB ceiling"
    )
    assert difference <= PLATEAU_ADDITIVE_LIMIT_MB, (
        f"4x the input added {difference:+.3f} MiB of marginal memory "
        f"({delta_100} MiB -> {delta_400} MiB), over the +{PLATEAU_ADDITIVE_LIMIT_MB} MiB "
        f"band; the writer is accumulating instead of flushing"
    )


def test_the_written_file_holds_every_record(s100_path: Path, tmp_path: Path) -> None:
    """The memory bound is only interesting if the output is actually complete."""
    import pyarrow.parquet as parquet

    output = tmp_path / "s100.parquet"
    profile = pipeline_in_subprocess(s100_path, output)
    _report("s100 -> parquet", profile)

    table = parquet.read_table(output)
    assert table.num_rows == profile["records"]
    assert table.num_rows == 291_200
    assert table.column_names[0] == "product_id"
    assert table.to_pydict()["product_id"][0] == "1"
