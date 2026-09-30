# gigaxml run report format

The machine-readable summary `gigaxml extract` and `gigaxml sample` write — to
`--report PATH`, or by default to `run-report.json` beside the output. It is written
**on success, on failure and on interruption**, so a caller can tell a complete output
from a partial one without parsing stderr.

## Versioning

```json
"schema_version": 1
```

| | |
|---|---|
| Type | integer |
| Required | **Yes**, in every report this build writes |
| Today's value | `1` |

`schema_version` describes the *shape* of the report. `tool_version` describes the tool
that wrote it — a patch release changes `tool_version` without changing the format, and
a format change is now visible on its own.

> **`schema_version` is not `version` in a checkpoint manifest.** They are independent
> contracts that move at their own pace: this file describes any run, a manifest
> describes one checkpointed run. See [`CHECKPOINT-FORMAT.md`](CHECKPOINT-FORMAT.md).

## Fields present in every report (24)

| Field | Type | Meaning |
|---|---|---|
| `schema_version` | integer | this format's version; see above |
| `status` | `"ok"` \| `"failed"` \| `"interrupted"` | three states, not two — a stopped run is neither a success nor a failure |
| `source`, `output` | string | paths as given |
| `format` | string | `csv` / `jsonl` / `parquet` |
| `record_path`, `fields` | string, array | what was extracted |
| `rows`, `rejected` | integer | written vs quarantined |
| `rejected_path` | string \| null | where the rejection log is |
| `error` | object \| null | `{type, message}` for a failed run, `null` otherwise |
| `output_complete` | boolean | true only once the finished file is actually in place |
| `partial_path` | string \| null | the `.tmp` a failed run left behind, if any |
| `elapsed_seconds`, `peak_rss_mb`, `throughput_records_per_s` | number \| null | measurements |
| `started_at`, `finished_at` | string (ISO 8601) | |
| `records` | object | `{accepted, rejected, seen}` |
| `config_hash` | string | hash of the parsed config |
| `environment` | object | `{python, python_implementation, os, os_release, machine}` |
| `input_identity`, `output_identity` | object \| null | `{path, size, sha256}`, or `null` when the file could not be read |
| `tool_version` | string | the tool's version, independent of `schema_version` |

**A field that cannot be measured is `null`, never `0` or `""`.** A zero-filled identity
would read as "this file has no content"; `null` reads as "this file could not be
identified", which is the only honest shape for it.

## Optional fields (added conditionally)

| Field | Present when |
|---|---|
| `input_identity_error` | the input could not be hashed (missing file, reading stdin) |
| `output_identity_error` | the output could not be hashed — a failed run, or a checkpoint directory whose parts directory is not a file |
| `checkpoint` | the run used `--checkpoint-every` |

Field counts as actually measured, all carrying `schema_version`:

| Scenario | Fields |
|---|---|
| successful `extract` or `sample` | 24 |
| failed run (`output_identity_error` present) | 25 |
| successful `--checkpoint-every` run | 26 |
| missing input (`input_identity_error` present) | 26 |

## Unknown keys

**As a consumer: tolerate them.** This tool adds fields — `checkpoint`,
`input_identity_error` and `output_identity_error` are each examples that once did not
exist — and each addition has so far raised the count without removing anything. Reading
only the keys you need, rather than asserting the complete set, is what keeps your code
working across releases.

**As a producer: this build writes only the keys listed here**, plus the conditional
three above.

## Unsupported versions

A consumer seeing `schema_version` greater than its own knows it is reading a report
from a newer format and can fall back to the fields it recognises.

**This tool never reads its own reports back** — no code path in `gigaxml` parses a run
report — so a report with an unexpected `schema_version` cannot cause a wrong action
here. It is your consumer code that should decide.

## Compatibility and upgrade

**No migration mechanism exists.** Version 1 is the first version; there is no older
report to convert and no tool that converts one.

What has changed so far is *addition*: fields have been added to this format during the
project's history, and none has been removed or repurposed. That is an observation
about the past, **not a promise about the future** — a future change that breaks
compatibility would raise `schema_version`, and that is exactly why the field exists.
