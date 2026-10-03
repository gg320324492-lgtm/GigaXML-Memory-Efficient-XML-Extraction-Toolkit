# Versioning

What this project promises not to change from 2.0 on, and what "promises" means here.

**A stability table nobody checks is a wish.** So every row below names the check behind
it, and where the honest answer is "nothing, by design", that is what the row says. Two
of the promises in this table are checked *against the code that shipped in 1.2.1*
rather than against this build's memory of it; those are measured further down, with the
numbers.

This file lives at the repository root rather than in `docs/`, because `docs/` is
gitignored: a stability policy that is not in the repository is a stability policy the
next maintainer never reads.

## The layers

| Layer | Stability | What counts as breaking it | What checks it |
|---|---|---|---|
| CLI commands and options | **stable from 2.0** | a command or an option disappearing, being renamed, changing from taking a value to not taking one, or changing from required to optional | `tests/unit/test_cli_surface.py` |
| Exit codes | **stable from 2.0** | a documented code changing what it means, or a new failure returning a code that already means something else | `tests/integration/test_exit_codes.py`, [ERRORS.md](ERRORS.md) |
| The config format, at `version: 1` | **stable from 2.0** | a config gigaxml 1.2.x accepted being refused; `version:` accepting anything but the integer `1` | `tests/unit/test_config.py`, [CONFIG-FORMAT.md](CONFIG-FORMAT.md) |
| The run report's fields | **stable from 2.0** | a top-level key disappearing or being renamed; a key's type changing | `tests/golden/test_cli_golden.py`, [RUN-REPORT-FORMAT.md](RUN-REPORT-FORMAT.md) |
| The checkpoint directory format | **stable from 2.0** | a checkpoint gigaxml 1.2.x wrote being refused by `--resume` | `tests/integration/test_checkpoint_cli.py`, [CHECKPOINT-FORMAT.md](CHECKPOINT-FORMAT.md) |
| The public Python API | **stable from 2.0** | a name in `gigaxml.__all__` disappearing, moving, or losing an annotation | `tests/unit/test_public_api.py`, [python-api.md](python-api.md) |
| Everything else in Python | **internal** | nothing — it may change in any release, and does | nothing, by design |
| **The desktop window's layout** | **not an API** | anything, without notice | nothing, by design |

The last row is the one people ask about, so it is worth saying why: a window is a
picture, and a picture that cannot be rearranged is a window nobody improves. Where the
window is promised something, it is the *behaviour* — that the window never holds your
document and that the extraction runs in a child process — not the arrangement of the
controls that reach it.

## What "stable" does not mean

- **It does not mean bugs go unfixed.** A fix is not a break. `2.0.1` exists to be a
  release with no new features in it.
- **It does not mean nothing is added.** New commands, new optional flags, new report
  fields and new config keys are additions, and additions are how a tool improves. Only
  removals, renames and silent changes are breaks.
- **It does not mean help text is frozen.** Rewording a help string is not a
  compatibility change, and freezing it would make every wording fix a policy question.
- **It does not mean default values are frozen.** That is a judgement, and it is stated
  rather than assumed: a default is what a user depends on most and changes least, so
  changing one deserves more care than "the test allows it". A default that changes
  should be in the release notes under what changed.

## The two claims that were measured against 1.2.1

Both of these are claims about artefacts **1.2.x produced**, so a test that runs 2.0
against itself would prove nothing. Both were measured with the code from the `v1.2.1`
tag, in a separate checkout, and the version each run reported was asserted before the
result was recorded.

| Claim | How it was checked | Result |
|---|---|---|
| A config 1.2.x wrote loads on 2.0 | 1.2.1 ran `inspect --generate-config` on a real document; 2.0.0rc1 then extracted with the file 1.2.1 had written | 5,000 rows, and the two outputs are **byte-identical** (`sha256 d17e7e2c…`) |
| A checkpoint 1.2.x wrote resumes on 2.0 | 1.2.1 ran 600,000 records with `--checkpoint-every`, was killed at 272,000, and 2.0.0rc1 resumed the same directory | resumed from exactly 272,000, read the remaining 328,000, **all 600,000 arrived** |
| The run report only gained fields | both versions wrote a report for the same input and the two key sets were compared | 23 keys → 24, **none removed**, one added (`schema_version`) |

The generated config carries no `version:` key at all, which is documented behaviour
rather than an accident: a config without one is this project's own format from before
versioning and loads exactly as it always has. The explicit `version: 1` spelling was
exercised separately, and `version: 2` is still refused.

## Where a version number lives

Four places, and all four have to agree or the package ships three answers to "what
version is this":

| Where | What it is |
|---|---|
| `pyproject.toml` | the package metadata every artefact carries |
| `src/gigaxml/__init__.py` | what `--version` prints and what the window shows |
| `tests/unit/test_version.py` | `RELEASE_VERSION` — "what are we releasing", written down once |
| `packaging/RELEASE_NOTES.md` | the version a downloader reads first |

`tests/unit/test_version.py` makes disagreement a test failure rather than a support
question.

**★ Replacing the number everywhere is wrong here, and this repository is a good example
of why.** `git grep 1.2.1` also finds the benchmark history, the Windows version
resource, the SemVer examples in `python-api.md`, and the line in
`REAL-WORLD-VALIDATION.md` recording which build produced those measurements. A
`sed -i 's/1\.2\.1/2.0.0/g'` would rewrite a measurement into a claim that 2.0 produced
it, and would rename a historical benchmark record. Change the four places; leave the
rest, and `git grep` afterwards to see that the history is still there.

## Release candidates

The candidates `2.0.0rc1` through `2.0.0rc4` are the versions 2.0 was rehearsed under,
and the classifier is restored to `Development Status :: 5 - Production/Stable` now that
2.0.0 has shipped — a classifier is written by hand, never recomputed from the version,
so it is a step of its own. That changes nothing in the table above: the promises are
the promises, and they hold for every build that has carried the 2.0 number. What the candidates were for is the artefacts — the installer,
the `.dmg`, the AppImage — which are the one thing a checkout cannot stand in for.

**`2.0.0` is where the classifier was moved to match the table.** The release is what the
promises were waiting for, and its classifier now reads
`Development Status :: 5 - Production/Stable`. That move was made by hand, for the reason
above: the classifier is not recomputed from the version, so a reader should treat it as a
label someone maintains deliberately rather than as something derived from the number.

`package.yml` takes its version from `pyproject.toml` rather than from the tag, so a tag
builds the version in the tree. The tag itself, the merge, and the upload are a person's
step and are not automated for that reason.

## Related

- [python-api.md](python-api.md) — which names are public, and the layering it already
  used before this file existed
- [ERRORS.md](ERRORS.md) — the exit codes, which are a stable surface
- [CONFIG-FORMAT.md](CONFIG-FORMAT.md), [RUN-REPORT-FORMAT.md](RUN-REPORT-FORMAT.md),
  [CHECKPOINT-FORMAT.md](CHECKPOINT-FORMAT.md) — the three formats whose stability is
  promised above