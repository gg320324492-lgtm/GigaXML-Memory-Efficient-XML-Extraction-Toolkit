"""The run report's provenance block: what a plain, non-checkpoint run leaves behind.

``--report`` already existed for the two things a caller cannot see from the exit code
-- did the output land complete, and what went wrong -- and it did that on ordinary runs.
What it did not carry was the answer to the questions that make a run auditable after
the fact: *which* file, *which* config, on *what* machine, at *what* cost.

**The property under test is that a field is either a real measurement or an explicit
null, and never a plausible-looking zero.** A report is read by machines and by people
six months later. ``"sha256": ""`` or ``"peak_rss_mb": 0.0`` reads as a measurement; a
null with a sentence attached reads as an absence, and only an absence can be acted on.
The counterexample tests at the bottom of this file are what hold that line -- each one
breaks the thing that produces a value and asserts the report admits it could not get
one, rather than reporting a zero that looks like a finding.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gigaxml.checkpoint import CheckpointError, config_identity
from gigaxml.cli import main
from gigaxml.config import load_config

REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <item><id>1</id><name>A</name></item>
  <item><id>2</id><name>B</name></item>
  <item><id>3</id><name>C</name></item>
</root>
"""

FIELDS = {"id": {"path": "id", "type": "int"}, "name": {"path": "name"}}


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "in.xml"
    source.write_text(SOURCE, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"record": "/root/item", "fields": FIELDS}, sort_keys=False),
        encoding="utf-8",
    )
    return source, config, tmp_path / "out.csv"


def read_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_plain_run_reports_its_provenance(tmp_path: Path) -> None:
    """No ``--checkpoint-every``: the ordinary path carries the full block too.

    The feature is not gated on the checkpoint machinery. If the report only appeared
    for resumed runs it would be missing exactly when nobody is looking.
    """
    source, config, output = write_inputs(tmp_path)
    report_path = tmp_path / "report.json"

    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    report = read_report(report_path)

    assert "checkpoint" not in report, "this is not a checkpointed run"
    assert "checkpoint" not in json.dumps(report)

    # The environment, named rather than inferred.
    assert report["environment"]["python"] == platform.python_version()
    assert report["environment"]["os"] == platform.system()

    # The input, identified by content and not by name.
    assert report["input_identity"]["path"].endswith("in.xml")
    assert report["input_identity"]["size"] == source.stat().st_size
    assert report["input_identity"]["sha256"] == sha256_of(source)

    # The output, same treatment -- so a later reader can tell the file was replaced.
    assert report["output_identity"]["sha256"] == sha256_of(output)

    # The config by what it *means*: two files that extract identically hash alike.
    assert report["config_hash"] == config_identity(load_config(config))
    assert len(report["config_hash"]) == 64

    # Timing, both ends, and monotonic enough to be worth reading.
    assert report["started_at"] <= report["finished_at"]
    assert report["elapsed_seconds"] > 0

    # Counts that add up, and a rate computed over them.
    assert report["records"] == {"accepted": 3, "rejected": 0, "seen": 3}
    assert report["records"]["accepted"] == report["rows"]
    assert report["throughput_records_per_s"] > 0

    # A real peak, not a placeholder. This is the field most likely to be faked, so it
    # is bounded from below by something no empty process could be.
    assert isinstance(report["peak_rss_mb"], float)
    assert report["peak_rss_mb"] > 1.0, "a real extraction cannot peak under 1 MiB"


def test_the_config_hash_tracks_meaning_not_formatting(tmp_path: Path) -> None:
    """Two configs that extract the same records report the same hash.

    A hash over the file's *bytes* would change when somebody reformats their YAML, and
    would then claim the extraction changed when nothing did. The report reuses
    ``config_identity``, which hashes the parsed config for exactly this reason.
    """
    source, config, _ = write_inputs(tmp_path)
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    noise = tmp_path / "reformatted.yaml"
    noise.write_text(
        "# a comment that changes no meaning\n"
        + yaml.safe_dump({"fields": FIELDS, "record": "/root/item"}, sort_keys=True),
        encoding="utf-8",
    )
    out_a, out_b = tmp_path / "a.csv", tmp_path / "b.csv"

    assert (
        main(["extract", str(source), "-c", str(config), "-o", str(out_a), "--report", str(first)])
        == 0
    )
    assert (
        main(["extract", str(source), "-c", str(noise), "-o", str(out_b), "--report", str(second)])
        == 0
    )

    assert read_report(first)["config_hash"] == read_report(second)["config_hash"]


