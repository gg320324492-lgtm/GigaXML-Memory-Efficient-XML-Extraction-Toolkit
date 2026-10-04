"""Every benchmark number can be traced to the run that produced it -- and the checks
that say so are themselves checked.

M17's invariant: *a benchmark number must be traceable to the code, the data and the run
that produced it.* Three months from now somebody asks where 43,385 rec/s came from, and
the answer has to be findable.

**The mutation is inside each test, not beside it.** A guard that has never been seen to
go red is a guard of unknown strength, and M15's lesson -- two of the three guards written
for M16's fetch script turned out to be hollow -- is that they are hollow until something
tries to break them. So each test here runs its own check against a deliberately doctored
copy of the data and asserts the check notices. If a check is weakened to the point where
it stops catching its mutation, the test that proves it has teeth goes red instead of
quietly agreeing.

**What is asserted here and what is deliberately not.** No test in this file asserts a
throughput or a memory *value*. The project's A11 decision is that performance numbers are
recorded, never asserted, because a suite that fails when a machine is busy is worse than
no suite. What is asserted is provenance: that the numbers can be recomputed, that the
identity is complete, that the history does not contain invented figures, and that the
memory measurement was taken by the process that did the work.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
from types import ModuleType

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent
BENCH = REPO / "benchmarks"
RESULTS = BENCH / "compare" / "results.json"
BASELINE = BENCH / "perf-baseline.json"
HISTORY = BENCH / "history"
WORKFLOWS = REPO / ".github" / "workflows"


def load_module(name: str, path: pathlib.Path) -> ModuleType:
    """Import a script by path -- benchmarks are scripts, not a package."""
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - a missing file is a bug
        raise AssertionError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def provenance() -> ModuleType:
    return load_module("bench_provenance", BENCH / "provenance.py")


@pytest.fixture(scope="module")
def results() -> dict:
    return json.loads(RESULTS.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def baseline() -> dict:
    return json.loads(BASELINE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# Criterion A -- the identity block
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", [RESULTS, BASELINE], ids=["results", "perf-baseline"])
def test_every_result_file_carries_a_complete_identity_block(
    provenance: ModuleType, path: pathlib.Path
) -> None:
    """Criterion A: the fields are present. A field nobody checks is a field nobody keeps."""
    record = json.loads(path.read_text(encoding="utf-8"))
    missing = provenance.missing_identity_fields(record)
    assert missing == [], f"{path.name} is missing identity fields: {missing}"
    assert record["schema_version"] == provenance.SCHEMA_VERSION


def test_the_commit_is_a_sha_and_not_a_branch_name(results: dict) -> None:
    """A branch name moves. The answer to "which code" has to be 40 characters that do not.

    The recovered value carries a sentence explaining how it was recovered, so the check
    is on the leading SHA rather than on the whole string.
    """
    commit = results["identity"]["git_commit"]
    sha = commit.split()[0]
    assert len(sha) == 40, f"not a 40-character SHA: {commit!r}"
    assert all(character in "0123456789abcdef" for character in sha)
    assert "main" not in commit.lower(), "a branch name is not an identity"
    # And the SHA is real: the commit it names is in this repository.
    completed = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
        cwd=str(REPO),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, f"{sha} is not a commit in this repository"


def test_a_missing_identity_field_is_reported_rather_than_defaulted(provenance: ModuleType) -> None:
    """The mutation: drop one field and the check must notice.

    `missing_identity_fields` checks presence and not truthiness, so a field holding an
    honest "not recorded" string passes while a field holding ``None`` does not. Both
    halves are asserted, because a check that only tested truthiness would refuse the
    honest string and a check that only tested presence would accept the hole.
    """
    honest = dict.fromkeys(provenance.REQUIRED_IDENTITY_FIELDS, "not recorded: deleted")
    assert provenance.missing_identity_fields({"identity": honest}) == []

    holed = dict(honest)
    del holed["dataset_sha256"]
    assert "dataset_sha256" in provenance.missing_identity_fields({"identity": holed})

    nulled = dict(honest)
    nulled["git_commit"] = None
    assert "git_commit" in provenance.missing_identity_fields({"identity": nulled})


def test_the_config_hash_is_taken_over_the_git_blob_not_the_working_tree(
    provenance: ModuleType,
) -> None:
    """The CRLF trap, made permanent.

    With ``core.autocrlf=true`` -- which this repository has -- a fresh clone of
    ``benchmarks/compare/gigaxml-config.yaml`` is 524 bytes of CRLF where the blob is 512
    bytes of LF, and their sha256 values differ. A ``config_sha256`` taken from the working
    tree is therefore true on the machine that recorded it and false on every other one.

    Measured during M17: blob ``b7b51aa9...`` (512 B), fresh clone ``ee8035a8...`` (524 B).
    The two assertions below are the trap stated as a test: the function returns the blob's
    digest, and mangling those same bytes' line endings changes the digest, so the choice
    is not cosmetic.
    """
    relative = "benchmarks/compare/gigaxml-config.yaml"
    recorded = provenance.blob_sha256(REPO, relative)

    blob = subprocess.run(
        ["git", "cat-file", "blob", f"HEAD:{relative}"],
        cwd=str(REPO),
        capture_output=True,
        check=False,
    )
    assert blob.returncode == 0
    assert recorded == provenance.sha256_bytes(blob.stdout), (
        "blob_sha256 did not return the committed bytes' digest"
    )

    crlf = blob.stdout.replace(b"\n", b"\r\n")
    assert len(crlf) > len(blob.stdout), "the fixture has no line endings to convert"
    assert provenance.sha256_bytes(crlf) != recorded, (
        "converting the line endings did not change the digest, so this test would pass "
        "for a reason that has nothing to do with the trap it exists to catch"
    )


def test_the_recorded_config_hash_is_the_blob_digest(provenance: ModuleType, results: dict) -> None:
    """The identity block's own claim, verified rather than trusted."""
    recorded = str(results["identity"]["config_sha256"]).split()[0]
    assert recorded == provenance.blob_sha256(REPO, "benchmarks/compare/gigaxml-config.yaml")


