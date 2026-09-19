"""Streaming XML parsing primitives.

Phase 1 shipped only :mod:`gigaxml.parser.streaming`. Phase 2 adds configuration
and field extraction, which live in :mod:`gigaxml.config` and
:mod:`gigaxml.fields`; there are still no placeholder modules for writers or
validation.
"""

from __future__ import annotations

from gigaxml.parser.streaming import (
    RecordPathError,
    RecordPathSpec,
    StreamingRecordReader,
    parse_record_path,
    resolve_record_tags,
)

__all__ = [
    "RecordPathError",
    "RecordPathSpec",
    "StreamingRecordReader",
    "parse_record_path",
    "resolve_record_tags",
]
