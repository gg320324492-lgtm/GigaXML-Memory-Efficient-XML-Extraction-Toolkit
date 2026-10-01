# `benchmarks/history/` — one entry per released version

A benchmark number is worth something only if you can get back to the run that produced
it. This directory is the map from a **released version** to whatever was measured at it,
including — mostly — the measurements that were never taken.

**A file here means one of two things, and `recorded` says which.**

| `recorded` | What the file is |
|---|---|
| `true` | A sweep ran. The entry carries the identity and the headline figures, and says which file holds the numbers. |
| `false` | **No measurement was taken at this version.** The entry says so, with the evidence. |

The `false` entries are the point. Four of the five versions in this repository have no
benchmark record, and three of them shipped *after* the last measurement was taken:

```
0.9.0  2026-09-27   no record — no benchmark result existed yet
1.0.0  2026-09-28   no record — the first results file post-dates this tag by 4 hours
1.1.0  2026-09-28   RECORDED, and recovered after the fact (see below)
1.2.0  2026-09-30   no record — the last measurement was 1 day 20 min earlier
1.2.1  2026-10-01   no record — the last measurement was 2 days earlier
```

Each `false` entry carries the commit dates that establish the absence, so the claim is
checkable rather than an assurance. **No entry in this directory contains a number that was
not measured.** That is the project's rule and it is the reason the directory is worth
keeping: a version whose performance is unknown says so, instead of inheriting its
neighbour's figures and looking measured.

## Why 1.1.0 is keyed by a version string and a commit, and why the commit is the key

`1.1.0.json` records `version_string_at_recording: "1.1.0"` **and**
`git_commit: 82e31eb4e35756a68507e49090ac0728d42c6dbb`, and the second is the one to
quote. The tree at `82e31eb` was **six commits past the `v1.1.0` tag** and `pyproject.toml`
read `1.1.0` at both. So the sweep did not measure the 1.1.0 release; it measured six
commits of work on top of it, under a version string nobody had bumped yet.

This is the same defect as the one `results.json` shipped with, one level up: that file
records `gigaxml: "0.1.0"` for a run whose tree was at 1.1.0, because the version was
read from the installed distribution rather than from the package. **A version string is
a label that moves when somebody remembers; a commit does not move.**

## Nothing is copied

`1.1.0.json` does not duplicate `benchmarks/compare/results.json`. One file holding the
numbers is one thing to keep correct; a 32 KB copy under `history/` is a second thing
that can drift out of step without anybody noticing. The entry carries the identity and
the headline figures, names the file the numbers live in, and
`tests/integration/test_benchmark_provenance.py` asserts from the other side that those
figures still match it.

## How an entry gets written

Run the benchmarks. Both scripts now write the identity themselves —

* `benchmarks/perf_baseline.py --update` writes `schema_version`, the identity block, the
  generator's **seed and the generated document's sha256**, and every individual run's
  rate and peak;
* `benchmarks/compare/run_comparison.py` writes the same, with `dataset_sha256` as a map
  keyed by size because it measures four documents.

So a new entry is a copy of the identity block plus the headline figures, not a
retrospective reconstruction. What is *not* automated, and should not be: deciding which
released version a sweep belongs to. That is a judgement about whether the tree at
`82e31eb` is "1.1.0" or "1.1.0 plus six commits", and the honest answer — recorded in the
file — is that it is the latter.