# --------------------------------------------------------------------------
# Criterion B -- summaries recompute from their raw values
# --------------------------------------------------------------------------


def data_points(record: dict) -> list[tuple[str, str, dict, dict]]:
    points = []
    for size, implementations in record["results"].items():
        for name, point in implementations.items():
            points.append((size, name, point["summary"], point["runs"]))
    return points


def disagreeing_fields(summary: dict, runs: list[dict], rows_in_document: int) -> list[str]:
    """Fields where the recorded summary and a recomputation differ. Empty means it agrees."""
    provenance = load_module("bench_provenance_b", BENCH / "provenance.py")
    recomputed = provenance.recompute_summary(runs, rows_in_document=rows_in_document)
    return [key for key, value in recomputed.items() if key in summary and summary[key] != value]


def test_every_summary_recomputes_from_its_own_raw_runs(results: dict) -> None:
    """Criterion B, the load-bearing half: median/min/max/stdev/p95 and the derived
    figures are all rebuildable from the per-run values stored beside them.

    All twelve data points agree, including the two awkward ones: the 1 GB pandas point
    where every repeat failed and the summary is a four-key failure shape rather than
    twelve statistics, and the 4 GB pandas point whose ``complete_output`` is False.
    """
    points = data_points(results)
    assert len(points) == 12, f"expected the recorded 12 data points, found {len(points)}"

    for size, name, summary, runs in points:
        differing = disagreeing_fields(
            summary, runs, rows_in_document=summary.get("rows_in_document", 0)
        )
        assert differing == [], f"{size}/{name}: summary disagrees with its own runs on {differing}"


def test_the_recompute_check_catches_a_edited_raw_value(results: dict) -> None:
    """The mutation, run every time: move one raw wall time and leave the summary alone.

    This is the assertion that gives the recompute check its strength. A hand-edited
    number, a summary written by an older version of the script with a different
    statistic, or a cherry-picked run all produce the same thing -- a summary that no
    longer follows from its own evidence -- and all of them are caught here rather than
    discovered by whoever quotes the figure next.
    """
    point = copy.deepcopy(results["results"]["100MB"]["gigaxml"])
    summary, runs = point["summary"], point["runs"]
    assert disagreeing_fields(summary, runs, summary["rows_in_document"]) == []

    runs[0]["wall_s"] = round(float(runs[0]["wall_s"]) - 0.5, 3)
    assert disagreeing_fields(summary, runs, summary["rows_in_document"]) != [], (
        "editing a raw wall time did not change the recomputation, so the recompute check "
        "has no teeth and this test would be asserting nothing"
    )


def test_the_recompute_check_catches_a_deleted_raw_run(results: dict) -> None:
    """The second mutation: drop a run. The median and the stdev both move.

    Worth its own case because dropping the slowest run is the easiest way to make a
    number look better, and because ``stdev_s`` is the field that notices -- a summary
    carrying a spread that no longer matches its sample count is not a rounding
    difference.
    """
    point = copy.deepcopy(results["results"]["4GB"]["gigaxml"])
    summary, runs = point["summary"], point["runs"]
    assert disagreeing_fields(summary, runs, summary["rows_in_document"]) == []

    del runs[0]
    differing = disagreeing_fields(summary, runs, summary["rows_in_document"])
    assert "median_s" in differing or "stdev_s" in differing


def test_p95_equals_max_at_five_repeats_and_the_code_says_why(results: dict) -> None:
    """A reader who assumes p95 interpolates will "find" a bug that is the definition.

    ``p95_index = round(0.95 * (n - 1))`` is ``n - 1`` for the project's five repeats, so
    p95 is the maximum. Asserted so that the next person to read ``p95_s == max_s`` in
    results.json learns it from a test instead of from a bug report.
    """
    for size, name, summary, _runs in data_points(results):
        if summary.get("ok_runs", 0) > 1:
            assert summary["p95_s"] == summary["max_s"], f"{size}/{name}"
            assert summary["ok_runs"] == 5, f"{size}/{name}: the sample size changed"


def test_complete_output_is_not_recomputable_without_the_expected_count(results: dict) -> None:
    """The one summary field that needs an input the runs do not contain.

    ``complete_output`` compares ``rows_written`` against ``rows_in_document``, and the
    second is the generator's expected count -- an input to the check, not a measurement.
    It is load-bearing rather than decorative: it is what caught the old 4 GB pandas point
    writing 10,485,760 of 11,915,264 rows and exiting 0, which a wall-clock check would
    have scored as a slow success.

    **That particular shape is no longer in results.json, and the test changed with the
    data rather than going green on nothing.** After the 2026-10-04 re-recording, pandas
    at 4 GB is killed by a memory-safety watchdog before it writes a row: every repeat has
    ``exit_code: 1``, ``rows_written: 0`` and ``peak_rss_mb: null``, so the summary is the
    four-key failure shape (``ok_runs: 0``) and carries no ``complete_output`` at all --
    "no successful run" and "an incomplete run" are different claims and the summary says
    the first. The property under test is unchanged, so it is demonstrated on a synthetic
    run list with the exit-0 partial write asserted as a *run*, plus the real check that
    the field still flips with its input on the data the sweep actually produced.
    """
    provenance = load_module("bench_provenance_c", BENCH / "provenance.py")

    # The historical case as raw runs: five repeats, each exiting 0, each writing
    # 10,485,760 of the document's 11,915,264 rows. Nothing but the expected count can
    # tell this apart from a complete extraction, which is the whole point of the field.
    partial = [
        {
            "repeat": repeat,
            "exit_code": 0,
            "peak_rss_mb": 26004.8,
            "rows_written": 10_485_760,
            "wall_s": 935.777,
        }
        for repeat in range(1, 6)
    ]
    assert all(run["exit_code"] == 0 for run in partial), "it exited 0 every time"
    assert (
        provenance.recompute_summary(partial, rows_in_document=11_915_264)["complete_output"]
        is False
    ), "a short write with exit 0 must not read as complete"
    # The wrong expected count -- the number actually written -- would give a clean pass,
    # which is why the count is an input the runs cannot supply.
    assert (
        provenance.recompute_summary(partial, rows_in_document=10_485_760)["complete_output"]
        is True
    )

    # And on the real sweep: the field still follows its input, on a point that is
    # complete. 2 GB of the condition is the generator's count; change it and the same
    # runs read as short.
    point = results["results"]["4GB"]["gigaxml"]
    summary, runs = point["summary"], point["runs"]
    assert summary["complete_output"] is True
    assert summary["rows_in_document"] == 11_915_264
    assert summary["rows_written_median"] == 11_915_264
    assert (
        provenance.recompute_summary(runs, rows_in_document=11_915_264)["complete_output"] is True
    )
    assert (
        provenance.recompute_summary(runs, rows_in_document=11_915_263)["complete_output"] is False
    )

    # The pandas point is the other failure shape, stated so the difference from the one
    # above is on the record: not "incomplete", but "no run succeeded".
    pandas_4gb = results["results"]["4GB"]["pandas"]["summary"]
    assert pandas_4gb["ok_runs"] == 0 and pandas_4gb["failed_runs"] == 5
    assert "complete_output" not in pandas_4gb


