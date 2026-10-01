# Benchmark methodology

Every performance number this project publishes has to answer one question: **where did
it come from?** Not "it looked reasonable" and not "the README says so" — a commit, a
machine, a document, a config, and the individual runs behind the summary.

This file is at the repository root on purpose. `docs/` is gitignored here, so a document
written there is a document nobody cloning the repository can see — the same point
[`REAL-WORLD-VALIDATION.md`](REAL-WORLD-VALIDATION.md) makes about its own contents, and
the reason [`SECURITY.md`](SECURITY.md) links to a policy note rather than writing one in
a directory nobody sees.

Three questions get answered below, and each of them has a check behind it in
[`tests/integration/test_benchmark_provenance.py`](tests/integration/test_benchmark_provenance.py).
**Each of those checks is itself checked**: every one of them runs against a deliberately
doctored copy of the data and fails if the check stops noticing. A guard that has never
been seen to go red is a guard of unknown strength.

---

## 1. The identity block

Every results file carries `schema_version` and an `identity` object. The fields, and
where each one comes from:

| Field | Source | Why it is in the block |
|---|---|---|
| `schema_version` | `provenance.SCHEMA_VERSION` | Distinguishes "this file predates the block" from "this file has holes in it". Two different problems. |
| `git_commit` | `git rev-parse HEAD` | **40 characters, never a branch name.** A branch moves; the commit does not. "Measured on main" is worth nothing the day after. A dirty tree is recorded in the string, because then the commit is a lower bound. |
| `gigaxml` | `gigaxml.__version__` | Read from the **package**, not from `importlib.metadata`. On an editable install the distribution reports whatever `pip install -e` recorded last, which is how a sweep of 1.1.0 recorded itself as 0.1.0. |
| `python` | `platform.python_version()` | 3.11 and 3.13 differ by more than most of the numbers here. |
| `lxml` | `lxml.etree.__version__` | The parser is the product's performance. |
| `os` | `platform.system()` + `release()` | |
| `cpu`, `machine` | `platform.processor()`, `platform.machine()` | |
| `memory_total_gb` | `psutil.virtual_memory().total` | Degrades to `"unavailable"` rather than raising. |
| `config_sha256` | the **git blob** of the config, or the sha256 of an inline config literal | See below — this one has a trap in it. |
| `config_sha256_source` | a sentence saying which of the two | |
| `dataset_sha256` | sha256 of the bytes that were read, plus the `generate` command and seed | The document a number came from. |
| `memory_method` | which counter answered, and who read it | Section 4. |

**Every probe degrades to a recorded string instead of raising.** An identity block that
failed because `git` was missing would turn a missing fact into a failed measurement,
which is the wrong trade in the other direction.

### The config hash is taken over the git blob, not the working tree

This repository has `core.autocrlf=true`. A fresh clone of
`benchmarks/compare/gigaxml-config.yaml` is **524 bytes of CRLF**; the committed blob is
**512 bytes of LF**; their sha256 values differ:

```
blob    512 B  b7b51aa97e7c7ff52c7de4ef8ce0c93bab47a1bc6cdcc68452b668e146f9d647
clone   524 B  ee8035a80c83856b20715c19201304f283d85d00f32f35a0d00ebcb5bb170bb1
```

A `config_sha256` taken from the working tree is therefore true on the machine that took
it and false on every other one. That is worse than recording nothing, because it looks
like an identity and fails verification on the first person who checks.

This is the same trap three times over in this project: `.gitattributes` in M14, a scratch
copy of `fhir-all.xsd` in M16, and here — where it lands directly on the thing criterion A
asks for. The general form: **on a repository with `core.autocrlf=true`, the working tree
is not the file, and hashing the working tree produces a number about your checkout rather
than about the project.**

`benchmarks/perf_baseline.py`'s config is the other case: a 253-byte string literal in the
source, with no working-tree/blob split at all, so it is hashed directly. That one is
portable by construction, and the field says so.

---

## 2. Raw runs, and summaries that follow from them

Every data point stores **all five individual runs** — wall time, peak memory, exit code,
rows written, and the memory method — beside the summary. The summary is not a separate
claim; it is a function of the runs, and
`provenance.recompute_summary()` rebuilds it independently.

**All twelve data points in `benchmarks/compare/results.json` recompute exactly.** The
definitions are `run_comparison.py`'s, and three of them are worth stating because a
reader who assumes otherwise will "find" a bug that is the code working as written:

* **`p95` is the maximum at five repeats.** `p95_index = round(0.95 * (n - 1))` is `n - 1`
  when `n` is 5. There is no interpolation.
* **`records_per_s_median` divides by the *rounded* median.** The rounding is visible in
  the file, so recomputing from the raw median disagrees in the last digit on long runs and
  would read as tampering.
* **Only runs with `exit_code == 0` contribute.** One data point has none.

