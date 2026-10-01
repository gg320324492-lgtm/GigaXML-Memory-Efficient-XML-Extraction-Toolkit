# What "atomically" means here

gigaxml writes its output to `<target>.tmp` and renames that file onto the target once the
run finishes. That is one sentence, and on its own it is not a promise — it is a mechanism.
A reader who has to work out for themselves how far the mechanism reaches is exactly the
reader this document exists for.

So here is the whole rule: **there are three layers, the first two are promised and the
third is not.**

| Layer | What it promises | Status in 1.2.1 |
|---|---|---|
| **[1](#layer-1--a-failed-run-publishes-nothing)** | A run that fails or is stopped publishes nothing. The target keeps the previous file. | **Promised, and true** |
| **[2](#layer-2--the-target-is-never-half-written)** | The target is either the previous complete file or the new complete one. Never a mixture. | **Promised, and true** |
| **[3](#layer-3--surviving-a-power-cut-is-not-promised)** | After a power cut, what is on the device. | **★ Not promised. See below.** |

★ **Layers 1 and 2 are about a process dying. They say nothing about a machine dying.**
The distinction is the entire content of this document, and the third layer is the one
people infer and the tool does not promise.

The mechanism is the same one used for every file gigaxml publishes by renaming: the
extracted output (CSV, JSONL, Parquet), each checkpoint part, the checkpoint manifest, and
the three GUI stores (settings, recent files, job history). The three layers below apply
to all of them.

---

## Layer 1 — a failed run publishes nothing

**Promised.** If a run fails, raises, is stopped by a signal, or is killed, it does not
publish a partial result. The target path is left holding whatever it held before the run
started — and on a first run, where there was nothing before, **it is left holding no file
at all**. There is no third state in which a target exists but describes a run that did not
finish.

★ **"Keeps the previous file" is not "keeps a file".** A reader checking a target after a
failed first run will find nothing there. That absence is the guarantee, not a gap in it:
the promise is that the target is never something gigaxml published from a failed run.

Where the partial output went, and why it is not deleted: it is still on disk at
`<target>.tmp`, and the run report names that path. Six hours of partial output is worth
keeping, and the name says what it is.

**What this layer does not say.** It says the file is not half-written. It does not say the
file is *right*. A config whose field paths do not match the document produces a complete,
well-formed, empty CSV — complete, and wrong. The signal for "the run produced what it
promised" is the run report's `output_complete` field, not the shape of the file. See
[`RUN-REPORT-FORMAT.md`](RUN-REPORT-FORMAT.md).

## Layer 2 — the target is never half-written

**Promised.** The target is either the previous complete file or the new complete file.
Never a mixture of the two, and never a truncation of the new one. This holds for a reader
that opens the target at any moment, including while a run is publishing it.

Two things make it true, and both are load-bearing:

**The partial file is a sibling of the target.** `writers.py` builds the partial path with
`Path.with_name`, which puts it in the target's own directory by construction — not in
`tempfile`'s directory, which is usually a different device. Measured on this machine:
`os.stat().st_dev` of the target's directory and the partial file's directory are equal, so
the rename is a rename.

★ **★ Same filesystem is a precondition, not a detail.** `os.replace` across devices is not
a rename at all — the implementation copies the bytes and deletes the source, and a crash
during that copy leaves a target that is neither the old file nor the new one, which is
exactly the state layer 2 says cannot happen. Keeping the two paths on one device by
construction is what makes the guarantee unconditional in practice rather than conditional
on a caller.

**The publish is one metadata operation.** After the last batch is flushed, `close()` calls
`os.replace`. Measured on this machine: renaming a 4 MiB file takes **0.253 ms**, and the
cost does not scale with the data — it is a directory entry change, not a copy. Measured
the same way: the target's inode changes across a publish, so the file is genuinely
replaced rather than rewritten in place.

On Windows there is one way layer 2 can fail, and it is reported rather than hidden: if
another program has the target open, `os.replace` fails with `WinError 5`. The error names
the partial file, and says what to do about it. POSIX has no equivalent restriction.

## Layer 3 — surviving a power cut is not promised

**★ Not promised, and not implemented.** gigaxml does not call `os.fsync`, does not open
any file with `O_SYNC`, and makes no claim about what is on the device after the power
goes off. There is not one fsync in `src/` — grep it; the count is zero.

The mechanism in layers 1 and 2 hands bytes to the operating system. It does not ask the
operating system to hand them to the disk. Those are different operations, and only the
second one survives losing power.

★ **★ What this costs you, stated precisely, because the two failure shapes are very
different:**

- **"The output is missing" is NOT what happens.** You will not find a half-written file.
  Layer 2 holds; the file was never in a mixed state at any point.
- **"The bytes were still in the operating system's cache" IS what can happen.** After a
  power cut, the data may never have reached the device.

★ **Which of the old file, the new file, or nothing is on disk afterwards is
filesystem-specific, and this project has not measured it and does not promise it.** The
run report will also be gone or stale, since it is written in place (see below). If a
power cut is a scenario you must survive, the answer is not this tool's output — take a
copy, or re-run.

**Why this is stated as "not promised" rather than quietly left unsaid.** The word
"atomic" invites a reader to supply their own definition, and the definition most people
supply is stronger than the one implemented. Writing down the boundary is what stops that
substitution. A reader who knows layer 3 is not promised can plan around it; a reader who
assumed it will find out during an incident.

★ **A partial implementation would be worse than none, and this is the specific reason.**
On Windows, fsyncing a *directory* is not reachable from Python at all — measured here:
`os.open(directory, os.O_RDONLY)` raises `PermissionError`. So a Windows implementation
could make the *file data* durable and leave the *directory entry* for the new name
un-durable. That is a promise with a hole in the middle, and the hole is the dangerous
kind: a reader told "the output is durable" would reasonably assume the path leads to it.
★ **On POSIX the equivalent is an fsync of the parent directory after the rename. That
specific behaviour is ★ UNVERIFIED here — this is a Windows machine and it was not tested
on macOS or Linux.** It is listed in the checklist below rather than asserted, because
asserting it from memory is exactly what this milestone exists to avoid.

### The same boundary reaches two other promises

- **`--checkpoint-every` / `--resume`.** gigaxml *does* promise that a stopped run can be
  resumed, and that is a real promise about a real interruption. It rests on the same
  committed parts on the same disk, so it inherits layer 3: after a power cut, a part or
  the manifest may not be there. The ordering invariant is deliberately on the safe side
  (a part reaches the disk before the manifest names it, so the manifest never lists a
  part that does not exist), which makes an interrupted resume *detectable* — it is not a
  licence to assume the bytes reached the device.
- **The rejection log** (`rejected.jsonl`) counts lines the same way: the count follows the
  flush, so the number in the run summary always matches what the operating system has.
  Reaching the *device* is layer 3, and is not promised there either.

---

## Where this document does not apply

Stating a boundary honestly means saying where it stops.

| Artefact | How it is written | Why it is not here |
|---|---|---|
| **The run report** (`run-report.json`) | **Written in place, with no `.tmp` and no rename.** | It is a summary of the run, not extracted data, and the run's exit code is what reports whether the data is usable. A summary that cannot be written is a warning, not a failure. The consequence is that after a power cut the report may be truncated or absent — which does not affect the output, whose layers 1 and 2 still held. |
| **The GUI's own stores** | Same `.tmp` + rename as the output, so layers 1 and 2 apply and layer 3 does not. | Nothing to add; listed so the mechanism's scope is not mistaken for a narrower one. |
| **`rejected.jsonl`** | Appended in place, flushed every 256 lines. | See above — layer 3 applies. |

★ **A reader who takes "every output lands atomically" from the README to mean every
artefact this tool writes has read it too widely.** The README's sentence is about the
extracted data, and this document is where that word gets its boundary.

---

## If fsync is ever added

★ **This section is information for a future decision. It is not a to-do list, and
milestone M11 explicitly decided not to implement it.** It is written down so that whoever
revisits the question does not have to re-measure, and — more importantly — so that nobody
later justifies the decision by an estimate that has now been measured.

**★ The cost is not the reason.** Measured on this machine (Windows 11, Python 3.13.14,
NTFS, a 200,000-row CSV through the real writer at the default batch size of 5,000):

| Measurement | Value |
|---|---|
| `os.fsync`, 1 MiB | **2.63 ms** |
| `os.fsync`, 8 MiB | **4.41 ms** |
| `os.fsync`, 64 MiB | **17.42 ms** |
| A run's 40 flushes, fsynced | **≈ 0.03 s, about 14% of write time** |
| The publish that already happens (`os.replace`, 4 MiB) | **0.253 ms** |

So a per-flush fsync would have been affordable at the default batch size. **Anyone who
later says "we did not fsync because it is too slow" is repeating a number this milestone
already measured, and it is not the right number.**

**The reason is that a partial guarantee is a false guarantee.** Before implementing, all
of these must be established on the platform being claimed — not on one of them and assumed
for the others:

1. **Can a directory's entries be made durable on this platform, from this language?**
   **Measured: no, on Windows** (`PermissionError` from `os.open` on a directory).
   ★ **Unverified on macOS and Linux.**
2. **Is the data fsync + the directory fsync together actually enough to keep the new name
   pointing at the new data after losing power?** Unverified everywhere.
3. **What does the filesystem do on `data=ordered` vs `data=writeback`?** Different answers,
   and the mount option is the user's, not this project's.
4. **Does it cost what §above measured on macOS and Linux?** The numbers above are one
   machine's.
5. **What does it do to the throughput claim?** The project publishes measured throughput;
   a durability feature that changes it has to be re-measured, not re-asserted.

★ **Until all five are answered for the platforms being claimed, the correct promise is the
one in layer 3: none.** That is a decision, not an omission.

---

## Where the rest of the promises are written

| Contract | Where |
|---|---|
| CLI commands, flags, exit codes | [`python-api.md`](python-api.md) §3 |
| Config file format | [`CONFIG-FORMAT.md`](CONFIG-FORMAT.md) |
| Run report format, `output_complete` | [`RUN-REPORT-FORMAT.md`](RUN-REPORT-FORMAT.md) |
| Checkpoint manifest and parts | [`CHECKPOINT-FORMAT.md`](CHECKPOINT-FORMAT.md) |
| Error names and what raises them | [`ERRORS.md`](ERRORS.md) |

The three layers above are pinned by `tests/integration/test_output_durability.py`, which
fails if a layer stops being stated — so this document cannot quietly stop being true.