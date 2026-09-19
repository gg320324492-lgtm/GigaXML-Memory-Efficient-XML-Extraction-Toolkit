"""Unit tests: structure inspection, candidate scoring and type inference.

Most of these use a tiny document written into ``tmp_path`` rather than a fixture,
because the properties under test are about *shape* -- which paths are tracked, how
they are rendered, what counts as a candidate -- and a three-element document says
that more clearly than a realistic one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.errors import InspectionError
from gigaxml.fields import FieldType
from gigaxml.inspect import (
    DEFAULT_MAX_PATHS,
    generate_config,
    infer_field_type,
    inspect_document,
)

MINIMAL = """<?xml version="1.0"?>
<catalog>
  <products>
    <product id="1"><name>Alpha</name><price currency="USD">10.00</price></product>
    <product id="2"><name>Beta</name><price currency="EUR">20.00</price></product>
  </products>
</catalog>
"""


def write(tmp_path: Path, text: str, name: str = "doc.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# --- type inference ---------------------------------------------------------


@pytest.mark.parametrize(
    "values,expected",
    [
        (["1", "2", "-3"], FieldType.INT),
        (["1.5", "-2.25", "1e3"], FieldType.FLOAT),
        (["yes", "no"], FieldType.BOOL),
        (["true", "false", "1", "0"], FieldType.BOOL),
        (["2023-11-14", "2024-02-29"], FieldType.DATE),
        (["Alpha", "Beta"], FieldType.STRING),
        (["1", "two"], FieldType.STRING),
    ],
)
def test_inference_picks_the_narrowest_type(values: list[str], expected: FieldType) -> None:
    field_type, evidence = infer_field_type(values)
    assert field_type is expected
    assert values[0] in evidence, "the evidence names the observed values"


def test_inference_never_returns_decimal() -> None:
    """The whole point of leaving ``decimal`` out of inference.

    ``49.90`` sampled from a document says nothing about whether the source meant a
    decimal or a string, and treating it as a float loses the trailing zero -- which
    is exactly the loss ``FieldType.DECIMAL`` exists to prevent. A human opts in.
    """
    money_like = [
        ["49.90"],
        ["49.90", "129.00"],
        ["0.1", "0.2", "0.3"],
        ["1.10", "2.20"],
        ["-0.01"],
        ["12345678901234567890.12"],
    ]
    for values in money_like:
        field_type, _ = infer_field_type(values)
        assert field_type is not FieldType.DECIMAL, values


def test_inference_only_ever_returns_an_inferable_type() -> None:
    """A structural guard: the allowed set cannot quietly grow."""
    allowed = {FieldType.STRING, FieldType.INT, FieldType.FLOAT, FieldType.BOOL, FieldType.DATE}
    samples = [
        [],
        ["1"],
        ["1.5"],
        ["yes"],
        ["2023-01-01"],
        ["x"],
        ["", " "],
        ["nan"],
        ["1_000"],
        ["Infinity"],
    ]
    for values in samples:
        assert infer_field_type(values)[0] in allowed, values


def test_inference_on_no_values_stays_string() -> None:
    field_type, evidence = infer_field_type([])
    assert field_type is FieldType.STRING
    assert "no values sampled" in evidence


def test_inference_evidence_reports_the_sample_size() -> None:
    _, evidence = infer_field_type(["1", "2", "3"])
    assert "3 distinct value(s)" in evidence


# --- inspection -------------------------------------------------------------


def test_every_path_is_counted_with_its_depth_and_shape(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL))

    by_path = {entry.path: entry for entry in report.paths}
    assert by_path["/catalog"].count == 1
    assert by_path["/catalog"].depth == 1
    assert by_path["/catalog/products"].child_tags == ("product",)
    assert by_path["/catalog/products/product"].count == 2
    assert by_path["/catalog/products/product"].depth == 3
    assert by_path["/catalog/products/product"].child_tags == ("name", "price")
    assert by_path["/catalog/products/product/name"].count == 2
    assert by_path["/catalog/products/product/name"].has_children is False
    assert report.elements_seen == 8


def test_attribute_names_are_collected(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL))
    product = report.entry_for("/catalog/products/product")
    assert product is not None
    assert product.attribute_names == ("@id",)

    price = report.entry_for("/catalog/products/product/price")
    assert price is not None
    assert price.attribute_names == ("@currency",)


def test_paths_are_sorted_by_count(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL))
    counts = [entry.count for entry in report.paths]
    assert counts == sorted(counts, reverse=True)


# --- candidate selection ----------------------------------------------------


def test_the_repeating_element_is_the_top_candidate(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL))
    assert report.candidates[0].path == "/catalog/products/product"
    assert report.candidates[0].count == 2


def test_candidates_carry_their_evidence(tmp_path: Path) -> None:
    candidate = inspect_document(write(tmp_path, MINIMAL)).candidates[0]
    assert "2 occurrences" in candidate.evidence
    assert "sibling structure consistency" in candidate.evidence
    assert candidate.shape_consistency == 1.0


def test_a_nested_repeating_structure_is_marked_as_nested(tmp_path: Path) -> None:
    """``.../product/price`` repeats as often as ``.../product`` and must not compete."""
    report = inspect_document(write(tmp_path, MINIMAL))
    nested = [c for c in report.candidates if report.nested_inside(c)]
    assert nested, "the document has nested repeating paths"
    for candidate in nested:
        assert candidate.path.startswith("/catalog/products/product/")


def test_nesting_is_found_even_when_the_nested_path_ranks_first(tmp_path: Path) -> None:
    """The case the annotation exists for, and the one it used to miss entirely.

    A nested path usually repeats more often than the record containing it, so it
    usually ranks *above* it. The scan used to return as soon as it reached the
    candidate itself, which meant it only ever saw candidates ranked higher -- so
    whenever the nested path was first, both were reported as unnested.

    This is one number away from the document in
    ``test_a_field_that_repeats_more_than_its_record_does_not_outrank_it``: each
    ``<i>`` gets two ``<tags>`` instead of one.
    """
    document = write(
        tmp_path,
        "<root>"
        "<i><tags><tag>a</tag></tags><tags><tag>b</tag></tags></i>"
        "<i><tags><tag>c</tag></tags><tags><tag>d</tag></tags></i>"
        "</root>",
    )
    report = inspect_document(document)
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert report.candidates[0].path == "/root/i/tags", "the nested path ranks first"
    assert by_path["/root/i/tags"].count == 4
    assert by_path["/root/i"].count == 2

    assert report.nested_inside(by_path["/root/i/tags"]) == "/root/i"
    assert report.nested_inside(by_path["/root/i"]) is None, "a container is not nested"
    assert report.nested_inside(report.candidates[0]) == "/root/i"


def test_the_container_side_of_a_tie_is_still_reported_as_unnested(tmp_path: Path) -> None:
    """Equal counts are the safe side: depth decides, so the container already wins."""
    document = write(
        tmp_path,
        "<root><i><tags><tag>a</tag></tags></i><i><tags><tag>b</tag></tags></i></root>",
    )
    report = inspect_document(document)

    assert report.candidates[0].path == "/root/i"
    assert report.nested_inside(report.candidates[0]) is None
    assert report.nested_inside(report.candidates[1]) == "/root/i"


def test_a_container_of_a_container_is_also_found(tmp_path: Path) -> None:
    """Nesting is about containment, not about the immediate parent."""
    document = write(
        tmp_path,
        "<root><a><b><c><d>x</d></c></b></a><a><b><c><d>y</d></c></b></a></root>",
    )
    report = inspect_document(document)
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert "/root/a/b" in by_path
    assert report.nested_inside(by_path["/root/a/b"]) == "/root/a"


def test_the_orders_and_lines_shape_is_annotated_not_reordered(
    fixtures_dir: Path,
) -> None:
    """A real-shaped case: 200 orders, 3 lines each.

    The line is the *inner* structure and repeats three times as often, so it ranks
    first and the default candidate is the line. The annotation is what tells the
    reader that ``/orders/order`` exists -- and the ranking is deliberately left
    alone, because "container first" gives a worse answer on other shapes.
    """
    report = inspect_document(fixtures_dir / "orders_lines.xml")
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert report.candidates[0].path == "/orders/order/line"
    assert by_path["/orders/order/line"].count == 600
    assert by_path["/orders/order"].count == 200
    assert report.nested_inside(by_path["/orders/order/line"]) == "/orders/order"
    assert report.nested_inside(by_path["/orders/order"]) is None


def test_a_nested_path_with_a_strictly_higher_count_is_annotated(
    fixtures_dir: Path,
) -> None:
    report = inspect_document(fixtures_dir / "nest_hot.xml")
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert report.candidates[0].path == "/root/item/tag"
    assert by_path["/root/item/tag"].count == 6
    assert report.nested_inside(by_path["/root/item/tag"]) == "/root/item"


def test_the_section_shape_keeps_the_inner_path_as_the_default(
    fixtures_dir: Path,
) -> None:
    """The counterexample to "container first": 2 sections, 1000 items each.

    Preferring the container would answer ``/root/section`` with 2 rows where the
    user almost certainly wants 2000 items. So the ranking stays as it is and the
    annotation does the explaining.
    """
    report = inspect_document(fixtures_dir / "section_many.xml")
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert report.candidates[0].path == "/root/section/item"
    assert by_path["/root/section/item"].count == 2000
    assert by_path["/root/section"].count == 2
    assert report.nested_inside(by_path["/root/section/item"]) == "/root/section"


def test_a_document_with_no_repeating_structure_offers_no_candidate(tmp_path: Path) -> None:
    document = write(tmp_path, "<root><a>1</a><b>2</b><c>3</c></root>")
    report = inspect_document(document)
    assert report.candidates == ()
    with pytest.raises(InspectionError, match="no record candidate"):
        generate_config(report)


def test_a_document_whose_paths_all_occur_once_offers_no_candidate(tmp_path: Path) -> None:
    """The remaining no-candidate case: nothing repeats, so there is nothing to pick."""
    document = write(tmp_path, "<root><a>1</a><b>2</b><c>3</c></root>")
    report = inspect_document(document)

    assert report.candidates == ()
    text = report.to_text()
    assert "no path repeats at least twice" in text
    assert "write the record path by hand" in text


def test_a_bare_leaf_document_now_yields_the_leaf_as_a_candidate(tmp_path: Path) -> None:
    """This test used to assert the opposite, and the change is deliberate.

    Until the bare-leaf rule changed, a document whose records are bare leaves got no
    candidate at all: the leaf was excluded because a leaf has a trivially perfect
    sibling consistency and usually repeats more often than the record containing it,
    so it would have taken first place. It is now admitted when no repeating element
    with structure sits above it, which is exactly this shape -- the log-file shape
    the tool gets pointed at.
    """
    document = write(tmp_path, "<root><item>1</item><item>2</item><item>3</item></root>")
    report = inspect_document(document)

    assert [candidate.path for candidate in report.candidates] == ["/root/item"]
    assert report.entry_for("/root/item").count == 3


def test_a_leaf_among_siblings_is_not_a_candidate(tmp_path: Path) -> None:
    """Otherwise every field of a record would outrank the record itself."""
    document = write(tmp_path, "<root><i><a>1</a><b>2</b></i><i><a>3</a><b>4</b></i></root>")
    report = inspect_document(document)
    assert [c.path for c in report.candidates] == ["/root/i"]


def test_a_field_that_repeats_more_than_its_record_does_not_outrank_it(
    tmp_path: Path,
) -> None:
    """The concrete failure the structure rule prevents.

    ``tags`` occurs once per product while ``tags/tag`` occurs three times per
    product, so ``.../tags/tag`` has both the higher count and the perfect
    consistency -- and would win on both terms.
    """
    document = write(
        tmp_path,
        "<root><i><tags><tag>a</tag><tag>b</tag><tag>c</tag></tags></i>"
        "<i><tags><tag>d</tag><tag>e</tag><tag>f</tag></tags></i></root>",
    )
    report = inspect_document(document)

    assert report.entry_for("/root/i/tags/tag").count == 6
    assert report.entry_for("/root/i").count == 2
    assert report.candidates[0].path == "/root/i"


def test_a_document_with_two_record_kinds_lists_both(fixtures_dir: Path) -> None:
    report = inspect_document(fixtures_dir / "two_records.xml")
    paths = [candidate.path for candidate in report.candidates]
    assert "/catalog/products/product" in paths
    assert "/catalog/orders/order" in paths
    assert report.candidates[0].path == "/catalog/products/product", "more occurrences ranks first"
    assert report.candidates[0].count == 3
    assert report.entry_for("/catalog/orders/order").count == 2


def test_more_occurrences_score_higher_within_one_document(tmp_path: Path) -> None:
    """The repeat term is relative to the document, so it is only comparable inside one.

    This is the property that has to hold: the more often a path repeats, the higher
    it scores. It is asserted within a single document because the term is normalised
    by that document's own most frequent path -- an absolute saturating curve was used
    at first, and it made the top candidate flip between documents of different sizes
    once both had saturated.
    """
    document = write(
        tmp_path,
        "<r>"
        + "".join("<i><a/></i>" for _ in range(3000))
        + "".join("<j><a/></j>" for _ in range(3))
        + "</r>",
    )
    by_path = {candidate.path: candidate for candidate in inspect_document(document).candidates}

    assert by_path["/r/i"].count == 3000
    assert by_path["/r/j"].count == 3
    assert by_path["/r/i"].score > by_path["/r/j"].score
    assert by_path["/r/i"].repeat_score == 1.0, "the most frequent path sets the scale"


# --- the path table and the depth limit -------------------------------------


def test_hitting_the_path_cap_is_reported_not_silent(tmp_path: Path) -> None:
    """Silent truncation would make the report look complete and be wrong."""
    document = write(
        tmp_path,
        "<root><a1/><a2/><a3/><a4/><a5/><a6/><a7/><a8/></root>",
    )
    report = inspect_document(document, max_paths=3)

    assert report.paths_truncated is True
    assert report.tracked_paths == 3
    assert report.max_paths == 3
    assert report.untracked_occurrences == 6, "the other six elements were counted"
    assert len(report.untracked_examples) == 6

    text = report.to_text()
    assert "WARNING" in text
    assert "path table is full" in text
    assert "not counted" in text, "the report is explicit about what it did not count"


def test_a_table_that_fits_is_not_reported_as_truncated(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL), max_paths=DEFAULT_MAX_PATHS)
    assert report.paths_truncated is False
    assert report.untracked_occurrences == 0
    assert "WARNING" not in report.to_text()


def test_exceeding_the_depth_limit_is_reported(tmp_path: Path) -> None:
    document = write(tmp_path, "<a><b><c><d><e/></d></c></b></a>")
    report = inspect_document(document, max_depth=3)

    assert report.depth_truncated is True
    assert report.max_depth_limit == 3
    assert report.max_depth_seen == 5
    assert "TRUNCATED" in report.to_text()
    assert report.entry_for("/a/b/c") is not None
    assert report.entry_for("/a/b/c/d") is None


# --- namespaces -------------------------------------------------------------


def test_a_default_namespace_is_reported_even_though_paths_render_bare(
    fixtures_dir: Path,
) -> None:
    """The trap: bare-looking paths still need the namespace in the config.

    ``<catalog xmlns="urn:example:shop">`` renders as the path ``/catalog/...``,
    but its elements really are ``{urn:example:shop}catalog``. A config built from
    those bare segments with no namespace map would match nothing at all.
    """
    report = inspect_document(fixtures_dir / "extract_default_ns.xml")
    assert report.namespaces == {"": "urn:example:shop"}
    assert report.candidates[0].path == "/catalog/products/product"

    text = generate_config(report)
    assert "namespaces:" in text
    assert "urn:example:shop" in text


def test_a_prefixed_namespace_is_reported_and_used_in_paths(fixtures_dir: Path) -> None:
    report = inspect_document(fixtures_dir / "extract_prefixed_ns.xml")
    assert report.namespaces == {"s": "urn:example:shop"}
    assert report.candidates[0].path == "/s:catalog/s:products/s:product"
    assert generate_config(report).count("s:") >= 4


def test_a_rebound_prefix_is_reported(tmp_path: Path) -> None:
    """A prefix that means two things cannot be one flat namespace map."""
    document = write(
        tmp_path,
        '<root xmlns:p="urn:one"><p:a><p:b/></p:a>'
        '<other xmlns:p="urn:two"><p:c/><p:c/></other></root>',
    )
    report = inspect_document(document)
    assert report.shadowed_prefixes == ("p",)
    assert "rebound" in report.to_text()


def test_a_candidate_that_rebinds_a_prefix_along_its_own_path_is_refused(
    tmp_path: Path,
) -> None:
    """Two sibling subtrees bind ``p`` differently, so ``/root/a/p:x`` is ambiguous.

    Both instances render as the same path with ``p`` meaning a different namespace,
    which one flat ``namespaces:`` block cannot describe. Guessing would be wrong
    half the time, so this refuses instead.
    """
    document = write(
        tmp_path,
        '<root><a xmlns:p="urn:one"><p:x><y/></p:x></a>'
        '<a xmlns:p="urn:two"><p:x><y/></p:x></a></root>',
    )
    report = inspect_document(document)

    # Sibling scopes are popped, so this is not *shadowing* -- the same prefix
    # simply means two things at two instances of one path.
    assert report.shadowed_prefixes == ()
    target = next(c for c in report.candidates if c.path.endswith("p:x"))
    assert target.missing_namespaces == ("p",)
    with pytest.raises(InspectionError, match="more than one URI"):
        generate_config(report, candidate_index=report.candidates.index(target) + 1)


def test_shadowing_outside_a_candidate_does_not_spoil_its_config(tmp_path: Path) -> None:
    """A candidate that is itself unambiguous must still produce a usable config.

    ``p`` is rebound in a sibling subtree, but the candidate lives wholly inside one
    binding, so its own map is unambiguous. A document-wide union would have
    excluded ``p`` and blocked a config that is perfectly expressible.
    """
    document = write(
        tmp_path,
        '<root xmlns:p="urn:one"><p:a><p:b/></p:a>'
        '<other xmlns:p="urn:two"><p:c><p:d/></p:c><p:c><p:d/></p:c></other></root>',
    )
    report = inspect_document(document)
    assert "p" in report.shadowed_prefixes, "the outer binding is still in scope inside <other>"

    candidate = next(c for c in report.candidates if c.path.endswith("p:c"))
    assert candidate.namespaces == {"p": "urn:two"}
    assert candidate.missing_namespaces == (), "its own path is unambiguous"

    text = generate_config(report, candidate_index=report.candidates.index(candidate) + 1)
    assert "urn:two" in text


# --- generate_config --------------------------------------------------------


def test_generated_config_carries_the_starting_point_warning(tmp_path: Path) -> None:
    text = generate_config(inspect_document(write(tmp_path, MINIMAL)))
    assert "STARTING POINT, not a conclusion" in text


def test_generated_config_lists_the_other_candidates(fixtures_dir: Path) -> None:
    text = generate_config(inspect_document(fixtures_dir / "two_records.xml"))
    assert "/catalog/orders/order" in text, "the alternative record is named, not hidden"


def test_generated_config_defaults_every_field_to_string(tmp_path: Path) -> None:
    text = generate_config(inspect_document(write(tmp_path, MINIMAL)))
    assert "type:" not in text
    assert "Every type is `string`" in text


def test_inferred_config_never_contains_decimal(tmp_path: Path) -> None:
    document = write(
        tmp_path,
        "<root>" + "".join(f"<i><p>{n}.50</p></i>" for n in (1, 2, 3)) + "</root>",
    )
    text = generate_config(inspect_document(document, collect_values=True), infer_types=True)
    assert "decimal" not in text.replace("`decimal` is never inferred", "").replace(
        "`decimal` by hand", ""
    )


def test_inferred_config_lists_evidence_per_field(tmp_path: Path) -> None:
    document = write(
        tmp_path,
        "<root>"
        + "".join(f'<i n="{n}"><s>{n}</s><d>2024-01-0{n}</d></i>' for n in (1, 2, 3))
        + "</root>",
    )
    text = generate_config(inspect_document(document, collect_values=True), infer_types=True)
    assert "n: int -- every sampled value is a whole number" in text
    assert "d: date -- every sampled value parses as an ISO date" in text
    assert "distinct value(s)" in text


def test_an_attribute_and_a_child_with_the_same_name_both_survive(tmp_path: Path) -> None:
    """``@name`` and ``name`` are different fields; one must not overwrite the other."""
    document = write(
        tmp_path,
        '<root><i name="a"><name>x</name></i><i name="b"><name>y</name></i></root>',
    )
    text = generate_config(inspect_document(document))
    assert "path: '@name'" in text
    assert "path: name" in text
    assert "name_2:" in text


def test_an_out_of_range_candidate_index_is_rejected(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL))
    with pytest.raises(InspectionError, match="does not exist"):
        generate_config(report, candidate_index=99)


# --- serialisation ----------------------------------------------------------


def test_the_json_view_is_serialisable_and_complete(tmp_path: Path) -> None:
    report = inspect_document(write(tmp_path, MINIMAL), collect_values=True)
    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["source"].endswith("doc.xml")
    assert payload["path_table"]["tracked"] == report.tracked_paths
    assert payload["candidates"][0]["path"] == "/catalog/products/product"
    assert "nested_inside" in payload["candidates"][0]
    assert payload["paths"][0]["path"]
    assert payload["namespaces"] == {}


# --- bare leaves: eligible unless a repeating structured ancestor owns them ---


def test_a_bare_leaf_with_nothing_above_it_is_the_record(tmp_path: Path) -> None:
    """The log-file shape: ``<root><line>text</line> x3</root>``.

    An earlier rule required structure of every candidate, so this document -- which
    is what a log file looks like -- got no candidate at all.
    """
    document = write(tmp_path, "<root><line>a</line><line>b</line><line>c</line></root>")
    report = inspect_document(document)

    assert [candidate.path for candidate in report.candidates] == ["/root/line"]
    assert report.candidates[0].count == 3
    assert report.candidates[0].child_tags == ()
    assert report.candidates[0].attribute_names == ()


def test_a_bare_leaf_inside_a_repeating_record_is_not_a_candidate(tmp_path: Path) -> None:
    """``.../product/tags/tag`` is a field of ``.../product``, not a record.

    A leaf has a trivially perfect sibling consistency and usually repeats more
    often, so admitting it would put a field above the record it belongs to.
    """
    document = write(
        tmp_path,
        "<root>"
        "<product><tags><tag>a</tag><tag>b</tag></tags></product>"
        "<product><tags><tag>c</tag><tag>d</tag></tags></product>"
        "</root>",
    )
    report = inspect_document(document)
    paths = [candidate.path for candidate in report.candidates]

    assert "/root/product" in paths
    assert "/root/product/tags/tag" not in paths
    assert report.entry_for("/root/product/tags/tag").count == 4


def test_an_ancestor_has_to_repeat_to_disqualify_a_leaf(tmp_path: Path) -> None:
    """Both halves of the test matter: structure *and* at least two occurrences.

    ``<root>`` has structure but occurs once, so it is a container rather than a
    record and must not disqualify what is inside it.
    """
    document = write(tmp_path, "<root><line>a</line><line>b</line></root>")
    report = inspect_document(document)

    assert report.entry_for("/root").count == 1
    assert report.entry_for("/root").child_tags == ("line",)
    assert [candidate.path for candidate in report.candidates] == ["/root/line"]


def test_an_ancestor_without_structure_does_not_disqualify_a_leaf(tmp_path: Path) -> None:
    """A repeating bare leaf cannot own another one; it has nothing to own it with."""
    document = write(
        tmp_path,
        "<root><mid>a</mid><mid>b</mid><mid>c</mid></root>",
    )
    report = inspect_document(document)

    assert [candidate.path for candidate in report.candidates] == ["/root/mid"]


def test_the_nearest_repeating_ancestor_is_what_matters(tmp_path: Path) -> None:
    """A leaf three levels down is owned by the repeating element above it, not the root."""
    document = write(
        tmp_path,
        "<root>"
        "<group><item><leaf>x</leaf></item><item><leaf>y</leaf></item></group>"
        "<group><item><leaf>z</leaf></item></group>"
        "</root>",
    )
    report = inspect_document(document)
    paths = [candidate.path for candidate in report.candidates]

    assert "/root/group/item" in paths
    assert "/root/group/item/leaf" not in paths


def test_the_mixed_shape_ranks_the_bigger_record_set_first(fixtures_dir: Path) -> None:
    """``mixed.xml``: 200 structured items beside 300 bare lines.

    Both are legitimate record sets, so both are candidates, and the one with more
    occurrences ranks first. This is the intended consequence of the rule, not a
    regression: a document can hold two record kinds and the tool says so rather
    than picking one.
    """
    report = inspect_document(fixtures_dir / "mixed.xml")
    by_path = {candidate.path: candidate for candidate in report.candidates}

    assert [candidate.path for candidate in report.candidates] == [
        "/root/logs/line",
        "/root/items/item",
    ]
    assert by_path["/root/logs/line"].count == 300
    assert by_path["/root/items/item"].count == 200


def test_the_three_shapes_from_the_rule_change(fixtures_dir: Path) -> None:
    """``bare_leaf`` gains a candidate, ``mixed`` gains one, ``flat_log`` is untouched."""
    bare = inspect_document(fixtures_dir / "bare_leaf.xml")
    assert [candidate.path for candidate in bare.candidates] == ["/root/line"]
    assert bare.candidates[0].count == 500

    flat = inspect_document(fixtures_dir / "flat_log.xml")
    assert [candidate.path for candidate in flat.candidates] == ["/root/line"]
    assert flat.candidates[0].count == 500


@pytest.mark.parametrize(
    "name,expected",
    [
        ("bare_leaf.xml", ("/root/line",)),
        ("flat_log.xml", ("/root/line",)),
        ("mixed.xml", ("/root/logs/line", "/root/items/item")),
        ("nsA.xml", ("/catalog/items/item",)),
        ("nsB.xml", ("/catalog/items/item",)),
        ("nsC.xml", ("/r:catalog/r:items/r:item",)),
        ("shadowed.xml", ("/root/set2/item",)),
        ("xml_lang.xml", ("/root/item",)),
        ("xml_space.xml", ("/root/item",)),
        ("xxe.xml", ()),
        ("nest_eq.xml", ("/root/i", "/root/i/tags")),
        ("nest_hot.xml", ("/root/item/tag", "/root/item")),
        ("orders_lines.xml", ("/orders/order/line", "/orders/order")),
        ("section_many.xml", ("/root/section/item", "/root/section")),
        (
            "two_records.xml",
            (
                "/catalog/products/product",
                "/catalog/products/product/price",
                "/catalog/orders/order",
            ),
        ),
        (
            "extract_default_ns.xml",
            (
                "/catalog/products/product",
                "/catalog/products/product/manufacturer",
                "/catalog/products/product/price",
                "/catalog/products/product/tags",
            ),
        ),
        (
            "extract_prefixed_ns.xml",
            (
                "/s:catalog/s:products/s:product",
                "/s:catalog/s:products/s:product/s:manufacturer",
                "/s:catalog/s:products/s:product/s:price",
                "/s:catalog/s:products/s:product/s:tags",
            ),
        ),
        (
            "namespaced.xml",
            (
                "/catalog/orders/order",
                "/catalog/products/product",
                "/catalog/products/product/manufacturer",
                "/catalog/products/product/price",
                "/catalog/products/product/tags",
            ),
        ),
        (
            "tiny.xml",
            (
                "/catalog/orders/order",
                "/catalog/products/product",
                "/catalog/products/product/manufacturer",
                "/catalog/products/product/price",
                "/catalog/products/product/tags",
            ),
        ),
    ],
)
def test_every_committed_document_keeps_the_candidates_it_had(
    fixtures_dir: Path, name: str, expected: tuple[str, ...]
) -> None:
    """Zero perturbation, pinned per document.

    Only the two documents the rule exists for -- ``bare_leaf`` and ``mixed`` -- may
    differ from what the previous rule produced; every other fixture and the
    generated dataset must be unchanged, or the rule is reaching further than it
    should.
    """
    report = inspect_document(fixtures_dir / name)
    assert tuple(candidate.path for candidate in report.candidates) == expected


def test_the_generated_dataset_keeps_its_candidates(s10_path: Path) -> None:
    report = inspect_document(s10_path)

    assert tuple(candidate.path for candidate in report.candidates) == (
        "/catalog/products/product",
        "/catalog/products/product/manufacturer",
        "/catalog/products/product/price",
        "/catalog/products/product/tags",
        "/catalog/orders/order",
    )
    assert report.candidates[0].count == 29_120
