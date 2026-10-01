# The gigaxml Python API

**Everything not named in this document is internal and may change in a minor release.**

That is the whole rule, and it is the one that makes the rest of Stage 3 safe to ship.
M7, M8 and M9 split the CLI, the inspection walk and the GUI into packages — three
refactors that move code between modules, and therefore three chances to break somebody's
import for no reason at all. A written promise about what is *not* public is what turns
those from a hazard into a refactor.

The companion documents are [`CONFIG-FORMAT.md`](CONFIG-FORMAT.md),
[`RUN-REPORT-FORMAT.md`](RUN-REPORT-FORMAT.md), [`CHECKPOINT-FORMAT.md`](CHECKPOINT-FORMAT.md)
and [`ERRORS.md`](ERRORS.md).

---

## 1. The three tiers

| Tier | What it means | What you may rely on |
|---|---|---|
| **stable** | Promised across minor versions. | The name, the signature, the meaning. A change here is a **major** release. |
| **beta** | Importable, documented, with runnable examples — but **not** promised. | That it works today. That a future minor release may move it. |
| **internal** | Not part of any contract. | Nothing. It may move, change shape, or disappear. |

**Beta is not a softer word for stable.** It means: here is the shape, here is code that
runs, and here is exactly where it leans on internals so you can judge whether you are
building on it. The two beta names below are the whole beta tier.

### 1.1 Stable

| Name | Tier | What it is |
|---|---|---|
| `GigaXMLError` | stable | Base class of every error gigaxml raises deliberately. **Catch this one** and branch on the subclasses below. |
| `CheckpointError` | stable | A checkpoint manifest is missing, unreadable, or a format this build does not know. |
| `ConfigError` | stable | The config is missing, unreadable, or does not parse. |
| `FieldPathError` | stable | A field path in the config is malformed. |
| `FieldTypeError` | stable | A value could not be converted to the field's declared type. |
| `InspectionError` | stable | An inspection run could not complete. |
| `InternalError` | stable | gigaxml failed in a way its own code did not expect. **Nothing raises this in 1.2.1** — it exists so the boundary is nameable. |
| `MissingRequiredFieldError` | stable | A `required: true` field had no value in a record. |
| `RecordPathError` | stable | The record path is malformed or matched nothing. |
| `RunInterruptedError` | stable | The run was stopped by a signal. |
| `SchemaError` | stable | The XSD could not be read or applied. |
| `SecurityError` | stable | A path escaped the output directory, or another refusal the tool makes on purpose. |
| `WriterError` | stable | The output could not be written. |
| `ExtractionConfig` | stable | A validated config. Produced by `parse_config` and `load_config`. |
| `load_config` | stable | Read and validate a config file. |
| `parse_config` | stable | Validate a config from an already-loaded mapping. |
| `FieldConfig` | stable | One validated field. **Get these from a config** — the class performs no validation, so build one by hand and nothing checks it. |
| `FieldType` | stable | The value of a field's `type:` key. |
| `StreamingRecordReader` | stable | Iterate the records of a large document in bounded memory. |

### 1.2 Beta

| Name | Tier | What it is |
|---|---|---|
| `extract_record` | beta | Turn one record element into plain Python values. |
| `ExtractionResult` | beta | What `extract_record` returns. |
| `create_writer` | beta | Build the writer for an output path. |
| `RowWriter` | beta | What `create_writer` returns; what you call `write` on. |

★ **Why these two are beta and not stable — the reasoning, not the verdict.** They do
compose into a complete, memory-bounded pipeline, and §2.2 runs it end to end. What keeps
them out of stable is that their shapes are dictated by things the library does not
control: `extract_record` takes an `lxml` element, because that is what the reader yields,
and `RowWriter.write` takes a `Mapping[str, object]` because that is what a CSV row is.
Both signatures move if the reader's or the writer's internals move, and neither is a
decision a caller made.

★ **Two edges, both measured while writing this document, both worth knowing before you
use them.** `ExtractionConfig` spells its field `record_path`, not `record` (guessing
`config.record` raises `AttributeError`), and `RowWriter.write` takes
`result.values` rather than the `ExtractionResult` itself — **passing the result fails at
`close()`, not at `write()`**, so a mistake here surfaces a long way from its cause.

