"""Reading the CLI's ``run-report.json``.

Qt-free on purpose: what a failed run *says* is worth testing without a window, and the
interesting part of ⑩ is the reading and the classification rather than the drawing.

**Why a report at all.** The CLI prints its summary to stdout only when the run succeeds;
on failure stdout is empty and the message goes to stderr as prose. The report is written
either way, and it carries ``error.type`` -- the exception class name. So the GUI can say
what kind of failure this was without matching any wording, which is the same rule the
project already follows for library messages: assert the limit, not the phrasing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from gigaxml.checkpoint import CHECKPOINT_FILENAME, CheckpointError, read_checkpoint
from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME
from gigaxml.writers import PARTIAL_SUFFIX

__all__ = [
    "LeftBehind",
    "RunFailure",
    "RunOutcome",
    "left_behind",
    "read_failure",
    "read_outcome",
    "report_path_for",
]


@dataclass(frozen=True)
class LeftBehind:
    """What a run that stopped left on disk.

    ``path`` is a ``.tmp`` in the ordinary case and the **parts directory** when
    checkpointing, because those two modes keep their incomplete work in different places:
    one file half-written, or a directory of finished parts and a manifest saying how far
    it got. The counts are ``None`` unless there is a manifest to read them from -- they are
    what the tool recorded, never a count taken from a file that is by definition not
    finished.
    """

    #: The half-written output, or the parts directory.
    path: Path
    #: Rows across the parts the manifest knows about, when there is a manifest.
    recorded_rows: int | None
    #: How many parts the manifest knows about, when there is one.
    recorded_parts: int | None

    @property
    def is_directory(self) -> bool:
        return self.path.is_dir()


@dataclass(frozen=True)
class RunOutcome:
    """What a run that finished actually produced.

    Read from the same report the failure path reads, and by the same rule: take the
    structured fields and never parse prose. ``rows`` and ``rejected`` are the two numbers
    worth showing; ``rejected`` matters most when it is not zero, because a run that
    skipped records still reports success and the count is the only sign.
    """

    #: Records written to the output.
    rows: int
    #: Records the extractor refused.
    rejected: int
    #: Where those refusals were written, when there were any.
    rejected_path: Path | None
    #: Wall-clock seconds the CLI reported.
    elapsed_seconds: float | None
    #: The finished file, as the CLI recorded it.
    output: Path | None
    #: The format it was written in.
    format: str | None


@dataclass(frozen=True)
class RunFailure:
    """What the report says about a run that did not finish.

    ``error_type`` is the CLI's own classification and the only thing worth dispatching on.
    It is ``None`` when there is no report to read at all -- which is a real case rather
    than a missing one: a config that will not load fails before the report is created, so
    the run leaves nothing behind but stderr.
    """

    #: The exception class name from the report, e.g. ``FieldTypeError``.
    error_type: str | None
    #: The CLI's message. Prose, shown to the user, never matched against.
    message: str
    #: Whether the finished file actually made it onto the target path.
    output_complete: bool
    #: Where the incomplete output is, when there is one.
    partial_path: Path | None
    #: Whether a report was found at all. ``False`` means the config never loaded.
    had_report: bool


def report_path_for(output: Path, *, checkpointing: bool = False) -> Path:
    """Where the CLI writes its run summary for this output.

    This restates the CLI's own rule -- beside the output, except when checkpointing, where
    ``--output`` names the parts directory and the summary goes inside it. The GUI has to
    *find* the file rather than name it, so the rule has to exist on this side too; what
    keeps the two from drifting apart is
    ``test_the_report_is_where_the_cli_put_it``, which runs the CLI without ``--report``
    and checks that this finds what it wrote.
    """
    name = DEFAULT_RUN_REPORT_FILENAME
    return (output / name) if checkpointing else (output.parent / name)


def left_behind(output: Path, *, checkpointing: bool = False) -> LeftBehind | None:
    """What a run to ``output`` left behind, if it did not finish.

    **The two modes keep their unfinished work in different places, and only one of them
    leaves a ``.tmp``.**

    Writing one file, the writer creates ``<output>.tmp`` before it writes anything and only
    gets rid of it by renaming it onto the target -- so its presence means the run that made
    it never got that far, and its absence means nothing was started.

    Checkpointing, every part is complete by construction and is renamed into place the
    moment it is full, so a ``.tmp`` exists only for the instant a part is being written.
    A run stopped between parts leaves **none**, and the thing that says it did not finish is
    the manifest's ``complete`` flag -- which is exactly what the CLI reads for its own
    ``output_complete`` in this mode. Reading the manifest here is the same decision, made
    on the same file.
    """
    if not checkpointing:
        path = output.with_name(output.name + PARTIAL_SUFFIX)
        return (
            LeftBehind(path=path, recorded_rows=None, recorded_parts=None)
            if path.is_file()
            else None
        )

    manifest = output / CHECKPOINT_FILENAME
    if manifest.is_file():
        try:
            checkpoint = read_checkpoint(manifest)
        except CheckpointError:
            # A manifest that cannot be read is a run that cannot be described, not one that
            # finished. Reported as unfinished, because something is there and it is not a
            # finished run.
            return LeftBehind(path=output, recorded_rows=None, recorded_parts=None)
        if not checkpoint.complete:
            return LeftBehind(
                path=output,
                recorded_rows=checkpoint.rows,
                recorded_parts=len(checkpoint.parts),
            )
        return None

    # No manifest: stopped before it wrote one. A part still in flight is the only trace.
    in_flight = sorted(output.glob(f"*{PARTIAL_SUFFIX}"))
    if in_flight:
        return LeftBehind(path=in_flight[-1], recorded_rows=None, recorded_parts=None)
    return None


def read_outcome(output: Path, *, checkpointing: bool = False) -> RunOutcome | None:
    """The summary of a run that finished, or ``None`` if there is no report to read.

    ``None`` is not an error here: a run can finish without leaving a report if the CLI was
    pointed at a different ``--report``, and the caller still knows it succeeded from the
    exit code. It says nothing about whether the run was interrupted -- that is
    :func:`left_behind`'s question, and it is asked separately.

    **In checkpoint mode the counts come from the manifest, not from the report's top level.**
    The report's ``rows`` is the *last part's* writer -- measured: an eight-record run in
    parts of two reports ``rows: 2`` -- because a run has one writer per part and only the
    last one is in hand at the end. The manifest is the record of the whole run, which is
    why the CLI reads it for ``output_complete`` in this mode, and the same reason applies
    to the numbers.
    """
    payload = _read_payload(output, checkpointing=checkpointing)
    if payload is None:
        return None
    rows = payload.get("rows")
    rejected = payload.get("rejected")
    if checkpointing:
        manifest = _read_manifest(output)
        if manifest is not None:
            rows, rejected = manifest.rows, manifest.rejected
    rejected_path = payload.get("rejected_path")
    elapsed = payload.get("elapsed_seconds")
    written = payload.get("output")
    return RunOutcome(
        rows=rows if isinstance(rows, int) else 0,
        rejected=rejected if isinstance(rejected, int) else 0,
        rejected_path=Path(rejected_path) if isinstance(rejected_path, str) else None,
        elapsed_seconds=float(elapsed) if isinstance(elapsed, (int, float)) else None,
        output=Path(written) if isinstance(written, str) else None,
        format=payload.get("format") if isinstance(payload.get("format"), str) else None,
    )


def _read_manifest(parts_dir: Path):  # noqa: ANN202 - a Checkpoint, or None
    """The manifest for a checkpointed run, or ``None`` if it cannot be read."""
    try:
        return read_checkpoint(parts_dir / CHECKPOINT_FILENAME)
    except CheckpointError:
        return None


def _read_payload(output: Path, *, checkpointing: bool) -> dict[str, object] | None:
    """The report as a mapping, or ``None`` if there is not a usable one."""
    path = report_path_for(output, checkpointing=checkpointing)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def read_failure(output: Path, *, checkpointing: bool = False) -> RunFailure | None:
    """The failure a run left beside ``output``, or ``None`` if it did not fail.

    ``None`` has two meanings and they are both "nothing to report": the run succeeded, or
    there is no report and nothing says it failed. The second is the caller's business --
    a run that exited non-zero with no report is still a failure, and
    :func:`failure_from_stderr` is what covers it.
    """
    payload = _read_payload(output, checkpointing=checkpointing)
    if payload is None:
        return None
    error = payload.get("error")
    if error is None:
        return None
    if not isinstance(error, dict):
        return None
    error_type = error.get("type")
    message = error.get("message")
    partial = payload.get("partial_path")
    return RunFailure(
        error_type=error_type if isinstance(error_type, str) else None,
        message=message if isinstance(message, str) else "",
        output_complete=payload.get("output_complete") is True,
        partial_path=Path(partial) if isinstance(partial, str) else None,
        had_report=True,
    )


def failure_from_stderr(
    lines: list[str], exit_code: int | None, error_type: str | None = None
) -> RunFailure:
    """The failure to show when there is no report to read.

    This is the config-that-will-not-load case: the CLI exits before it creates a report,
    so there is no ``error.type`` in a file. ``error_type`` can still be supplied by a
    caller that *has* one -- the loader raises in-process, so the GUI knows the exception's
    class even though nothing was written down. Passing it is what lets a bad namespace
    prefix reach the namespace advice instead of the general one.

    With nothing to go on, ``error_type`` stays ``None`` and nothing here tries to invent
    one. Matching the wording would be the one thing worse than not classifying -- it would
    break the first time a message is reworded, and it would look like it still worked.

    **Every line is kept, not just the first.** The CLI writes some failures as a short
    sentence followed by indented detail -- a refused ``--resume`` names the source size and
    sha256 it recorded beside the ones it found -- and that detail is the part the user can
    act on. Taking the first line alone turns "here are the two values that differ" into
    "cannot resume", which is the thing the CLI's own message exists to avoid. A leading
    ``error: `` is dropped because it is the CLI's log prefix rather than part of what it
    said; the report's ``error.message`` has no prefix, so dropping it here keeps the two
    sources saying the same thing.
    """
    said = [line.rstrip() for line in lines if line.strip()]
    if said:
        said[0] = said[0].strip().removeprefix("error: ")
    return RunFailure(
        error_type=error_type,
        message="\n".join(said) or f"the run failed with exit code {exit_code}",
        output_complete=False,
        partial_path=None,
        had_report=False,
    )
