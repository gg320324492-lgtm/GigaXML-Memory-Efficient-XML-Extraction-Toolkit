"""What has been run: a list built by reading the CLI's own run reports.

**The rule this module exists to keep, and why it is not negotiable.** The plan says the
Job History and the run report are *the same data* — the report comes first, the GUI only
displays it, and the order cannot be reversed or the GUI would have to parse the checkpoint
format itself. So every number in a :class:`HistoryEntry` below is read out of a
``run-report.json`` the CLI wrote. **Nothing here re-parses a document, and nothing here
reads a ``checkpoint.json``.** The only thing this module keeps of its own is a list of
*directories to look in*, which is a pointer and not a record: the same distinction
``RecentFiles`` draws when it remembers which documents were opened.

**Why that distinction is the whole design.** The alternative — the GUI writing its own
``jobs.json`` with its own copy of the numbers — is what this project has spent eleven gates
not doing, in a different place: two sources that agree today and drift tomorrow, and a
regression scanner reading a history file that was never a measurement. So when the report
lacks a field, the answer is :data:`None` and a row that says so, never a zero and never a
second opinion.

**A missing measurement must look missing.** :func:`gigaxml.run.peak_rss_mb` returns
``None`` on a platform that cannot measure, and says in its own docstring that filling the
gap with ``0.0`` would be worse than omitting it. That reasoning is inherited here: a history
row reading ``0.0`` MiB peak looks like a run that allocated nothing, and a reader scanning
for a regression would find it plausible. So ``rows``, ``rejected``, ``elapsed_seconds`` and
``peak_rss_mb`` are all ``int | None`` / ``float | None``, and the panel is required to render
the difference between "zero" and "not recorded" — see
``test_gui_history.py::test_a_report_that_cannot_be_read_is_shown_not_hidden``.

**Nothing here may import PySide6**, for the reason every other module in ``gigaxml.gui``
carries in its docstring: the reading, the sorting and the corrupt-file cases are worth
testing without a display, and a test that needs a window is a test that will not be run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from gigaxml.checkpoint import (
    CHECKPOINT_FILENAME,
    Checkpoint,
    CheckpointError,
    read_checkpoint,
)
from gigaxml.run import DEFAULT_RUN_REPORT_FILENAME

__all__ = [
    "DEFAULT_HISTORY_LIMIT",
    "HistoryEntry",
    "JobHistory",
    "directory_to_scan",
    "interrupted_run",
    "read_report",
    "scan",
]

#: How many directories to remember. Long enough to cover a working life, short enough that
#: a scan stays instant — the cost of scanning is one ``read_text`` per directory, not a walk
#: of anything, so this is about how much the list is worth looking at.
DEFAULT_HISTORY_LIMIT = 50


def directory_to_scan(output: Path | str, *, checkpointing: bool) -> Path:
    """Where the report for a run to ``output`` will be found.

    **The same rule the CLI uses, restated on this side**, because the GUI has to *find* a
    report rather than be handed one. The CLI puts the summary beside the output file, and
    inside the output directory when ``--output`` names the parts directory instead — see
    :func:`gigaxml.gui.run_report.report_path_for`, which is the version with the test that
    keeps the two from drifting. This returns the *directory* to scan rather than the report
    path, because one directory is what gets remembered.
    """
    target = Path(output)
    return target if checkpointing else target.parent


@dataclass(frozen=True)
class HistoryEntry:
    """One past run, as its report describes it.

    Every field is ``| None`` because the report may not carry it, and a run that stopped
    early carries less than one that finished. ``readable`` is the outermost of them: it is
    ``False`` when the file is there and could not be turned into any of the rest, which is a
    different thing from a report that is not there at all.

    **``checkpoint_every`` is a reconstruction, and it is labelled as one.** The CLI requires
    ``--resume`` to be paired with ``--checkpoint-every`` (it is the part size, a parameter of
    the run rather than a preference), and the report does not record that flag by name. It
    is recovered from the largest part the report lists, which is the part size exactly: a
    part is committed when it is full, so only the last one can be short. :data:`None` when
    the report has no parts to read it from, which is a run that cannot be resumed anyway.
    """

    #: The report this entry was read from. Kept so a caller can show where it came from.
    report_path: Path
    #: The directory that was scanned to find it.
    directory: Path

    source: Path | None = None
    output: Path | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_seconds: float | None = None
    peak_rss_mb: float | None = None
    rows: int | None = None
    rejected: int | None = None
    config_hash: str | None = None
    record_path: str | None = None
    output_format: str | None = None
    status: str | None = None
    error_type: str | None = None

    #: The parts directory of a checkpointed run, when the report says there was one.
    checkpoint_directory: Path | None = None
    #: The part size, recovered from the parts the report lists. See the class docstring.
    checkpoint_every: int | None = None
    #: How far the run got, cumulative across resumes.
    records_consumed: int | None = None
    #: Whether the run consumed the whole source, and **which file says so**.
    #:
    #: ``"report"`` -- it reached its end and wrote a report, which says it did not finish.
    #: ``"manifest"`` -- it was stopped and wrote no report, so only the checkpoint knows.
    #: ``None`` -- neither says, and nothing can be claimed. A run that *failed* partway
    #: and one that was *stopped* partway both leave ``complete: false``, and only one had
    #: anything to say, which is why the two are kept apart.
    complete: bool | None = None
    complete_source: str | None = None

    #: ``False`` when the file exists but could not be read as a report. Everything else is
    #: ``None`` in that case, and :attr:`unreadable_reason` says what happened.
    readable: bool = True
    #: Why it could not be read, in words. Never inferred from a missing field.
    unreadable_reason: str | None = None
    #: Whether a report file was found at all. **``False`` with :attr:`readable` also false
    #: is not a corrupt file -- it is a run that was stopped, which never got to write one.
    #: See :func:`interrupted_run` for why that case is here rather than skipped.
    has_report: bool = True

    # -- what a caller asks ------------------------------------------------

    @property
    def is_ok(self) -> bool:
        """Whether the run finished.

        **``False`` for a report that could not be read**, and that is deliberate: an
        unreadable report is not evidence of success. The distinction a reader needs is
        between "it finished" and "we cannot tell", and this property cannot express the
        second — :attr:`is_known` is what pairs with it.
        """
        return self.status == "ok"

    @property
    def is_known(self) -> bool:
        """Whether the report was read at all, so the status is a fact rather than a guess."""
        return self.readable

    @property
    def can_resume(self) -> bool:
        """Whether this run stopped partway, and so may be continued.

        **Asks the report when there is one and the checkpoint when there is not** — see
        :attr:`complete_source`. Both are the same question asked of whichever file exists,
        and neither asks the other.

        **It does not promise the resume will work, and it must not.** Whether the parts on
        disk are still intact is ``validate_resume``'s business, checked by hashing both
        sides, and a history list that pre-judged it would be a second implementation of
        that check. So this can say "the run did not finish"; the run itself gets to say
        whether the source and the config still match, in as many words as it takes.
        """
        return self.checkpoint_directory is not None and self.complete is False

    @property
    def sort_key(self) -> tuple[int, float]:
        """Newest first: a run with no timestamp sorts below every run that has one.

        The negative timestamp is what puts the newest at the top without the caller having
        to reverse anything, and the leading flag keeps the runs that have no time at the
        bottom rather than interleaved with them at the epoch.
        """
        stamp = self.finished_at or self.started_at
        return (1, 0.0) if stamp is None else (0, -stamp.timestamp())


def _text(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    return value if isinstance(value, str) and value else None


def _number(payload: dict[str, object], key: str) -> float | None:
    """A numeric field, or ``None``. ``bool`` is excluded: it is an ``int`` in Python."""
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _count(payload: dict[str, object], key: str) -> int | None:
    value = payload.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _moment(payload: dict[str, object], key: str) -> datetime | None:
    """An ISO timestamp, or ``None``.

    ``Z`` is rewritten to ``+00:00`` because :meth:`datetime.fromisoformat` did not accept it
    before 3.11 and this package supports 3.11 through 3.13 — a report written by one
    interpreter and read by another must not be the thing that depends on the version.
    """
    text = _text(payload, key)
    if text is None:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _checkpoint_fields(payload: dict[str, object]) -> dict[str, object]:
    """The checkpoint block, or an empty mapping. Never raises on its shape."""
    block = payload.get("checkpoint")
    return block if isinstance(block, dict) else {}


def _largest_part_rows(block: dict[str, object]) -> int | None:
    """The part size, as the largest part the report lists.

    Read from the **report's** copy of the parts, not from the manifest. Both list the same
    parts, and the report is the one this module is allowed to read: a history list that
    opened ``checkpoint.json`` to answer a question the report could answer would be the
    second reader the module docstring rules out.
    """
    parts = block.get("parts")
    if not isinstance(parts, list):
        return None
    sizes = [
        part["rows"]
        for part in parts
        if isinstance(part, dict)
        and isinstance(part.get("rows"), int)
        and not isinstance(part.get("rows"), bool)
    ]
    return max(sizes) if sizes else None


def read_report(report_path: Path | str, *, directory: Path | str | None = None) -> HistoryEntry:
    """Read one report, whether or not it can be read.

    **Returns an entry either way, never ``None``.** A caller listing history has to be able
    to say "there is a report here and I could not read it" — which is a different row from
    "there is no report here", and a caller that only got the readable ones would show the
    user an empty list where a broken file is sitting on disk. That is the failure this
    return type exists to prevent.
    """
    path = Path(report_path)
    where = Path(directory) if directory is not None else path.parent
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return HistoryEntry(
            report_path=path,
            directory=where,
            readable=False,
            unreadable_reason=f"the report could not be read: {exc.strerror or exc}",
        )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return HistoryEntry(
            report_path=path,
            directory=where,
            readable=False,
            unreadable_reason=f"the report is not valid JSON: {exc}",
        )
    if not isinstance(payload, dict):
        return HistoryEntry(
            report_path=path,
            directory=where,
            readable=False,
            unreadable_reason="the report is not a JSON object",
        )

    block = _checkpoint_fields(payload)
    error = payload.get("error")
    error_block = error if isinstance(error, dict) else {}
    checkpoint_dir = _text(block, "directory")
    source = _text(payload, "source")
    output = _text(payload, "output")
    return HistoryEntry(
        report_path=path,
        directory=where,
        source=Path(source) if source is not None else None,
        output=Path(output) if output is not None else None,
        started_at=_moment(payload, "started_at"),
        finished_at=_moment(payload, "finished_at"),
        elapsed_seconds=_number(payload, "elapsed_seconds"),
        peak_rss_mb=_number(payload, "peak_rss_mb"),
        rows=_count(payload, "rows"),
        rejected=_count(payload, "rejected"),
        config_hash=_text(payload, "config_hash"),
        record_path=_text(payload, "record_path"),
        output_format=_text(payload, "format"),
        status=_text(payload, "status"),
        error_type=_text(error_block, "type"),
        checkpoint_directory=Path(checkpoint_dir) if checkpoint_dir is not None else None,
        checkpoint_every=_largest_part_rows(block),
        records_consumed=_count(block, "records_consumed"),
        complete=block.get("complete") if isinstance(block.get("complete"), bool) else None,
        complete_source="report" if isinstance(block.get("complete"), bool) else None,
    )


def interrupted_run(directory: Path | str) -> HistoryEntry:
    """A run that was stopped, and so left no report behind.

    **Why this row exists, and why it reads one more file.** Measured, on a 4 GiB document
    with parts of a thousand records, stopping the child at 4 s and at 6 s: 66 and 193 parts
    were on disk, the manifest said ``complete: false`` and named 65,000 and 192,000 records
    consumed, and ``run-report.json`` was **absent both times**. The reason is in the CLI's
    shape rather than in a policy: the summary is written on the way out, and a process that
    was terminated has no way out. So a history built from reports alone cannot show the
    runs that were interrupted -- which are exactly the runs somebody opens a resume manager
    for.

    **The manifest is read for one decision and one flag, and nothing else.** What the plan
    forbids is the GUI parsing the checkpoint format to *show data*: to add up parts for a
    row count, to work out how many records were consumed, to hash anything. None of that
    happens here. What this does is answer one question -- *may this be continued* -- and
    the manifest is its only authority; there is no report to ask. The execution panel has
    been reading the same file with the same reader since Phase 8A
    (:meth:`ExecutionPanel.unfinished_run_here`), so this is an existing path rather than a
    new kind of access.

    **Read through :func:`gigaxml.checkpoint.read_checkpoint`, and read with it:**

    * ``complete`` -- the one field the decision turns on;
    * the **part size**, as the largest part the manifest lists, because ``--resume`` needs
      ``--checkpoint-every`` and a run with no report has no other place to say what it was.

    Both come from the project's own reader, which checks the format version and the
    required keys. Parsing the JSON here would be a second answer to what a valid
    checkpoint is, and it would not notice a version it does not understand.

    **What is still not shown.** ``records_consumed`` is right there in the manifest and is
    deliberately left alone: it is a count to *display*, and displaying counts from the
    checkpoint is exactly what the rule draws the line at. The row says a run was stopped
    and can be continued, and the execution panel -- which has always shown that number --
    shows how far it got.
    """
    path = Path(directory)
    manifest = path / CHECKPOINT_FILENAME
    entry = HistoryEntry(
        report_path=path / DEFAULT_RUN_REPORT_FILENAME,
        directory=path,
        checkpoint_directory=path,
        readable=False,
        has_report=False,
        unreadable_reason="the run was stopped before it wrote a report",
    )
    try:
        checkpoint = read_checkpoint(manifest)
    except CheckpointError as exc:
        # A manifest that cannot be read is a run that cannot be described. Said rather
        # than swallowed: the row is still here, it just cannot promise a resume.
        reason = f"{entry.unreadable_reason}, and its checkpoint could not be read: {exc}"
        return replace(entry, unreadable_reason=reason)
    return replace(
        entry,
        # The path, handed on unchanged. **Not checked against anything** -- the CLI's
        # `validate_resume` compares this source's size and hash against the ones the
        # manifest recorded and refuses if they differ, and a pre-check here would either
        # duplicate that or make a promise the refusal is there to keep. All this needs to
        # do is put the argument back the way the panel had it.
        source=_recorded_path(checkpoint),
        complete=checkpoint.complete,
        complete_source="manifest",
        checkpoint_every=_part_size(checkpoint),
    )


def _recorded_path(checkpoint: Checkpoint) -> Path | None:
    """The source path a checkpoint recorded, or ``None`` if it recorded none.

    Read, not verified. The manifest stores the path beside a size and a hash precisely so
    that a later run can check all three, and checking is ``validate_resume``'s job; what
    is wanted here is the string to put on the command line.
    """
    recorded = checkpoint.source.get("path")
    return Path(recorded) if isinstance(recorded, str) and recorded else None


def _part_size(checkpoint: Checkpoint) -> int | None:
    """The part size, as the largest part a manifest lists.

    A part is committed when it is full, so only the last one can be short and the largest
    is the size exactly. ``None`` when the manifest lists no parts, which is a run stopped
    before it committed any -- there is nothing to size a continuation to.
    """
    sizes = [part.rows for part in checkpoint.parts]
    return max(sizes) if sizes else None


def scan(directories: list[Path | str] | tuple[Path | str, ...]) -> list[HistoryEntry]:
    """Every report under ``directories``, newest first.

    **One level, by name, never a walk.** The CLI names its summary
    ``run-report.json`` and puts it either beside the output or inside the parts directory,
    so the two cases are one lookup in a known directory. Recursing would be both slower
    and wrong: a parts directory can hold thousands of files, and a directory that happens
    to be named like a document is not somewhere a history list should be looking.

    Three cases, and the difference between the last two is the point:

    * a directory with a readable report — a run that reached its end, one way or another;
    * a directory with a report that cannot be parsed — a row saying so, never a silent drop;
    * a directory with a manifest and **no** report — a run that was stopped, which is
      :func:`interrupted_run`. Only the *presence* of the manifest is looked at; its
      contents are the CLI's, and this module does not open it.
    """
    entries: list[HistoryEntry] = []
    seen: set[Path] = set()
    for raw in directories:
        directory = Path(raw)
        if directory in seen:
            continue
        seen.add(directory)
        report = directory / DEFAULT_RUN_REPORT_FILENAME
        if report.is_file():
            entries.append(read_report(report, directory=directory))
        elif (directory / CHECKPOINT_FILENAME).is_file():
            entries.append(interrupted_run(directory))
    return sorted(entries, key=lambda entry: entry.sort_key)


class JobHistory:
    """The directories to look in, remembered across launches.

    **This stores pointers, not records.** The only thing written is a list of directories
    that have been run into; every fact about a run is read back out of the report the CLI
    left there. The distinction is what keeps the history honest: there is no way for a
    remembered row to disagree with the run it describes, because there is no remembered row.

    Args:
        store: the JSON file holding the directory list. Nothing is written by construction,
            so building a window does not touch the disk.
        limit: how many directories to keep. Older ones fall off the end.
    """

    def __init__(
        self,
        store: Path | str,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> None:
        self._store = Path(store)
        self._limit = limit

    @property
    def store(self) -> Path:
        """Where this list is kept. Exposed so a caller can tell the user."""
        return self._store

    def directories(self) -> tuple[Path, ...]:
        """The remembered directories, most recent first.

        A corrupt or absent store reads as an empty list rather than raising: this is a
        cache of where to look, and a window that will not open because it went bad is a
        worse outcome than a list that has to be rebuilt by running something.
        """
        return tuple(item for item, _ in self._records())

    def note(
        self,
        output: Path | str,
        *,
        checkpointing: bool,
        config: Path | str | None = None,
    ) -> None:
        """Remember where a run to ``output`` leaves its report.

        Called when a run ends, not when it starts, so the directory is one the CLI has
        actually written into. The directory is computed with the CLI's own placement rule
        — see :func:`directory_to_scan` — rather than by looking for the file afterwards,
        because a run that was cancelled before writing anything should still be
        remembered as a place worth looking.

        **``config`` is remembered as a path, and that is the one thing the report cannot
        give back.** ``--config`` is required by the CLI, and the report records the *hash of
        the parsed config* rather than where it was read from — deliberately, since the same
        extraction can be described by files in two places. So the GUI keeps the path it
        used, and the CLI's own ``validate_resume`` remains the only thing that decides
        whether the config still matches. A GUI that recomputed the hash to check first
        would be a second implementation of that check, which is the same duplication the
        module docstring rules out.
        """
        directory = directory_to_scan(output, checkpointing=checkpointing)
        if not str(directory).strip():
            return
        remembered = dict(self._records())
        known = [item for item in remembered if item != directory]
        pair = None if config is None else (directory, str(config))
        self._write([directory, *known], remembered=remembered, config=pair)

    def config_for(self, directory: Path | str) -> Path | None:
        """The config file remembered for ``directory``, if there is one.

        ``None`` means the panel must not guess: the report's ``config_hash`` cannot be
        turned back into a path, so a run whose config was not remembered is a run the
        Resume Manager has to ask about rather than start.
        """
        remembered = dict(self._records())
        stored = remembered.get(Path(directory))
        return Path(stored) if isinstance(stored, str) and stored else None

    def entries(self) -> list[HistoryEntry]:
        """Every remembered run, newest first. See :func:`scan` for what a row can say."""
        return scan(self.directories())

    def forget(self, directory: Path | str) -> None:
        """Stop looking in one directory. The user asking is the only reason to drop it."""
        target = Path(directory)
        remembered = dict(self._records())
        remembered.pop(target, None)
        self._write([item for item in self.directories() if item != target], remembered=remembered)

    def clear(self) -> None:
        self._write([])

    def _records(self) -> list[tuple[Path, str | None]]:
        """The stored ``(directory, config)`` pairs, most recent first."""
        try:
            payload = json.loads(self._store.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict):
            return []
        raw = payload.get("directories")
        if not isinstance(raw, list):
            return []
        configs = payload.get("configs")
        mapping = configs if isinstance(configs, dict) else {}
        return [
            (Path(item), mapping.get(item) if isinstance(mapping.get(item), str) else None)
            for item in raw
            if isinstance(item, str) and item.strip()
        ]

    def _write(
        self,
        directories: list[Path],
        *,
        remembered: dict[Path, str | None] | None = None,
        config: tuple[Path, str] | None = None,
    ) -> None:
        """Persist the list, trimmed to the limit.

        Written to a sibling temporary file and moved into place, for the reason
        ``RecentFiles._write`` gives: a store half-written by a crash would read back as
        empty, which loses every entry rather than one.

        ``config`` is the one ``(directory, path)`` pair being changed, named explicitly by
        the caller that already knows both halves. Every other remembered config is carried
        over untouched, and one whose directory has been dropped goes with it — a map entry
        pointing at a directory no longer scanned is a pointer to nothing.
        """
        trimmed = directories[: self._limit]
        known = dict(remembered or {})
        if config is not None:
            known[config[0]] = config[1]
        payload = {
            "directories": [str(item) for item in trimmed],
            "configs": {str(key): value for key, value in known.items() if value},
        }
        self._store.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._store.with_name(self._store.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self._store)
