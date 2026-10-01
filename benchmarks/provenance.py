"""Provenance for benchmark numbers: which code, which machine, which data.

**The question this module exists to answer.** Three months from now somebody asks
"where did this 43,385 rec/s come from?" and the answer has to be *findable*, not
*reconstructible from memory*. That needs three things attached to every number at the
moment it is recorded, because none of them can be recovered later:

* **which commit** -- a branch name moves, so a recorded ``main`` is worth nothing a
  day later; the 40-character SHA does not move.
* **which machine** -- throughput and peak memory are properties of a computer as much
  as of a program, and the numbers are meaningless without it.
* **which data and which config** -- a benchmark that read a different document measures
  a different thing, and the row counts alone do not identify bytes.

**Two of those three are already lost for the one existing record, and this module says
so rather than papering over it.** ``benchmarks/compare/results.json`` was recorded on
2026-09-28 without any of it: the commit is *recoverable* (there is a gap in the commit
timeline and only one commit can have been HEAD, see ``benchmarks/history/1.1.0.json``),
the tool version in it is *wrong* in a way the code already documents, and the dataset
identity is *gone* -- ``data/b100m.xml`` and its two larger siblings were deleted, and
``run_comparison.py`` never recorded their hashes. So the invariant this module serves is
half-retrofit and half-forward-looking, and both halves are labelled.

**Why the config hash is taken over the git blob and not the file on disk.** With
``core.autocrlf=true`` -- which this repository has -- a fresh clone of
``benchmarks/compare/gigaxml-config.yaml`` is 524 bytes with CRLF line endings while the
blob is 512 bytes with LF, and their sha256 differ:

    blob     512 B  b7b51aa97e7c7ff52c7de4ef8ce0c93bab47a1bc6cdcc68452b668e146f9d647
    clone    524 B  ee8035a80c83856b20715c19201304f283d85d00f32f35a0d00ebcb5bb170bb1

A ``config_sha256`` taken from the working tree is therefore true on the machine that
took it and false everywhere else, which is worse than recording nothing: it looks like
an identity and fails verification on the first person who checks. Hash the blob, and
say in the field name that the blob is what was hashed. This is the same trap as
``.gitattributes`` in M14 and a scratch copy of ``fhir-all.xsd`` in M16, landing for the
third time on the one thing it should never touch.

**None of this can fail a benchmark.** Every probe degrades to a recorded string saying
it could not be read. An identity block that raises because ``git`` is missing would turn
a missing fact into a failed measurement, which is the wrong trade in the other
direction.
"""

from __future__ import annotations

import hashlib
import json
import platform
import statistics
import subprocess
from pathlib import Path
from typing import Any, Final

__all__ = [
    "MEMORY_METHOD_SELF_READ",
    "REQUIRED_IDENTITY_FIELDS",
    "SCHEMA_VERSION",
    "SIGNIFICANT_MEMORY_RISE",
    "SIGNIFICANT_THROUGHPUT_DROP",
    "blob_sha256",
    "git_commit",
    "identity",
    "is_significant",
    "load_results",
    "missing_identity_fields",
    "recompute_perf_baseline",
    "recompute_summary",
    "sha256_bytes",
]

#: Bumped when the *shape* of a results file changes, so a reader can tell "this file
#: predates the identity block" from "this file has an identity block with holes in it".
#: The two are different problems and conflating them is how a missing field becomes an
#: invisible one.
SCHEMA_VERSION: Final = 2

#: Recorded verbatim in a results file, and asserted by the tests, so that a number
#: cannot be read without also reading how its memory was measured. The phrasing is
#: "self-read in the process that did the work" rather than a bare counter name,
#: because the counter is the easy half: the failure this guards against is a parent
#: reading a live child, which on this machine reports a frozen ~4.1 MiB for an 800 MiB
#: process.
MEMORY_METHOD_SELF_READ: Final = "peak_wset, self-read in the process that did the work"

#: Every one of these is criterion A's list. A results file that is missing one of them
#: is refused by :func:`missing_identity_fields` rather than quietly accepted, because a
#: field nobody checks is a field nobody maintains.
REQUIRED_IDENTITY_FIELDS: Final = (
    "schema_version",
    "git_commit",
    "gigaxml",
    "python",
    "lxml",
    "os",
    "cpu",
    "memory_total_gb",
    "config_sha256",
    "dataset_sha256",
    "memory_method",
)

