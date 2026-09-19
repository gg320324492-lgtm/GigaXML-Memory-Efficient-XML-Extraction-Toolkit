"""Streaming XML parsing primitives.

Phase 1 deliberately ships only :mod:`gigaxml.parser.streaming`. Field extraction,
writers and validation land in later phases; there are no placeholder modules.
"""

from __future__ import annotations

from gigaxml.parser.streaming import (
    RecordPathError,
    StreamingRecordReader,
    resolve_record_tag,
    resolve_record_tags,
)

__all__ = [
    "RecordPathError",
    "StreamingRecordReader",
    "resolve_record_tag",
    "resolve_record_tags",
]