def test_the_perf_baseline_records_that_its_numbers_cannot_be_recomputed(
    provenance: ModuleType, baseline: dict
) -> None:
    """Criterion B on the file CI actually gates: it fails, and it says so.

    ``perf-baseline.json`` has a median and a repeat count and no per-run values, so its
    headline number cannot be recomputed from anything. Asserting the *absence* with its
    reason is the honest form: a test that quietly skipped the file would leave the one
    number this project asserts in CI as the one number nothing checks.
    """
    assert "rates" not in baseline and "peaks" not in baseline
    with pytest.raises(ValueError, match="cannot be recomputed"):
        provenance.recompute_perf_baseline(baseline)
    assert "cannot be recomputed" in baseline["identity"]["summary_recomputable"]


def test_the_perf_baseline_says_out_loud_that_it_was_not_written_by_its_own_script(
    baseline: dict,
) -> None:
    """The finding, pinned so it cannot be quietly dropped from the file.

    The committed baseline has ``measured_on.runner`` and ``measured_on.note``, which
    ``perf_baseline.py`` has never written at any commit, and lacks ``rates``, ``peaks``
    and ``machine``, which it has always written. So it was not produced by ``--update``.
    The recorded numbers may well come from a real run whose output someone typed up --
    what is established is the narrower claim, and it is the one that matters: no run in
    this repository produced this file.
    """
    recorded = baseline["identity"]["provenance_of_these_numbers"]
    assert "HAND-AUTHORED" in recorded
    assert "runner" in recorded and "rates" in recorded
    assert baseline["measured_on"]["runner"].startswith("GitHub Actions")


# --------------------------------------------------------------------------
# Criterion F -- the memory method is a constraint, not a label
# --------------------------------------------------------------------------


def peak_looks_self_read(peak_mb: float) -> bool:
    """Is this reading large enough to be the work, rather than the apparatus around it?

    The threshold is not a guess, and it is not "the point below which a parent sits"
    either -- that was the belief this helper used to encode, and CI disproved it. A
    parent watching a live child read **4.05 MiB on this machine** and **273.117 MiB on
    the windows-latest runner**, both frozen, for a child that had allocated 256 MiB.
    The level is a property of the runner (how big its interpreter is), not of the
    method, so no single cutoff can separate them and none is claimed to.

    What 200 MiB actually separates is **the work from the apparatus**, using the probe
    below: a child that allocates 384 MiB and self-reads is above it; a process that
    has finished and released everything is far below it. That is the question the
    harness's numbers are actually at risk of failing, it is the same question on all
    three platforms, and it is the one
    :func:`test_the_comparison_harness_records_the_peak_the_worker_read` asks by
    running the real harness against a child with a known allocation.
    """
    return peak_mb > 200.0


#: The child of :func:`run_probe`. Two lines of protocol around one measurement: it says
#: when the ballast is provably held, and it does not let go until told to, so the parent
#: samples a child at a **known** point in its life rather than 0.15 s into a startup
#: whose duration is a property of the runner.
PROBE_SOURCE = """import json
import sys

import psutil

from gigaxml.run import peak_rss_mb, peak_rss_source

me = psutil.Process()
ballast = [bytearray(8 * 1024 * 1024) for _ in range(32)]
for block in ballast:
    block[::4096] = b"\x01" * len(block[::4096])
print("HELD", flush=True)
sys.stdin.read(1)
ballast.clear()
print(json.dumps({"child_peak": peak_rss_mb(), "child_source": peak_rss_source()}), flush=True)
"""