#: Criterion D's formal-comparison thresholds, deliberately **not** the CI ones. CI runs
#: on a shared runner and asserts 60% of a reference recorded on that same runner; these
#: are for comparing two runs a person made on a controlled machine, and they are much
#: tighter in one direction and much looser in the other. Mixing the two is the mistake
#: criterion D is about, so they are named differently and never share a constant.
SIGNIFICANT_THROUGHPUT_DROP: Final = 0.20
SIGNIFICANT_MEMORY_RISE: Final = 0.25

_UNAVAILABLE: Final = "unavailable"


def sha256_bytes(data: bytes) -> str:
    """The digest of some bytes, for configs that live in the source rather than on disk."""
    return hashlib.sha256(data).hexdigest()


def git_commit(repo_root: Path | str) -> str:
    """The 40-character SHA of HEAD, or a recorded reason it could not be read.

    ``rev-parse HEAD`` and never a branch name: a benchmark recorded against ``main`` is
    ambiguous the moment ``main`` moves, and by the time somebody needs it, ambiguously.

    A dirty working tree is worth recording too, because the commit then understates
    what ran. Rather than refuse, the marker says so in the string -- a benchmark taken
    from a dirty tree is still a real measurement, it is just one whose commit is a
    lower bound.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:  # pragma: no cover - git absent from PATH
        return f"unavailable ({exc.__class__.__name__})"
    if completed.returncode != 0:
        return "unavailable (not a git repository)"
    sha = completed.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        return f"unreadable ({sha!r})"
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git absent from PATH
        return sha
    if dirty.returncode == 0 and dirty.stdout.strip():
        return f"{sha} (working tree was dirty when recorded)"
    return sha


def blob_sha256(repo_root: Path | str, relative_path: str) -> str:
    """The sha256 of a path's **committed bytes**, not of the copy in the working tree.

    See the module docstring: with ``core.autocrlf=true`` those are different files on
    different machines, and only the blob is the same everywhere. Falls back to hashing
    the working tree when the path is not in the index at all -- an untracked config is
    still a config somebody ran, and refusing to describe it helps nobody -- and says so
    in the returned string so the weaker provenance is visible rather than assumed.
    """
    try:
        completed = subprocess.run(
            ["git", "cat-file", "blob", f"HEAD:{relative_path}"],
            cwd=str(repo_root),
            capture_output=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git absent from PATH
        completed = None
    if completed is not None and completed.returncode == 0:
        return sha256_bytes(completed.stdout)
    path = Path(repo_root) / relative_path
    if not path.is_file():
        return f"{_UNAVAILABLE} ({relative_path} is neither committed nor present)"
    return f"working-tree, uncommitted: {sha256_bytes(path.read_bytes())}"


def _distribution_version(name: str) -> str:
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:
        return _UNAVAILABLE


def _gigaxml_version() -> str:
    """The version of the code that ran, read from the package and not the distribution.

    ``importlib.metadata`` reports whatever ``pip install -e`` recorded last, so a tree
    whose ``__version__`` has moved on still answers with the old number. That is not
    hypothetical: ``results.json`` says ``0.1.0`` for a run whose tree was at 1.1.0.
    """
    try:
        import gigaxml

        return str(gigaxml.__version__)
    except Exception:
        return _distribution_version("gigaxml")


def _total_memory_gb() -> str | float:
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024**3), 1)
    except Exception:
        return _UNAVAILABLE


def identity(
    repo_root: Path | str,
    *,
    dataset_sha256: str,
    config_sha256: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The identity block a results file carries.

    Args:
        repo_root: the repository the numbers were measured in.
        dataset_sha256: digest of the bytes that were read. Pass a string that says why
            it is unknown rather than omitting the key -- a missing key reads as "not
            applicable" and an honest ``"not recorded; the file was deleted"`` does not.
        config_sha256: digest of the config that was used. For a file, use
            :func:`blob_sha256`; for a config that lives in the source, use
            :func:`sha256_bytes` on the literal.
        extra: additional provenance the caller has and this function cannot know.

    Returns:
        A dict with every field in :data:`REQUIRED_IDENTITY_FIELDS`, plus ``extra``.
    """
    try:
        import lxml.etree

        lxml_version = lxml.etree.__version__
    except Exception:  # pragma: no cover - lxml is a runtime dependency
        lxml_version = _UNAVAILABLE

    block: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit(repo_root),
        "gigaxml": _gigaxml_version(),
        "python": platform.python_version(),
        "lxml": lxml_version,
        "os": f"{platform.system()} {platform.release()}",
        "cpu": platform.processor() or platform.machine(),
        "machine": platform.machine(),
        "memory_total_gb": _total_memory_gb(),
        "config_sha256": config_sha256,
        "config_sha256_source": "git blob of the committed bytes, not the working tree",
        "dataset_sha256": dataset_sha256,
        "memory_method": MEMORY_METHOD_SELF_READ,
    }
    if extra:
        block.update(extra)
    return block


