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

from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME

__all__ = ["RunFailure", "read_failure", "report_path_for"]


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


def read_failure(output: Path, *, checkpointing: bool = False) -> RunFailure | None:
    """The failure a run left beside ``output``, or ``None`` if it did not fail.

    ``None`` has two meanings and they are both "nothing to report": the run succeeded, or
    there is no report and nothing says it failed. The second is the caller's business --
    a run that exited non-zero with no report is still a failure, and
    :func:`failure_from_stderr` is what covers it.
    """
    path = report_path_for(output, checkpointing=checkpointing)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
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
    """
    first = next((line.strip() for line in lines if line.strip()), "")
    return RunFailure(
        error_type=error_type,
        message=first or f"the run failed with exit code {exit_code}",
        output_complete=False,
        partial_path=None,
        had_report=False,
    )