### The one field that cannot be recomputed from the runs

`complete_output` compares `rows_written` against `rows_in_document`, and the second is
the generator's **expected** row count — an input to the check, not a measurement. So
`recompute_summary` takes it as a parameter.

It is load-bearing rather than decorative. It is what caught **pandas at 4 GB writing
10,485,760 of 11,915,264 rows and exiting 0** on all five repeats: 1,429,504 rows short,
`exit_code: 0`, and a wall clock that would have scored it as a slow success.

### A peak that was never measured

`peak_rss_mb()` returns `None` on a platform that cannot report a high-water mark, and
`is_significant()` reports the memory comparison as *not made* with a note saying which
side was missing. Filling it with `0.0` would make every later comparison read as a large
improvement. A missing measurement has to look missing.

---

## 3. Two thresholds, two purposes, never mixed

| | Used for | Throughput | Memory |
|---|---|---|---|
| **Formal comparison** | two runs on a controlled machine | significant past a **−20%** drop | significant past a **+25%** rise |
| **CI** | one job on a shared runner | fails below **60%** of the recorded reference | fails above **60 MiB** absolute |

They are not substitutes. The CI number is loose *because* a shared runner is a different
computer from the last one and a neighbouring job moves the figure; a tighter gate there
is red within a week for reasons that have nothing to do with the code, which is the
failure A11 exists to prevent. The formal numbers are for a machine you control, where a
20% move is a real change worth investigating.

★ **A documented inconsistency, recorded rather than silently reconciled.** `benchmarks/README.md`
and the code say 60%; the comment on the `performance-baseline` job in
`.github/workflows/test.yml` and roadmap item A11 say 50%. The code is what runs, and it
enforces 60% — which is a **40% drop**, not a 50% one. M17 did not change the threshold;
this is a documentation fix for M19.

---

## 4. How memory is measured, and how that is enforced

**The rule: the peak is read inside the process that does the work.** A parent reading a
live child does not get a slow reading, it gets a *wrong* one. Measured on this machine,
2026-10-02, on a child that allocated 800 MiB and read its own high-water mark:

```
child's own peak_wset   817.9 MiB
parent's view, 5 samples   4.1, 4.1, 4.1, 4.1, 4.1 MiB
```

That 4.1 MiB is the frozen figure `run_comparison.py`'s own comment says a comparison
sweep once reported *for every implementation at every size*. The two methods differ by
813.8 MiB on an 800 MiB allocation, which is what makes it possible to write a check that
distinguishes them.

### The method is reported, not asserted

`run_comparison.py` used to write `"peak_rss_method": "peak_wset (self-read in child)"` as
a **hardcoded string**. Its wrapper is:

```python
peak = getattr(info, "peak_wset", None) or info.rss
```

— a real fallback, for a platform whose psutil build has no `peak_wset`, that returns
*current* RSS. A run that had quietly fallen back would have been filed as a peak
measurement, and nothing would have noticed.

The wrapper now has the child print **which counter answered**, and the recorder stores
that. The measurement is unchanged — same counter, same process, same moment — but the
record says which one it was:

```
__GIGAXML_PEAK__421892096 peak_wset
```

### What enforces it

`test_the_comparison_harness_records_the_peak_the_worker_read` runs the real
`run_once()` against a child that allocates 384 MiB and asserts the recorded peak exceeds
200 MiB. A parent-read harness would report ~4 MiB and go red. The test's own mutation —
feeding the threshold the recorded 4.1 MiB — is asserted to be rejected, so the threshold
cannot be widened into meaninglessness unnoticed.

`perf_baseline.py`'s claim is verified structurally instead: the `PROBE` is handed to
`python -c` and calls `gigaxml.run.peak_rss_mb()` there, and the test asserts `run_once`
references none of `psutil`, `memory_info`, `getrusage`, `PeakWorkingSet` or `VmHWM` — the
parent must not touch a memory API at all.

★ **What the frozen-figure probe does and does not separate.** It distinguishes *whose
process read the counter*, which is what criterion F is about. It does **not** distinguish
*peak* from *current*: in that probe the child's `peak_wset` and its `rss` coincide,
because the reading is taken while the allocation is still held. Both distinctions are
worth having; only the first is what these guards rest on.

---

## 5. Where the performance check runs, and why that is right

`benchmarks/perf_baseline.py` runs in **exactly one CI job**, on `ubuntu-latest`, with no
matrix. `tools/ci_selfcheck.py shape` asserts all three of those properties statically, and
the test suite runs that command against a doctored workflow with an OS matrix to confirm
it goes red.

The reason is measurement, not preference. On the same 2 MB document:

| | Throughput |
|---|---|
| this machine, Windows 11, 2 MB | **43,212 rec/s** |
| shared ubuntu runner, 10 MB (the recorded reference) | **19,312 rec/s** |

