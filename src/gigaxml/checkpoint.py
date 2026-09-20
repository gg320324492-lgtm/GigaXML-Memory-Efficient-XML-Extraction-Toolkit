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
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from gigaxml.config import ExtractionConfig
from gigaxml.errors import CheckpointError
from gigaxml.writers import PARTIAL_SUFFIX

__all__ = [
    "CHECKPOINT_FILENAME",
    "CHECKPOINT_VERSION",
    "DEFAULT_PART_FORMAT",
    "Checkpoint",
    "PartRecord",
    "config_identity",
    "part_name",
    "read_checkpoint",
    "source_identity",
    "validate_resume",
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


def read_checkpoint(path: str | Path) -> Checkpoint:
    """Read and validate a manifest.

    Raises:
        CheckpointError: the file is missing, is not JSON, is not an object, is a
            newer format version than this build understands, or is missing a
            required key.
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

    version = raw.get("version")
    if version != CHECKPOINT_VERSION:
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} has format version {version!r}, but "
            f"this build writes version {CHECKPOINT_VERSION}"
        )

    required = ("source", "config", "records_consumed", "rejected", "parts", "complete")
    missing = [key for key in required if key not in raw]
    if missing:
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} is missing {missing}; it cannot be "
            f"trusted to describe a run"
        )

    source = raw["source"]
    if not isinstance(source, dict) or "sha256" not in source:
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} has a malformed 'source' block: {source!r}"
        )

    parts = raw["parts"]
    if not isinstance(parts, list):
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} has a malformed 'parts' list: {parts!r}"
        )

    try:
        records = tuple(
            PartRecord(name=str(part["name"]), rows=int(part["rows"])) for part in parts
        )
    except (TypeError, KeyError, ValueError) as exc:
        raise CheckpointError(
            f"the checkpoint at {str(target)!r} has a malformed part entry: {exc}"
        ) from exc

    return Checkpoint(
        source=dict(source),
        config=str(raw["config"]),
        records_consumed=int(raw["records_consumed"]),
        rejected=int(raw["rejected"]),
        parts=records,
        complete=bool(raw["complete"]),
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
