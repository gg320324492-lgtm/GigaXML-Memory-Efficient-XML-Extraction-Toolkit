"""Integration tests: namespaces on *attributes*, and the reserved ``xml`` prefix.

This file exists because of a hole in the fixture set: every document under
``tests/fixtures/`` before this had namespace prefixes on **elements** and none on
**attributes**, so nothing exercised the path where an attribute's prefix is the only
place that prefix appears. ``inspect`` dropped it, and the config it generated either
failed to load or -- worse, for ``xml:lang`` -- loaded, matched nothing, and
extracted ``None`` with exit code 0.

Each test drives the whole chain a user would: ``inspect --generate-config`` ->
``load_config`` -> ``extract``, asserting per value. A row count would not have
caught any of this.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.cli import main
from gigaxml.config import ExtractionConfig, load_config
from gigaxml.errors import InspectionError
from gigaxml.fields import extract_record
from gigaxml.inspect import generate_config, inspect_document
from gigaxml.parser.streaming import StreamingRecordReader

XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
DC_NAMESPACE = "http://purl.org/dc/elements/1.1/"


def run_chain(source: Path, config_path: Path) -> tuple[ExtractionConfig, list[dict]]:
    """inspect -> generate-config -> load_config -> extract, the way a user would."""
    assert main(["inspect", str(source), "--generate-config", str(config_path)]) == 0
    config = load_config(config_path)
    rows = [
        dict(extract_record(record, config.fields).values)
        for record in StreamingRecordReader(source, config.record_path, config.namespaces or None)
    ]
    return config, rows


# --- the five cases the fix has to cover ------------------------------------


def test_a_prefix_declared_on_an_ancestor_reaches_the_config(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    """``nsA``: ``dc`` appears only on the attribute, so only the attribute can report it."""
    config, rows = run_chain(fixtures_dir / "nsA.xml", tmp_path / "c.yaml")

    assert config.namespaces == {"dc": DC_NAMESPACE}
    assert [row["creator"] for row in rows] == ["c1", "c2", "c1"]
    assert [row["name"] for row in rows] == ["Alpha", "Beta", "Gamma"]


def test_an_attribute_prefix_survives_a_default_namespace_too(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    """``nsB``: the element's default namespace is already handled; the attribute's is not."""
    config, rows = run_chain(fixtures_dir / "nsB.xml", tmp_path / "c.yaml")

    assert config.namespaces == {"": "urn:example:items", "dc": DC_NAMESPACE}
    assert [row["creator"] for row in rows] == ["c1", "c2", "c1"]


def test_an_attribute_prefix_survives_prefixed_element_names(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    """``nsC`` is the decisive case: ``r`` reaches the map, and ``dc`` must too.

    Element prefixes were already registered, so a document whose elements are
    prefixed could look like it worked while its attribute prefix was silently
    dropped -- the error message said ``known prefixes: ['r']``.
    """
    config, rows = run_chain(fixtures_dir / "nsC.xml", tmp_path / "c.yaml")

    assert config.namespaces == {"dc": DC_NAMESPACE, "r": "urn:example:root"}
    assert config.record_path == "/r:catalog/r:items/r:item"
    assert [row["creator"] for row in rows] == ["c1", "c2", "c1"]
    assert [row["name"] for row in rows] == ["Alpha", "Beta", "Gamma"]


def test_an_attribute_prefix_from_a_grandparent_reaches_the_config(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    """``shadowed``: the declaration is two levels up and appears nowhere else."""
    config, rows = run_chain(fixtures_dir / "shadowed.xml", tmp_path / "c.yaml")

    assert config.namespaces == {"a": "urn:outer"}
    assert [row["x"] for row in rows] == ["1", "2"]
    assert [row["name"] for row in rows] == ["Alpha", "Beta"]


@pytest.mark.parametrize(
    "fixture,slot,expected",
    [
        ("xml_lang.xml", "lang", ["en", "fr", "en"]),
        ("xml_space.xml", "space", ["preserve", "default", "preserve"]),
    ],
)
def test_the_reserved_xml_prefix_is_registered(
    fixtures_dir: Path, tmp_path: Path, fixture: str, slot: str, expected: list[str]
) -> None:
    """``xml:lang`` / ``xml:space``: the silent-corruption case.

    XML binds the ``xml`` prefix to its own namespace implicitly -- it never appears
    in an element's ``nsmap`` -- so the prefix was unknown, the segment rendered as a
    bare ``lang``, and the generated config *loaded fine* and extracted ``None`` for
    every row with exit code 0. Nothing failed; the data was simply empty.
    """
    config, rows = run_chain(fixtures_dir / fixture, tmp_path / "c.yaml")

    assert config.namespaces == {"xml": XML_NAMESPACE}
    assert [row[slot] for row in rows] == expected
    assert all(row[slot] is not None for row in rows), "the whole point: not None"


def test_the_xml_prefix_is_reported_with_its_real_uri(fixtures_dir: Path) -> None:
    report = inspect_document(fixtures_dir / "xml_lang.xml")
    candidate = report.candidates[0]

    assert candidate.attribute_names == ("@xml:lang",)
    assert candidate.namespaces == {"xml": XML_NAMESPACE}
    assert candidate.missing_namespaces == ()


def test_an_explicit_declaration_of_the_xml_prefix_is_not_shadowing(tmp_path: Path) -> None:
    """Declaring ``xmlns:xml`` for its own URI is legal and must not be flagged."""
    source = tmp_path / "explicit.xml"
    source.write_text(
        f'<root xmlns:xml="{XML_NAMESPACE}">'
        '<item xml:lang="en"><name>a</name></item>'
        '<item xml:lang="fr"><name>b</name></item>'
        "</root>",
        encoding="utf-8",
    )
    report = inspect_document(source)

    assert report.shadowed_prefixes == ()
    assert report.candidates[0].namespaces == {"xml": XML_NAMESPACE}


# --- the guard must see attributes ------------------------------------------


def test_missing_namespaces_is_not_blind_to_attributes(fixtures_dir: Path) -> None:
    """The structured signal said "fine" while the config could not be loaded.

    ``missing_namespaces`` only looked at the path and the child tags, so a candidate
    whose path needs no namespace but whose attribute does reported an empty tuple.
    """
    for name in ("nsA.xml", "nsB.xml", "nsC.xml", "shadowed.xml", "xml_lang.xml"):
        candidate = inspect_document(fixtures_dir / name).candidates[0]
        assert candidate.missing_namespaces == (), name
        assert candidate.namespaces, f"{name} must carry the prefixes it uses"


def test_an_attribute_prefix_rebound_along_one_path_is_refused(tmp_path: Path) -> None:
    """Two sibling subtrees bind ``a`` differently, so ``@a:x`` has no single meaning.

    ``/root/set/item`` repeats across both subtrees with ``a`` standing for a
    different namespace each time. One flat ``namespaces:`` block cannot describe
    that, so this refuses rather than picking one binding and being wrong half the
    time. It is also the case the old guard could not see, because it never looked at
    attributes.
    """
    source = tmp_path / "attr_shadowed.xml"
    source.write_text(
        '<root><set xmlns:a="urn:one">'
        '<item a:x="1"><n>a</n></item><item a:x="2"><n>b</n></item></set>'
        '<set xmlns:a="urn:two">'
        '<item a:x="3"><n>c</n></item><item a:x="4"><n>d</n></item></set></root>',
        encoding="utf-8",
    )
    report = inspect_document(source)
    candidate = report.candidates[0]

    assert candidate.path == "/root/set/item"
    assert candidate.count == 4
    assert candidate.missing_namespaces == ("a",)
    with pytest.raises(InspectionError, match="more than one URI"):
        generate_config(report)


def test_the_accidental_success_case_still_works(tmp_path: Path) -> None:
    """``<r:item r:creator="c1">`` used to work by luck, not by design.

    The attribute prefix happened to equal the element prefix, so the element's own
    ``note_namespace`` registered it. It must keep working now that attributes
    register their own prefixes.
    """
    source = tmp_path / "same_prefix.xml"
    source.write_text(
        '<r:root xmlns:r="urn:r">'
        '<r:item r:creator="c1"><r:name>Alpha</r:name></r:item>'
        '<r:item r:creator="c2"><r:name>Beta</r:name></r:item>'
        "</r:root>",
        encoding="utf-8",
    )
    config, rows = run_chain(source, tmp_path / "c.yaml")

    assert config.namespaces == {"r": "urn:r"}
    assert [row["creator"] for row in rows] == ["c1", "c2"]


# --- nothing else moved -----------------------------------------------------


def test_the_six_untouched_documents_generate_identical_configs(fixtures_dir: Path) -> None:
    """The fix must not change the output for a document with no prefixed attribute.

    The digest drops the ``# Source:`` line, which echoes the caller's spelling of the
    path -- ``data\\s10.xml`` on Windows, ``data/s10.xml`` elsewhere -- and would
    otherwise make the value platform-dependent.
    """
    import hashlib

    def digest(text: str) -> str:
        body = "\n".join(line for line in text.splitlines() if not line.startswith("# Source:"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    expected = {
        "extract_default_ns.xml": (
            "df41f7be2db9f8b3dfa4ceddab1c008365a52ed876c7471258e376acb27b99f8"
        ),
        "extract_prefixed_ns.xml": (
            "5d5095a8c28a677ab1bd37a5bd027bd2d08a7a3009f79c966ee084168968e7b7"
        ),
        "namespaced.xml": ("1e685fea39b7abd22902b1092dd27b526f06fd1e0a2dcd49ef0e09b97782eb1d"),
        "tiny.xml": ("bfb9b3b6c40ad414e5e2f6bc9c196b51e8bccf92961ea6032250f16e9caf6e5b"),
        "two_records.xml": ("9bf83525570f27aafdb861f6fea0c48a6c658ddd50016b90a1eb980c9f9e4b59"),
    }
    actual = {
        name: digest(generate_config(inspect_document(fixtures_dir / name), candidate_index=1))
        for name in expected
    }
    assert actual == expected


def test_the_cli_reports_the_xml_prefix_in_its_json_report(fixtures_dir: Path) -> None:
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(["inspect", str(fixtures_dir / "xml_lang.xml"), "--json"]) == 0
    payload = json.loads(buffer.getvalue())

    assert payload["candidates"][0]["namespaces"] == {"xml": XML_NAMESPACE}
    assert payload["candidates"][0]["attribute_names"] == ["@xml:lang"]