def missing_identity_fields(record: dict[str, Any]) -> list[str]:
    """Which required fields a results file is missing. Empty means it qualifies.

    Deliberately checks presence, not truthiness: a field holding
    ``"not recorded: the file was deleted"`` is present and honest, while a field
    holding ``""`` or ``None`` is a hole that reads as a fact.
    """
    identity_block = record.get("identity") if "identity" in record else record
    if not isinstance(identity_block, dict):
        return list(REQUIRED_IDENTITY_FIELDS)
    return [
        field
        for field in REQUIRED_IDENTITY_FIELDS
        if field not in identity_block or identity_block[field] in (None, "")
    ]


def is_significant(
    *,
    throughput_before: float,
    throughput_after: float,
    memory_before: float | None = None,
    memory_after: float | None = None,
) -> dict[str, Any]:
    """Criterion D: is a change significant on a controlled machine?

    Throughput counts as significant past a **20% drop** and memory past a **25% rise**,
    which are the project's formal-comparison numbers. These are not the CI numbers and
    must not be substituted for them: CI asserts 60% of a reference recorded on a shared
    runner, because a tighter gate there is red within a week for reasons that have
    nothing to do with the code. Two thresholds, two purposes.

    Memory is skipped rather than guessed when either side is missing, and the answer
    says which side was missing -- a peak this platform could not read is not a peak of
    zero, and treating it as one is how a regression hides inside an absent measurement.
    """
    if throughput_before <= 0:
        raise ValueError(f"throughput_before must be positive, got {throughput_before!r}")
    throughput_ratio = throughput_after / throughput_before
    memory_note: str | None = None
    memory_ratio: float | None = None
    if memory_before is None or memory_after is None:
        missing = "before" if memory_before is None else "after"
        memory_note = f"not compared: the {missing} peak was not measured"
    else:
        if memory_before <= 0:
            raise ValueError(f"memory_before must be positive, got {memory_before!r}")
        memory_ratio = memory_after / memory_before

    return {
        "throughput_ratio": round(throughput_ratio, 4),
        "throughput_significant": throughput_ratio < (1.0 - SIGNIFICANT_THROUGHPUT_DROP),
        "memory_ratio": None if memory_ratio is None else round(memory_ratio, 4),
        "memory_significant": None
        if memory_ratio is None
        else memory_ratio > (1.0 + SIGNIFICANT_MEMORY_RISE),
        "memory_note": memory_note,
        "thresholds": {
            "throughput_drop": SIGNIFICANT_THROUGHPUT_DROP,
            "memory_rise": SIGNIFICANT_MEMORY_RISE,
            "note": "formal comparison on a controlled machine; NOT the CI gate, which is 60%",
        },
    }


