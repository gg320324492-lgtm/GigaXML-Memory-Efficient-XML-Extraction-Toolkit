# GigaXML

A production-oriented toolkit for profiling, validating and extracting structured data
from multi-gigabyte XML files with bounded memory — as a command line and as a desktop
application.

```bash
pip install gigaxml
gigaxml inspect catalog.xml          # see the structure
gigaxml extract catalog.xml -c config.yaml -o rows.csv
```

The window does the same things: **GigaXML**

---

## What is in this release

| | |
|---|---|
| **Version** | 1.2.0 — the version in this build's file properties, and the one the package reports |
| **Platforms** | Windows x64 · **macOS Apple Silicon** · Linux x86_64 — see [the macOS note](#macos) before you download |
| **Licence** | MIT |
| **Signed** | **No Developer ID certificate — read [Unsigned binaries](#unsigned-binaries) before you install** |

- A desktop window over a streaming extractor: the window never holds your document, and
  the extraction runs in a child process, so the interface stays responsive on a file
  larger than memory.
- Drag a file in, or open it from the window. Structure view proposes record paths; the
  fields panel builds a config; the results panel opens the output.
- Batch runs one document per child process, in order, and can be stopped.
- `inspect`, `extract`, `sample` and `generate` are all available on the command line, and
  the packaged application is the same program.
- The interface runs in English or Chinese, chosen in the settings panel — see
  [Languages](#languages).

### What changed in 1.2.0

**A run now writes a report about itself.** `extract` and `sample` write a
machine-readable summary — `run-report.json` beside the output unless you point
`--report` somewhere else. It is written on success **and** on failure, and it carries
the things you would otherwise have to guess: the source's size and sha256, a fingerprint
of the parsed config, the peak RSS, the elapsed time, the throughput, and
`output_complete`, which is true only once the finished file is actually in place. So a
caller can tell a partial output from a complete one without parsing a message.

> **This is a behaviour change worth noticing**: a file called `run-report.json` now
> appears next to your output where there was none before. A script that globs the output
> directory will see it. Pass `--report` to put it elsewhere if that matters.

**`status` has a third value.** It was `ok` or `failed`; it is now `ok`, `failed`, or
`interrupted`. A run stopped by a signal — Ctrl-C, or `kill` on POSIX — used to leave no
report at all, and now leaves one that says so, along with how far it had got. That is
the one a summary is most likely to get wrong, since an interrupted run is neither a
success nor a failure.

**What "interrupted" cannot cover.** `SIGINT` is covered on every platform and `SIGTERM`
on POSIX. **On Windows `TerminateProcess` is not, and cannot be** — the task manager, and
anything else that kills without a signal, leaves the process no code to run. A stopped
run then leaves its parts and its manifest and no report.

**A document can be read from a pipe.** `-` as the source means stdin, in `extract`,
`inspect` and `sample` alike, at the same bounded memory — the reader does not know or
care which it has. The two options that verify a source by hashing it (`--resume` and
`--checkpoint-every`) are refused with an explanation rather than misbehaving quietly,
because a stream cannot be read twice.

**Field types can come from an XSD.** Point the config at a schema with a `schema:` key
and the declared types win: a column the schema types as `xs:decimal` keeps `19.50`
where the text would have arrived as `19.5`. It needs the optional `gigaxml[xsd]` extra,
and **a schema changes types, not the parse** — the streaming reader never consults it,
and the package works with `xmlschema` absent.

**The window has a job history and a resume manager.** Past runs with their source, row
count, time, peak, output and outcome, newest first — read out of the run reports rather
than remembered by the window, so a run started in a terminal shows up too. A stopped run
can be carried into the Execute tab from there. The window still never parses your
document; that is enforced by a test that walks the AST of every module under
`src/gigaxml/gui/` and fails if a parser becomes reachable from one.

**Three worked examples ship in the repository**, each runnable by copying the commands
out of its README: Simple English Wikipedia's full 1.6 GB dump (560,605 articles, peak
about 36 MiB), a generated ERP item master and order log with a worked example of money
as `decimal`, and one day of PubMed's baseline export — that last one chosen because two
of what it does with that document are limits, and both are written down rather than left
to be hit: `date` is a date and not a date-time, and a record's repeating children do not
become columns.

### What changed in 1.1.0

**The generated data changed.** The synthetic generator's country pool held a region code
beside nine sovereign states, and the field it feeds is `<manufacturer><country>` — so
every dataset this tool produced was presenting a part of China as a country. The code is
gone, and a test now pins what may be in that pool. Hong Kong and Macao are in the same
position and were never in it; they are covered by the same test, so they cannot be added
by accident either.

**If you generated datasets with an earlier version, their contents and their `sha256`
will differ from this build's.** The pool losing a member changes the random sequence
behind a given seed. Nothing about the command line, the config format or the output
format changed — only which bytes a seed produces.

---

## What the numbers are

Measured on the machine this was built on, with the six-field configuration below — a
nested path and a type conversion, the shapes a real config uses:

| | |
|---|---|
| **Input** | 4142.72 MiB, 11,915,264 records |
| **Time** | 284.67 s (14.6 MiB/s, 41,856 records/s) |
| **Peak memory** | 33.703 MiB |
| **Increase over the post-import baseline** | **5.059 MiB** |

The same run at 1 GiB added 5.105 MiB — four times the input, and the increase did not
move. Something accumulating per record would have made the four-gigabyte figure four
times the one-gigabyte figure; it is flat to within a percent. A one-field configuration
runs at 37 MiB/s and is quoted nowhere as the headline: it is the easiest member of this
family to run, and quoting it alone would overstate what a real config costs. Every
number here comes from a script in the repository; see the README's Benchmarks section
for how to reproduce them.

---

## Installing

### Windows

1. Download `gigaxml-gui-windows.zip`.
2. Extract it to a folder you own — the application does not need an installer to run, and
   nothing is written outside that folder except the settings file under
   `%APPDATA%\GigaXML`.
3. Run `gigaxml-gui\gigaxml-gui.exe`.

There is also an installer, `GigaXML-Setup-1.1.0.exe`, built by the same pipeline as the
zip — its version comes from the package, not from a hand-typed file. It installs per
user, so there is no administrator prompt: no services, no drivers, just the application
into a folder you own, with a Start-menu shortcut and an optional desktop one. Its
uninstaller removes the installation directory entirely — after an uninstall the only
thing that survives is your settings under `%APPDATA%\GigaXML`, which is deliberate, so
a reinstall keeps your configuration. **It is unsigned, exactly like the zip**: the same
SmartScreen warning, the same one-time allow — see
[Unsigned binaries](#unsigned-binaries).

### macOS

**This build is Apple Silicon (arm64) and it will not start on an Intel Mac.** There is no
Intel build in this release: an Intel binary would reach Apple Silicon Macs only through
Rosetta, which Apple has announced it will phase out. If you have an Intel Mac, use
`pip install "gigaxml[gui]"` instead — the Python package runs on either architecture.

1. Download `gigaxml-gui-macos.tar.gz` and extract it. You get `GigaXML.app`.
2. Move it to `/Applications`.
3. The first launch will be blocked — see below.

### Linux

1. Download `GigaXML-x86_64.AppImage`.
2. `chmod +x GigaXML-x86_64.AppImage`.
3. Run it. If your system has no FUSE, use `--appimage-extract-and-run`.

### From a terminal

```bash
pip install gigaxml          # the CLI
pip install "gigaxml[gui]"   # and the window
gigaxml-gui
```

Parquet output needs the `parquet` extra: `pip install "gigaxml[parquet]"`. CSV and JSONL
need nothing beyond the base install.

---

## Unsigned binaries

**These builds carry no Developer ID certificate.** That is what "unsigned" means here,
and it is worth being precise, because there are two different kinds of signature on a
macOS binary and only one of them is missing:

- **Ad-hoc signing — present.** Apple Silicon refuses to execute a binary with no
  signature at all: the kernel kills it before your double-click reaches it. Every
  executable here is ad-hoc signed, automatically, as part of the build. This layer is
  what makes the application *run*; it is not something you configure, check, or need to
  think about, and the packaged application has been started and exercised on an
  Apple Silicon machine exactly so that this claim is measured rather than assumed.
- **Developer ID signing and notarisation — absent.** This is the layer that vouches for
  *who* built the binary, and it is what Gatekeeper and SmartScreen ask about. It needs a
  paid certificate and a real organisation behind it. This is the layer this release does
  not have, and every warning below is that absence speaking, not a broken download.

The distinction is stated because the two failures look nothing alike and read as each
other: a missing ad-hoc signature is "the application cannot be opened" with no way
forward, while a missing Developer ID is a warning with a stated workaround. What you
will see here is the second kind. The application runs.

This is stated plainly because the alternative — a download that trips a security warning
with no explanation — is how people end up disabling protection entirely, which is a
worse outcome than a warning they understand.

### What will happen, and what it means

| Platform | What you will see | What it actually means |
|---|---|---|
| **Windows** | SmartScreen: *"Windows protected your PC"* | The file has no reputation yet and no signature. Nothing has been scanned or found wrong — there is simply nothing vouching for it. |
| **Windows** | *"The publisher could not be verified"* | Same. The publisher field is empty. |
| **macOS** | *"GigaXML cannot be opened because the developer cannot be verified"* | Gatekeeper, because there is no Developer ID certificate on the file. |
| **macOS** | *"cannot be opened because it is from an unidentified developer"* | Older wording for the same check. |
| **Linux** | Possibly nothing; some desktops warn about unsigned `.AppImage` | Depends on the desktop environment. |

### How to allow it, properly

**Windows — one time, per file:**

1. Click **More info** on the warning (it is the small text at the bottom).
2. Click **Run anyway**.

Or from PowerShell, which unblocks a whole folder at once:

```powershell
Unblock-File -Path .\gigaxml-gui\gigaxml-gui.exe
```

**macOS — one time, per file:**

Right-click the app (or the `.dmg`) and choose **Open**, then **Open** again in the dialog
that appears. That is the intended path and it only ever needs doing once per file.

If you prefer the command line, after moving the app to `/Applications`:

```bash
xattr -dr com.apple.quarantine /Applications/GigaXML.app
```

**Linux:** nothing to do. If your desktop warns, the AppImage is not signed and that is
expected.

### What we are not going to tell you to do

**Do not turn off SmartScreen, do not disable Gatekeeper, and do not add an exclusion to
your antivirus.** Those settings protect you from other people's malware and turning them
off to accommodate one download is a bad trade. Unblocking a single file you have chosen to
run is not the same thing, and it is all that is needed.

### What signing would cost

| | Windows | macOS |
|---|---|---|
| **Certificate** | OV code-signing, or EV | Apple Developer ID Application certificate |
| **Typical cost** | OV: roughly US$70–200/year. EV: US$300–500/year, and a long application process | Included with a paid Apple Developer Program membership: **US$99/year** |
| **Also needed** | A timestamp server, and the EV route requires a registered organisation | Apple **notarisation**, which is free but requires the app to be signed first and uploaded |
| **Effect** | Removes the SmartScreen warning after reputation accrues (a few downloads on Windows 10/11) | Removes the Gatekeeper block; the app opens normally, including on a machine that has never run it before |

Neither is bought for this release. If signing happens in a later one, it will be stated
here rather than assumed.

---

## Languages

The interface runs in **English and Chinese**. The language is chosen in the settings
panel and remembered between launches; changing it takes effect the next time the
application starts, and the panel says so beside the control. English is the default.

Two kinds of text are deliberately **not** translated, in either direction:

- **Command-line output stays English** — `--help`, error messages, the `--progress`
  stream. It is written for people reading terminals and scripts, it is asserted on by
  the project's own tests, and changing it would be changing a stable contract for
  decoration.
- **Field-panel validation messages stay English.** What that panel shows you is the
  sentence the configuration library itself produced, unedited — rewording it in the
  window would be the first step towards a second implementation of the same rules, and
  the two would drift apart the first time either changed. So under a Chinese interface
  you may well see a Chinese panel carrying an English validation sentence. That is
  deliberate, not a missed translation.

---

## What the window needs

Nothing, once installed. The application carries its own extractor, so it does not need
Python, and it does not need `gigaxml` installed separately — the same executable is both
the window and the command line.

For a source install:

```bash
pip install "gigaxml[gui]"
```

which pulls PySide6 — a large dependency: 640 MiB installed on Windows for this release,
measured, and the packaged downloads above are about 200 MiB apiece, which the packaging
pipeline measures rather than estimates.

---

## Command line

The window and the command line are the same program. `gigaxml-gui extract ...` is
`gigaxml extract ...`.

```bash
gigaxml generate --size 1GB --seed 42 -o big.xml
gigaxml inspect big.xml --json
gigaxml extract big.xml -c config.yaml -o rows.csv --format csv --progress
gigaxml sample big.xml --rows 5
```

`extract` streams: it releases each record as the next one arrives, so memory stays flat
regardless of file size. `inspect` walks the file once without knowing the record path in
advance, and reports what it found — including elements that appear only once, which are
the ones a sampling approach misses.

---

## Non-goals

Copied from the README, where this list lives; the two are kept identical on purpose.

- No full XPath 3.1 — XPath is evaluated only inside a single record subtree.
- No arbitrary byte-offset seek/resume — XML byte offsets are not a safe parse boundary.
- No AI/ML structure inference — confidence values are deterministic statistics.
- No real customer data — everything runs on synthetic, reproducible datasets.
- No fabricated benchmarks — every performance claim comes from a runnable script.

---

## Known limitations

Copied from the README, where this list lives; the two are kept identical on purpose.

- **`--resume` re-parses and skips; it does not seek.** XML cannot be re-entered
  mid-stream, so continuing a run means reading from the beginning and discarding the
  records already accounted for. On a 403 MB file that costs 8.7 s against 17.1 s to
  extract, so resuming saves roughly half of what you had already done. `--help` says so
  too.
- **`inspect` is slower than `extract`** — 17.3 MiB/s against 38.2 MiB/s on the same
  1 GB file. It maintains several parallel bookkeeping stacks per element. It is also the
  command you run once on a document, not in a loop.
- **An inferred config treats containers as leaves.** `--generate-config` proposes direct
  children and attributes; a field whose element has children of its own is read as
  concatenated text, so `<tags><tag>a</tag><tag>b</tag></tags>` becomes `ab`. Nested
  paths (`manufacturer/name`) have to be written by hand, as the generated comments say.
- **Types are inferred from a sample**, and `decimal` is never inferred. If a field is
  money, set `type: decimal` yourself — `float` cannot represent 49.90 exactly.
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

Two more, about the packaged builds specifically, are stated in their own sections above:
there is no Developer ID certificate ([Unsigned binaries](#unsigned-binaries)), and the
macOS build is Apple Silicon only ([Installing](#macos)).

---

## Source

<https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit>

MIT licensed.
