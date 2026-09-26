# GigaXML

Memory-efficient XML extraction for very large files, as a desktop application.

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
| **Version** | 0.1.0 — the version in this build's file properties, and the one the package reports |
| **Platforms** | Windows, macOS, Linux |
| **Licence** | MIT |
| **Signed** | **No — read [Unsigned binaries](#unsigned-binaries) before you install** |

- A desktop window over a streaming extractor: the window never holds your document, and
  the extraction runs in a child process, so the interface stays responsive on a file
  larger than memory.
- Drag a file in, or open it from the window. Structure view proposes record paths; the
  fields panel builds a config; the results panel opens the output.
- Batch runs one document per child process, in order, and can be stopped.
- `inspect`, `extract`, `sample` and `generate` are all available on the command line, and
  the packaged application is the same program.

---

## Installing

### Windows

1. Download `gigaxml-gui-windows.zip`.
2. Extract it to a folder you own — the application does not need an installer to run, and
   nothing is written outside that folder except the settings file under
   `%APPDATA%\GigaXML`.
3. Run `gigaxml-gui\gigaxml-gui.exe`.

There is also an installer, `GigaXML-Setup-x.y.z.exe`, which adds a Start-menu shortcut and
an uninstaller. **It is unsigned** — see below.

### macOS

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

---

## Unsigned binaries

**These builds are not code-signed.** Nothing here is signed, and no claim in this release
should be read as saying otherwise. This is stated plainly because the alternative — a
download that trips a security warning with no explanation — is how people end up
disabling protection entirely, which is a worse outcome than a warning they understand.

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

## What the window needs

Nothing, once installed. The application carries its own extractor, so it does not need
Python, and it does not need `gigaxml` installed separately — the same executable is both
the window and the command line.

For a source install:

```bash
pip install "gigaxml[gui]"
```

which pulls PySide6 (about 150–200 MB, the bulk of the download).

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

## Known limitations

- **Unsigned**, as above. This is the only one that affects installation.
- **The window is English only.** There is no translation yet.
- **Very large single fields are held in memory.** Records stream, but one field value that
  is itself gigabytes will still exhaust memory — there is no streaming mode for a single
  value, because there is nothing to stream it into.
- **`sample` reads what it samples into memory.** It is meant for looking at a file, not for
  measuring one.
- **Parquet output needs `pyarrow`**, which is not installed by default: `pip install
  "gigaxml[parquet]"`.
- **Checkpoint resume is per-part.** A resumed run re-reads from the last committed part,
  so it can be slower than an uninterrupted run over the same data.
- **No streaming input.** The document must be a local file; a `.xml.gz` file is fine, a
  URL is not.

---

## Source

<https://github.com/gg320324492-lgtm/GigaXML-Memory-Efficient-XML-Extraction-Toolkit>

MIT licensed.
