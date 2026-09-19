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
    "ConfigError",
    "FieldPathError",
    "FieldTypeError",
    "GigaXMLError",
    "MissingRequiredFieldError",
    "RecordPathError",
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
