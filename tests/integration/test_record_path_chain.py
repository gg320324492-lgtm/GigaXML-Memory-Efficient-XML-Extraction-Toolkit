"""Integration tests: a record path must match the whole ancestor chain.

The defect these tests pin down was silent, which is why it mattered. Matching on
the record element's own tag alone meant ``/catalog/products/product`` also
matched every ``<product>`` sitting under ``<returns>``: a document carrying two
branches with the same leaf name would merge two different schemas into a single
output stream and still report a plausible-looking row count. Nothing raises,
nothing logs, the data is just wrong.

The fixtures below reproduce exactly that shape -- ``/root/a/item`` and
``/root/b/item`` in one document -- plus decoys at a different depth and under a
third branch. Note that the document deliberately contains **no** ``<nope>``
element: ``/root/nope/item`` must fail even though ``<item>`` exists all over the
document, which is precisely the case leaf-only matching used to get wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gigaxml.parser.streaming import RecordPathError, StreamingRecordReader

#: Two sibling branches ending in the same element name, a third branch holding a
#: decoy record, and one record buried one level deeper. There is no ``<nope>``.
TWO_BRANCHES = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <a>
    <item id="a1"><value>alpha</value></item>
    <item id="a2"><value>beta</value></item>
    <item id="a3"><value>gamma</value></item>
  </a>
  <b>
    <item id="b1"><value>delta</value></item>
    <item id="b2"><value>epsilon</value></item>
  </b>
  <other>
    <item id="decoy"><value>zeta</value></item>
  </other>
  <a>
    <nested><item id="deep"><value>eta</value></item></nested>
  </a>
</root>
"""

#: Same idea with namespaces: the ancestors carry an explicit prefix, the record
#: node inherits the default namespace, and one branch keeps everything in the
#: prefixed namespace so a per-segment resolution mistake shows up.
NAMESPACED = """<?xml version="1.0" encoding="UTF-8"?>
<root xmlns="urn:example:root" xmlns:p="urn:example:p">
  <p:a>
    <item id="a1"><value>alpha</value></item>
    <item id="a2"><value>beta</value></item>
  </p:a>
  <p:b>
    <item id="b1"><value>delta</value></item>
  </p:b>
  <p:c>
    <p:item id="c1"><p:value>gamma</p:value></p:item>
  </p:c>
</root>
"""

#: Comments and a processing instruction sitting between the ancestor and the
#: record. lxml's iterparse emits no events for either, so they must not shift
#: the ancestor stack.
WITH_NOISE = """<?xml version="1.0" encoding="UTF-8"?>
<root>
  <!-- a comment before the branch -->
  <a>
    <!-- a comment between the branch and the record -->
    <item id="a1"><value>alpha</value></item>
    <?pi interleaved?>
    <item id="a2"><value>beta</value></item>
  </a>
  <b>
    <item id="b1"><value>delta</value></item>
  </b>
</root>
"""

NS = {"": "urn:example:root", "p": "urn:example:p"}


@pytest.fixture
def two_branches(tmp_path: Path) -> Path:
    path = tmp_path / "two_branches.xml"
    path.write_text(TWO_BRANCHES, encoding="utf-8")
    return path


@pytest.fixture
def namespaced_branches(tmp_path: Path) -> Path:
    path = tmp_path / "namespaced_branches.xml"
    path.write_text(NAMESPACED, encoding="utf-8")
    return path


@pytest.fixture
def noisy_branches(tmp_path: Path) -> Path:
    path = tmp_path / "noisy_branches.xml"
    path.write_text(WITH_NOISE, encoding="utf-8")
    return path


def _ids(path: Path, record_path: str, namespaces: dict[str, str] | None = None) -> list[str]:
    """Collect record ids, which must happen before the element is recycled."""
    reader = StreamingRecordReader(path, record_path, namespaces)
    return [record.get("id") or "" for record in reader]


# --- the two branches the auditor's gate names ------------------------------


def test_first_branch_yields_only_its_own_records(two_branches: Path) -> None:
    """``/root/a/item`` must yield the three a-records and nothing else."""
    assert _ids(two_branches, "/root/a/item") == ["a1", "a2", "a3"]


def test_second_branch_yields_only_its_own_records(two_branches: Path) -> None:
    assert _ids(two_branches, "/root/b/item") == ["b1", "b2"]


def test_the_two_branches_are_disjoint_and_do_not_leak_decoys(two_branches: Path) -> None:
    left = _ids(two_branches, "/root/a/item")
    right = _ids(two_branches, "/root/b/item")
    assert set(left).isdisjoint(right)
    assert "decoy" not in left + right
    assert "deep" not in left + right


