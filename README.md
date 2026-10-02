# GigaXML

Extract structured data from multi-gigabyte XML files **at constant, bounded memory** — as
a command line and as a desktop application.

**If your extraction task is fixed and small, write the twenty lines of `lxml` yourself.**
That is the right call, and the [measured comparison](#gigaxml-vs-alternatives) says so
before anything else: a hand-written script is about **1.5× faster** than this toolkit,
and no amount of toolkit changes that. What is left over is the part you would otherwise
have to build — on a **4.05 GiB** file of **11,915,264** records, GigaXML peaks at
**33.7 MiB, 5.1 MiB above its own baseline, and the increase does not move when the input
quadruples.**

## Install and run

```bash
pip install gigaxml                                                   # "gigaxml[gui]" adds the app
gigaxml inspect big.xml --generate-config config.yaml --infer-types  # propose a config
gigaxml extract big.xml -c config.yaml -o out.csv                     # extract
```

Output is **CSV, JSONL or Parquet** — `--format`, one writer and one code path — written in
parts and moved into place atomically, so a failed or cancelled run leaves no half-written
file behind. Fields, types and the output format live in `config.yaml` rather than in code,
and `inspect` writes a starting one for a document it has never seen.

## Why GigaXML?

Each of these is a behaviour, not an adjective — the last section of
[the comparison](benchmarks/compare/REPORT.md) shows each one happening:

- **Bounded memory, with a harness that can fail.** 4 GB at 33.7 MiB peak (5.1 MiB over
  baseline), measured by [`benchmarks/bench_extraction.py`](benchmarks/bench_extraction.py)
  in a subprocess — with a reversed test that commits the mistake (loading the document)
  and requires it to blow past the budget, so the claim cannot pass vacuously.
- **Bad records don't end the run unless you want them to.** `on_error: quarantine`
  extracts every good record and writes the bad ones to `rejected.jsonl` with their
  index, field, error and raw value. In the comparison's corrupted-record experiment the
  hand-written scripts kept only a header; GigaXML kept 290,899 rows plus the evidence.
- **Interrupted runs resume — after checking what changed.** `--checkpoint-every` commits
  the output in parts; `--resume` continues from the last committed part and refuses the
  resume if the source's sha256 or the config hash differs, printing both values.
- **Fields live in config, not code.** Switching fields, types or output format is an
  edit to a YAML file — and `gigaxml inspect <file>` proposes a starting config for any
  document you point it at, without knowing its structure in advance.
- **Three output formats are the same flag.** CSV, JSONL and Parquet share the writer,
  the batching and the atomic move — switching is `--format`, not new code. What "atomic"
  does and does not cover is written down in
  [`OUTPUT-DURABILITY.md`](OUTPUT-DURABILITY.md).
- **A desktop application for people who do not use terminals** — the same engine behind
  a window (`pip install "gigaxml[gui]"`), with structure analysis, field building,
  preview, batch runs and a progress bar that provably moves.

## What this is

The point of this project is not "it can parse XML" — plenty of tools can. The point is
**constant, bounded peak memory while extracting from 4 GB / 10 GB files**, and being able
to prove it with a reproducible measurement harness.

Measured on this machine, on a generated 4.05 GiB file holding 11,915,264 records:

| | |
|---|---|
| **Input** | 4142.72 MiB, 11,915,264 records |
| **Time** | 284.67 s (14.6 MiB/s, 41,856 records/s) |
| **Peak RSS** | 33.703 MiB |
| **Increase over the post-import baseline** | **5.059 MiB** |

The same run at 1 GiB (2,978,816 records) added **5.105 MiB** — four times the input and
the increase did not move. Something that accumulated per record would make the four
gigabyte figure four times the one gigabyte figure; it is flat to within a percent. Every
number here comes from a script in this repository; see [Benchmarks](#benchmarks).

The configuration used above reads six fields, including a nested path and a type
conversion — the shapes a real config uses:

```yaml
record: /catalog/products/product
fields:
  id:           {path: '@id'}
  type:         {path: '@type'}
  name:         {path: name}
  category:     {path: category}
  price:        {path: price, type: float}
  manufacturer: {path: manufacturer/name}
```

## Installing

Three extras, all optional. Parquet output needs `parquet`
(`pip install "gigaxml[parquet]"`); CSV and JSONL do not. Reading field types from an XSD
needs `xsd` (`pip install "gigaxml[xsd]"`), and the tool works without it — it never
consults a schema while parsing. The desktop application needs `gui`, which pulls PySide6
(640 MiB installed, measured on Windows).

The application is also packaged per platform — an unsigned Windows build, an Apple
Silicon `.dmg` and an x86_64 AppImage — under
[Releases](https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit/releases),
and [its release notes](packaging/RELEASE_NOTES.md) say what each build runs on and what
the unsigned warnings mean.

From source, if you would rather:

```bash
git clone https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit.git
cd GigaXML-Memory-Efficient-XML-Extraction-Toolkit
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"     # POSIX: .venv/bin/python
```

`lxml` and `pyyaml` are the only required dependencies.

## Thirty seconds

Step one on its own — `gigaxml inspect big.xml` — does not read the whole document: it
reports the structure and the repeating paths it found, and ranks them. The config that
step two writes is explicitly a **starting point**, not a conclusion; read the comments
in it.

For a quick look at the data before committing to a full run:

```bash
gigaxml sample big.xml -c config.yaml -n 20 -o first20.jsonl
```

### What it looks like

![gigaxml inspect reading the structure of a document](assets/inspect.gif)

*Finding the records in a document that opens with a licence comment.*

![gigaxml extract processing a four-gigabyte file](assets/extract-4g.gif)

*Extracting 11.9 million records from 4.05 GiB.*

> **Both of these are animations rendered from the tools' real output, not screen
> recordings.** The text is what the tools actually printed and the timings are the
> measured ones, but the frames are drawn rather than captured — this machine's sandbox
> does not permit screen capture. Each frame carries the same note.

## The run report

Every run writes a machine-readable summary of itself, to `run-report.json` beside
`--output` unless `--report` names somewhere else, **on success and on failure** — the exit
code tells a shell script whether the data is usable, and the report tells everything else.
[`RUN-REPORT-FORMAT.md`](RUN-REPORT-FORMAT.md) documents all 24 fields; these are the ones
worth knowing before a script depends on them:

| | means |
|---|---|
| `status: ok` | finished, and the output is in place |
| `status: failed` | stopped on an error — a bad value, an unwritable target, a config that will not load |
| `status: interrupted` | stopped by a signal — Ctrl-C, or `kill` on POSIX |
| `output_complete` | true only once the finished file is there, so a partial output is distinguishable without parsing a message |
| `config_hash` | a digest of the *parsed* config, not its bytes, so adding a comment does not change it |

A measurement that could not be taken is `null` rather than zero. A document read from a
pipe has `input_identity: null` with an `input_identity_error` beside it saying why, because
a stream has no length to stat and no content to hash, and a `0.00` there would claim the
document was empty.

**What "interrupted" covers, and what it cannot.** A stop signal arrives as an exception the
ordinary error path already handles, so the report is written on the way out. `SIGINT` —
Ctrl-C — is covered on every platform, `SIGTERM` on POSIX. **On Windows `TerminateProcess`
is not, and cannot be**: the task manager, and anything else that kills without a signal,
leaves the process no code to run, so no handler in any language would fire. A stopped run
then leaves its parts and its manifest and no report.

## Reading from a pipe

`-` as the source means **stdin**, in all three commands:

```bash
curl -s https://example.org/dump.xml.gz | gunzip | gigaxml extract - -c config.yaml -o out.csv
cat catalog.xml | gigaxml inspect -
```

A stream is parsed exactly as a file is, at the same bounded memory — the reader does not
know or care which it has. What a stream cannot do is be verified, so the two options that
work by hashing the source are refused with an explanation rather than silently
misbehaving:

```
$ gigaxml extract - --checkpoint-every 1000 ...
error: --checkpoint-every cannot be used with standard input: a checkpoint records the
source's sha256 so a resume can check it, and a stream cannot be read twice to compute
one. Save the document to a file, or drop the flag.
```

## Types from an XSD

If the document has a schema, the config's optional `schema` key
([`CONFIG-FORMAT.md`](CONFIG-FORMAT.md)) points at it and the declared types win. The same
document, same records, with and without the schema — and the difference is exactly the
field the schema says is money:

```
# without the schema, `price` is text:      9.99, 19.5
# with it, `xs:decimal` keeps the value:    9.99, 19.50
```

**A schema changes types, not the parse.** The streaming reader never consults it, and the
package works with `xmlschema` absent — there is a test that hides the module and imports
the whole product to keep that true. It is applied once, when the config is loaded, and
what it produces is a config with the types filled in; that config is also what the run
report's `config_hash` covers, so a run whose schema changed has a different fingerprint
from one that did not.

A schema that will not load gives you the compiler's own answer — tag, position and path —
rather than a traceback:

```
error: 'catalog.xsd' is not a usable XSD: Unexpected child with tag 'xs:sequence' at
position 2: ... Path: /xs:schema/xs:element/xs:complexType/xs:sequence/...
```

One the sandbox policy refuses is reported as a refusal rather than dropped, and an import
that resolves to nothing fails the run instead of compiling a schema with declarations
missing — [what a schema may not reach is written down](SECURITY.md), and
[the error classes and exit codes are in `ERRORS.md`](ERRORS.md).

## The desktop application

`gigaxml-gui`, from `pip install "gigaxml[gui]"`.

Eight steps, in the order they are done, each usable without touching the command line: open
a document, **Analyse** it for record candidates, build the field list from a candidate,
**Preview** a sample, retype any field, **Export**, watch it run, and continue a run that
stopped.

**The window never parses your document.** It starts the CLI as a child process and reads
what the CLI prints, so opening the application does not undo the memory claim — which is
the whole point of having one. That is enforced rather than promised:
`tests/integration/test_gui_no_parsing.py` walks the AST of every module under
`src/gigaxml/gui/` and fails if a parser is reachable from one.

**Job History and Resume Manager** are the window's view of the run reports: past runs with
their source, row count, time, peak, output and outcome, newest first, and a button that
carries a stopped run into the Execute tab. Pressing Resume fills the panel and stops
there — the run is started by pressing Start, because continuing somebody's earlier work is
a decision and this project does not make it on their behalf.

## Examples

[`examples/`](examples/) has three documents that were not made for this tool, each runnable
by copying the commands out of its README:

* **[Wikipedia](examples/wikipedia/)** — the full Simple English article dump, 1.6 GB.
  560,605 articles, peak about 36 MiB. Run it to see the memory claim on a document
  nobody designed for this tool.
* **[ERP](examples/erp/)** — a generated item master and order log in one file, with a
  worked example of money as `decimal` against what `float` does to the same values. The
  row counts are exact because `--seed` fixes the data.
* **[PubMed](examples/pubmed/)** — one day of the baseline export. Chosen because two of
  what it does with that document are **limits**, and both are written down there: a date
  the tool will not reassemble, and repeated children that do not become columns.

## Benchmarks

Three generated datasets, two configs, measured in a subprocess with `psutil`:

```
dataset   fields     input MiB      records        s   MiB/s     peak    delta
------------------------------------------------------------------------------
100MB     6 fields      100.57      290,900     6.92    14.5   33.281    5.113
100MB     1 field       100.57      290,900     2.69    37.4   31.203    2.617
1GB       6 fields     1033.65    2,978,816    70.66    14.6   33.676    5.105
1GB       1 field      1033.65    2,978,816    27.62    37.4   31.496    2.922
4GB       6 fields     4142.72   11,915,264   284.67    14.6   33.703    5.059
4GB       1 field      4142.72   11,915,264   112.04    37.0   31.500    2.973
```

`peak` and `delta` are MiB; `delta` is against the same process's post-import baseline.
`peak` is `PeakWorkingSetSize` — the maximum over the process's life, not the current RSS
at the end, which reads 10–14% lower. `--size` is approximate: `--size 1GB` produces
1033.65 MiB, not 1024, and the sizes above are the measured ones. Both numbers are reported
because either alone can be misread — peak includes about 32 MiB of interpreter and library
overhead, delta is what the workload is responsible for.

The one-field rows are a control, not the headline: a single-field config is the easiest
member of this family to run, and quoting it alone would overstate what a real config
costs. Field count costs about **2.5×** in throughput.

To reproduce, generate the datasets and run the harness in [`benchmarks/`](benchmarks).
[`BENCHMARK-METHODOLOGY.md`](BENCHMARK-METHODOLOGY.md) is how those numbers are taken, what
the two thresholds are each for, and what enforces the memory one:

```bash
gigaxml generate --size 100MB -o data/b100m.xml
gigaxml generate --size 1GB   -o data/b1g.xml
gigaxml generate --size 4GB   -o data/b4g.xml
python benchmarks/bench_extraction.py          # runs each pair 5x by default (--repeat N)
```

Every raw run is kept in `results.json`; the printed rows carry the median across the
repeats. The comparison against hand-written `lxml`, `xmltodict` and `pandas.read_xml` —
including the sizes where the hand-written script is faster — is in
[GigaXML vs Alternatives](#gigaxml-vs-alternatives).

## GigaXML vs Alternatives

Measured, not argued: four implementations perform the **same extraction task** (six
fields, including a nested path and a float conversion, into identically-ordered CSV) on
100 MB / 1 GB / 4 GB generated documents — five runs each, medians reported, raw values
in [`benchmarks/compare/results.json`](benchmarks/compare/results.json), every script
one readable file in [`benchmarks/compare/`](benchmarks/compare/). A fourth dataset is
real: [Simple Wikipedia's full article dump](benchmarks/compare/REPORT.md), namespace
and all.

### vs raw `lxml` (hand-written)

**The hand-written script wins on raw throughput at every size, by roughly 1.5×, and
that is stated first because it is true:**

| Implementation | 100 MB | 1 GB | 4 GB | Peak RSS (100 MB → 4 GB) |
|---|---|---|---|---|
| hand-written `lxml` | **4.76 s** | **46.5 s** | **173.7 s** | 24.8 MB → 25.1 MB |
| GigaXML | 6.71 s | 67.9 s | 268.6 s | 33.2 MB → 33.5 MB |

A fixed task, known fields and one script you maintain: that is the case where the
hand-written script is the better tool on time, and no amount of toolkit changes that.

**On memory, the two are now within about 8 MB of each other and both are flat across a 40×
range** — and that is a correction, not a win. Two numbers in an earlier version of this
table were not measurements of the thing they claimed to measure: GigaXML's 19.4 / 19.4 /
19.1 MB was the harness's own overhead, and the hand-written script's 1222 MB was real but
had been attributed to libxml2 rather than to the actual cause, calling `clear()` without
unlinking — `clear()` empties an element, it does not detach it, so every element outside
the record stayed reachable. The full account, and the ways this comparison has been unfair
to itself, are in [the report](benchmarks/compare/REPORT.md). **The knowledge is learnable
— a determined script can land within about 8 MB of the tool** — and what GigaXML adds is
that you never have to find either trap, or find out which of your measurements are really
the harness describing itself.

What the throughput buys, each measured rather than asserted: a corrupted record
mid-file ends the hand-written script (exit 1, header only in the CSV) while
`on_error: quarantine` extracts the 290,899 good records and writes the bad one to
`rejected.jsonl` with its index, field and raw value; an interrupted GigaXML run
resumes from its last committed part after verifying the source and config are
unchanged; switching fields is a YAML edit instead of a code edit; switching output to
JSONL or Parquet is a flag; and every output lands atomically — a failed or cancelled
run never leaves a half-written file where a reader would find it.

★ **"Atomically" is defined, not assumed.** The two guarantees above are about a *process*
dying: a failed run publishes nothing, and the target is never a mixture. **Surviving a
power cut is not promised** — nothing is fsynced, so the bytes may still be in the
operating system's cache when the power goes. That is a different failure from a
half-written file, and
[`OUTPUT-DURABILITY.md`](OUTPUT-DURABILITY.md) says which one you are getting, layer by
layer.

### vs `xmltodict`

xmltodict's streaming mode (`item_depth`) does keep the document from accumulating —
but its peak still climbs with the file (43.4 MB at 100 MB, 707.3 MB at 4 GB), which
the comparison records rather than hides — and it is the right pick when you want the
whole document as Python dicts, not a field extraction. For the extraction task it costs
about 1.2× the time against GigaXML (5.70 s vs 6.71 s per 100 MB is GigaXML's loss;
56.8 s vs 67.9 s per 1 GB and 224.4 s vs 268.6 s per 4 GB are xmltodict's), and it
shares the hand-written script's weakness on bad records: one
malformed value ends the run.

### vs `pandas.read_xml`

`read_xml` is the fastest route *into a DataFrame* and the natural pick when the goal
is analysis, not extraction. Three measured limits. It cannot reach the nested
`manufacturer/name` field (its output in the comparison carries the column empty; on the
five columns it *can* extract it is still correct — byte-for-byte against the other
implementations for all 290,900 rows at 100 MB, and for all 10,485,760 it delivered at
4 GB). On the 1 GB document it fails outright — `lxml.etree.XMLSyntaxError: switching
encoding` from pandas 3.0.6, reproducible with both path and file-object inputs, having
already committed 6.3 GB — while the 100 MB file of the same generator parses fine. And
on the 4 GB document **it returns exit 0 and writes 1,429,504 rows fewer than the
document contains** — it stopped at exactly 10 × 2²⁰ and wrote nothing further, with no
error, no warning, and a structurally perfect CSV that any tool will open.

That last one is the reason this suite counts output rows rather than trusting the
document's record count. A rate measured over a truncated output is not a rate: an
earlier version of this comparison divided pandas's 4 GB runtime by the document's
record count and credited it with about 20% more throughput for a run that dropped 12%
of the data. **For the extraction-to-file task `read_xml` is therefore not a contender at
large sizes; for analysis of documents that fit, it is.**

---

## Non-goals

- No full XPath 3.1 — XPath is evaluated only inside a single record subtree.
- No arbitrary byte-offset seek/resume — XML byte offsets are not a safe parse boundary.
- No parallel or distributed extraction. The memory ceiling this project promises is a
  property of a single run: N workers each bounded are N times the memory, and the number
  a reader actually cares about — the total — stops being bounded. Parallelism here would
  mean redefining the one claim worth making.
- No AI/ML structure inference — confidence values are deterministic statistics.
- No customer data — the datasets here are generated, or public dumps of Wikipedia and
  PubMed that anyone may fetch. Nothing that is not already published is used.
- No fabricated benchmarks — every performance claim comes from a runnable script, and the
  three in `examples/` come with the number to check them against.

## Known limitations

- **`--resume` re-parses and skips; it does not seek.** XML cannot be re-entered
  mid-stream, so continuing a run means reading from the beginning and discarding the
  records already accounted for. On a 403 MB file that costs 8.7 s against 17.1 s to
  extract, so resuming saves roughly half of what you had already done. `--help` says so
  too.
- **Parquet output carries a fixed cost that CSV and JSONL do not.** The reader stays
  flat for every format, but the Arrow library behind Parquet allocates a working set of
  its own: measured on a 403 MB document, the whole pipeline peaks at 99 MB writing Parquet
  against about 25 MB writing CSV on the same data, of which roughly 20 MB is simply
  `import pyarrow`. That cost does not scale with `--batch-size` (1,000 / 5,000 / 20,000
  all land within 8 MB of each other), so it is Arrow's workspace rather than a buffer
  this project controls. It is stated here rather than left to be discovered: CSV and
  JSONL remain the formats to reach for when a hard memory ceiling matters.
- **`inspect` is slower than `extract`** — 17.3 MiB/s against 38.2 MiB/s on the same
  1 GB file. It maintains several parallel bookkeeping stacks per element. It is also the
  command you run once on a document, not in a loop.
- **An inferred config treats containers as leaves.** `--generate-config` proposes direct
  children and attributes; a field whose element has children of its own is read as
  concatenated text, so `<tags><tag>a</tag><tag>b</tag></tags>` becomes `ab`. Nested
  paths (`manufacturer/name`) have to be written by hand, as the generated comments say.
- **Types are inferred from a sample**, and `decimal` is never inferred. If a field is
  money, set `type: decimal` yourself — `float` cannot represent 49.90 exactly. An XSD
  will do it for you if the document has one, and it can declare `xs:decimal` where a
  sample cannot tell 49.90 from 49.9.
- **`date` is a date, not a date-time.** It is `datetime.date.fromisoformat`, so
  `2026-05-18` converts and `2026-05-18T12:42:53Z` does not — there is no type that spans
  both, and a field carrying a timestamp is carried across as text. A document that stores
  a date as three elements (`2026`, `01`, `28`) comes out as three columns: assembling them
  is a decision about what the data means, and the padding is a choice (`01` versus `Jan`)
  the document does not settle. `examples/pubmed/` has this in full.
- **Records are rows, so a record's repeating children are not columns.** Several `<Author>`
  under one `<PubmedArticle>` is not something this tool flattens, and it does not invent a
  column for it. The way to reach them is to make the child the record — a field path is
  relative to the record and can walk back outward — and `gigaxml inspect` is what shows
  the candidates for that decision. `examples/pubmed/` works it through.
- **There is no join.** Two tables extracted from one document are two files, and
  relating them is the reader's. `examples/erp/` shows a three-line join in ordinary
  Python, including the count of orders that matched nothing, which is the number worth
  checking.
- **Parsing limits are not configurable.** Entities are never expanded, the network is
  never touched, and no DTD is loaded; documents nested deeper than 256 levels, carrying a
  single text node over about 10 MB, or amplified by entities are refused rather than
  partially read. These are deliberate and there are no flags to turn them off.
- **`--checkpoint-every` verifies the parts on disk before resuming**, which costs one
  pass over the output at about **200 MiB/s** — about 10 ms for 2 MiB of CSV, negligible for
  Parquet, whose row counts come from file metadata. It grows with the size of the
  output, not the input. Measured by `benchmarks/bench_resident.py`.
- **A resume is dominated by starting the process, not by checking the output.** Against
  an already-complete manifest on a 403 MiB source, the command takes about 770 ms: some
  400 ms of that is interpreter startup and imports, and most of the rest is hashing the
  source to confirm it has not changed. The part check itself is about 10 ms.
- **One field value that is itself gigabytes is held in memory.** Records stream, but
  there is no streaming mode for a single value, because there is nothing to stream it
  into.
- **`sample` reads what it samples into memory.** It is meant for looking at a file, not
  for measuring one.
- **Input is a local file, never a URL.** A `.xml.gz` file is fine; a document that lives
  behind HTTP is out of scope.

## Documentation

| | |
|---|---|
| [CONFIG-FORMAT.md](CONFIG-FORMAT.md) | every config key, its default, and what an unrecognised one does |
| [ERRORS.md](ERRORS.md) | error classes, exit codes, and what to do about each |
| [RUN-REPORT-FORMAT.md](RUN-REPORT-FORMAT.md) | all 24 fields of the run report, and how it is versioned |
| [CHECKPOINT-FORMAT.md](CHECKPOINT-FORMAT.md) | the on-disk layout `--checkpoint-every` writes and `--resume` reads |
| [OUTPUT-DURABILITY.md](OUTPUT-DURABILITY.md) | what "atomically" covers, layer by layer — and the layer it does not |
| [SECURITY.md](SECURITY.md) | the three boundaries: network, schema, filesystem |
| [BENCHMARK-METHODOLOGY.md](BENCHMARK-METHODOLOGY.md) | how the performance numbers are taken, and what enforces the memory one |
| [REAL-WORLD-VALIDATION.md](REAL-WORLD-VALIDATION.md) | the same extraction run against Wikipedia, PubMed, an ERP export and FHIR |
| [python-api.md](python-api.md) | driving the package from Python, and the one rule about what is public |

## Development

```bash
pytest -q                                   # unit + integration
pytest -q tests/performance                 # memory and throughput; not in CI
ruff check .
ruff format --check .

python tools/check_comments.py              # comment and docstring size in src/
python tools/ci_selfcheck.py shape          # the workflow's own claims
python tools/ci_selfcheck.py tracked        # nothing tracked is excluded by .gitignore
python tools/ci_selfcheck.py pins           # release actions pinned to a commit SHA
```

Performance tests are excluded from CI: they measure memory and throughput, take minutes,
and are not a pass/fail signal. The four `tools/` commands are the repository's own
checks — they read files rather than running the code, so they need nothing installed, and
CI runs all of them.

## License

MIT — see [LICENSE](LICENSE).
