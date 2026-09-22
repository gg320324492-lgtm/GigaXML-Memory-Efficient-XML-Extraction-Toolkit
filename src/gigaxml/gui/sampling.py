"""Reading what ``sample`` wrote. No Qt, no window, no parsing of XML.

**Why a temporary file rather than stdout.** ``sample`` has no stdout mode. ``-o -`` is
rejected outright -- *"cannot infer an output format from '-'"* -- and adding ``--format``
makes it print the run summary and still write no rows anywhere. So a preview is: run the
child with ``-o <somewhere>``, then read that file back. The alternative, reading the
document in the window's process, is the one thing this application exists not to do.

**One mechanism, two callers.** The preview panel samples the config the user is editing;
the structure panel samples a config generated from a candidate, to show what values that
candidate actually holds. Both go through :func:`sample_args` and :func:`table_from`, so
the second is not a second implementation -- which is what the brief asks for when it says
the example values must come from the preview's mechanism.

**The rejected records come from the child, not from us.** ``sample`` writes
``rejected.jsonl`` beside its output when the config says ``on_error: quarantine``, and
names it in the summary. Nothing here decides what counts as a rejection; the file is read
and shown.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "SampledTable",
    "discard_run_directory",
    "generate_config_args",
    "make_run_directory",
    "read_rejected",
    "read_table",
    "sample_args",
    "table_from",
]

#: The format the preview asks for. CSV because a spreadsheet-shaped grid of text is what
#: the panel shows, and because it needs no extra dependency to read back.
SAMPLE_SUFFIX = ".csv"

#: Every directory this module makes begins with this, and :func:`discard_run_directory`
#: refuses to touch anything that does not. The guard is the point: a cleanup routine that
#: removes whatever path it is handed is one bad argument away from deleting something
#: that was not its to delete.
RUN_PREFIX = "gigaxml-"


def make_run_directory(prefix: str = "gigaxml-preview-") -> pathlib.Path:
    """A fresh directory for one run's output.

    Fresh rather than reused: ``sample`` writes ``rejected.jsonl`` beside its output, so a
    previous run's rejections would otherwise still be sitting there to be read as if they
    belonged to this one.
    """
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def discard_run_directory(directory: pathlib.Path | None) -> bool:
    """Remove a directory this module made, and only one of those.

    Called before a panel starts its next run, so a session that samples a hundred times
    leaves one directory behind rather than a hundred. Returns whether anything was
    removed.

    **Refuses anything whose name does not start with** :data:`RUN_PREFIX`, and anything
    that is not a directory. A cleanup helper that deletes whatever it is given is a
    helper that will eventually be given the wrong thing -- and this one runs unattended,
    once per preview.
    """
    if directory is None:
        return False
    target = pathlib.Path(directory)
    if not target.name.startswith(RUN_PREFIX):
        return False
    if not target.is_dir():
        return False
    shutil.rmtree(target, ignore_errors=True)
    return True


def sample_args(
    source: pathlib.Path | str,
    config: pathlib.Path | str,
    limit: int,
    output: pathlib.Path | str,
) -> list[str]:
    """The command line for one preview.

    ``-n`` and ``-o`` are both required by the subcommand; passing the limit through
    rather than sampling everything and truncating here means the panel shows what the CLI
    would write, which is the only thing it can honestly show.
    """
    return [
        "sample",
        str(source),
        "-c",
        str(config),
        "-n",
        str(limit),
        "-o",
        str(output),
    ]


def generate_config_args(
    source: pathlib.Path | str,
    destination: pathlib.Path | str,
    candidate: int,
) -> list[str]:
    """The command line that turns a candidate into a runnable config.

    ``--candidate`` is 1-based, matching what ``inspect`` prints and what the candidate
    table shows. Used by the structure panel so its example values come from the same
    sampling mechanism the preview uses.
    """
    return [
        "inspect",
        str(source),
        "--generate-config",
        str(destination),
        "--candidate",
        str(candidate),
    ]


@dataclass(frozen=True)
class SampledTable:
    """What one preview produced."""

    headers: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    requested: int = 0
    written: int = 0
    rejected: int = 0
    rejected_path: pathlib.Path | None = None
    short_of_request: bool = False
    document_exhausted: bool = False
    note: str = ""
    #: Everything the child said on stdout, kept so nothing is lost by being summarised.
    summary: Mapping[str, object] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def read_table(path: pathlib.Path | str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Read a written sample back as headers and rows.

    CSV and JSON Lines are both understood, because both are formats ``sample`` can write
    and a caller that switched the suffix should not be told its output is unreadable.
    Every cell is returned as text: the panel is showing what the file says, and the file
    says text.
    """
    location = pathlib.Path(path)
    if not location.is_file():
        return (), ()
    text = location.read_text(encoding="utf-8")
    if location.suffix.lower() in {".jsonl", ".ndjson"}:
        return _table_from_jsonl(text)
    return _table_from_csv(text)


def _table_from_csv(text: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        headers = tuple(next(reader))
    except StopIteration:
        return (), ()
    return headers, tuple(tuple(row) for row in reader)


def _table_from_jsonl(text: str) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Headers in first-seen order across every object, so a missing key is visible."""
    objects: list[dict[str, object]] = []
    headers: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        objects.append(payload)
        for key in payload:
            if key not in headers:
                headers.append(key)
    rows = tuple(tuple(_cell(payload.get(key)) for key in headers) for payload in objects)
    return tuple(headers), rows


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def read_rejected(path: pathlib.Path | str | None) -> tuple[dict[str, str], ...]:
    """The rejected records, as the child wrote them.

    One JSON object per line, each carrying ``index``, ``record_path``, ``error``,
    ``message``, ``field`` and ``raw``. Missing or unreadable means no rejections, which
    is the ordinary case -- ``rejected.jsonl`` only exists under ``on_error: quarantine``.
    """
    if path is None:
        return ()
    location = pathlib.Path(path)
    if not location.is_file():
        return ()
    entries: list[dict[str, str]] = []
    for line in location.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            entries.append({str(key): _cell(value) for key, value in payload.items()})
    return tuple(entries)


def table_from(summary: Mapping[str, object] | None, output: pathlib.Path | str) -> SampledTable:
    """Turn a finished run's summary and its output file into a table.

    The counts come from the child's summary rather than from counting rows here: if the
    file and the summary disagree, that disagreement is worth seeing, and recomputing one
    from the other would hide it.
    """
    payload = summary or {}
    headers, rows = read_table(output)
    rejected_path = payload.get("rejected_path")
    return SampledTable(
        headers=headers,
        rows=rows,
        requested=_as_int(payload.get("requested")),
        written=_as_int(payload.get("written")),
        rejected=_as_int(payload.get("rejected")),
        rejected_path=pathlib.Path(rejected_path) if isinstance(rejected_path, str) else None,
        short_of_request=bool(payload.get("short_of_request")),
        document_exhausted=bool(payload.get("document_exhausted")),
        note=payload.get("note") if isinstance(payload.get("note"), str) else "",
        summary=payload,
    )


def _as_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value
