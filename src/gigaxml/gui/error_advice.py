"""Turning a failure into something the user can act on.

Qt-free, and separate from the panel that draws it: what advice a given ``error.type``
gets is a decision worth testing on its own, and it is the part that would silently rot.

**Dispatch on the type, never on the message.** The report carries the exception class
name, which is stable; the message is prose that gets reworded. A panel that matched the
wording would keep working right up until it did not, and would look fine in between.
"""

from __future__ import annotations

from dataclasses import dataclass

from gigaxml.errors import FieldPathError, WriterError
from gigaxml.run import QUARANTINABLE

__all__ = [
    "KIND_CHECK_CONFIG",
    "KIND_FREE_TARGET",
    "KIND_NAMESPACES",
    "KIND_QUARANTINE",
    "Advice",
    "advice_for",
]

#: Set the run's ``on_error`` to ``quarantine``, so the bad records are skipped.
KIND_QUARANTINE = "quarantine"
#: Get the program holding the target file to let go of it.
KIND_FREE_TARGET = "free-the-target"
#: A field path names a namespace prefix the config never declared.
KIND_NAMESPACES = "namespaces"
#: Look at the config -- the run never got as far as reading records.
KIND_CHECK_CONFIG = "check-config"

#: The error types that mean "this record is bad" rather than "this run is broken".
#:
#: **Derived from the CLI's own list rather than typed out again.** ``QUARANTINABLE`` is
#: what the extractor itself uses to decide whether skipping a record can help; a second
#: list here would be a second answer to that question, and the two would disagree the
#: first time it changed.
_QUARANTINABLE: frozenset[str] = frozenset(cls.__name__ for cls in QUARANTINABLE)


def advice_for(error_type: str | None) -> Advice:
    """What to suggest for this failure.

    ``None`` -- and any type this does not know -- gets the unclassified advice. That is
    not a failure of this function: a config that will not load produces no report at all,
    so there is genuinely nothing to classify, and guessing from the message is exactly
    what this module refuses to do.
    """
    if error_type in _QUARANTINABLE:
        return Advice(
            kind=KIND_QUARANTINE,
            headline="A record did not match the field types you gave",
            detail=(
                "Setting on_error to quarantine skips the records that fail and keeps the "
                "rest. The run will finish, and the skipped records go to the rejection "
                "log — so the output is complete for every record that matched, and the "
                "ones that did not are accounted for rather than dropped silently."
            ),
        )
    if error_type == WriterError.__name__:
        return Advice(
            kind=KIND_FREE_TARGET,
            headline="The finished file could not be put in place",
            detail=(
                "On Windows this usually means the target is open in another program. "
                "Close it and run again. Nothing is lost: the complete output is in the "
                ".tmp file beside the target, and it can be renamed by hand."
            ),
        )
    if error_type == FieldPathError.__name__:
        return Advice(
            kind=KIND_NAMESPACES,
            headline="A field path uses a namespace prefix the config does not declare",
            detail=(
                "The namespace panel lists the prefixes in scope for the records the "
                "document has, which is what the paths are resolved against — so it is "
                "where the difference is visible: a prefix that is not there is one the "
                "records do not use, and either the path has a typo or the config was "
                "written for a different document. It only has that list once the document "
                "has been analysed; if it looks empty, analyse first."
            ),
        )
    return Advice(
        kind=KIND_CHECK_CONFIG,
        headline="The run stopped before it read any records",
        detail=(
            "This is usually the config rather than the document — a path that does not "
            "match, or a namespace prefix the config never declared. The field panel "
            "checks both as you edit, so a config that fails here is one that was changed "
            "after the fact."
        ),
    )


@dataclass(frozen=True)
class Advice:
    """A suggestion, and the identifier the window acts on.

    ``kind`` is what the window switches on to decide which button does what; the two
    strings are what the user reads. Keeping them apart means the wording can change
    without anything downstream caring.
    """

    kind: str
    headline: str
    detail: str
