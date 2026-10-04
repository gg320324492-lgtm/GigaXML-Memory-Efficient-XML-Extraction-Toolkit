# `benchmarks/history/` — one entry per released version

A benchmark number is worth something only if you can get back to the run that produced
it. This directory is the map from a **released version** to whatever was measured at it,
including — mostly — the measurements that were never taken.

**A file here means one of two things, and `recorded` says which.**

| `recorded` | What the file is |
|---|---|
| `true` | A sweep ran. The entry carries the identity and the headline figures, and says which file holds the numbers. |
| `false` | **No measurement was taken at this version.** The entry says so, with the evidence. |

The `false` entries are the point. Eight of the ten versions in this repository have no
benchmark record, and six of them shipped *after* the last measurement was taken; the two
that are recorded are `1.1.0`, recovered after the fact, and `2.0.0`, swept directly
(see below):

```
0.9.0  2026-09-27   no record — no benchmark result existed yet
1.0.0  2026-09-28   no record — the first results file post-dates this tag by 4 hours
1.1.0  2026-09-28   RECORDED, and recovered after the fact (see below)
1.2.0  2026-09-30   no record — the last measurement was 1 day 20 min earlier
1.2.1  2026-10-01   no record — the last measurement was 2 days earlier
2.0.0rc1 2026-10-03   no record — the build failed on all three platforms, so nothing was measured
2.0.0rc2 2026-10-03   no record — the build succeeded on all three platforms, and no sweep was run at it
2.0.0rc3 2026-10-03   no record — the build and the release assembly both completed, but the binary shipped under the previous version, and no sweep was run at it
2.0.0rc4 2026-10-03   no record — this is the first candidate whose tree and tag name the same version, and no sweep is planned at it
2.0.0    2026-10-03   RECORDED, 2026-10-04 — the first version published to PyPI, swept at a commit whose extraction code equals the tag's (see below)
```

The last row is the one release in the directory that is not a rehearsal. `2.0.0.json`
records `released` as the tag's own creation date and `released_as` as the commit it points
at, at `8aee010`, the same commit as rc4's tree. It is the first version in this repository
that `Publish to PyPI` did not skip: that job runs only for a tag whose name carries no
`rc`, so all four candidates were passed over and the tag-to-index path had never been
walked until this one. A published version can be yanked but not deleted, which is why this
tag cannot be re-cut the way a candidate can, and why the entry had to be written rather
than the tag moved. It is also the only release here that was swept *after* it shipped:
the sweep ran on 2026-10-04, one day past the tag, at a commit whose extraction code is
identical to the tag's (`8aee010` and the sweep's commit differ by fifteen commits and by
nothing under `src/`, which `2.0.0.json` records with the commands that establish it). See
**Why there are more than three rc entries** below for what each candidate exposed.

The rc4 row is dated now that its tag is cut, at `7ac99e9`. `2.0.0rc4.json` records
`released` as the tag's own creation date and `released_as` as the commit it points at.
That tag built on all three platforms and created a Release page, and the binary it
shipped reported `2.0.0rc4` — the version agreement rc3 lacked, which is why rc4 exists;
see **Why there are more than three rc entries** below.

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

## Why there are more than three rc entries, and what each one records

A release candidate is a rehearsal, and these four rehearsed different things. They are
kept separately because their `recorded: false` is not the same absence four times, and an
entry that only said "no record" would let a reader assume the wrong one.

* `2.0.0rc1` — **the build failed.** All three platform jobs died on the same line of
  `packaging/gigaxml.spec`, `int("0rc1")`. No artifact was produced at all, so a
  measurement was impossible in principle. `2.0.0rc1.json` says so with the evidence.
* `2.0.0rc2` — **the build succeeded and a later job failed.** Linux, Windows and macOS all
  built and uploaded an artifact, downloadable from the run; the `release` job (assembly,
  SBOM, checksums, the Release page) died on `ModuleNotFoundError: No module named 'yaml'`
  from `tools/release_checksums.py`, a job that declared an interpreter and no
  dependencies. The tag therefore has artifacts and **no Release page**. The measurement
  was still not taken.
