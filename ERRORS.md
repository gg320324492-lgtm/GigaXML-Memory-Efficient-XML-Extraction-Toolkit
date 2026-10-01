# gigaxml errors and exit codes

How a failure is reported, and what to do about it. Every deliberate failure is one
line on stderr and a non-zero exit code; nothing in gigaxml prints a Python traceback
for a problem with your input.

## Exit codes

| Code | Meaning | What a script should do |
|---|---|---|
| `0` | success | continue |
| `1` | **bad input, config, command or environment** | fix the input; do not retry unchanged |
| `2` | malformed command line | fix the invocation. **This one is argparse's**, not gigaxml's. |
| `3` | **the run was interrupted** (SIGINT, or SIGTERM on POSIX) | nothing is wrong — resume it, or run it again |
| `4` | **gigaxml failed unexpectedly** | this is a bug in gigaxml; report it |

**`1`, `2` and `0` have meant what they mean since the first release and are unchanged.**
Newer codes were added beside them rather than by giving old ones new meanings, so a
script written against any earlier version behaves the same.

### Why `3` is not `1`

A run stopped by Ctrl-C and a run that failed are opposite situations, and a script has
to treat them differently: after an error, running the same command again produces the
same error; after a stop, the run was going fine and picking it up is safe. Under one
code the script would have to read stderr to tell them apart.

An interrupted run still writes its report, saying `interrupted` and how far it got.

### Why `4` exists

`4` is reserved for failures inside gigaxml — the case where nothing is wrong with your
document, config or command. Nothing raises it today by design; it is the code a script
can check to distinguish "our bug" from "your input".

By default the message is one line and says so, without a traceback. Set
`GIGAXML_DEBUG=1` to get the full traceback, which is what a bug report needs:

```bash
GIGAXML_DEBUG=1 gigaxml extract ... 2> report.txt
```

## Error classes

Every error gigaxml raises deliberately descends from `GigaXMLError`, so `except
GigaXMLError` covers all of them. All of these also descend from `ValueError` except
where noted, because that is what they are: a value was handed over that the library
cannot use.

```
GigaXMLError
├── ConfigError             the config is malformed
├── RecordPathError         the record path cannot be resolved or matches nothing
├── FieldPathError          a field path is not a valid relative path
├── FieldTypeError          a value could not be converted to its declared type
├── MissingRequiredFieldError   a field marked required was absent
├── InspectionError         inspect cannot produce what was asked
├── CheckpointError         a checkpointed run cannot be continued as asked
├── WriterError             an output could not be set up or written
├── SecurityError           a resource was refused by one of gigaxml's own policies
├── SchemaError             an XSD could not be used
├── RunInterruptedError     the run was stopped by a signal   (not a ValueError)
└── InternalError           gigaxml failed unexpectedly       (not a ValueError)
```

`RunInterruptedError` and `InternalError` are deliberately **not** `ValueError`: no
value was handed over that could have been rejected.

### What to do about each

| Error | Meaning | Action |
|---|---|---|
| `ConfigError` | the config is malformed, or has a key that is not recognised | fix the config. Unknown keys are refused on purpose — a typo would otherwise silently produce wrong data. |
| `RecordPathError` | the record path matched nothing | check the path against the document, and supply `namespaces:` if the document uses one. |
| `FieldPathError` | a field path is not a valid relative path | remove a leading `/`, `..`, a predicate or a wildcard. |
| `FieldTypeError` | a value could not be converted | fix the value, or change the field's declared `type:`. |
| `MissingRequiredFieldError` | a required field was absent | fix the document, or drop `required: true`. |
| `InspectionError` | `inspect` found no usable candidate | check the document; it may genuinely have no repeating structure. |
| `CheckpointError` | the checkpoint cannot be resumed | the manifest is missing, unreadable, or describes a different run. Start a fresh run, or resume against the source and config it was made from. |
| `WriterError` | the output could not be written | check the path and permissions. For Parquet, install the extra: `pip install "gigaxml[parquet]"`. |
| `SecurityError` | gigaxml refused on purpose | nothing is broken. The message names the rule — a schema reaching outside its directory, or an output about to overwrite its input. Change the path. |
| `SchemaError` | the XSD could not be used | check the schema file and that it declares the record path. For XSD support, install the extra: `pip install "gigaxml[xsd]"`. |
| `RunInterruptedError` | stopped by a signal | nothing is wrong; resume or re-run. |
| `InternalError` | a bug in gigaxml | report it, with `GIGAXML_DEBUG=1` output. |

### A note on `SecurityError` and `SchemaError`

These two are new and are subtypes of existing behaviour, not changes to it. Both
descend from `GigaXMLError`, so code written before they existed catches them exactly
as it did; the subtype only lets a caller tell *why* something was refused without
parsing the message. A schema refused because it reached outside its directory is a
`SecurityError`; a schema that simply will not compile is a `SchemaError`.

## Where to report a bug

**There is no reporting address yet.** This document will carry one when there is one;
until then, keep the `GIGAXML_DEBUG=1` output — it is the thing a report needs.
