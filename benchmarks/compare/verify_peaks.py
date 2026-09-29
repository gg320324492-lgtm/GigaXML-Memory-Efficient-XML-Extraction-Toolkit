"""Guard: no implementation's peak RSS may be below an idle interpreter's.

**The counterexample this exists for is real.** The first version of the comparison
runner reported ``peak_rss_mb: 4.1`` for every implementation at every size -- below
the ~15 MB an idle ``python -m`` interpreter with ``psutil`` loaded actually occupies.
That number was physically impossible (a process running ``iterparse`` over 100 MB
cannot be smaller than an idle interpreter) and it stood in the README's public
comparison table until the audit caught it.

So this guard measures the floor instead of assuming it: it runs an **empty script
through the runner's own wrapper** -- the same ``WRAPPER`` source the runner launches,
imported from ``run_comparison`` rather than restated here, so the two cannot drift
apart -- takes the peak that wrapper reports as the interpreter's idle ceiling, and
then requires every ``peak_rss_mb`` in ``results.json`` to be at least that. A frozen
or mis-scoped reading (4.1) fails against the floor immediately, with the
implementation named.

**A second blind spot, which the floor test above cannot catch.** A reading that is
merely *at* the floor, rather than below it, passes while being just as meaningless:
if the harness sampled a process that never did the work -- a wrapper that shelled out
and left the extraction in a grandchild, say -- the reading would be the wrapper's own
overhead, and a value of floor+0.2 would sail through. Worse, such a reading looks
*ideal* in a report: flat across every input size, because it never saw the input. So
this guard also reports the **gap** between each reading and the floor, and warns when
that gap is inside the measurement's own noise. The gap is the only part of the number
that belongs to the implementation; the rest is the apparatus.

The floor and the noise band are **measured at guard time, never hardcoded** -- they
move with the Python version, psutil version and machine, and a constant would
silently stop matching any of them. The noise is the spread of repeated floor runs,
which is the same spread a real reading carries.

Usage::

    python benchmarks/compare/verify_peaks.py
    python benchmarks/compare/verify_peaks.py --results <path-to-results.json>
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_RESULTS = HERE / "results.json"

sys.path.insert(0, str(HERE))
from run_comparison import PEAK_MARKER, WRAPPER  # noqa: E402

#: How many times the empty script is run to establish the floor. Three is enough to
#: see whether repeated runs of the *same* thing agree; more would only make the guard
#: slow, and this runs in CI.
FLOOR_RUNS = 3

#: The smallest gap above the floor that still counts as "the implementation did
#: something", in MiB, when the observed spread is tighter than that. A real extraction
#: costs tens of megabytes over an idle interpreter; anything under this is a harness
#: artefact however quiet the machine is.
MIN_MEANINGFUL_GAP_MIB = 3.0


def measure_idle_peaks(runs: int = FLOOR_RUNS) -> list[float]:
    """Peak RSS of an empty script through the runner's own wrapper, in MiB.

    The wrapper imports psutil and executes an empty file with ``__name__ ==
    "__main__"`` -- exactly the machinery around every measured implementation, minus
    the work. Its peak is the floor no real implementation may fall below.

    Repeated, because a single run gives a number but not its error bar, and the
    near-floor test below needs to know how much of a difference is real.
    """
    empty = HERE / "_idle_floor_target.py"
    empty.write_text("", encoding="utf-8")
    peaks: list[float] = []
    try:
        for _ in range(runs):
            completed = subprocess.run(
                [sys.executable, "-c", WRAPPER, str(empty)],
                cwd=HERE,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            lines = [ln for ln in completed.stderr.splitlines() if ln.startswith(PEAK_MARKER)]
            assert lines, f"the idle-floor run produced no peak reading: {completed.stderr[-300:]}"
            peaks.append(int(lines[-1][len(PEAK_MARKER) :]) / (1024 * 1024))
    finally:
        empty.unlink(missing_ok=True)
    return peaks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=pathlib.Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()

    results = json.loads(args.results.read_text(encoding="utf-8"))
    peaks = measure_idle_peaks()
    floor = max(peaks)
    spread = floor - min(peaks)
    # A reading has to clear the floor by more than the noise to be worth anything.
    threshold = max(3 * spread, MIN_MEANINGFUL_GAP_MIB)
    print(
        f"idle interpreter floor (measured now over {len(peaks)} runs, "
        f"wrapper self-read): {floor:.1f} MiB "
        f"(readings {', '.join(f'{p:.1f}' for p in peaks)}; spread {spread:.1f} MiB)"
    )
    print(
        f"a reading must exceed the floor by more than {threshold:.1f} MiB to be "
        "credited with having done the work"
    )

    failures = 0
    suspicious = 0
    checked = 0
    for size, implementations in results["results"].items():
        for name, data in implementations.items():
            for run in data.get("runs", []):
                peak = run.get("peak_rss_mb")
                if peak is None:
                    continue
                checked += 1
                if peak < floor:
                    print(
                        f"BELOW FLOOR: {size} {name} (run {run.get('repeat')}): "
                        f"{peak} MiB < {floor:.1f} MiB idle floor -- this reading is "
                        "physically impossible for a running interpreter and must be "
                        "re-measured, not reported"
                    )
                    failures += 1
                elif peak - floor <= threshold:
                    print(
                        f"MAY NOT HAVE MEASURED THE WORK: {size} {name} "
                        f"(run {run.get('repeat')}): {peak} MiB is only "
                        f"{peak - floor:.1f} MiB above the {floor:.1f} MiB floor, "
                        "within the harness's own noise. A reading this close to an "
                        "idle interpreter usually means the sampled process did not do "
                        "the extraction -- check whether the implementation shells out "
                        "-- and a flat one across sizes is the tell, because a real "
                        "extraction's curve is not perfectly level."
                    )
                    suspicious += 1

    print(f"{checked} peak readings checked against the {floor:.1f} MiB floor")
    if failures:
        print(
            f"{failures} reading(s) below the floor -- the sampler that produced "
            "them is broken; fix the sampler, do not report the numbers"
        )
    if suspicious:
        print(
            f"{suspicious} reading(s) within the noise of the floor -- these may not "
            "have measured the work at all; confirm the extraction runs in the sampled "
            "process before reporting them"
        )
    if failures:
        return 1
    if suspicious:
        return 1
    print("every peak reading is at or above the idle floor, and clear of its noise")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