def run_probe() -> dict:
    """Run :data:`PROBE_SOURCE`; return what the child read and what the parent saw.

    The parent's samples are taken between the child's ``HELD`` line and its release, so
    the child is certainly holding 256 MiB while they are taken -- which is the whole
    reason the version of this probe that preceded it was unreliable. It sampled at a
    fixed 0.15 s and called the answer "frozen"; the figure it got was however far the
    child had got by then, and on a slow enough runner that is most of the allocation.
    """
    import psutil

    compile(PROBE_SOURCE, "<memory-probe>", "exec")
    process = subprocess.Popen(
        [sys.executable, "-c", PROBE_SOURCE],
        stdout=subprocess.PIPE,
        stdin=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None and process.stdin is not None
    assert process.stdout.readline().strip() == "HELD", "the probe never reached its held state"
    watcher = psutil.Process(process.pid)
    seen = [watcher.memory_info().rss / 1048576 for _ in range(3)]
    process.stdin.write("x")
    process.stdin.flush()
    out, _ = process.communicate(timeout=120)
    measured = json.loads(out.strip())
    measured["parent_saw"] = seen
    return measured


def test_a_current_rss_reading_is_not_the_peak_the_harness_needs() -> None:
    """The measurement the threshold above rests on -- as a relation, not a constant.

    One process, one moment, two counters. Measured here 2026-10-03, on a call that
    allocated 256 MiB and returned, so the allocator had already been given it back by
    the time the second number was taken::

        peak_rss_mb()      281.2 MiB
        memory_info().rss   25.8 MiB

    **The 255.4 MiB between them is the size of the mistake this repository made once.**
    The harness used to read ``info.rss`` in a ``finally`` block -- exactly the moment
    the ballast is gone -- and file the result as a peak. On a Linux runner that produced
    14.4 MiB for a child that had allocated 384, a number no interpreter could have
    produced, which is the only reason anything noticed.

    Asserted as a **relationship** rather than as a frozen figure, and that is the whole
    difference from the guard it replaces. An absolute number here goes stale on every
    new runner, and this one already had: a 0.15 s sampling race read 4.05 MiB on this
    machine and 273.117 MiB on windows-latest, from identical code. The relationship
    holds wherever the OS keeps a high-water mark at all, which is the claim worth
    making -- and it is the one the harness actually depends on.
    """
    import psutil

    from gigaxml.run import peak_rss_mb

    me = psutil.Process()

    def hold() -> None:
        ballast = [bytearray(8 * 1024 * 1024) for _ in range(32)]
        for block in ballast:
            block[::4096] = b"\x01" * len(block[::4096])

    peak = None
    hold()
    peak = peak_rss_mb()
    hold()  # a second call, so the first one's blocks are unreferenced when this returns
    current = me.memory_info().rss / (1024 * 1024)
    assert peak is not None, "this platform has no high-water mark, so there is nothing to compare"
    assert peak > 190, f"the probe did not allocate what it allocates ({peak} MiB)"
    assert current < peak / 2, (
        f"current RSS read {current:.1f} MiB against a peak of {peak:.1f} MiB. These are "
        f"supposed to be different quantities: a reading taken after the work is finished "
        f"is not a peak, and a harness that reports one as the other is the failure this "
        f"repository has already shipped once."
    )


def test_a_parent_reads_the_childs_current_rss_and_the_harness_reads_its_peak() -> None:
    """Whose process read the counter: the other half of criterion F, measured.

    The child holds 256 MiB, says so, and waits to be released. The parent samples it
    three times in that window. **The parent's figure is recorded, not asserted**, and
    that is the point: it reads 4.05 MiB on this machine and 273.117 MiB on the
    windows-latest runner, frozen in both cases, for a child that had allocated 256 MiB.
    Which process the counter came from changes what the number *means*; how large the
    number is changes with the runner. Freezing the second is what made the version of
    this test that it replaces go red on every machine except the one that wrote it.

    What is asserted is the half that does not move: on this platform the child can read
    a real peak, and that peak reflects the allocation rather than the interpreter.
    """
    measured = run_probe()
    child = measured["child_peak"]
    assert child is not None, (
        f"this platform has no high-water mark ({measured['child_source']}); a platform "
        f"that cannot measure is not one whose measurement can be called wrong"
    )
    assert child > 190, f"the child did not allocate what the probe allocated ({child} MiB)"
    assert measured["child_source"], "the child did not say which counter it read"
    assert peak_looks_self_read(child), (
        f"the child's own peak of {child} MiB failed the check that every harness reading "
        f"has to pass, so the check is measuring the wrong thing"
    )


def test_the_comparison_harness_records_the_peak_the_worker_read(provenance: ModuleType) -> None:
    """Criterion F's executable check: the harness's own number comes from the child.

    ``run_comparison.py`` used to write ``peak_rss_method`` as a hardcoded string, so a
    harness rewritten to have the parent read the child would have kept claiming the
    self-read and nothing would have noticed. Now the child reports which counter
    answered, the recorder stores that, and this test holds the whole arrangement by
    running the real ``run_once`` against a child that allocates a known amount.

    Skipped where the platform cannot report a peak at all: a platform that cannot measure
    is not a platform whose measurement can be called wrong.
    """
    if provenance.MEMORY_METHOD_SELF_READ is None:  # pragma: no cover - defensive
        pytest.skip("no memory method recorded")

    from gigaxml.run import peak_rss_mb, peak_rss_source

    if peak_rss_mb() is None:  # pragma: no cover - platform dependent
        pytest.skip("this platform cannot report a peak working set")

    run_comparison = load_module("bench_run_comparison", BENCH / "compare" / "run_comparison.py")
    import tempfile

    with tempfile.TemporaryDirectory() as work:
        work_dir = pathlib.Path(work)
        script = work_dir / "ballast.py"
        # A child that allocates and exits cleanly, with an output file for the recorder
        # to size. The point is the peak it reports, not the work.
        ballast_source = (
            "import pathlib\n"
            "ballast = [bytearray(8 * 1024 * 1024) for _ in range(48)]\n"
            "for block in ballast:\n"
            "    block[::4096] = b'\\x01' * len(block[::4096])\n"
            f"pathlib.Path({str(work_dir / 'out.csv')!r}).write_text('a,b\\n1,2\\n')\n"
        )
        # Compiled here, before it is run. This test's whole claim is about a number the
        # child reports, and a child that dies of a SyntaxError reports a perfectly
        # plausible peak of "just the interpreter" -- so a quoting mistake in the string
        # above would turn the check into a pass that means nothing. Compiling it first
        # puts the failure at the line that caused it.
        compile(ballast_source, str(script), "exec")
        script.write_text(ballast_source, encoding="utf-8")
        source = work_dir / "in.xml"
        source.write_text("<catalog/>", encoding="utf-8")
        payload = run_comparison.run_once(script, source, work_dir / "out.csv")

    assert payload["exit_code"] == 0, (
        f"the ballast child failed: {payload['stderr_tail']!r}. A crashed child allocates "
        f"nothing, so its peak is meaningless and every assertion below would be "
        f"measuring the interpreter."
    )
    assert payload["peak_rss_mb"] > 200, (
        f"the harness reported {payload['peak_rss_mb']} MiB for a child that allocated "
        f"384 MiB. That is the shape of a parent reading a live child, not of the child "
        f"reading itself."
    )
    assert "self-read" in payload["peak_rss_method"], payload["peak_rss_method"]
    # **The counter is checked against the one this platform actually uses**, not
    # against a literal. The literal used to be "peak_wset", which is psutil's name for
    # a field that only exists on Windows -- so on Linux and macOS this assertion could
    # only ever have been satisfied by a string, and the number beside it was current
    # RSS. Comparing against ``peak_rss_source()`` ties the recorded method to the
    # function that produced it, and a harness that reverted to any fallback fails here
    # on every platform rather than passing on the one with the field.
    named = payload["peak_rss_method"]
    assert named.startswith(peak_rss_source() or "nothing"), (
        f"the recorded method names a counter this platform does not use: {named!r}"
    )
    # **Compared as a name, not as a substring.** An earlier version of this line was
    # ``"rss (" not in payload["peak_rss_method"]`` and went red on macOS, where the
    # counter is ``ru_maxrss (getrusage RUSAGE_SELF, bytes)`` and the substring sits
    # inside ``ru_maxrss``. A substring test for "did it fall back" cannot be written
    # without catching the real thing: every peak counter's name is a superset of a
    # current one. So the platform's own counter name is compared whole.
    counter = (peak_rss_source() or "").split(" (")[0]
    assert counter != "rss", (
        f"this platform's high-water mark is called {counter!r}, which is the name of a "
        f"current reading; a peak filed under it would be the fallback wearing a label"
    )


def test_the_harness_reads_the_childs_peak_and_supplies_no_number_of_its_own() -> None:
    """Criterion F structurally: the parent measures nothing, so it cannot report wrong.

    The runtime check above proves the number is right on the machine that ran it. This
    proves the arrangement that makes it right, and it holds on every platform and every
    machine, which is the property the old frozen-figure probe was reaching for and
    could not reach through a constant.

    Two halves, because either alone is a way to pass for the wrong reason:

    * ``run_once`` -- the parent -- references no memory API at all. It parses a number
      out of a line the child printed. A parent that measured the child would satisfy
      the runtime check on a machine where the reading happened to be large, which is
      exactly what windows-latest did.
    * the wrapper calls ``gigaxml.run.peak_rss_mb`` and contains no ``or info.rss``-shaped
      fallback. This is the line that produced 14.4 MiB for a 384 MiB child on a Linux
      runner, under a comment that said it was "a different quantity wearing the same
      label".
    """
    source = (BENCH / "compare" / "run_comparison.py").read_text(encoding="utf-8")
    wrapper = source[source.index("WRAPPER = ") : source.index("PEAK_MARKER = ")]
    # **The wrapper's comments are excluded, and that is the point of the filter.** The
    # comments are where the fallback is *described* -- by name, so the next reader
    # knows what it was -- and a check that grepped the whole string would fail on its
    # own documentation. What has to be absent is the fallback as *code*.
    code = "\n".join(line for line in wrapper.splitlines() if not line.strip().startswith("#"))

    assert "peak_rss_mb()" in code, (
        "the wrapper no longer reads the product's peak counter, so it has gone back to "
        "reading something platform-dependent"
    )
    assert "peak_rss_source()" in code, (
        "the wrapper does not report which counter answered, so peak_rss_method is "
        "being written by something other than the measurement"
    )
    assert "__GIGAXML_PEAK__" in code, "the wrapper prints a marker the recorder does not look for"
    for fallback in ("info.rss", "memory_info", "psutil", "import psutil"):
        assert fallback not in code, (
            f"the wrapper still runs {fallback!r}: a current-RSS reading filed under the "
            f"name peak is the failure this file exists to prevent"
        )

    recorder = source[source.index("def run_once") : source.index("def summarise")]
    for forbidden in ("psutil", "memory_info", "getrusage", "PeakWorkingSet", "VmHWM"):
        assert forbidden not in recorder, (
            f"run_once references {forbidden}, so the parent may be measuring the child "
            f"rather than reading what the child reported"
        )


def test_the_named_counter_is_the_one_this_platform_documents() -> None:
    """The counter's *name* is checked against the documentation, not against itself.

    The check above -- the recorded method matches what ``peak_rss_source()`` reports --
    is a consistency check, and a consistency check cannot catch both sides moving
    together. Verified by mutation: renaming the counter to something this platform does
    not have left that test green, because the recorder and the function agreed with
    each other and both were wrong.

    So the name is pinned to ``peak_rss_mb``'s own docstring, on whichever platform is
    running. No table of platforms is written here: the module already documents which
    counter it reads on each one, and a second copy in a test is the thing that drifts.
    A rename that outruns the documentation fails here instead of being published in
    ``results.json`` as the method that produced a number.
    """
    import inspect

    from gigaxml.run import peak_rss_mb, peak_rss_source

    source = peak_rss_source()
    if source is None:  # pragma: no cover - platform dependent
        pytest.skip("this platform has no high-water mark to name")
    assert peak_rss_mb() is not None, "the counter is named but nothing reads it"
    counter = source.split(" (")[0]
    documented = inspect.getdoc(peak_rss_mb) or ""
    assert counter in documented, (
        f"peak_rss_source() reports {counter!r} and peak_rss_mb's own docstring never "
        f"mentions it. The name a benchmark records as its method has to be the counter "
        f"the module says it reads."
    )
    # And the other half of the same claim: a name that is only a platform's field, with
    # no reader behind it, is the shape of the fallback this repository removed.
    assert "psutil" not in source, (
        f"the reported counter still names psutil ({source!r}); the product's reader does "
        f"not go through psutil and a method string that says it does is wrong twice"
    )


def test_the_memory_method_check_rejects_a_parent_read() -> None:
    """The mutation: feed the check the number a parent would have seen.

    Without this, :func:`peak_looks_self_read` is a comparison with no demonstrated
    failure -- a helper that returns True for everything passes every caller. The
    recorded 4.1 MiB is what a parent's view of a live child measured on this machine.
    """
    assert peak_looks_self_read(817.9) is True
    assert peak_looks_self_read(384.0) is True
    assert peak_looks_self_read(4.1) is False, (
        "the frozen parent-view figure passed the self-read check, which would make every "
        "criterion F guard in this file decorative"
    )
    assert peak_looks_self_read(24.4) is False, "the CI baseline's own peak must not pass"
    # And the recorded shape of the harness's own bug on a Linux runner: 14.4 MiB for a
    # child that had allocated 384, which is a number no interpreter could produce.
    assert peak_looks_self_read(14.4) is False, (
        "the fallback reading the runner reported must not pass"
    )
    # **What 273.117 is, and why it is here.** The parent-side probe read 4.05 MiB on
    # this machine and 273.117 MiB on windows-latest for the same child. The second does
    # pass, and that is not a hole in the check -- it is the check saying plainly that it
    # separates *the work from the apparatus*, not parent from child. Asserted so the
    # next reader learns it from a test rather than from a Windows-only red build.
    assert peak_looks_self_read(273.117) is True


def test_the_harness_names_the_counter_it_read_rather_than_asserting_it() -> None:
    """The label is parsed out of the child's output, not typed in beside it.

    ``getattr(info, 'peak_wset', None) or info.rss`` is a real fallback: on a platform
    whose psutil has no ``peak_wset`` it returns *current* RSS, which is a different
    quantity. Before M17 the recorder wrote "peak_wset" unconditionally, so a fallback run
    would have been filed as a peak measurement.
    """
    source = (BENCH / "compare" / "run_comparison.py").read_text(encoding="utf-8")
    wrapper = source[source.index("WRAPPER = ") : source.index("PEAK_MARKER = ")]
    assert "counter" in wrapper, "the wrapper does not report which counter answered"
    recorder = source[source.index("peak_lines = [") : source.index("output_size =")]
    assert "reported[1]" in recorder, (
        "the recorder does not read the counter name out of the child's line, so "
        "peak_rss_method is still a constant"
    )
    assert 'peak_rss_method": "peak_wset' not in recorder, (
        "the recorder still writes a hardcoded method string"
    )


def test_the_perf_baseline_probe_reads_its_peak_inside_the_child() -> None:
    """The CI baseline's method claim, verified against the code that makes it.

    The claim is "self-read in the child, via ``gigaxml.run.peak_rss_mb()``". It holds
    structurally: the PROBE is handed to ``python -c`` and calls the product's own peak
    counter, so the parent never touches the child's memory. Asserted structurally because
    a runtime check here would cost a subprocess per run for a property of the text.
    """
    source = (BENCH / "perf_baseline.py").read_text(encoding="utf-8")
    probe = source[source.index("PROBE = ") : source.index("def run_once")]
    assert "peak_rss_mb()" in probe, "the probe no longer self-reads a peak"
    assert "psutil" not in probe, "the probe has started measuring from outside"
    run_once = source[source.index("def run_once") : source.index("def main")]
    # The parent must not touch a memory API at all: not psutil, not the platform
    # counters, not a resource call. A substring search for "peak" would fire on the
    # docstring, which is why this looks for the APIs instead.
    for forbidden in ("psutil", "memory_info", "getrusage", "PeakWorkingSet", "VmHWM"):
        assert forbidden not in run_once, (
            f"run_once references {forbidden}, so the parent may be measuring the child "
            f"rather than reading what the child reported"
        )


# --------------------------------------------------------------------------
# Criterion D -- two thresholds, two purposes, never mixed
# --------------------------------------------------------------------------


def test_the_significance_thresholds_are_the_formal_ones(provenance: ModuleType) -> None:
    """Criterion D: -20% throughput and +25% memory, for a controlled machine.

    Each case is one step either side of the line, because a check that is off by one
    percent is indistinguishable from a check that is off by twenty.
    """
    assert provenance.SIGNIFICANT_THROUGHPUT_DROP == 0.20
    assert provenance.SIGNIFICANT_MEMORY_RISE == 0.25

    at_floor = provenance.is_significant(throughput_before=100_000, throughput_after=80_000)
    assert at_floor["throughput_significant"] is False, "exactly -20% is not past -20%"

    past = provenance.is_significant(throughput_before=100_000, throughput_after=79_000)
    assert past["throughput_significant"] is True

    memory_under = provenance.is_significant(
        throughput_before=100_000, throughput_after=100_000, memory_before=40.0, memory_after=50.0
    )
    assert memory_under["memory_significant"] is False, "exactly +25% is not past +25%"

    memory_over = provenance.is_significant(
        throughput_before=100_000, throughput_after=100_000, memory_before=40.0, memory_after=50.1
    )
    assert memory_over["memory_significant"] is True


def test_a_peak_that_was_never_measured_is_not_a_peak_of_zero(provenance: ModuleType) -> None:
    """A missing measurement has to look missing, or a regression hides inside it.

    ``peak_rss_mb()`` returns ``None`` on a platform that cannot report a high-water
    mark, and the instinct to make that ``0.0`` is the one that matters: every later
    comparison then reads as a large *improvement*.
    """
    missing_after = provenance.is_significant(
        throughput_before=100_000, throughput_after=100_000, memory_before=40.0, memory_after=None
    )
    assert missing_after["memory_significant"] is None
    assert "after" in missing_after["memory_note"]

    missing_before = provenance.is_significant(
        throughput_before=100_000, throughput_after=100_000, memory_before=None, memory_after=40.0
    )
    assert missing_before["memory_significant"] is None
    assert "before" in missing_before["memory_note"]


def test_the_ci_gate_is_a_different_and_looser_number(provenance: ModuleType) -> None:
    """The two thresholds must not be substituted for one another.

    CI runs on a shared runner and asserts 60% of a reference recorded on that same
    runner. A formal comparison on a controlled machine calls -20% significant. Using
    the CI number for a formal comparison would fail every run; using the formal number in
    CI would be red within a week for reasons that have nothing to do with the code, which
    is the failure A11 exists to prevent.
    """
    source = (BENCH / "perf_baseline.py").read_text(encoding="utf-8")
    assert "MIN_THROUGHPUT_RATIO = 0.60" in source
    assert provenance.SIGNIFICANT_THROUGHPUT_DROP != 0.60
    assert (
        "NOT the CI gate"
        in provenance.is_significant(throughput_before=100_000, throughput_after=100_000)[
            "thresholds"
        ]["note"]
    )


# --------------------------------------------------------------------------
# Criterion E -- where the performance job runs, and why that is right
# --------------------------------------------------------------------------


def performance_jobs() -> list[tuple[str, str, dict]]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = _yaml(path)
        for name, job in (workflow.get("jobs") or {}).items():
            steps = yaml_dump(job)
            if "perf_baseline.py" in steps:
                found.append((path.name, name, job))
    return found


def _yaml(path: pathlib.Path) -> dict:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def yaml_dump(value: object) -> str:
    import yaml

    return yaml.safe_dump(value)


def test_exactly_one_job_runs_the_performance_check_and_it_is_ubuntu() -> None:
    """Criterion E, first question: does the M14 constraint still hold?

    It does, and it is the right constraint. The quantity is throughput in records per
    second, and this milestone measured why cross-platform comparison is not merely
    imprecise but wrong by more than a factor of two: on the same 2 MB document this
    machine sustains 43,212 rec/s while the shared ubuntu runner the baseline was recorded
    on sustains 19,312 rec/s on a 10 MB one. A gate that compared those two numbers would
    measure the computers, not the code.
    """
    jobs = performance_jobs()
    assert len(jobs) == 1, f"expected one performance job, found {[j[1] for j in jobs]}"
    workflow_name, job_name, job = jobs[0]
    assert job.get("runs-on") == "ubuntu-latest", (
        f"{workflow_name}:{job_name} runs on {job.get('runs-on')!r}, not ubuntu-latest"
    )
    assert "matrix" not in job, (
        "the performance job has a matrix, so it expands to several machines compared "
        "against one baseline"
    )


def test_a_cross_platform_performance_job_would_be_refused() -> None:
    """The mutation: a matrix and a second platform, which the shape check must reject.

    ``tools/ci_selfcheck.py shape`` is what enforces this, and it is checked here by
    running it against a doctored copy of the workflow rather than by reading it. A
    matrix under a fixed ``runs-on`` is the dangerous form -- the label does not change,
    so the file still reads as single-platform while quietly producing N runs compared
    against one reference.
    """
    import yaml

    workflow = WORKFLOWS / "test.yml"
    completed = subprocess.run(
        [sys.executable, "tools/ci_selfcheck.py", "shape"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    # **Bytes, not text.** This repository has ``core.autocrlf=true``, so a fresh
    # checkout of this workflow is 23,032 bytes of CRLF while the committed blob is
    # 22,609 bytes of LF. A first version of this test read the file with
    # ``read_text`` and wrote it back with ``newline=""``, which round-trips the text
    # through universal newlines and therefore **rewrote 423 CRLFs as LFs**. The content
    # was identical, the test passed, CI would have passed, and the working tree was left
    # dirty with a line-ending change nobody asked for -- found by ``git status``
    # afterwards, not by the test. Byte-for-byte is the only restore that is exact.
    original = workflow.read_bytes()
    # Whatever line endings this checkout has, the mutation has to match them: a fresh
    # Windows clone of this file is CRLF and a Linux one is LF, and a test that only
    # worked on one of them would be a test that quietly stops testing.
    newline = b"\r\n" if b"\r\n" in original else b"\n"
    anchor = b"  performance-baseline:" + newline
    assert anchor in original, (
        f"the mutation anchor is not in the file; it holds neither {newline!r} nor the "
        f"other line ending, so the workflow changed shape"
    )
    matrix_block = newline.join(
        [
            b"  performance-baseline:",
            b"    strategy:",
            b"      matrix:",
            b"        os: [ubuntu-latest, windows-latest]",
            b"",
        ]
    )
    doctored_bytes = original.replace(anchor, matrix_block, 1)
    assert doctored_bytes != original, "the mutation did not land: no performance-baseline job"
    try:
        workflow.write_bytes(doctored_bytes)
        try:
            mutated = subprocess.run(
                [sys.executable, "tools/ci_selfcheck.py", "shape"],
                cwd=str(REPO),
                capture_output=True,
                text=True,
                check=False,
            )
        finally:
            workflow.write_bytes(original)
    finally:
        assert workflow.read_bytes() == original, (
            "the workflow was not restored byte for byte; the repository is left modified"
        )

    assert mutated.returncode != 0, (
        "a performance job with an OS matrix was accepted, so criterion E's constraint is "
        "not actually enforced"
    )
    assert "performance" in (mutated.stdout + mutated.stderr).lower()
    assert yaml.safe_load(doctored_bytes) is not None


# --------------------------------------------------------------------------
# Criterion C -- versioned history, and no invented numbers
# --------------------------------------------------------------------------


def history_entries() -> dict[str, dict]:
    return {
        path.stem: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(HISTORY.glob("*.json"))
    }


def test_every_released_version_has_a_history_entry() -> None:
    """The map is complete, so "was it measured?" has an answer for every version.

    Derived from the tags rather than from a hand-written list, so a release that skips
    the directory is caught here instead of by whoever goes looking.
    """
    tags = subprocess.run(
        ["git", "tag", "-l"], cwd=str(REPO), capture_output=True, text=True, check=False
    )
    released = {tag.lstrip("v") for tag in tags.stdout.split() if tag.startswith("v")}
    assert released, "no tags found, so this test would pass vacuously"
    assert released == set(history_entries()), (
        f"history covers {sorted(history_entries())} but the repository has released "
        f"{sorted(released)}"
    )


def test_a_version_with_no_measurement_carries_no_numbers() -> None:
    """Criterion C: the honest entry is a file with no numbers in it.

    Tested by walking the whole entry rather than by checking for one particular key,
    because "no fabricated figures" is a property of the file, not of a field. A version
    with no measurement gets a stub that says so and cites the commit dates that establish
    it; anything numeric in one of these would be an invention wearing a version number.
    """
    unrecorded = {
        version: entry for version, entry in history_entries().items() if not entry["recorded"]
    }
    assert unrecorded, "expected some versions to have no record"

    def numbers_in(value: object, path: str = "") -> list[str]:
        found = []
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("schema_version",):
                    continue
                found += numbers_in(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                found += numbers_in(item, f"{path}[{index}]")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            found.append(f"{path} = {value!r}")
        return found

    for version, entry in unrecorded.items():
        assert entry["version"] == version
        assert entry["why_there_is_no_record"], f"{version} does not say why"
        stray = numbers_in(entry)
        assert stray == [], f"{version} carries numbers despite having no record: {stray}"


def test_the_recorded_entry_points_at_a_file_that_still_agrees_with_it(
    results: dict,
) -> None:
    """The history entry is generated from the results file; this is the other direction.

    If the numbers are ever re-recorded, the history entry has to be regenerated or this
    goes red -- which is the point. A history that silently describes a superseded
    measurement is worse than no history, because it is a history of the wrong thing.

    **Which entry is checked is derived, not hardcoded.** ``results.json`` is a
    single-slot file: a new sweep overwrites it. It used to hold 1.1.0's recovered
    numbers, and a re-recording at 2.0.0 replaced them (2026-10-04). So the entry this
    test must check is the one whose ``numbers_live_in`` is that file *and* whose
    ``authoritative_key`` is the commit the file records -- 2.0.0's. Pinning the name
    ``1.1.0`` here would go red the moment the slot was reused, which is correct but
    names the wrong cause; the check is that the file and the entry that claims it agree,
    whichever entry that is today. 1.1.0's own headline is still on disk, but it is now
    a frozen historical record rather than the contents of the shared slot -- see
    ``1.1.0.json``'s ``numbers_live_in``.
    """
    recorded = {
        version: entry
        for version, entry in history_entries().items()
        if entry["recorded"] and entry["numbers_live_in"] == "benchmarks/compare/results.json"
    }
    assert recorded, "no recorded entry points at benchmarks/compare/results.json"
    # The file records its own commit; the entry that claims the file must be the one
    # for that commit. This is the assertion that keeps the choice derived rather than a
    # name typed here.
    commit = results["identity"]["git_commit"].split()[0]
    agreeing = [
        version
        for version, entry in recorded.items()
        if entry["authoritative_key"]["git_commit"] == commit
    ]
    assert len(agreeing) == 1, (
        f"expected exactly one recorded entry whose commit is the results file's ({commit}), "
        f"found {agreeing} among {sorted(recorded)}"
    )
    entry = recorded[agreeing[0]]
    assert entry["recorded"] is True
    assert entry["numbers_live_in"] == "benchmarks/compare/results.json"
    assert (REPO / entry["numbers_live_in"]).is_file()

    for key, headline in entry["headline"].items():
        size, name = key.split("/")
        summary = results["results"][size][name]["summary"]
        # `.get`, not `[...]`: the 1 GB pandas point is the failure shape, whose summary
        # has four keys and no statistics at all. Its headline entry holds nulls, and
        # that is the honest representation of a point where every repeat failed.
        assert headline["records_per_s_median"] == summary.get("records_per_s_median"), key
        assert headline["peak_rss_mb_median"] == summary.get("peak_rss_median_mb"), key
        assert headline["complete_output"] == summary.get("complete_output"), key
        assert headline["recomputes_from_own_runs"] is True, key


def test_the_recorded_entry_says_the_commit_is_the_key_not_the_version() -> None:
    """A version string is a label that moves; a commit does not.

    The entry is named ``1.1.0`` and records ``version_string_at_recording: 1.1.0`` while
    the tree it measured was six commits past the ``v1.1.0`` tag. Quoting the version
    without the commit would be the same error ``results.json`` made with ``0.1.0``, one
    level up, so the file has to carry both and say which one to use.
    """
    entry = history_entries()["1.1.0"]
    key = entry["authoritative_key"]
    assert key["git_commit"].startswith("82e31eb4")
    assert key["commits_past_v1_1_0"] == 6
    assert key["recovered"] is True
    assert "SIX COMMITS PAST" in key["why_this_is_the_key"]
    assert entry["version_string_at_recording"] == "1.1.0"

    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "v1.1.0", key["git_commit"]],
        cwd=str(REPO),
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, "v1.1.0 is not an ancestor of the recorded commit"
    count = subprocess.run(
        ["git", "rev-list", "--count", f"v1.1.0..{key['git_commit']}"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    assert int(count.stdout.strip()) == key["commits_past_v1_1_0"], (
        "the recorded offset no longer matches the repository"
    )


# --------------------------------------------------------------------------
# The invariant itself
# --------------------------------------------------------------------------


def test_three_months_from_now_the_number_can_be_traced(results: dict, baseline: dict) -> None:
    """The milestone's own sentence, as a checklist against the two files that hold numbers.

    Not a new check so much as the summary of the ones above, written out because the
    question "can this number be traced" deserves one answer that names every part:
    the commit, the machine, the config, the data, the raw runs, and the method the memory
    was measured with.

    **"which tool" is derived, not the literal that was current the day this was written.**
    The check is that the version the results file names is a version this repository
    recorded and has a history entry for -- not that it is the particular string "1.1.0",
    which stopped being true when the file was re-recorded at 2.0.0 (2026-10-04). A
    hardcoded version here fails on the next sweep for a reason that has nothing to do
    with traceability; deriving it keeps the question the same.
    """
    identity = results["identity"]
    named = [version for version in history_entries() if version in identity["gigaxml"]]
    assert len(named) == 1, (
        f"results.json names a tool version ({identity['gigaxml']!r}) that no single history "
        f"entry matches; matched {named}"
    )
    traceable = {
        "which commit": identity["git_commit"].split()[0],
        "which tool": bool(named),
        "which interpreter": bool(identity["python"]),
        "which lxml": bool(identity["lxml"]),
        "which machine": all(identity[f] for f in ("os", "cpu", "machine", "memory_total_gb")),
        "which config": len(str(identity["config_sha256"])) == 64,
        "which data": identity["dataset_sha256"],  # a map now, not a not-recorded string
        # Five recorded runs per data point, not five *successful* ones: the 1 GB pandas
        # point failed every repeat and its summary is a four-key failure shape, which is
        # still five raw runs on disk and still recomputes.
        "which runs": all(len(runs) == 5 for _, _, _, runs in data_points(results)),
        "which memory method": "self-read" in identity["memory_method"],
    }
    for part, value in traceable.items():
        assert value, f"a number in results.json cannot be traced to {part}"

    # The CI baseline is the one that fails, and the file says so.
    assert "cannot be recomputed" in baseline["identity"]["summary_recomputable"]
    assert "HAND-AUTHORED" in baseline["identity"]["provenance_of_these_numbers"]