# --- The counterexamples -----------------------------------------------------
#
# Each of these breaks the thing that produces a value. A guard that still passed
# afterwards would be a guard that cannot fail, and the project has already paid for
# believing one that could not: a sampler that reported every reading as 4.1 MB looked
# like a perfect result for three rounds because nothing ever checked whether a process
# running iterparse over 100 MB could be smaller than an idle interpreter.


def test_an_unidentifiable_input_is_reported_missing_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break ``source_identity``: the report must admit it, not invent a hash.

    This is the shape a stdin run takes for real -- an input that cannot be stat'ed or
    re-read -- so the path exercised here is the one the feature was written for, not
    a contrived one.
    """
    import gigaxml.cli as cli

    real = cli.source_identity

    def refuse(source: object) -> dict[str, object]:
        # Only the input stops being identifiable. The output was written to a real
        # path, so refusing that too would test a scenario and not a field.
        if str(source).endswith("in.xml"):
            raise CheckpointError(f"cannot identify the source {source!r}: not seekable")
        return real(source)

    monkeypatch.setattr(cli, "source_identity", refuse)

    source, config, output = write_inputs(tmp_path)
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    report = read_report(report_path)

    assert report["input_identity"] is None, "a source with no hash must not have a hash"
    assert "input_identity_error" in report
    assert "not seekable" in report["input_identity_error"]

    # The whole point: none of the shapes a zero could hide behind.
    assert report["input_identity"] != {"sha256": "0" * 64}
    assert "0" * 64 not in json.dumps(report)
    assert report["input_identity_error"] != ""

    # The rest of the run is unaffected: one missing measurement does not poison the
    # fields that were measured.
    assert report["output_identity"]["sha256"] == sha256_of(output)
    assert report["records"]["accepted"] == 3


def test_an_unidentifiable_output_is_reported_missing_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break ``source_identity`` only for the output: same contract, other field."""
    import gigaxml.cli as cli

    real = cli.source_identity

    def refuse_outputs(target: object) -> dict[str, object]:
        if str(target).endswith("out.csv"):
            raise CheckpointError(f"cannot identify the source {target!r}: vanished")
        return real(target)

    monkeypatch.setattr(cli, "source_identity", refuse_outputs)

    source, config, output = write_inputs(tmp_path)
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    report = read_report(report_path)

    assert report["output_identity"] is None
    assert "vanished" in report["output_identity_error"]
    assert report["input_identity"]["sha256"] == sha256_of(source), (
        "breaking one identity must not blank the other"
    )


