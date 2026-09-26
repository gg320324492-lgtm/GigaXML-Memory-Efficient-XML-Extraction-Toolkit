"""The packaged binary, exercised as a packaged binary.

**What this is not.** Gate 11 could not check this and recorded why: its probe dressed a
normal interpreter up as a frozen one, got ``rc=0``, and printed ``Python 3.13.14`` because
the interpreter ate the arguments. That result proved nothing and would have been easy to
mistake for a pass. So every assertion below runs the artifact, and where a claim is about
what a *child* did, the child is identified by pid from outside the process that started it.

Skipped, loudly, when there is no build to test -- these are not a substitute for the
pipeline's own smoke test, which runs on all three platforms after each tagged build.
They exist so that a local ``pyinstaller`` run is checked rather than assumed.
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
from typing import Final

import psutil
import pytest

from tests._frozen_gui_probe import REPO_ROOT, probe_frozen_gui

ARTIFACT: Final = REPO_ROOT / "dist" / "gigaxml-gui" / "gigaxml-gui.exe"

pytestmark = pytest.mark.performance


def _require_artifact() -> pathlib.Path:
    """The built binary, or a skip that says how to make one."""
    if not ARTIFACT.is_file():
        pytest.skip(
            f"no frozen build at {ARTIFACT}; run:\n"
            "  python -m PyInstaller packaging/gigaxml.spec --noconfirm "
            "--distpath dist --workpath build"
        )
    return ARTIFACT


@pytest.fixture(scope="module")
def dataset(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """A document big enough that the progress stream has several entries in it.

    Two megabytes finished in 0.073 s and produced one progress line, which reads like a
    broken progress bar and is not one. The size is chosen so the stream has something to
    report, not so the test takes long.
    """
    from gigaxml.cli import main as cli_main

    target = tmp_path_factory.mktemp("frozen-data") / "s20.xml"
    code = cli_main(["generate", "--size", "20MB", "--seed", "11", "-o", str(target)])
    assert code == 0, f"the generator failed: {code}"
    assert target.is_file()
    return target


def test_the_artifact_answers_its_own_version() -> None:
    """`--version` must come from this project's parser, not from an interpreter's.

    The specific failure this catches: something on PATH answers, or the executable is
    really a shim, or the arguments are consumed before the application's parser sees them.
    A Python version printed here would be the tell.
    """
    artifact = _require_artifact()
    env = dict(os.environ)
    # Only the artifact's own directory: nothing that could satisfy a console script.
    env["PATH"] = str(artifact.parent)
    env.pop("PYTHONHOME", None)

    completed = subprocess_run([str(artifact), "--version"], env)

    assert completed.returncode == 0, completed.stderr
    from gigaxml import __version__

    assert completed.stdout.strip() == f"gigaxml {__version__}", (
        f"expected our own version line, got {completed.stdout.strip()!r}"
    )


def test_the_artifact_runs_the_cli_without_anything_installed(
    tmp_path: pathlib.Path,
) -> None:
    """`generate` then `inspect`, with an empty PATH beside the artifact.

    The claim is that the packaged binary carries its own CLI, so a machine with nothing
    installed can still extract. Inspect is the stronger half: it needs no config file, so
    a failure can only mean the binary or its bundled CLI.
    """
    artifact = _require_artifact()
    env = dict(os.environ)
    env["PATH"] = str(artifact.parent)
    env.pop("PYTHONHOME", None)

    source = tmp_path / "gen.xml"
    generated = subprocess_run(
        [str(artifact), "generate", "--size", "2MB", "--seed", "3", "-o", str(source)],
        env,
    )
    assert generated.returncode == 0, generated.stderr
    assert source.is_file(), "the artifact could not generate a document"

    inspected = subprocess_run([str(artifact), "inspect", str(source), "--json"], env)
    assert inspected.returncode == 0, inspected.stderr
    report = json.loads(inspected.stdout)
    assert report["elements_seen"] > 0, report
    assert report["candidates"], report


def test_the_artifact_reports_progress_while_extracting(
    tmp_path: pathlib.Path,
    dataset: pathlib.Path,
) -> None:
    """Gate 4 on the packaged binary: the progress stream still arrives, and is real.

    **Progress goes to stderr**, with the summary on stdout. An earlier version of this
    check grepped the stdout capture, found nothing, and nearly reported a broken progress
    bar on a build that was working -- the same shape as the false green Gate 11 recorded,
    arrived at from the other direction.
    """
    artifact = _require_artifact()
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "record": "/catalog/products/product",
                "fields": {"product_id": {"path": "@id"}, "name": {"path": "name"}},
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out.csv"
    env = dict(os.environ)
    env["PATH"] = str(artifact.parent)
    env.pop("PYTHONHOME", None)

    completed = subprocess_run(
        [
            str(artifact),
            "extract",
            str(dataset),
            "-c",
            str(config),
            "-o",
            str(output),
            "--format",
            "csv",
            "--progress",
        ],
        env,
    )
    assert completed.returncode == 0, completed.stderr

    lines = [line for line in completed.stderr.splitlines() if '"event": "progress"' in line]
    assert lines, f"no progress lines in stderr:\n{completed.stderr[:400]}"

    records = [json.loads(line)["records"] for line in lines]
    assert records == sorted(records), "progress went backwards"
    assert records[-1] > records[0], "progress never advanced: only one entry"

    rows = sum(1 for _ in output.open(encoding="utf-8", newline="")) - 1
    assert rows == records[-1], (
        f"the last progress said {records[-1]} records but {rows} rows were written"
    )


def test_the_window_drives_its_own_cli(tmp_path: pathlib.Path, dataset: pathlib.Path) -> None:
    """G6 items 6 and 7: a packaged window, a real child, and a completed extraction.

    Three things have to hold together, and each could pass while another fails:

    * the window starts a child at all -- a ``None`` here would look like "nothing to do";
    * the child is the artifact itself, with no ``-m`` -- a module launch handed to a
      frozen build is a program with no module system, and the failure reads as "the app
      ignored my arguments";
    * the child finished the work -- the number of rows has to equal the records the GUI
      was last told about, which is the packaged form of Gate 11's G4.
    """
    artifact = _require_artifact()
    payload = probe_frozen_gui(artifact, dataset, tmp_path / "run")

    assert payload["child_count"] >= 1, f"the window started no child process: {payload}"
    assert payload["has_module_flag"] is False, (
        f"the command still asks for a module launch, which a frozen binary cannot do: "
        f"{payload['command_preview']}"
    )
    assert payload["exit_code"] == 0, payload
    assert payload["output_exists"] is True, payload
    assert payload["rows_written"] > 0, f"no rows were written: {payload}"

    # The GUI's own view, not the CLI's: the number that moved a progress bar.
    assert payload["gui_progress_updates"] > 1, (
        f"the GUI was told about progress only {payload['gui_progress_updates']} time(s), "
        f"so a packaged progress bar would not move: {payload}"
    )
    assert payload["gui_last_records"] == payload["rows_written"], (
        f"the GUI last saw {payload['gui_last_records']} records but "
        f"{payload['rows_written']} rows were written: {payload}"
    )

    child = payload["child_was_frozen"]
    assert child["parsed_by_artifact"] is True, (
        f"the child did not answer with this project's version, so something other than "
        f"the artifact is being run: {child}"
    )

    # Every child really existed as a process while the run was going. A pid that was
    # never alive would make the count above meaningless.
    for pid in payload["child_pids"]:  # type: ignore[union-attr]
        assert isinstance(pid, int)


def test_the_window_stays_up() -> None:
    """The window opens and does not exit straight away.

    The cheapest honest form of "it starts": a process that returns immediately has proved
    nothing about Qt loading, and one that crashes on a missing plugin says so here rather
    than on a user's machine.
    """
    artifact = _require_artifact()
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PATH"] = str(artifact.parent)
    env.pop("PYTHONHOME", None)

    process = subprocess.Popen(
        [str(artifact)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            process.wait(timeout=8)
        alive = True
    except Exception:  # pragma: no cover - the happy path is a TimeoutExpired above
        alive = process.poll() is None
    finally:
        if process.poll() is None:
            handle = psutil.Process(process.pid)
            for child in handle.children(recursive=True):
                child.kill()
            handle.kill()
        process.wait(timeout=10)

    assert alive, "the window exited instead of opening"


def test_the_release_notes_do_not_claim_a_version_the_build_does_not_have() -> None:
    """The notes must not describe a different build from the one being shipped.

    Added after the first draft said ``0.9.0`` while the binary said ``0.1.0``. That is the
    same failure as writing "signed" about an unsigned build: a document that is wrong
    about the thing a reader most needs to check. The number in the notes is read back and
    compared with the package, so the two cannot drift apart quietly.
    """
    _require_artifact()
    from gigaxml import __version__

    notes = (REPO_ROOT / "packaging" / "RELEASE_NOTES.md").read_text(encoding="utf-8")

    # The version cell, not any mention: the body is allowed to talk about other releases.
    row = next(
        (line for line in notes.splitlines() if line.strip().startswith("| **Version**")),
        None,
    )
    assert row is not None, "the release notes have no version row"
    assert __version__ in row, f"the notes claim {row.strip()!r} but this build is {__version__}"

    # And the signing claim has to be there, in the negative. A notes file that simply omits
    # the subject is how a reader ends up surprised by SmartScreen.
    lowered = notes.lower()
    assert "not code-signed" in lowered, "the release notes do not say the binaries are unsigned"
    for warning in ("smartscreen", "gatekeeper"):
        assert warning in lowered, f"the notes never mention {warning}"


def subprocess_run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a command and capture both streams as text."""
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env=env,
    )
