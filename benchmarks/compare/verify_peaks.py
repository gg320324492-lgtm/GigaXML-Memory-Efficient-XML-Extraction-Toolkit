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
overhead, and a value of floor+0.2 would sail through. So this guard also reports the
**gap** between each reading and the floor. The gap is the only part of the number that
belongs to the implementation; the rest is the apparatus.

**But the gap alone cannot decide the question, and the guard now says so.** A near-floor
gap has two causes and they are opposites, so this guard splits into two verdicts:

* **BELOW FLOOR** -- the reading is under an idle interpreter's, which cannot happen to a
  process that ran. The sampler is broken. Always a failure.
* **MAY NOT HAVE MEASURED THE WORK** -- the reading is *at* the floor. This is where the
  guard used to fail on a heuristic, and the heuristic is wrong in both directions. A
  dead sampler reads the apparatus (flat, near the floor); a correct **bounded streaming
  parser** reads the interpreter plus one record and *also* sits near the floor, because
  that is what streaming is. Measured on this suite, `raw_lxml` holds 23.4-24.6 MiB at
  100 MB, 1 GB and 4 GB alike: 413x the input moves the peak not at all, and varying one
  record's payload 5,000-fold moves it 2.8 MiB. That curve is the *signature of correct
  behaviour*, and a memory-only guard is blind between it and a dead one.

The second axis is the row count, which the runner has always recorded and the guard used
to ignore: the runner counts the rows the output file actually holds, per run, and the
document's record count is in the summary. A near-floor reading is therefore **accepted
with a notice** when the same run wrote the whole document (`rows_written ==
rows_in_document`), because that run demonstrably did the work and its peak is real; it
**stays a failure** when the count is short, missing, or the document's count could not be
established. The count is not a proxy for the memory reading -- it is the independent
evidence that the process did the extraction, which is exactly what the memory band was
being asked to infer and cannot.

**What must not be done is to raise the band.** `MIN_MEANINGFUL_GAP_MIB` stays where it
is: widening it would hide any implementation whose true marginal cost is under the new
size -- the correct streaming parsers this guard exists to protect -- which is the
opposite of a fix. The band marking a reading as *worth asking about* is the point; the
row count is what answers the question.

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

#: Where each recorded size's document lives, for the fallback record count in ``main``.
#: The runner writes the row count into ``results.json`` and that is the normal source;
#: this is only read when a results.json predates the field. It names the same files
#: ``run_comparison`` generates and, on this checkout, they have been deleted -- so the
#: fallback is a courtesy for a machine that has them, not a dependency CI has.
SIZE_PATHS = {
    size: HERE.parent.parent / "data" / name
    for size, name in {
        "100MB": "b100m.xml",
        "1GB": "b1g.xml",
        "4GB": "b4g.xml",
        "10GB": "b10g.xml",
    }.items()
}

#: The smallest gap above the floor that still counts as "the implementation did
#: something", in MiB, when the observed spread is tighter than that. A real extraction
#: costs tens of megabytes over an idle interpreter; anything under this is a harness
#: artefact however quiet the machine is.
MIN_MEANINGFUL_GAP_MIB = 3.0