### 1.3 Internal

**Not part of the contract, and their internal layout is not part of it either:**

- **`gigaxml.cli`** — the command-line interface. M7 split it into `gigaxml.cli_pkg/`.
- **`gigaxml.inspect` and `gigaxml.inspection`** — the inspection walk. M8 split it into
  a package, and its internal layout is new as of that milestone.
- **`gigaxml.gui`** — the PySide6 window. **The GUI's layout is not an API** (§4).
- **`gigaxml.checkpoint`**, **`gigaxml.xsd`**, **`gigaxml.run`**, **`gigaxml.progress`**.
- Everything else: `gigaxml.fields` beyond the names in §1.1/§1.2,
  `gigaxml.writers` beyond `create_writer` and `RowWriter`,
  `gigaxml.parser` beyond `StreamingRecordReader`.

★ **`gigaxml.cli`, `gigaxml.inspect` and `gigaxml.gui` internal layout is not part of the
contract, and this sentence is what licensed M7–M9.** Import from them if you like, but a
minor release may move anything inside them, and your code is then out of contract with us
rather than merely unlucky.

Run the tool through its **command line**, which *is* a contract (§3). Importing
`gigaxml.cli.main` to call it in-process is the one exception, and it is what the project's
own tests do.

### 1.4 Deprecated

**Nothing is deprecated in 1.2.1.** A name appears here when it still works and will be
removed in a **major** release, with the replacement named. There are none, because this is
the first release to declare a public API and therefore has nothing to withdraw.

---

## 2. Running examples

Each of these is **runnable as written** — it writes its own document and config — and the
test suite executes every block in this file, so none of them can quietly become
pseudo-code.

### 2.1 The stable surface: read a document, bounded memory

```python
from pathlib import Path
import tempfile

from gigaxml import StreamingRecordReader, GigaXMLError, load_config

work = Path(tempfile.mkdtemp())
(work / "catalog.xml").write_text(
    "<catalog><products>"
    '<product id="1"><name>Alpha</name></product>'
    '<product id="2"><name>Beta</name></product>'
    "</products></catalog>",
    encoding="utf-8",
)
(work / "config.yaml").write_text(
    "record: /catalog/products/product\nfields:\n  id:\n    path: '@id'\n  name:\n    path: name\n",
    encoding="utf-8",
)

try:
    config = load_config(work / "config.yaml")
except GigaXMLError as exc:
    raise SystemExit(exc)

# One record at a time, and nothing to clean up: the reader closes any stream it opened
# itself as the walk ends, and leaves a stream you passed in alone. It is re-iterable, so
# take one iterator and consume that -- slicing the reader itself re-reads from the top.
reader = StreamingRecordReader(work / "catalog.xml", config.record_path, config.namespaces)
for record in reader:
    print(record.tag, len(list(record)))
```

### 2.2 The beta surface: a pipeline you assemble yourself

```python
from pathlib import Path
import tempfile

from gigaxml import (
    ExtractionConfig,
    StreamingRecordReader,
    create_writer,
    extract_record,
    load_config,
)

work = Path(tempfile.mkdtemp())
(work / "catalog.xml").write_text(
    "<catalog><products>"
    '<product id="1"><name>Alpha</name><price currency="USD">1.50</price></product>'
    '<product id="2"><name>Beta</name><price currency="EUR">2.50</price></product>'
    "</products></catalog>",
    encoding="utf-8",
)
(work / "config.yaml").write_text(
    "record: /catalog/products/product\n"
    "fields:\n"
    "  product_id:\n"
    "    path: '@id'\n"
    "  price:\n"
    "    path: price\n"
    "    type: float\n",
    encoding="utf-8",
)

config: ExtractionConfig = load_config(work / "config.yaml")
writer = create_writer(work / "out.csv", config.fields)
reader = StreamingRecordReader(work / "catalog.xml", config.record_path, config.namespaces)
for record in reader:
    # ★ `.values`, not the ExtractionResult -- see §1.2.
    writer.write(extract_record(record, config.fields).values)
writer.close()
print((work / "out.csv").read_text(encoding="utf-8"))
```

### 2.3 Errors

