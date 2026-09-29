"""A piped document must cost the same bounded memory as a file.

Standard input is the one source that cannot be seeked, re-read, or measured before the
run starts. The obvious way to add it -- read the stream into a buffer and hand the
buffer to the parser -- works, produces identical output, passes every functional test,
and is wrong: it makes peak memory a function of the document instead of a constant
overhead. Nothing about the output would show it. Only this measurement does.

So this compares the same reader on the same 403 MB document twice, once by path and
once through a handle, and requires three things of the piped run: the same record
count, the same peak within a small band, and a peak far below the size of what it read.
The last one is the load-bearing assertion -- it is what a buffered implementation
cannot satisfy, and it is why the ratio is stated against the input rather than against
a fixed number of megabytes.

The counters are the process's own ``PeakWorkingSetSize``, self-read inside the child.
A parent reading a live child on this machine gets a frozen or absent figure, which is
how an entire comparison sweep once reported 4.1 MB for every implementation at every
size; see ``tests/_mem.py``.

Excluded from CI with the rest of the performance suite: two 403 MB extractions, about
80 seconds. Run it directly to change the threshold.
"""

from __future__ import annotations

import pathlib

import pytest

from tests._mem import empty_baseline_mb, pipeline_via_cli

pytestmark = pytest.mark.performance

#: The 403 MB document. Its manifest records 1,164,800 records, which the assertions
#: below hold the run to -- a reader that found fewer would be a reader that stopped
#: early, and the memory of a run that stopped early means nothing.
DATASET = pathlib.Path(__file__).resolve().parents[2] / "data" / "s400.xml"

#: How far the two peaks may differ. Measured at 0.11 MiB, so this is ~40x the observed
#: gap: wide enough that a noisy machine does not turn the suite red, tight enough that
#: "the pipe costs extra memory" would have to mean something other than a megabyte.
PEAK_BAND_MIB = 8.0

#: The piped run's peak, as a fraction of the document it read. Measured at 0.079 --
#: about 8%, which is the interpreter plus the writers. A run that buffered the input
#: would sit at or above 1.0; the ceiling is 0.5, so the assertion fails by a factor of
#: six rather than by a rounding error.
PEAK_OVER_INPUT_RATIO = 0.5


@pytest.fixture(autouse=True)
def _require_dataset() -> None:
    if not DATASET.is_file():
        pytest.skip(f"{DATASET} not generated; run `gigaxml generate --size 400MB`")


def test_a_piped_document_is_streamed_not_buffered(tmp_path: pathlib.Path) -> None:
    """Same records, same peak band, and a peak far below the size of the input."""
    input_mb = DATASET.stat().st_size / (1024 * 1024)

    # Both sides go through the real CLI. That is the point: an earlier version of this
    # check handed sys.stdin.buffer straight to the harness's own pipeline function,
    # which exercised the reader and nothing above it. The CLI's "-" branch -- the line
    # that turns "-" into a stream -- was therefore never run, and replacing it with
    # io.BytesIO(sys.stdin.buffer.read()) left this test reporting a flat 8% of the
    # document while the tool read all 403 MB of it into memory. A guard has to guard
    # the code that ships, and the code that ships is the command line.
    from_file = pipeline_via_cli(DATASET, tmp_path / "file.csv", tmp_path)
    from_stdin = pipeline_via_cli(DATASET, tmp_path / "stdin.csv", tmp_path, from_stdin=True)
    empty = empty_baseline_mb()

    for label, profile in (("file", from_file), ("stdin", from_stdin)):
        print(
            f"[pipe] {label:<5} peak={profile['peak_mb']:8.3f} MiB  "
            f"over-empty={profile['peak_mb'] - empty:6.3f}  "
            f"records={profile['records']:,}  {profile['seconds']:6.1f}s"
        )
    print(
        f"[pipe] difference {abs(from_file['peak_mb'] - from_stdin['peak_mb']):.3f} MiB   "
        f"input {input_mb:.1f} MiB   stdin peak is "
        f"{from_stdin['peak_mb'] / input_mb:.1%} of the document"
    )

    # Both runs must actually have read the document, or the memory below says nothing.
    assert from_file["records"] == 1_164_800
    assert from_stdin["records"] == from_file["records"]
    assert from_stdin["output_mb"] > 0, "the piped run wrote nothing"

    # The peak must be a property of the machinery, not of how the bytes arrived.
    assert abs(from_stdin["peak_mb"] - from_file["peak_mb"]) <= PEAK_BAND_MIB, (
        f"the piped run peaked {from_stdin['peak_mb']:.3f} MiB against "
        f"{from_file['peak_mb']:.3f} MiB from a file, a gap of "
        f"{abs(from_stdin['peak_mb'] - from_file['peak_mb']):.3f} MiB over a "
        f"{PEAK_BAND_MIB} MiB band"
    )

    # And it must be far below what it read. This is the assertion a buffered
    # implementation cannot pass while still producing identical output.
    assert from_stdin["peak_mb"] < input_mb * PEAK_OVER_INPUT_RATIO, (
        f"the piped run peaked at {from_stdin['peak_mb']:.3f} MiB while reading "
        f"{input_mb:.1f} MiB; a source that cannot be seeked is being buffered rather "
        "than streamed, which is invisible in the output and fatal in the memory"
    )

    # Over the empty interpreter, both leave a small, flat cost -- the same shape the
    # file-based performance tests assert.
    for label, profile in (("file", from_file), ("stdin", from_stdin)):
        increment = float(profile["peak_mb"]) - empty
        assert increment > 0, f"the {label} peak is below an empty interpreter"
