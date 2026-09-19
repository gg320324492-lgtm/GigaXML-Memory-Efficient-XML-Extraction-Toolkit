"""Streaming XML parsing primitives.

Phase 1 deliberately ships only :mod:`gigaxml.parser.streaming`. Field extraction,
writers and validation land in later phases; there are no placeholder modules.
"""

from __future__ import annotations

from gigaxml.parser.streaming import (
    RecordPathError,
    RecordPathSpec,
    StreamingRecordReader,
    parse_record_path,
    resolve_record_leaf_tag,
    resolve_record_tags,
)

__all__ = [
    "RecordPathError",
    "RecordPathSpec",
    "StreamingRecordReader",
    "parse_record_path",
    "resolve_record_leaf_tag",
    "resolve_record_tags",
]
