"""The exception hierarchy every gigaxml error descends from.

One base class exists so a caller can mount a single policy for "the input or the
configuration is wrong" without also swallowing genuine bugs. Phase 5's
``on_error`` switch is the intended consumer: it needs to tell a *rejected record*
(bad type, missing required field) apart from a *broken run* (a config that cannot
be parsed, a record path that matches nothing).

Every concrete error also inherits :class:`ValueError`, because that is what they
are -- the caller handed over a value the library cannot use. Keeping ``ValueError``
in the bases means ``except ValueError`` written before this hierarchy existed
still catches them, so the split is additive rather than breaking.

The classes carry data, not just prose, wherever a later phase needs to write it
somewhere: :class:`FieldTypeError` keeps ``field`` and ``raw`` for
``rejected.jsonl``, and :class:`MissingRequiredFieldError` keeps ``field``.
"""

from __future__ import annotations

__all__ = [
    "CheckpointError",
    "ConfigError",
    "FieldPathError",
    "FieldTypeError",
    "GigaXMLError",
    "InspectionError",
    "MissingRequiredFieldError",
    "RecordPathError",
    "RunInterruptedError",
    "WriterError",
]


class GigaXMLError(Exception):
    """Base class for every error gigaxml raises deliberately.

    Not raised directly. Catching it separates "the caller's input, configuration
    or document is wrong" from "gigaxml has a bug" -- the two want different
    responses, and Phase 5's per-record error policy only applies to the former.
    """


class ConfigError(GigaXMLError, ValueError):
    """The extraction config is malformed.

    Raised for unknown keys at any level, a missing ``record`` or ``fields``, a
    ``path`` missing from a field, a ``type`` that is not one of the supported
    names, a ``required`` that is not a boolean, and a ``namespaces`` block that
    is not a prefix-to-URI string map.

    Unknown keys are a hard error on purpose. A misspelled ``type:`` would
    otherwise fall back to the default and quietly emit strings where numbers were
    meant -- silent data corruption, which is worse than a failed run.
    """


class RecordPathError(GigaXMLError, ValueError):
    """Raised when ``record_path`` cannot be resolved or matches nothing.

    Deliberately *not* a silent empty result. Three mistakes land here, and all
    three would otherwise hide behind a plausible-looking "extracted 0 records"
    report: a namespace that was not supplied, an ancestor chain that does not
    exist in this document (the leaf name alone is not enough to match), and a
    root-anchored path whose leading segments do not name the document root.

    Defined here rather than in :mod:`gigaxml.parser.streaming` so that
    :mod:`gigaxml.paths` can raise it without importing the reader -- the reader
    imports the shared path helpers, so the reverse would be a cycle.
    ``gigaxml.parser.streaming`` re-exports it, so both import paths work.
    """


class FieldPathError(GigaXMLError, ValueError):
    """A field path is not a valid relative element path.

    Field paths are relative to the record element, so they may not start with
    ``/`` or ``//``; and Phase 2 deliberately implements no XPath, so ``..``,
    predicates (``a[b]``), wildcards (``*``) and functions are all rejected here
    rather than silently ignored.
    """


class FieldTypeError(GigaXMLError, ValueError):
    """A field's text could not be converted to its declared type.

    Carries the offending field name and the raw text, because Phase 5 writes
    both into ``rejected.jsonl`` -- a message alone would force the caller to
    re-parse prose to recover them.

    Attributes:
        field: the configured field name, e.g. ``"price"``.
        raw: the text that failed to convert, already whitespace-normalised.
    """

    def __init__(self, message: str, *, field: str, raw: str) -> None:
        super().__init__(message)
        self.field = field
        self.raw = raw


class MissingRequiredFieldError(GigaXMLError, ValueError):
    """A field marked ``required: true`` was absent from a record.

    Attributes:
        field: the configured field name that could not be found.
    """

    def __init__(self, message: str, *, field: str) -> None:
        super().__init__(message)
        self.field = field