```python
from gigaxml import ConfigError, FieldTypeError, GigaXMLError, RunInterruptedError


def run_safely(operation):
    """One handler for everything gigaxml raises on purpose."""
    try:
        return operation()
    except RunInterruptedError:
        # Ctrl-C or another signal: this was going fine, pick it up where it stopped.
        raise
    except FieldTypeError:
        # A value would not convert. The message says which field and what it got.
        raise
    except ConfigError as exc:
        raise SystemExit(f"the config is wrong: {exc}")
    except GigaXMLError:
        # Everything else the tool refuses on purpose, by name.
        raise
```

**Catch `GigaXMLError` first if you catch it at all** — it is the base of all of the above,
and a clause above it makes the ones below unreachable.

---

## 3. What counts as a contract

These are the promises a release makes. Everything in §1.1 is one; so is each of these:

| Contract | Version | Where it is specified |
|---|---|---|
| **CLI command names** — `extract`, `inspect`, `sample`, `generate` | stable | `gigaxml --help` |
| **CLI flags that are already documented** | stable | `gigaxml <command> --help`, and this project adds a flag only with a deprecation first |
| **Config file format** | **v1** | [`CONFIG-FORMAT.md`](CONFIG-FORMAT.md) |
| **Run report format** | **v1** | [`RUN-REPORT-FORMAT.md`](RUN-REPORT-FORMAT.md) |
| **Checkpoint manifest format** | **v1** | [`CHECKPOINT-FORMAT.md`](CHECKPOINT-FORMAT.md) |
| **Exit codes** — 0 ok, 1 bad input, 2 argparse usage, 3 interrupted, 4 internal | stable | [`ERRORS.md`](ERRORS.md) |
| **Public Python API** | see §1 | this document |
| ~~**GUI layout**~~ | **not an API** | see below |

### 3.1 What SemVer means here

- **patch** (`1.2.1` → `1.2.2`) — a bug is fixed or a security hole is closed. **No**
  contract changes. A config that loaded before still loads; a flag that existed still
  exists; an output that was correct is still correct.
- **minor** (`1.2.1` → `1.3.0`) — a backwards-compatible feature is added: a new command,
  a new documented flag, a new field in the JSON output that older readers ignore. Nothing
  in §3 changes shape, and nothing in §1.1 is removed.
- **major** (`1.2.1` → `2.0.0`) — a contract becomes incompatible. This is the only kind of
  release that may break a stable name, a documented flag, or a config/run-report format.

★ **Config, run report and checkpoint formats are versioned separately from the package**,
and this is deliberate: a format version is a promise about **files on disk**, which outlive
any particular release of the program that reads them. A config written by 1.2.1 must load
in 2.0.0, or the version number in the file is a lie.

### 3.2 The GUI layout is not an API

**The window's widgets, their arrangement, its menus and its tab order are not a contract
and may change in a minor release.** They belong to Qt, and Qt itself does not promise them
across versions. Nothing in this project should depend on them, and if you find yourself
reading `gui/panels/execution.py` to automate the window, what you want is the CLI (§3).

---

## 4. Where the stable names live, and why `import gigaxml` stays cheap

| Name | Module of record |
|---|---|
| the 13 error classes | `gigaxml.errors` |
| `ExtractionConfig`, `load_config`, `parse_config` | `gigaxml.config` |
| `FieldConfig`, `FieldType`, `ExtractionResult`, `extract_record` | `gigaxml.fields` |
| `StreamingRecordReader` | `gigaxml.parser.streaming` |
| `create_writer`, `RowWriter` | `gigaxml.writers` |

Import them from **`gigaxml`** — that is the stable spelling, and it is the one that will
keep working. The modules above are where the definitions live today; they are listed so
you can find the documentation and the docstrings, **not** so you can import from them.

★ **Why the top-level import is not simply eager.** Measured on the machine this document
was written on: `gigaxml.errors` costs 2.0 ms and loads no third-party module, while the
pipeline modules cost 30–54 ms each and pull in `lxml` (and `yaml`, for configs). So the
error classes are imported directly and the ten pipeline names resolve on first use
through [PEP 562](https://peps.python.org/pep-0562/). `import gigaxml` stays at single-digit
milliseconds and pulls in no third-party package, while `from gigaxml import
StreamingRecordReader` still costs exactly what it always cost. Both halves of that trade
are asserted in `tests/unit/test_public_api.py`.