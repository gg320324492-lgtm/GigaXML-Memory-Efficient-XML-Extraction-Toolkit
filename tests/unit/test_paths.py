"""Unit tests for ``record_path`` -> lxml tag resolution.

The bug this guards against is nasty because it is silent: ``iterparse(tag=...)``
matches qualified names, so a bare local name against a namespaced document
returns zero records and no error. The negative cases below are therefore as
important as the positive ones.
"""

from __future__ import annotations

import pytest

from gigaxml.parser.streaming import RecordPathError, resolve_record_tag


def test_plain_path_yields_bare_local_name() -> None:
    assert resolve_record_tag("/catalog/products/product") == "product"


def test_prefixed_segment_is_expanded() -> None:
    tag = resolve_record_tag("/c:products/c:product", {"c": "urn:example:catalog"})
    assert tag == "{urn:example:catalog}product"


def test_default_namespace_applies_to_bare_segment() -> None:
    tag = resolve_record_tag("/catalog/products/product", {"": "urn:example:catalog"})
    assert tag == "{urn:example:catalog}product"


def test_missing_default_key_leaves_bare_name() -> None:
    """A map with only prefixed entries must not imply a default namespace."""
    tag = resolve_record_tag("/catalog/products/product", {"c": "urn:example:catalog"})
    assert tag == "product"


def test_only_the_last_segment_matters() -> None:
    assert resolve_record_tag("/a/b/c/product", None) == "product"
    assert resolve_record_tag("/x/y/product", {"": "urn:z"}) == "{urn:z}product"


@pytest.mark.parametrize("bad", ["catalog/products/product", "", "/", "//", "/ /"])
def test_malformed_path_raises(bad: str) -> None:
    with pytest.raises(RecordPathError):
        resolve_record_tag(bad, None)


def test_unknown_prefix_raises() -> None:
    with pytest.raises(RecordPathError, match="not present in the namespace map"):
        resolve_record_tag("/c:product", {"x": "urn:x"})


def test_empty_uri_raises() -> None:
    with pytest.raises(RecordPathError, match="empty URI"):
        resolve_record_tag("/c:product", {"c": ""})