def recompute_summary(runs: list[dict[str, Any]], *, rows_in_document: int) -> dict[str, Any]:
    """Rebuild a summary from its raw per-run values.

    This is the whole of criterion B, and it is deliberately a *separate* function from
    whatever produced the summary, so that agreement between the two is evidence rather
    than a tautology.

    The definitions are ``run_comparison.py``'s, copied rather than imported because that
    file is a script whose import-time behaviour is not a contract:

    * ``p95_index = min(n - 1, round(0.95 * (n - 1)))`` -- with the project's five repeats
      that index is ``n - 1``, so **p95 equals max at this sample size**. Recorded here
      because a reader who assumes p95 interpolates will "discover" a bug that is the
      definition working as written.
    * ``records_per_s_median`` divides by the **rounded** median, not the raw one. The
      rounding is visible in the file, so recomputing from the raw median disagrees in
      the last digit on long runs and would read as tampering.
    * only runs with ``exit_code == 0`` contribute, matching the recorder.

    ``rows_in_document`` is a **parameter, not something read from the runs**, and this is
    the one summary field that cannot be recomputed from the raw values alone:
    ``complete_output`` is a comparison against the generator's expected row count, so
    the expected count is an input to the check. A data point that silently stopped early
    is exactly what that field exists to catch -- on the recorded 4 GB pandas run it did,
    writing 10,485,760 of 11,915,264 rows and exiting 0 -- so it is worth the awkwardness
    of an explicit input rather than dropping the field.
    """
    ok = [r for r in runs if int(r["exit_code"]) == 0]
    if not ok:
        return {
            "records_per_s_median": None,
            "ok_runs": 0,
            "failed_runs": len(runs),
            "note": "every repeat failed; see runs[].stderr_tail",
        }

    wall = [float(r["wall_s"]) for r in ok]
    peak = [float(r["peak_rss_mb"]) for r in ok]
    rows_written = int(ok[0]["rows_written"])
    ordered = sorted(wall)
    p95_index = min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))
    median_s = round(statistics.median(wall), 3)

    return {
        "median_s": median_s,
        "min_s": round(min(wall), 3),
        "max_s": round(max(wall), 3),
        "stdev_s": round(statistics.stdev(wall), 3) if len(wall) > 1 else 0.0,
        "p95_s": round(ordered[p95_index], 3),
        "peak_rss_median_mb": round(statistics.median(peak), 1),
        "records_per_s_median": int(rows_written / median_s),
        "rows_written_median": int(statistics.median([int(r["rows_written"]) for r in ok])),
        "complete_output": rows_written == rows_in_document,
        "ok_runs": len(ok),
        "failed_runs": len(runs) - len(ok),
    }


def load_results(path: Path | str) -> dict[str, Any]:
    """Read a results file. A helper so callers do not each pick their own encoding."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def recompute_perf_baseline(recorded: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the CI baseline's two numbers from the raw per-run lists beside them.

    ``perf_baseline.py`` records every individual run's rate and peak in ``rates`` and
    ``peaks`` and summarises them as the median rate and the maximum peak. Recomputing
    those two from the raw lists is criterion B for this file, and it is a real check
    rather than a formality: a baseline whose headline number was hand-edited, or whose
    summary was written by an older version of the script with a different statistic,
    stops agreeing with its own raw values and that is the moment it gets caught.

    **One caveat, stated rather than discovered later.** The recorder rounds each rate to
    one decimal *and* rounds the median of the unrounded rates, in that order. For an odd
    number of repeats the median *is* one of the values, so the two agree and the
    recompute is exact. For an even number it does not: the median is the mean of the two
    middle values, and rounding them before averaging can land on a different tenth than
    averaging first. The default is five, and :func:`recompute_perf_baseline` reports the
    count so a caller can see which case it is in rather than assume the equality held.

    Raises:
        ValueError: the raw per-run lists are absent. That is not a hypothetical: the
            committed ``perf-baseline.json`` has a median and a repeat count and no raw
            values, so its headline number cannot be recomputed at all. The message says
            so rather than surfacing a ``KeyError`` from three frames down.
    """
    for key in ("rates", "peaks"):
        if key not in recorded:
            raise ValueError(
                f"this baseline has no {key!r}, so its summary cannot be recomputed from "
                f"anything. It records repeats={recorded.get('repeats')!r} and a "
                f"median, and the individual runs behind them are gone. Re-record it with "
                f"perf_baseline.py --update from the CI job, which writes both lists."
            )
    rates = [float(rate) for rate in recorded["rates"]]
    peaks = [float(peak) for peak in recorded["peaks"]]
    if not rates or len(rates) != len(peaks):
        raise ValueError(f"rates and peaks must be non-empty and the same length, got {rates!r}")
    return {
        "records_per_s": round(statistics.median(rates), 1),
        "peak_rss_mb": round(max(peaks), 1),
        "repeats": len(rates),
        "exact": len(rates) % 2 == 1,
    }
