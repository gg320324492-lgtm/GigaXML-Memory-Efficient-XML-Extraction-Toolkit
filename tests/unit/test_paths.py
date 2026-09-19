"""Unit tests for ``record_path`` -> lxml tag resolution.

The bug this guards against is nasty because it is silent: ``iterparse(tag=...)``
matches qualified names, so a bare local name against a namespaced document
returns zero records and no error. The negative cases below are therefore as
important as the positive ones.

The second half covers **full-chain** resolution: the reader matches every
segment of the path against the open-element stack, so each segment has to
resolve its own namespace prefix and default namespace. Resolving only the final
segment would let a same-named element under a different branch match.
"""

from __future__ import annotations

import pytest

from gigaxml.parser.streaming import RecordPathError, resolve_record_tag, resolve_record_tags


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


# --- full-chain resolution ---------------------------------------------------
#
# ``resolve_record_tag`` above is a convenience accessor for the record element's
# own tag. The reader does not use it for matching -- it uses the whole chain
# below, because a leaf-only match silently merges sibling branches that end in
# the same element name.


def test_full_chain_resolves_one_tag_per_segment() -> None:
    assert resolve_record_tags("/catalog/products/product") == [
        "catalog",
        "products",
        "product",
    ]


def test_full_chain_applies_default_namespace_to_every_bare_segment() -> None:
    assert resolve_record_tags("/catalog/products/product", {"": "urn:example:catalog"}) == [
        "{urn:example:catalog}catalog",
        "{urn:example:catalog}products",
        "{urn:example:catalog}product",
    ]


def test_full_chain_resolves_each_segment_independently() -> None:
    """A prefixed ancestor and a default-namespaced record must not share a URI."""
    chain = resolve_record_tags(
        "/c:products/product",
        {"c": "urn:example:catalog", "": "urn:example:default"},
    )
    assert chain == ["{urn:example:catalog}products", "{urn:example:default}product"]


def test_full_chain_can_mix_prefixed_segments_throughout() -> None:
    chain = resolve_record_tags("/c:products/c:product", {"c": "urn:example:catalog"})
    assert chain == ["{urn:example:catalog}products", "{urn:example:catalog}product"]


def test_single_segment_path_resolves_to_one_tag() -> None:
    assert resolve_record_tags("/product") == ["product"]
    assert resolve_record_tags("/c:product", {"c": "urn:x"}) == ["{urn:x}product"]


def test_full_chain_rejects_unknown_prefix_in_an_ancestor() -> None:
    with pytest.raises(RecordPathError, match="not present in the namespace map"):
        resolve_record_tags("/nope:products/product", {"c": "urn:x"})


def test_full_chain_rejects_empty_uri_in_an_ancestor() -> None:
    with pytest.raises(RecordPathError, match="empty URI"):
        resolve_record_tags("/c:products/product", {"c": ""})


@pytest.mark.parametrize("bad", ["/bad name/product", "/a/bad name/c", "/a/b/9lives"])
def test_full_chain_rejects_invalid_segment_anywhere(bad: str) -> None:
    with pytest.raises(RecordPathError, match="not a valid XML name"):
        resolve_record_tags(bad)


@pytest.mark.parametrize("bad", ["catalog/products/product", "", "/", "//", "/ /"])
def test_full_chain_rejects_malformed_path(bad: str) -> None:
    with pytest.raises(RecordPathError):
        resolve_record_tags(bad)


@pytest.mark.parametrize(
    ("path", "namespaces"),
    [
        ("/catalog/products/product", None),
        ("/c:products/c:product", {"c": "urn:example:catalog"}),
        ("/catalog/products/product", {"": "urn:example:catalog"}),
        ("/a/b/c/product", None),
        ("/c:products/product", {"c": "urn:example:catalog", "": "urn:example:default"}),
    ],
)
def test_final_segment_accessor_agrees_with_the_chain(
    path: str,
    namespaces: dict[str, str] | None,
) -> None:
    assert resolve_record_tag(path, namespaces) == resolve_record_tags(path, namespaces)[-1]


def test_namespace_map_is_not_mutated() -> None:
    namespaces = {"": "urn:x"}
    resolve_record_tags("/a/b", namespaces)
    assert namespaces == {"": "urn:x"}