Cross-platform throughput numbers are not merely imprecise, they differ by more than a
factor of two. A gate that compared them would measure the computers.

**This is why M16's Windows and macOS legs do not extend here.** They run the same test
directories; `performance-baseline` stays single-platform. Widening it would produce N
runs, each on a different machine, compared against one reference.

The one performance signal in CI is deliberately loose. Its failure is invisible to every
other test in the repository: `benchmarks/README.md` records that a per-record loop doing
no useful work and changing no output at all — byte-for-byte identical CSV, so all
functional tests stay green — dropped throughput to 51% of the reference and turned the
check red. **That figure is the README's claim from when the baseline was written, not a
measurement M17 took**; M17 did not re-run it, because reproducing it means editing the
product's hot path on purpose, and the brief puts a 4 GB-scale re-measurement out of
scope. It is the reason the check exists, and it is worth re-verifying before anyone
treats the 60% floor as arbitrary.

---

## 6. `benchmarks/history/` — including the versions nobody measured

One entry per released version, and **`recorded: false` is a real answer**:

```
0.9.0  2026-09-27   no record — no benchmark result existed yet
1.0.0  2026-09-28   no record — the first results file post-dates this tag by 4 hours
1.1.0  2026-09-28   RECORDED, and recovered after the fact
1.2.0  2026-09-30   no record — the last measurement was 1 day 20 min earlier
1.2.1  2026-10-01   no record — the last measurement was 2 days earlier
```

Each `false` entry carries the commit dates that establish the absence, so "there is no
record" is a checkable claim rather than an assurance. **No entry contains a number that
was not measured.** A test walks every unrecorded entry and fails if any numeric value
appears in one.

### Two honest gaps in the one record that exists

`benchmarks/compare/results.json` was recorded on 2026-09-28 without any of this, and
**two of the three things it now needs are unrecoverable**:

* **The dataset identity is gone.** `data/b100m.xml` and its two larger siblings were
  deleted. `run_comparison.py` never hashed them and never recorded the `generate` command
  or its seed. `gigaxml generate` *is* byte-reproducible at a known seed — measured, two
  runs are identical — but an unrecorded seed cannot be guessed from a digest. This is the
  one field the invariant cannot supply, and it is why both scripts now record the seed and
  the digest together.
* **The tool version in the file is wrong.** It says `gigaxml: 0.1.0`; the tree was at
  1.1.0. Commit `e837d7c` fixed exactly that reading, five and a half hours after the
  sweep ran. The wrong value is left in place and the corrected one recorded beside it,
  because that is what was written down at the time.

The commit **is** recoverable, by a method worth stating: the sweep's own `recorded_at` is
`2026-09-28T19:07:29+00:00`, and the commit timeline has a gap spanning that moment —
`82e31eb` at 18:25 local the previous day, `53c5b21` at 08:39 the next. Only one commit can
have been HEAD in between. **That is a lower bound, not a proof:** the working tree at that
moment need not have matched it.

### The version string is not the key

`benchmarks/history/1.1.0.json` is named for the version string at recording time and
records `"1.1.0"` — while the tree it measured was **six commits past the `v1.1.0` tag**,
with `pyproject.toml` reading `1.1.0` at both. It did not measure the 1.1.0 release. The
commit is the key; the file says so, and a test verifies the offset against the repository
rather than trusting the sentence.

---

## 7. Re-recording, and one thing not to do

```bash
python benchmarks/perf_baseline.py --update    # CI reference: run in the job, not a terminal
python benchmarks/perf_baseline.py             # check against it
python benchmarks/compare/run_comparison.py --sizes 100MB --repeats 5
```

**Do not re-record `perf-baseline.json` from a development machine.** A reference taken on
a desktop asserted a 42,246 rec/s floor against a runner that sustains 19,312 and turned
the guard red on unchanged code. The check runs on the shared runner, so the reference has
to be right about that machine.

★ **A fact about the committed baseline, established by comparing key sets.** The script
has written `rates`, `peaks` and `measured_on.machine` at every commit that has existed,
and has never written `measured_on.runner` or `measured_on.note`. The committed file has
`runner` and `note` and lacks the other three, and nothing else in the repository writes
that path. So it **was not produced by `--update`** — its 19,312 rec/s has no run behind
it in this repository. That does not establish that no run ever happened; someone may have
run the script, read the numbers it printed, and written them up with a careful note. What
is established is the narrower claim that matters, and the file records it. Until the next
`--update` from the job, the check prints a notice on every run saying its reference
cannot be recomputed, and the build stays green — a red for a bookkeeping reason on
unchanged code is the failure mode A11 exists to prevent.

The next `--update` closes it: the script writes `schema_version`, the identity block, the
generator's seed and the document's digest, and every individual run's rate and peak.