def test_the_decoy_branch_is_reachable_under_its_own_path(two_branches: Path) -> None:
    """``other/item`` exists -- it is the path that selects it, not the leaf name."""
    assert _ids(two_branches, "/root/other/item") == ["decoy"]


def test_record_deeper_than_the_path_is_not_matched(two_branches: Path) -> None:
    """An extra ancestor segment breaks the chain: ``a/nested/item`` is not ``a/item``."""
    assert _ids(two_branches, "/root/a/nested/item") == ["deep"]


# --- the chain must fail loudly when it is wrong ----------------------------


def test_wrong_ancestor_raises_instead_of_matching_the_leaf(two_branches: Path) -> None:
    """There is no ``<nope>``, yet ``<item>`` is everywhere: leaf-only would pass."""
    reader = StreamingRecordReader(two_branches, "/root/nope/item")
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_missing_leaf_under_an_existing_ancestor_raises(two_branches: Path) -> None:
    reader = StreamingRecordReader(two_branches, "/root/a/nope")
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_both_gate_paths_fail_loudly_and_the_good_one_succeeds(two_branches: Path) -> None:
    """The auditor's release gate, in one place: pass, raise, raise."""
    assert _ids(two_branches, "/root/a/item") == ["a1", "a2", "a3"]
    for bad in ("/root/nope/item", "/root/a/nope"):
        with pytest.raises(RecordPathError, match="matched 0 elements"):
            list(StreamingRecordReader(two_branches, bad))


# --- suffix semantics are explicit, not accidental --------------------------


def test_a_trailing_sub_path_matches_anywhere_the_chain_ends(two_branches: Path) -> None:
    """A shorter path is a suffix match; the docs promise this, so assert it."""
    assert _ids(two_branches, "/a/item") == ["a1", "a2", "a3"]
    assert _ids(two_branches, "/b/item") == ["b1", "b2"]


def test_a_single_segment_path_matches_every_branch(two_branches: Path) -> None:
    """One segment cannot discriminate; this is why the leaf alone is not enough."""
    assert _ids(two_branches, "/item") == ["a1", "a2", "a3", "b1", "b2", "decoy", "deep"]


# --- namespaces resolve per segment ----------------------------------------


def test_prefixed_ancestor_with_default_namespaced_record(namespaced_branches: Path) -> None:
    assert _ids(namespaced_branches, "/p:a/item", NS) == ["a1", "a2"]
    assert _ids(namespaced_branches, "/p:b/item", NS) == ["b1"]


def test_fully_prefixed_chain_resolves_every_segment(namespaced_branches: Path) -> None:
    assert _ids(namespaced_branches, "/p:c/p:item", NS) == ["c1"]


def test_ancestor_in_the_wrong_namespace_raises(namespaced_branches: Path) -> None:
    """``/a/item`` resolves to the default namespace, but ``a`` lives in ``p``."""
    reader = StreamingRecordReader(namespaced_branches, "/a/item", NS)
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_record_in_the_wrong_namespace_raises(namespaced_branches: Path) -> None:
    """``p:c`` holds ``p:item``; the bare ``item`` in the path cannot match it."""
    reader = StreamingRecordReader(namespaced_branches, "/p:c/item", NS)
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_omitting_the_namespace_map_raises_instead_of_reporting_zero(
    namespaced_branches: Path,
) -> None:
    """Same path as the positive case, minus the map: bare names match no URIs."""
    reader = StreamingRecordReader(namespaced_branches, "/a/item")
    assert reader.record_chain == ("a", "item")
    with pytest.raises(RecordPathError, match="matched 0 elements"):
        list(reader)


def test_an_unknown_prefix_fails_before_the_document_is_opened(
    namespaced_branches: Path,
) -> None:
    """A path naming a prefix the map does not define is a config error, not a scan."""
    with pytest.raises(RecordPathError, match="not present in the namespace map"):
        StreamingRecordReader(namespaced_branches, "/p:a/item", {"x": "urn:example:x"})


# --- the ancestor stack must not be shifted by non-element nodes ------------


def test_comments_and_processing_instructions_do_not_shift_the_chain(
    noisy_branches: Path,
) -> None:
    """iterparse delivers no events for comments/PIs, so the stack stays aligned."""
    assert _ids(noisy_branches, "/root/a/item") == ["a1", "a2"]
    assert _ids(noisy_branches, "/root/b/item") == ["b1"]


# --- the resolved chain is inspectable -------------------------------------


def test_record_chain_exposes_every_resolved_segment(two_branches: Path) -> None:
    reader = StreamingRecordReader(two_branches, "/root/a/item")
    assert reader.record_chain == ("root", "a", "item")
    assert reader.record_tag == "item"
    assert reader.record_path == "/root/a/item"
