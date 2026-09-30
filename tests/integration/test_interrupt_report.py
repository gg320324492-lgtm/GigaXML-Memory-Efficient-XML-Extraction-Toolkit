"""A run that was stopped leaves a report saying so.

**Why this needs a real child and a real signal.** The whole of the feature is what the
CLI does when a signal arrives, and nothing in-process exercises that: a signal handler is
installed by ``main`` and uninstalled when it returns, and a test that raised the exception
by hand would be testing its own construction of the situation rather than the code that
has to survive it. So every test here starts the CLI as a process, stops it the way a user
would, and reads the report it left.

**And the interruption is timed by the run, not by the clock.** The test waits for the
manifest to appear -- which the CLI writes as each part is committed, so its appearance is
the run being under way -- and only then sends the signal. A ``sleep`` would be a bet on
how fast the machine is, and that is the bet that made the last version of the progress
test green here and red on every CI platform. The document is built with enough parts that
there is work left after the manifest appears, so the signal cannot land after the run has
already finished.

**Every wait has an exit that is not a timeout.** Waiting for a positive condition with
nothing else to wait for turns "the thing never happened" into a three-minute hang whose
message says nothing. Each loop below also watches the child, so a child that died early or
ignored the signal is reported as that, in one sentence, immediately.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from gigaxml.checkpoint import CHECKPOINT_FILENAME, read_checkpoint
from gigaxml.errors import RunInterruptedError
from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME

#: How long a child may take to notice a signal, write its report and exit. Generous for a
#: slow runner, and short enough that a hang is a failure rather than a wait.
EXIT_TIMEOUT_S = 60.0

#: How long to wait for the manifest before deciding the run never got going.
MANIFEST_TIMEOUT_S = 120.0

#: Parts per document is 2,000 at ten records each, which is the shape that makes the
#: timing work: the manifest appears after the first part, and 1,999 are still to go.
RECORDS = 20_000
PART_SIZE = 10

#: Windows refuses Ctrl-C to a process in a new process group unless the child asks for it
#: back, so on that platform the child is this snippet wrapped around the normal entry
#: point rather than the console script. Everywhere else the command is left alone.
_WINDOWS_OPENER = (
    "import ctypes, sys;"
    "ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False);"
    "from gigaxml.cli import main;"
    "raise SystemExit(main(sys.argv[1:]))"
)


def _document(tmp_path: Path) -> Path:
    path = tmp_path / "big.xml"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("<root>")
        for number in range(RECORDS):
            handle.write(f"<item><id>{number}</id></item>")
        handle.write("</root>")
    return path


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps({"record": "/root/item", "fields": {"id": {"path": "id", "type": "int"}}}),
        encoding="utf-8",
    )
    return path


def _command(document: Path, config: Path, parts: Path) -> list[str]:
    args = [
        "extract",
        str(document),
        "-c",
        str(config),
        "-o",
        str(parts),
        "--format",
        "csv",
        "--checkpoint-every",
        str(PART_SIZE),
    ]
    if sys.platform == "win32":
        return [sys.executable, "-c", _WINDOWS_OPENER, *args]
    return [sys.executable, "-m", "gigaxml.cli", *args]


def _start(document: Path, config: Path, parts: Path) -> subprocess.Popen[str]:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":  # pragma: no cover - platform branch
        # A new process group is what lets the parent address the child alone; the child
        # opting back into Ctrl-C is what _WINDOWS_OPENER is for.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    return subprocess.Popen(
        _command(document, config, parts),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **kwargs,  # type: ignore[arg-type]
    )


def _wait_for_manifest(proc: subprocess.Popen[str], parts: Path, *, timeout: float) -> None:
    """Wait until the run has committed a part, or fail saying why it did not.

    **The child is watched as well as the file.** A run that never writes a manifest is one
    of three things -- it died, it is slower than the timeout, or the signal it was sent
    stopped it before it began -- and the first and third are worth telling apart from the
    second, so the loop checks rather than sleeps through.
    """
    manifest = parts / CHECKPOINT_FILENAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if manifest.is_file():
            return
        if proc.poll() is not None:
            _out, err = proc.communicate()
            pytest.fail(
                "the run ended before it committed a part, so there was nothing to stop. "
                f"exit code {proc.returncode}, stderr: {(err or '').strip()[-400:]}"
            )
        time.sleep(0.02)
    proc.kill()
    proc.communicate()
    pytest.fail(
        f"the run never committed a part in {timeout:.0f}s, so the signal would have "
        "landed after the work was done rather than during it"
    )


def _stop(proc: subprocess.Popen[str]) -> tuple[int, str]:
    """Send the stop signal and wait for the child to be gone.

    Raises:
        AssertionError: if the child outlives :data:`EXIT_TIMEOUT_S`, which on a platform
            where the signal does not reach a handler means it is running the extraction to
            completion and will report success. The timeout is the exit, not the only one:
            that run exits on its own shortly after, and the assertions below then fail on
            the status -- which names the fault far better than a wait does.
    """
    if sys.platform == "win32":  # pragma: no cover - platform branch
        proc.send_signal(signal.CTRL_C_EVENT)
    else:
        os.kill(proc.pid, signal.SIGINT)
    try:
        _out, err = proc.communicate(timeout=EXIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:  # pragma: no cover - only on a platform that ignores it
        proc.kill()
        _out, err = proc.communicate()
        pytest.fail(
            "the child ignored the stop signal and was still running after "
            f"{EXIT_TIMEOUT_S:.0f}s; stderr: {(err or '').strip()[-400:]}"
        )
    return proc.returncode, err or ""


# --- 判据 1, 2, 3: the report exists, the exit is non-zero, the numbers are real --


def test_a_stopped_run_writes_a_report_that_says_it_was_interrupted(
    tmp_path: Path,
) -> None:
    """**The feature.** Stop a real run; the report says ``interrupted`` and the exit is
    non-zero.

    Both halves matter and they fail in opposite ways. A report saying ``interrupted`` with
    an exit of 0 would tell a script the work was done; an exit of 1 with no report would
    leave nothing to read, which is the state this whole change exists to end.
    """
    document = _document(tmp_path)
    config = _config(tmp_path)
    parts = tmp_path / "parts"

    proc = _start(document, config, parts)
    try:
        _wait_for_manifest(proc, parts, timeout=MANIFEST_TIMEOUT_S)
        code, err = _stop(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an assertion failure
            proc.kill()
            proc.communicate()

    report_path = parts / DEFAULT_RUN_REPORT_FILENAME
    assert report_path.is_file(), (
        f"the run was stopped and left no report at all; stderr: {err.strip()[-400:]}"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert code != 0, "a stopped run exited 0, which reports success for a partial output"
    assert report["status"] == "interrupted", report["status"]
    assert report["output_complete"] is False, (
        "a stopped run left a complete output, or the report claims it did"
    )
    assert report["error"]["type"] == "RunInterruptedError"
    assert "interrupted" in report["error"]["message"], report["error"]
    assert "SIGINT" in err, f"the child did not say why it stopped: {err.strip()[-300:]}"


def test_the_numbers_in_that_report_are_the_ones_on_disk(tmp_path: Path) -> None:
    """**判据 3: measured, not zero and not guessed.**

    The manifest is the independent party. It is written as each part is committed, and it
    is not touched by the code that writes the report, so agreeing with it means the
    report's counts describe the run rather than describing the interruption.

    ``rows`` is compared against ``records_consumed`` divided by the part size rather than
    against a total: in checkpoint mode the report's top-level ``rows`` is the *last part's*
    writer, which ``gigaxml.gui.run_report.read_outcome`` documents and which is why the
    checkpoint block exists at all.
    """
    document = _document(tmp_path)
    config = _config(tmp_path)
    parts = tmp_path / "parts"

    proc = _start(document, config, parts)
    try:
        _wait_for_manifest(proc, parts, timeout=MANIFEST_TIMEOUT_S)
        code, _err = _stop(proc)
    finally:
        if proc.poll() is None:  # pragma: no cover - only on an assertion failure
            proc.kill()
            proc.communicate()
    assert code != 0, "the run was stopped but reported success"

    report_path = parts / DEFAULT_RUN_REPORT_FILENAME
    # Checked before reading rather than by the read failing. With the signal handling
    # removed, Python's own ``KeyboardInterrupt`` lands wherever it lands -- measured, in
    # the middle of ``os.replace`` inside ``write_checkpoint`` -- and nothing catches it, so
    # no report is written and the read raises ``FileNotFoundError`` about a file the user
    # has never heard of. The same cause, said in the same words in both tests, and said
    # here rather than as an exception from a line that is only the messenger.
    assert report_path.is_file(), (
        "the run was stopped and left no report at all; stderr: "
        f"{_err.strip()[-400:] if _err else ''}"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    manifest = read_checkpoint(parts / CHECKPOINT_FILENAME)

    block = report["checkpoint"]
    assert block["complete"] is False, "a stopped run recorded itself as having finished"
    assert block["parts"], "the run committed no part, so this says nothing about the counts"
    assert block["records_consumed"] >= manifest.records_consumed, (
        f"the report says {block['records_consumed']} records consumed, fewer than the "
        f"{manifest.records_consumed} the manifest says are committed on disk"
    )
    assert block["records_consumed"] - manifest.records_consumed < PART_SIZE, (
        f"the report is {block['records_consumed'] - manifest.records_consumed} records "
        f"ahead of the disk, which is more than the {PART_SIZE} of a single unwritten part"
    )
    # The parts, the same bound for the same reason: the report counts a part the moment
    # it is committed and the manifest is written just after, so a signal landing between
    # the two leaves the report one ahead. Measured, and it is the only shape seen.
    assert len(block["parts"]) - len(manifest.parts) in (0, 1), (
        f"the report lists {len(block['parts'])} parts against the manifest's "
        f"{len(manifest.parts)}; the report cannot be more than one ahead, because one "
        "part is being written at a time"
    )
    assert manifest.records_consumed > 0, "nothing was committed, so there is no number to trust"

    assert report["elapsed_seconds"] > 0, "a run that did work cannot have taken no time"
    assert report["peak_rss_mb"] is not None, "a measurement that cannot be taken must be absent"


# --- 判据 6: nothing else changed ------------------------------------------


def test_a_finished_run_still_says_ok(tmp_path: Path) -> None:
    """The handlers are installed and removed around every command, so the ordinary ones
    have to be untouched by that. Checked by the status rather than by the exit code, since
    that is what a report is read for."""
    document = tmp_path / "small.xml"
    document.write_text(
        "<root>" + "".join(f"<item><id>{n}</id></item>" for n in range(5)) + "</root>",
        encoding="utf-8",
    )
    config = _config(tmp_path)
    # A directory, because the command always carries --checkpoint-every; with a file
    # name there the run would create a directory of that name and put its report inside.
    out = tmp_path / "single"
    proc = _start(document, config, out)
    _out, _err = proc.communicate(timeout=EXIT_TIMEOUT_S)

    assert proc.returncode == 0
    report = json.loads((out / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["status"] == "ok", report["status"]
    assert report["output_complete"] is True


def test_a_failed_run_still_says_failed(tmp_path: Path) -> None:
    """A record that will not convert is a problem with the data, not with the run's
    ending -- and a signal is the other thing. Keeping the two apart is the whole reason
    ``status`` has three values rather than two."""
    document = tmp_path / "bad.xml"
    document.write_text("<root><item><id>not-a-number</id></item></root>", encoding="utf-8")
    config = _config(tmp_path)
    out = tmp_path / "single"
    proc = _start(document, config, out)
    _out, err = proc.communicate(timeout=EXIT_TIMEOUT_S)

    assert proc.returncode != 0
    assert "FieldTypeError" in err or "not convertible" in err, err.strip()[-300:]
    report = json.loads((out / DEFAULT_RUN_REPORT_FILENAME).read_text(encoding="utf-8"))
    assert report["status"] == "failed", report["status"]
    assert report["error"]["type"] == "FieldTypeError"


# --- the classification itself ---------------------------------------------


def test_the_three_statuses_come_from_the_error_and_not_from_the_words() -> None:
    """Dispatched on the class. Matching a message is the one thing worse than not
    classifying: it breaks the first time the message is reworded and looks like it still
    works, which is the failure this project has already paid for once."""
    from gigaxml.cli import _status_for
    from gigaxml.errors import ConfigError

    assert _status_for(None) == "ok"
    assert _status_for(ConfigError("anything at all")) == "failed"
    assert _status_for(RunInterruptedError("anything at all")) == "interrupted"
    # A message that says the same words does not change the classification.
    assert _status_for(ConfigError("the run was interrupted")) == "failed"


def test_an_interruption_carries_the_signal_it_came_from() -> None:
    """A number says nothing to somebody who pressed Ctrl-C."""
    exc = RunInterruptedError("the run was interrupted by SIGINT", signum=2, signame="SIGINT")
    assert exc.signum == 2
    assert exc.signame == "SIGINT"
    assert str(exc) == "the run was interrupted by SIGINT"
