"""Reading the CLI's progress stream. Pure functions, no Qt, no I/O.

This module exists on its own because it is the part of the GUI that can be tested
properly. A widget test can only really assert that a widget was constructed; what
actually goes wrong in a GUI like this is the plumbing -- a line that does not parse, a
field that is missing, a stream that ends early. All of that is decided here, in plain
Python, with no window in sight.

**Nothing in this module may import PySide6.** The same goes for
:mod:`gigaxml.gui.cli_process`; see the note there.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: The value of the ``event`` key that marks a progress line. Anything else that parses
#: as a JSON object is not something this version knows how to consume, and is ignored
#: rather than treated as an error -- a newer CLI emitting a new event type should not
#: break an older window.
PROGRESS_EVENT = "progress"


@dataclass(frozen=True)
class Progress:
    """One line of the CLI's progress stream.

    The fields are the ones the CLI documents. ``records`` counts every record the run
    has accounted for, including any a resume skipped, so a progress bar built from it
    continues rather than restarts. ``rows`` counts rows accepted for writing, which can
    be lower than ``records`` -- a quarantined record is consumed but writes nothing.
    ``part`` is present only for a checkpointed run.
    """

    records: int
    rows: int
    rejected: int
    elapsed_seconds: float
    part: int | None = None

    @property
    def missing_rows(self) -> int:
        """Records consumed that produced no row. Quarantined records, normally."""
        return max(0, self.records - self.rows)


def parse_line(line: str) -> Progress | None:
    """Turn one stderr line into a :class:`Progress`, or ``None`` if it is not one.

    The CLI writes progress as JSON objects and warnings as plain text, deliberately, so
    that a consumer tells them apart by trying to parse. This is that consumer.

    Returns ``None`` -- rather than raising -- for anything that is not a progress line:
    a warning, a blank line, a partial line from a process that died mid-write, or a
    JSON object with a different ``event``. A GUI must not fall over because the child
    printed something unexpected; the original line is still available to show the user.
    """
    stripped = line.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("event") != PROGRESS_EVENT:
        return None

    records = _as_int(payload.get("records"))
    rows = _as_int(payload.get("rows"))
    if records is None or rows is None:
        # A progress line without counts is not usable as progress. Treating it as one
        # would mean showing a number nobody sent.
        return None

    return Progress(
        records=records,
        rows=rows,
        rejected=_as_int(payload.get("rejected")) or 0,
        elapsed_seconds=_as_float(payload.get("elapsed_seconds")) or 0.0,
        part=_as_int(payload.get("part")),
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def fraction_done(done: int, total: int | None) -> float | None:
    """``done / total``, or ``None`` when the total is unknown.

    The total comes from ``inspect``'s exact per-candidate count. When the user has
    edited the record path by hand there is no such count, and the honest answer is to
    show no percentage at all: an invented denominator produces a bar that fills at the
    wrong speed, which is worse than a bar that only counts.
    """
    if total is None or total <= 0:
        return None
    return min(1.0, max(0.0, done / total))


def format_eta(elapsed_seconds: float, done: int, total: int | None) -> str:
    """A rough "time remaining", or an empty string when it cannot be known.

    Returns nothing before there is enough to go on: with no total, or with nothing
    done yet, any estimate would be a guess dressed up as information. The caller shows
    the raw counts in that case.
    """
    if total is None or done <= 0 or elapsed_seconds <= 0:
        return ""
    remaining = total - done
    if remaining <= 0:
        return "done"
    per_record = elapsed_seconds / done
    seconds = int(remaining * per_record)
    if seconds < 60:
        return f"{seconds}s left"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s left"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m left"