def test_an_unmeasurable_peak_is_null_not_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Break the platform peak reader: ``null``, while every other field survives.

    ``peak_rss_mb`` is the field most worth faking -- it is the one a regression is
    read off -- so a run that cannot measure it has to say so rather than report the
    process as having used nothing.
    """
    import gigaxml.cli as cli

    monkeypatch.setattr(cli, "peak_rss_mb", lambda: None)

    source, config, output = write_inputs(tmp_path)
    report_path = tmp_path / "report.json"
    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(output),
                "--report",
                str(report_path),
            ]
        )
        == 0
    )
    report = read_report(report_path)

    assert report["peak_rss_mb"] is None
    assert report["peak_rss_mb"] != 0.0
    assert report["input_identity"]["sha256"] == sha256_of(source)
    assert report["records"]["accepted"] == 3


def test_the_peak_reader_returns_a_plausible_number_on_this_platform() -> None:
    """The reader itself, bounded below by an empty interpreter, never by a guess.

    On Windows this is ``PeakWorkingSetSize`` through ``psapi``, reached with declared
    argument types. Without them ctypes truncates the 64-bit pseudo-handle
    ``GetCurrentProcess`` returns, the call fails, and a caller that treats failure as
    "no measurement" quietly reports nothing at all -- which is how this shipped
    returning ``None`` on every run until the signatures were added.

    There is deliberately no upper bound. The counter is a maximum over the whole
    process's life, so its value depends on what else ran first: measured inside this
    suite it is ~23 MiB, and measured after a few hundred other tests in the same
    process it is over 4 GiB. Both are correct readings of the same counter, and a test
    that pinned an upper bound would be asserting a fact about the test suite's memory
    appetite rather than about the reader.
    """
    from gigaxml.run import peak_rss_mb

    measured = peak_rss_mb()
    if measured is None:
        pytest.skip(f"this platform ({sys.platform}) cannot report a peak working set")
    assert measured > 1.0, "an interpreter importing lxml occupies more than 1 MiB"


def test_the_linux_vmhwm_reading_converts_kibibytes() -> None:
    """``/proc/self/status`` parsing and its unit, tested on any platform.

    A platform branch that only runs on Linux cannot be exercised from a Windows
    checkout, and that is not an abstract risk: this branch shipped assuming
    ``ru_maxrss`` was kibibytes, reached a runner where it was not, and reported a
    4.2 GiB peak for a process holding 4 MiB -- with every other assertion in this file
    still green, because 4202 is a perfectly plausible number for a peak.

    So the parse and the conversion are pulled into a plain function and checked here
    against the kernel's own format, including a line that must be ignored and a file
    that has no such line at all.
    """
    from gigaxml.run import _vmhwm_mib

    status = (
        "Name:\tgigaxml\n"
        "VmPeak:\t   4096100 kB\n"
        "VmSize:\t   4095900 kB\n"
        "VmHWM:\t      92160 kB\n"
        "VmRSS:\t      45056 kB\n"
    )
    # 92160 kB is 90 MiB. If the conversion is skipped the answer comes back 92160, and
    # a reader that returned that would look like a process holding 90 GiB.
    assert _vmhwm_mib(status) == 90.0, (
        f"parsed {_vmhwm_mib(status)} MiB from a 92160 kB VmHWM, which is 90 MiB"
    )
    assert _vmhwm_mib("Name:\tpython\nVmRSS:\t  1 kB\n") is None, (
        "a status file with no VmHWM must read as unavailable, not as zero"
    )
    assert _vmhwm_mib("") is None


def test_the_peak_reader_tracks_memory_growth() -> None:
    """Allocate, touch, and the high-water mark must move. This is what makes it a peak.

    A reader that returned the current working set, a cached constant, or a field that
    never got written would all satisfy a "returns a plausible number" test and all fail
    here. It is the closest thing to the project's standing rule about memory
    measurements -- prove the measurement can go up, or you have not shown it measures.

    **Run in a fresh interpreter, and that is the whole subtlety.** A peak working set
    is a maximum over the process's *life*, so it never falls: inside the full suite
    this process has already peaked past 4 GiB, and allocating 64 MiB after that moves
    nothing -- correctly. The assertion is about the reader, not about how much memory
    the surrounding suite has already touched, so it gets a process that starts empty.
    That is also why the same reasoning is why a *drop* in peak is not evidence of a
    leak fix.
    """
    probe = (
        "import sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import psutil\n"
        "from gigaxml.run import peak_rss_mb\n"
        "before = peak_rss_mb()\n"
        "if before is None:\n"
        "    print('unavailable'); raise SystemExit(0)\n"
        "hog = bytearray(64 * 1024 * 1024)\n"
        "for offset in range(0, len(hog), 4096):\n"
        "    hog[offset] = 1\n"
        # A peak is a maximum over the process's life, so it can never be below what
        # the process is holding right now. Printing the current working set alongside
        # it is what catches a unit error: a reader that read bytes where kibibytes
        # were expected reports a number 1024x too large and looks like a process that
        # allocated four gigabytes. That is not a hypothetical -- a Linux runner whose
        # getrusage reported a different unit than the one assumed turned a fresh
        # interpreter into a 4.2 GiB peak here, and every other assertion in this file
        # passed happily.
        "print(before, peak_rss_mb(), psutil.Process().memory_info().rss / (1024 * 1024))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr[-400:]
    line = completed.stdout.strip().splitlines()[-1]
    if line == "unavailable":
        pytest.skip(f"this platform ({sys.platform}) cannot report a peak working set")

    before, after, resident = (float(value) for value in line.split())
    assert after > before, (
        f"the peak did not move after touching 64 MiB ({before:.1f} -> {after:.1f} MiB); "
        "this reader is not reporting a peak working set"
    )

    # The unit check, with a megabyte of slack. The slack is not softness: the peak and
    # the working set are read a few microseconds apart and the interpreter allocates
    # in between, so a sub-megabyte inversion is a race rather than a wrong answer. A
    # unit error is off by 1024, and nothing that small can hide inside a megabyte.
    assert resident <= after + 1.0, (
        f"the peak {after:.1f} MiB is below the current working set {resident:.1f} MiB, "
        "which is impossible: a maximum over the process's life is at least the present"
    )
    assert after < resident * 4 + 64, (
        f"the peak {after:.1f} MiB against a {resident:.1f} MiB working set -- the reader "
        "is off by a factor, and the usual one is a unit this platform did not expect"
    )