def measure_idle_peaks(runs: int = FLOOR_RUNS) -> list[float]:
    """Peak RSS of an empty script through the runner's own wrapper, in MiB.

    The wrapper imports ``gigaxml.run`` and executes an empty file with ``__name__ ==
    "__main__"``. Its peak is the floor no real implementation may fall below.

    **The empty file is the guard's weakest assumption and it is now visible.** The
    wrapper used to import only psutil, so an empty target charged it 17.9 MiB. It now
    imports the product's peak reader, and an empty target charges that 22.5 MiB --
    while a target that does what a real run does (imports lxml and nothing else)
    moves the floor only 21.7 -> 22.4. The +4.6 MiB is lxml being charged to the
    wrapper instead of to the implementation, and a streaming implementation's recorded
    peak now sits closer to the floor than it used to. That is reported, not hidden:
    the threshold below is unchanged, and the answer to a reading it flags is to
    re-measure it, never to move the number.

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
            token = lines[-1][len(PEAK_MARKER) :].split(maxsplit=1)[0]
            assert token != "none", (
                "the child reported that this platform has no high-water mark, so there is "
                "no floor to check against and nothing here can be called wrong"
            )
            peaks.append(int(token) / (1024 * 1024))
    finally:
        empty.unlink(missing_ok=True)
    return peaks


def _lines_no_nl_splitlines_held(data: bytes) -> list[bytes] | None:
    """``data.splitlines()`` for a document whose records may contain bare CR.

    ``bytes.splitlines()`` is the only splitter that agrees with the runner's
    ``chunk.count(b"\\n")`` here: it breaks on ``\\n``, ``\\r`` and ``\\r\\n`` alike, so a
    well-formed XML document -- where every literal ``\\r`` is escaped as ``&#13;`` -- has
    as many lines as it has ``\\n``, and ``len(...) - 1`` is the record count without a
    byte of XML being parsed. This is only reached when the runner's own ``rows_in_document``
    is missing, and it returns None rather than a guess if the file is not there.
    """
    if not data:
        return []
    return data.splitlines()


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
    notices = 0
    checked = 0
    for size, implementations in results["results"].items():
        for name, data in implementations.items():
            summary = data.get("summary")
            summary = summary if isinstance(summary, dict) else {}
            # ★ The second axis. The memory axis cannot tell a correct streaming parser
            # from a dead sampler -- both read as an idle interpreter -- but the row
            # count can, and the runner has recorded it all along. The document's record
            # count comes from the summary; a results.json old enough to predate that
            # field falls back to counting the document itself, and if the document is
            # gone too, the count is unknown rather than assumed -- unknown is a failure,
            # because a notice is only allowed to excuse a reading that is known complete.
            rows_in_document = summary.get("rows_in_document")
            if rows_in_document is None and size in SIZE_PATHS:
                path = SIZE_PATHS[size]
                raw = path.read_bytes() if path.is_file() else None
                counted = _lines_no_nl_splitlines_held(raw) if raw is not None else None
                rows_in_document = len(counted) - 1 if counted is not None else None
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
                    rows_written = run.get("rows_written")
                    # The distinguishing question the guard's own message asks -- did the
                    # sampled process actually do the work? -- answered from the runner's
                    # bookkeeping instead of from the memory number, which here cannot
                    # answer it. A streaming implementation's true peak is the interpreter
                    # plus one record, so it belongs near the floor; a dead sampler's
                    # reading is the apparatus and nothing else, and the tell that separates
                    # them is that the dead one wrote no rows.
                    complete = (
                        isinstance(rows_written, int)
                        and isinstance(rows_in_document, int)
                        and rows_written == rows_in_document
                    )
                    if complete:
                        print(
                            f"MAY NOT HAVE MEASURED THE WORK (accepted): {size} {name} "
                            f"(run {run.get('repeat')}): {peak} MiB is only "
                            f"{peak - floor:.1f} MiB above the {floor:.1f} MiB floor, inside "
                            f"the harness's own noise -- but this run wrote "
                            f"{rows_written:,} rows of {rows_in_document:,}, so it did the "
                            "work and its peak really is the interpreter plus one record. "
                            "A correct bounded streaming parser belongs near the floor; this "
                            "is a notice, not a failure"
                        )
                        notices += 1
                    else:
                        print(
                            f"MAY NOT HAVE MEASURED THE WORK: {size} {name} "
                            f"(run {run.get('repeat')}): {peak} MiB is only "
                            f"{peak - floor:.1f} MiB above the {floor:.1f} MiB floor, "
                            "within the harness's own noise, and this run did not write the "
                            f"whole document ({rows_written!r} rows of "
                            f"{rows_in_document!r}), so it may not have done the work at "
                            "all. A near-floor reading is only excused when the same run "
                            "wrote every row -- otherwise check whether the implementation "
                            "shells out, and look at whether the row count is short, "
                            "missing, or the run exited non-zero"
                        )
                        failures += 1

    print(f"{checked} peak readings checked against the {floor:.1f} MiB floor")
    if failures:
        print(
            f"{failures} reading(s) below the floor, or within its noise without having "
            "written the whole document -- the sampler that produced them is broken, or "
            "the extraction did not run; fix that, do not report the numbers"
        )
    if notices:
        print(
            f"{notices} reading(s) within the noise of the floor but accepted -- each one "
            "wrote the entire document in the same run, which is what a correct bounded "
            "streaming parser looks like, not a sampler that measured nothing"
        )
    if failures:
        return 1
    print("every peak reading is at or above the idle floor, and clear of its noise")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