* `2.0.0rc3` — **the build and the release assembly both completed, and the version was
  wrong.** The `yaml` defect is repaired (the `release` job installs `pyyaml`) and guarded
  on every pull request by `tools/ci_selfcheck.py deps`, which checks that each job installs
  what its `tools/*.py` steps import; the whole path from tag to Release page ran through
  for the first time and the Release page exists. But a build reads its version from
  `pyproject.toml`, not from the tag, and the tree was not bumped when the tag was cut, so
  the tag and the Release page announced `2.0.0rc3` while the binary inside them reported
  `2.0.0rc2`. The measurement was again not taken. The tag guard added for rc2
  (`test_the_tag_at_head_names_the_version_this_build_reports`) failed on this tag on all
  five legs of `test.yml` — the first time it ran, on the release that exercised it.
* `2.0.0rc4` — **the first candidate whose tree and tag name the same version.** Each of
  the four version sources (`pyproject.toml`, `gigaxml.__version__`, the release notes and
  the tag) reads `2.0.0rc4`, so the tag guard passes on the tree rather than catching it.
  The tag, cut at `7ac99e9`, built on all three platforms, created a Release page, and the
  binary it shipped reported `2.0.0rc4`. No measurement is planned here either.
* `2.0.0` — **the release, and the first version published to PyPI.** Cut at `8aee010`,
  the same tree as rc4, so no code changed between them; what changed is that the name
  carries no `rc`, and `Publish to PyPI` therefore ran instead of skipping. That makes it
  the first version a person can install from an index, and the first whose artifacts
  cannot be withdrawn the way a candidate's can. No measurement was taken here either —
  there is a `2.0.0.json` and it says so, rather than the figures being borrowed from
  1.1.0 or the candidates.

Nothing is measured at any candidate, for the reason `1.2.1.json` gives: the figures that
belong in this directory are taken at a version somebody installs. `2.0.0` is the first
entry here that *is* such a version rather than a rehearsal for one, and it is `recorded`
— a sweep ran on 2026-10-04 and its figures are in `benchmarks/compare/results.json`. The
distinction between a rehearsal and a release is still what the pipeline does (build,
release, publish) and it is still not a measurement; `2.0.0` is recorded because a
measurement was taken, not because the pipeline ran on it. What made that measurement
belong to the tag rather than to the fifteen commits above it is a fact about the code and
not about the version string: `git diff --name-only v2.0.0 <sweep commit> -- src/` is
empty, so the sweep measured the tag's own extraction code. `2.0.0.json` records the
commit, the command that shows the source gap is empty, and the one line they change
outside documentation (a packaging classifier), so the claim is checkable rather than
asserted. Writing `recorded: true` for a version whose code had *changed* since the sweep
would still be the substitution this directory refuses; this entry is the other case.

## Nothing is copied

A recorded entry does not duplicate `benchmarks/compare/results.json`. One file holding the
numbers is one thing to keep correct; a 32 KB copy under `history/` is a second thing
that can drift out of step without anybody noticing. The entry carries the identity and
the headline figures, names the file the numbers live in, and
`tests/integration/test_benchmark_provenance.py` asserts from the other side that those
figures still match it.

**`results.json` is a single slot, and the re-recording moved it.** It held `1.1.0`'s
recovered numbers from 2026-09-29 until 2026-10-04, when the sweep was re-run at `2.0.0`
and overwrote it. So the entry the checker derives to hold (the one whose
`authoritative_key` commit equals the file's own recorded commit) is now `2.0.0`'s, and
`1.1.0.json`'s headline has become the frozen copy the section above warns against —
not by choice, but because the file it pointed at was reused. `1.1.0.json`'s
`numbers_live_in_note` says so, and the checker picks the entry from the file's commit
rather than from a name typed into the test, so it followed the slot to `2.0.0` instead of
going red on a rename. A future sweep that overwrites the slot again moves it again by the
same rule; what must *not* happen is a new entry claiming a file that holds some other
sweep's numbers, which is the check that keeps this honest.

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

**A bump is not finished until the entry exists.** `tests/unit/test_version.py` binds
`RELEASE_VERSION` to a file in this directory, so the "move the version" step and the
"write the entry" step are one action: bump the number without adding the entry and that
test goes red on the pull request, before the tag is cut. It is satisfied by *an* entry,
never by a particular one — a version with no measurement gets a `recorded: false` stub
with its reason, and the completeness check in
`tests/integration/test_benchmark_provenance.py` still holds the whole directory against
the tags. The obligation exists at the bump rather than the tag because a tag is the last
moment it is cheap: `2.0.0rc3`, `2.0.0rc4` and `2.0.0` each bumped the version and skipped
the entry, and for `2.0.0` the omission was only discovered by a red CI run on an
already-published PyPI version.