class InspectionError(GigaXMLError, ValueError):
    """``inspect`` cannot produce what was asked of it.

    Raised when a document offers no usable record candidate, when a requested
    candidate index is out of range, and when a candidate cannot be expressed as a
    config at all (a namespace prefix rebound to different URIs along the path).
    Distinct from the path and config errors because nothing here is wrong with
    the user's input -- the *document* is simply not what the request assumed.
    """


class CheckpointError(GigaXMLError, ValueError):
    """A checkpointed run cannot be continued as asked.

    Raised for a manifest that is missing, unreadable, truncated, of an unknown
    format version or structurally wrong; for a manifest that describes a different
    source or a different config than the one being resumed with; and for a parts
    directory that cannot be used as one.

    Descends from :class:`GigaXMLError` for the same reason as the rest: one
    ``except`` clause covers everything the library raises deliberately. It is
    deliberately **not** a ``WriterError`` -- nothing is wrong with the output, the
    run is simply not the run the checkpoint describes.
    """


class WriterError(GigaXMLError, ValueError):
    """An output could not be set up or written.

    Raised for an output path whose format cannot be determined, an unusable batch
    size, a row whose keys do not match the configured fields, and a missing
    optional dependency -- Parquet needs ``pyarrow``, which is an extra rather
    than a requirement, so that case gets an actionable message instead of an
    ImportError traceback.

    Descending from :class:`GigaXMLError` keeps the promise that one ``except``
    clause can cover everything the library raises deliberately.
    """


class RunInterruptedError(GigaXMLError):
    """The run was stopped by a signal, and had an exit on which to write its report.

    **What it is for.** The run report is written on the way out of a command, so a run
    that never gets out leaves nothing behind -- measured on a 4 GiB document, stopping the
    process at 4 s left 66 committed parts and a manifest saying ``complete: false``, and
    no report at all. Anything reading the reports afterwards therefore cannot see the runs
    that were stopped, which are the ones most likely to be picked up again.

    A signal handler that raises this turns a stop into an ordinary unwinding path: the
    ``except`` clauses the library already has write the report on the way past, so the run
    leaves a record of how far it got instead of only the parts it managed to commit.

    **What it does not cover, and this is the important half.** A process given no chance
    to run its own code cannot write anything:

    * ``SIGINT`` -- Ctrl-C. Covered on every platform, and the one that matters, because
      stopping a long run is something people do deliberately.
    * ``SIGTERM`` -- ``kill``, ``systemctl stop``, a container's stop signal. Covered on
      POSIX. **On Windows there is no such thing**: ``os.kill(pid, SIGTERM)`` calls
      ``TerminateProcess``, the process is gone at once, and no handler runs.
    * **``TerminateProcess`` -- the Windows task manager, and anything else that kills
      without a signal. Not covered on any platform, and cannot be.** There is no code to
      run and no moment in which to run it.

    So this closes the common case and leaves the rest. A report saying a run was
    interrupted is worth more than no report, and it is **not** a promise that every
    interrupted run leaves one.

    **A ``GigaXMLError`` because that is what puts a stopped run where a failed one already
    is.** No new code is needed on the way out: the report is written from an ``except``
    that exists, and :func:`gigaxml.cli.main` turns the exception into a one-line message
    and a non-zero exit code. The name is not the built-in ``InterruptedError``, which
    exists on every Python this package supports and means something else.

    It is not an error about the document, the config or the
    output -- it is an external signal -- but nothing in the tree treats those three
    differently on the way out, and the one place that does care dispatches on this class
    rather than on the message. Inheriting :class:`ValueError` is **not** done here: no
    caller hands over a value, so the promise the rest of this module makes about
    ``except ValueError`` does not apply.

    Attributes:
        signum: the signal number, or ``None`` when the platform did not supply one.
        signame: its name where there is a name -- ``SIGINT``, ``SIGTERM`` -- and
            ``""`` otherwise. Shown to the user in place of a number, because ``2`` and
            ``15`` say nothing to somebody who pressed Ctrl-C.
    """

    def __init__(self, message: str, *, signum: int | None = None, signame: str = "") -> None:
        super().__init__(message)
        self.signum = signum
        self.signame = signame
