"""Guard: no implementation's peak RSS may be below an idle interpreter's.

**The counterexample this exists for is real.** The first version of the comparison
runner reported ``peak_rss_mb: 4.1`` for every implementation at every size -- below
the ~15 MB an idle ``python -m`` interpreter with ``psutil`` loaded actually occupies.
That number was physically impossible (a process running ``iterparse`` over 100 MB
cannot be smaller than an idle interpreter) and it stood in the README's public
comparison table until the audit caught it.

So this guard measures the floor instead of assuming it: it runs an **empty script
through the same self-read wrapper** the runner uses, takes the peak that wrapper
reports as the interpreter's idle ceiling, and then requires every ``peak_rss_mb`` in
``results.json`` to be at least that. A frozen or mis-scoped reading (4.1) fails
against the floor immediately, with the implementation named.

The floor is **measured at guard time, never hardcoded** -- it moves with the Python
version, psutil version and machine, and a constant would silently stop matching any
of them.

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


def measure_idle_floor() -> float:
    """Peak RSS of an empty script through the runner's own wrapper, in MiB.

    The wrapper imports psutil and executes an empty file with ``run_name="__main__"``
    -- exactly the machinery around every measured implementation, minus the work. Its
    peak is the floor no real implementation may fall below.
    """
    empty = HERE / "_idle_floor_target.py"
    empty.write_text("", encoding="utf-8")
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import psutil, runpy, sys\n"
                    "process = psutil.Process()\n"
                    "sys.argv = [sys.argv[0]]\n"
                    "try:\n"
                    "    runpy.run_path(sys.argv[0], run_name='__main__')\n"
                    "finally:\n"
                    "    info = process.memory_info()\n"
                    "    peak = getattr(info, 'peak_wset', None) or info.rss\n"
                    "    print(f'__GIGAXML_PEAK__{peak}', file=sys.stderr)\n"
                ),
                str(empty),
            ],
            cwd=HERE,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    finally:
        empty.unlink(missing_ok=True)
    marker = "__GIGAXML_PEAK__"
    lines = [ln for ln in completed.stderr.splitlines() if ln.startswith(marker)]
    assert lines, f"the idle-floor run produced no peak reading: {completed.stderr[-300:]}"
    return int(lines[-1][len(marker) :]) / (1024 * 1024)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=pathlib.Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()

    results = json.loads(args.results.read_text(encoding="utf-8"))
    floor = measure_idle_floor()
    print(f"idle interpreter floor (measured now, wrapper self-read): {floor:.1f} MiB")

    failures = 0
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

    print(f"{checked} peak readings checked against the {floor:.1f} MiB floor")
    if failures:
        print(f"{failures} reading(s) below the floor -- the sampler that produced "
              "them is broken; fix the sampler, do not report the numbers")
        return 1
    print("every peak reading is at or above the idle floor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
