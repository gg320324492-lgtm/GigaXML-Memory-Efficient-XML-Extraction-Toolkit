"""Checkpoints: committed parts, and the manifest that describes them.

A run with ``--checkpoint-every`` writes its output as a series of ``part-NNNNN.<ext>``
files in a directory, and after each part is committed it rewrites
``checkpoint.json``. An interrupted run can then be continued with ``--resume``.

**Resume is not a seek, and the wording has to say so.** There is no byte offset to
seek to: XML cannot be re-entered mid-stream without the parser state that got you
there. ``--resume`` re-parses the source from the beginning and *skips* the records
already accounted for. Measured on a 403 MB / 1,164,800-record file, iterating every
record and doing nothing costs 8.7s against 17.1s to extract and write it -- skipping
costs about **half** of processing. So resume saves roughly half of what was already
done, which is a lot when you were nearly finished and almost nothing when you were
just starting. Saying "continues where it left off" would imply otherwise, and this
module exists partly to avoid saying that.

**The two identities are what make resume safe.** A checkpoint records the source as
(path, size, full-file sha256) and the config as a hash of the *parsed* config, not of
the config file. The file hash would flag a run as changed after somebody fixed a typo
in a comment; the parsed hash will not. Hashing 403 MB costs 0.26s against the 8.7s
fast-forward, about 3%, so there is no reason to fall back on a size-and-mtime guess.

**Part size is not a memory knob.** :class:`~gigaxml.writers.ParquetWriter` writes one
row group per batch as the batch fills, so a part's total row count never accumulates
in memory -- output memory is decided by ``batch_size``. ``--checkpoint-every``
therefore controls *how much work an interruption costs you*, and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gigaxml.config import ExtractionConfig
from gigaxml.errors import CheckpointError
from gigaxml.writers import PARTIAL_SUFFIX, _import_pyarrow

__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "DEFAULT_PART_FORMAT",
    "Checkpoint",
    "PartRecord",
    "config_identity",
    "count_part_rows",
    "part_name",
    "read_checkpoint",
    "require_intact_parts",
    "source_identity",
    "validate_resume",
    "verify_parts",
    "write_checkpoint",
]

#: Name of the manifest, inside the parts directory.
CHECKPOINT_FILENAME: Final = "checkpoint.json"

#: Manifest format version, so a future change can be detected rather than misread.
CHECKPOINT_VERSION: Final = 1

#: Format used for parts when ``--format`` is not given. A directory has no extension
#: to infer from, so there is nothing else to go on.
DEFAULT_PART_FORMAT: Final = "parquet"

#: Bytes read at a time when hashing the source.
_HASH_CHUNK: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class PartRecord:
    """One committed part.

    Attributes:
        name: the part's file name, relative to the parts directory.
        rows: rows in it. Recorded rather than re-read: reading every part back to
            count rows would cost more than the thing being counted.
    """

    name: str
    rows: int

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "rows": self.rows}


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """The manifest describing a checkpointed run.

    Attributes:
        source: the source identity, as returned by :func:`source_identity`.
        config: the config identity, as returned by :func:`config_identity`.
        records_consumed: records taken from the reader so far, written or rejected.
            This is the number ``--resume`` fast-forwards past, so it has to count
            every record that was *dealt with*, not just the ones that were written.
        rejected: records quarantined so far.
        parts: the committed parts, in order.
        complete: whether the whole source was consumed.
        version: the manifest format version.
    """

    source: dict[str, object]
    config: str
    records_consumed: int
    rejected: int
    parts: tuple[PartRecord, ...]
    complete: bool
    version: int = CHECKPOINT_VERSION

    @property
    def rows(self) -> int:
        """Rows across every committed part."""
        return sum(part.rows for part in self.parts)

    def to_dict(self) -> dict[str, object]:
        """A JSON-serialisable view."""
        return {
            "version": self.version,
            "source": self.source,
            "config": self.config,
            "records_consumed": self.records_consumed,
            "rejected": self.rejected,
            "parts": [part.to_dict() for part in self.parts],
            "complete": self.complete,
        }


def part_name(index: int, extension: str) -> str:
    """The file name of part number ``index``, zero-padded so sorting works."""
    return f"part-{index:05d}.{extension}"


def source_identity(source: str | Path) -> dict[str, object]:
    """Identify a source file by path, size and content hash.

    The hash is over the whole file. It is the only component that can tell "the same
    file" from "a different file with the same name and length", and at 0.26s for
    403 MB it is cheap enough that a weaker test would be a false economy.
    """
    path = Path(source)
    try:
        size = path.stat().st_size
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CheckpointError(f"cannot identify the source {str(path)!r}: {exc}") from exc
    return {"path": str(path), "size": size, "sha256": digest.hexdigest()}


def config_identity(config: ExtractionConfig) -> str:
    """A hash of the *parsed* config, stable across cosmetic edits.

    Built from what the run actually depends on -- the record path, the namespace map,
    the error policy and each field's path and type, in config order -- rather than
    from the config file's bytes. Hashing the file would make "somebody added a
    comment" look like "the extraction changed", which is exactly the wrong way to be
    wrong: it would refuse a resume that is perfectly safe.
    """
    payload = {
        "record_path": config.record_path,
        "namespaces": dict(sorted(config.namespaces.items())),
        "on_error": config.on_error.value,
        "fields": [
            {
                "name": field.name,
                "path": field.raw_path,
                "type": field.type.value,
                "required": field.required,
            }
            for field in config.fields
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def count_part_rows(path: str | Path, extension: str, *, header: bool) -> int:
    """Count the data rows in one part.

    Parquet is read from its metadata, which costs nothing. The line-oriented formats
    are counted by reading the file, which is the only way to be sure -- and is worth
    it: this number is what stops a resumed run from skipping records that are not
    there.

    **This cost scales with the output, not the input.** Measured on a 403 MB source
    producing 12 parts / **24.9 MiB of CSV**, :func:`verify_parts` takes **660 ms**
    against 15.46 s for a full extraction, about **4.3%**. Ten times the output means
    ten times that -- a 250 MiB part set would cost roughly 6.6 s, every resume. For
    Parquet the metadata read makes it negligible, which is one more reason the default
    part format is Parquet.

    Args:
        path: the part file.
        extension: ``csv``, ``jsonl`` or ``parquet``.
        header: whether the part carries a header row. Only the first **CSV** part
            does -- JSONL and Parquet have no header -- so the caller passes
            ``extension == "csv" and index == 0``.
    """
    target = Path(path)
    if extension == "parquet":
        _pa, parquet = _import_pyarrow()
        return int(parquet.ParquetFile(target).metadata.num_rows)
    with target.open("rb") as handle:
        lines = sum(1 for _ in handle)
    return lines - 1 if header and lines else lines


def verify_parts(
    checkpoint: Checkpoint,
    parts_dir: str | Path,
    extension: str,
) -> list[str]:
    """Check every part the manifest names against the file on disk.

    Returns:
        One human-readable problem per part that is missing, unreadable, or has a
        different number of rows than the manifest claims. Empty when everything
        agrees.

    **Why this exists.** ``records_consumed`` is the number of records a resume skips.
    If a part it was derived from is gone, skipping that many records walks straight
    past rows that will never be written by anyone -- and the run finishes reporting
    success. That is a silent loss of data, which is the failure this project has
    spent every phase trying to eliminate; it is worse than an error, because nothing
    about the output says anything is wrong.

    **Cost.** One pass over the parts: measured at **660 ms for 24.9 MiB of CSV**,
    about 4.3% of a full extraction of the same source, and it grows with the size of
    the *output* rather than the input. Parquet parts are read from their metadata and
    cost almost nothing. The alternative is trusting a number that may describe files
    which are no longer there.
    """
    directory = Path(parts_dir)
    problems: list[str] = []
    for index, part in enumerate(checkpoint.parts):
        path = directory / part.name
        if not path.is_file():
            problems.append(f"missing:  {part.name}")
            continue
        try:
            found = count_part_rows(path, extension, header=extension == "csv" and index == 0)
        except Exception as exc:
            # Deliberately broad: a corrupt part can fail as an OSError from the
            # filesystem or as an Arrow error from the metadata reader, and every one
            # of those means the same thing here -- this part cannot be vouched for.
            # The exception is reported, never swallowed.
            problems.append(f"unreadable: {part.name} ({exc})")
            continue
        if found != part.rows:
            problems.append(f"rows differ: {part.name} (checkpoint {part.rows}, file {found})")
    return problems


def require_intact_parts(
    checkpoint: Checkpoint,
    parts_dir: str | Path,
    extension: str,
) -> None:
    """Refuse to continue when the parts on disk do not match the manifest.

    Raises:
        CheckpointError: any part is missing, unreadable, or a different size.
    """
    problems = verify_parts(checkpoint, parts_dir, extension)
    if not problems:
        return
    raise CheckpointError(
        "cannot resume: the checkpoint lists parts that are missing or changed.\n"
        + "\n".join(f"       {problem}" for problem in problems)
        + "\n       Nothing was written. Restore the parts, or remove the checkpoint "
        "to start over."
    )


#: The only shape a part name may have: exactly what :func:`part_name` writes, no
#: separators and no traversal. Every other spelling -- ``../``, a drive letter, an
#: absolute path, a plain other filename -- is rejected rather than joined onto the
#: parts directory, because a manifest chooses that name and a manifest is not ours.
#:
#: **Five digits or more**, because ``part_name`` pads to five and does not cap there:
#: ``f"part-{index:05d}"`` gives ``part-100000.csv`` at the hundred-thousandth part, and a
#: reader that only took five would refuse a file this tool had just written. Exactly five
#: would be the same bug facing the other way -- ``part-0000.csv`` is a name no run
#: produces, so the width is a floor and not a ceiling. See
#: ``tests/security/test_checkpoint_manifest.py``, which asserts both ends of that.
PART_NAME_PATTERN: Final = re.compile(r"^part-\d{5,}\.(?:csv|jsonl|parquet)$")

#: A sha256 as :meth:`hashlib.sha256().hexdigest` writes one: 64 **lowercase** hex
#: digits. The case is part of the check rather than folded away, because the value
#: written by this tool is lowercase -- an uppercased one is a file somebody edited.
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


def _untrusted(target: Path, detail: str) -> CheckpointError:
    """A refusal for a manifest whose *contents* are wrong.

    Every field-level rejection is raised through this so they all open the same way.
    A reader who sees ``cannot be trusted`` knows the manifest is the problem -- not the
    file's encoding, not their config, not the source document -- and knows that nothing
    was resumed. The detail after it says which field and what was expected, because
    "the checkpoint is malformed" leaves nobody with anything to do.
    """
    return CheckpointError(
        f"the checkpoint at {str(target)!r} cannot be trusted: {detail}. "
        "Nothing was resumed. Delete the checkpoint to start a fresh run."
    )


def _field(raw: dict[str, object], key: str, target: Path) -> object:
    """One manifest field, required and with no conversion of its type.

    The conversion this replaces -- ``int(raw[k])``, ``bool(raw[k])``, ``str(raw[k])`` --
    was the defect: ``int("100")`` and ``bool("false")`` never raise, so a manifest
    could hand back any value it liked under the type the caller expected. Checking with
    ``type(v) is ...`` instead means the value arrives exactly as JSON gave it, or the
    manifest is refused.
    """
    if key not in raw:
        raise _untrusted(target, f"it is missing {key!r}")
    value = raw[key]
    return value


def _integer(raw: dict[str, object], key: str, target: Path, *, minimum: int = 0) -> int:
    """A field that must be a JSON integer (not a bool, not a string) of ``minimum`` or more.

    ``type(...) is int`` rather than ``isinstance(..., int)`` is deliberate: ``bool`` is a
    subclass of ``int`` in Python, so ``isinstance(True, int)`` is True and a manifest
    carrying ``"records_consumed": true`` would sail through an isinstance check. The same
    distinction is why ``!=`` could not be used on ``version``: ``True == 1`` and
    ``1.0 == 1`` are both true, so a value comparison accepts a bool and a float.
    """
    value = _field(raw, key, target)
    if type(value) is not int:
        raise _untrusted(target, f"{key!r} must be a JSON integer, got {value!r}")
    if value < minimum:
        raise _untrusted(target, f"{key!r} must be at least {minimum}, got {value!r}")
    return value


def _hex_digest(raw: dict[str, object], key: str, target: Path) -> str:
    """A field that must be a sha256 digest as this tool writes one."""
    value = _field(raw, key, target)
    if type(value) is not str or not _SHA256_PATTERN.match(value):
        raise _untrusted(
            target, f"{key!r} must be 64 lowercase hexadecimal characters, got {value!r}"
        )
    return value


def _check_source(raw: dict[str, object], target: Path) -> dict[str, object]:
    """The ``source`` block, checked field by field.

    All three are required, not two: :func:`source_identity` writes all three and
    :func:`validate_resume` reads them all, so a manifest missing one describes a run
    that cannot be compared against anything. Refusing here rather than letting the
    mismatch surface as "the source has changed" keeps a malformed manifest from
    masquerading as a changed file.
    """
    source = _field(raw, "source", target)
    if type(source) is not dict:
        raise _untrusted(target, f"'source' must be a JSON object, got {source!r}")
    if "path" not in source or "size" not in source or "sha256" not in source:
        raise _untrusted(target, "'source' must hold 'path', 'size' and 'sha256'")
    if type(source["path"]) is not str:
        raise _untrusted(target, f"'source.path' must be a string, got {source['path']!r}")
    _integer(source, "size", target)
    _hex_digest(source, "sha256", target)
    return dict(source)


def _check_parts(raw: dict[str, object], target: Path) -> tuple[PartRecord, ...]:
    """The ``parts`` list, one entry at a time."""
    parts = _field(raw, "parts", target)
    if type(parts) is not list:
        raise _untrusted(target, f"'parts' must be a JSON array, got {parts!r}")

    records: list[PartRecord] = []
    for index, part in enumerate(parts):
        if type(part) is not dict:
            raise _untrusted(target, f"'parts[{index}]' must be a JSON object, got {part!r}")
        if "name" not in part or "rows" not in part:
            raise _untrusted(target, f"'parts[{index}]' must hold 'name' and 'rows'")
        name = part["name"]
        if type(name) is not str or not PART_NAME_PATTERN.match(name):
            raise _untrusted(
                target,
                f"'parts[{index}].name' must be a part filename of the form "
                f"part-00000.csv -- 'part-' then at least five digits then .csv, "
                f".jsonl or .parquet -- got {name!r}",
            )
        rows = _integer(part, "rows", target)
        records.append(PartRecord(name=name, rows=rows))
    return tuple(records)


def read_checkpoint(path: str | Path) -> Checkpoint:
    """Read and validate a manifest, refusing anything it did not write itself.

    **A manifest is hostile input.** It lives on disk, it describes a run that has
    already committed work, and ``--resume`` will trust it to decide how many records to
    skip and which files to open. So every field is checked for the exact JSON type it
    must have and the range it must sit in; nothing is converted, defaulted, or looked
    past. The write side is unaffected -- :func:`write_checkpoint` still emits the same
    bytes it always has, because a stricter reader has to accept everything the existing
    writer produced.

    Raises:
        CheckpointError: the file is missing or unreadable (an I/O problem), or its
            contents do not describe a run this build could resume (a trust problem).
            The two are worded differently on purpose: the first is about the file, the
            second says plainly that the manifest cannot be trusted.
    """
    target = Path(path)
    if not target.is_file():
        raise CheckpointError(
            f"no checkpoint at {str(target)!r}; there is nothing to resume. Run "
            f"without --resume to start a fresh checkpointed run."
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} could not be read: {exc}. It may have "
            f"been truncated by a crash; a run cannot be resumed from it."
        ) from exc

    if not isinstance(raw, dict):
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} must contain a JSON object, got "
            f"{type(raw).__name__}"
        )

    # Checked by type before value: True == 1 and 1.0 == 1, so a bare value comparison
    # would accept a boolean and a float for a version number. The refusal is worded like
    # every other content refusal -- it is the same kind of problem, a manifest that does
    # not describe a run this build can resume.
    version = _field(raw, "version", target)
    if type(version) is not int or version != CHECKPOINT_VERSION:
        raise _untrusted(
            target,
            f"it declares format version {version!r}, but this build reads version "
            f"{CHECKPOINT_VERSION}",
        )

    config = _hex_digest(raw, "config", target)
    records_consumed = _integer(raw, "records_consumed", target)
    rejected = _integer(raw, "rejected", target)
    # A run cannot have quarantined more records than it read. Nothing checked this
    # before, so a manifest could claim any number of rejections at all.
    if rejected > records_consumed:
        raise _untrusted(
            target,
            f"'rejected' is {rejected} but 'records_consumed' is only "
            f"{records_consumed}: it cannot reject more records than it read",
        )

    complete = _field(raw, "complete", target)
    if type(complete) is not bool:
        raise _untrusted(
            target,
            f"'complete' must be a JSON boolean, got {complete!r}. A string such as "
            f'"false" is not false here, and a run that believes it finished would '
            f"skip the records nobody wrote.",
        )

    return Checkpoint(
        source=_check_source(raw, target),
        config=config,
        records_consumed=records_consumed,
        rejected=rejected,
        parts=_check_parts(raw, target),
        complete=complete,
        version=CHECKPOINT_VERSION,
    )


def write_checkpoint(path: str | Path, checkpoint: Checkpoint) -> None:
    """Write the manifest atomically.

    Same mechanism as the writers: a ``.tmp`` beside the target, renamed into place
    once complete. A manifest that was half-written when the power went would be worse
    than no manifest at all -- it would describe a run that never happened, and the
    next ``--resume`` would trust it.
    """
    target = Path(path)
    partial = target.with_name(target.name + PARTIAL_SUFFIX)
    text = json.dumps(checkpoint.to_dict(), indent=2, ensure_ascii=False) + "\n"
    try:
        partial.write_text(text, encoding="utf-8")
        partial.replace(target)
    except OSError as exc:
        raise CheckpointError(f"could not write the checkpoint to {str(target)!r}: {exc}") from exc


def validate_resume(
    checkpoint: Checkpoint,
    source: str | Path,
    config: ExtractionConfig,
) -> None:
    """Refuse to resume when the run would not be the same run.

    The message names every component that differs, with both values, because "the
    source has changed" leaves the user with nothing to act on.

    Raises:
        CheckpointError: the source or the config does not match the checkpoint.
    """
    current_source = source_identity(source)
    current_config = config_identity(config)

    problems: list[str] = []
    if checkpoint.source.get("sha256") != current_source["sha256"]:
        problems.append(
            f"  source content:\n"
            f"    checkpoint: {checkpoint.source.get('size')} bytes, "
            f"sha256 {checkpoint.source.get('sha256')}\n"
            f"    now:        {current_source['size']} bytes, "
            f"sha256 {current_source['sha256']}"
        )
    elif checkpoint.source.get("path") != current_source["path"]:
        problems.append(
            f"  source path:\n"
            f"    checkpoint: {checkpoint.source.get('path')!r}\n"
            f"    now:        {current_source['path']!r}"
        )

    if checkpoint.config != current_config:
        problems.append(
            f"  config:\n    checkpoint: {checkpoint.config}\n    now:        {current_config}"
        )

    if problems:
        raise CheckpointError(
            "cannot resume: the run would not be the same run.\n"
            + "\n".join(problems)
            + "\n  Nothing was written. Use --resume only against the source and "
            "config the checkpoint was made from, or remove the checkpoint to start "
            "over."
        )
