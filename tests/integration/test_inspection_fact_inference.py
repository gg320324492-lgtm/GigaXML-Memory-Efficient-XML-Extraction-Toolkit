"""Fact and inference, told apart -- in the code, in the JSON, and in the human output.

★ **This milestone's actual subject.** Splitting ``inspect.py`` into modules was the
mechanism; the thing being bought is that "how many times did this path occur" and
"this is probably the record" stopped being the same kind of claim. They are counted
versus judged, they can be wrong for entirely different reasons, and before this
milestone they were computed a few lines apart in one function body.

So these tests are not about the split -- the golden suite already proves the bytes did
not move -- they are about the distinction surviving in a form a program can read.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gigaxml.inspect import inspect_document
from gigaxml.inspection.models import FACT, FIELD_KINDS, INFERENCE, InspectionReport

DOCUMENT = """<?xml version="1.0" encoding="UTF-8"?>
<catalog>
  <products>
    <product id="1"><name>Alpha</name><price currency="USD">1.50</price></product>
    <product id="2"><name>Beta</name><price currency="USD">2.50</price></product>
    <product id="3"><name>Gamma</name><price currency="EUR">3.50</price></product>
  </products>
  <orders>
    <order id="90"><total>10.00</total></order>
    <order id="91"><total>20.00</total></order>
  </orders>
</catalog>
"""


@pytest.fixture
def report(tmp_path: Path) -> InspectionReport:
    document = tmp_path / "catalog.xml"
    document.write_text(DOCUMENT, encoding="utf-8")
    return inspect_document(str(document))


# --- the machine-readable half: every field says which kind of claim it is ---


def test_every_candidate_field_is_classified_as_fact_or_inference(
    report: InspectionReport,
) -> None:
    """★ **The label is exhaustive, and that is the point.**

    A partial label is worse than none: a consumer that reads ``FIELD_KINDS`` and
    finds a field missing has to decide whether the omission means "fact" or "not yet
    thought about", and there is no way to tell those apart. So the mapping must cover
    every field a serialised candidate carries.

    Checked against the **real** serialised candidate rather than against
    ``Candidate.to_dict`` alone, because the report adds one field of its own --
    ``nested_inside`` -- after the candidate has serialised. Measuring the hand-written
    list instead would have declared the mapping complete while leaving that field
    unlabelled in the JSON a caller actually receives.
    """
    serialised = report.to_dict()["candidates"][0]

    assert set(FIELD_KINDS) == set(serialised), (
        "FIELD_KINDS and the serialised candidate have drifted apart: "
        f"unlabelled {sorted(set(serialised) - set(FIELD_KINDS))}, "
        f"stale {sorted(set(FIELD_KINDS) - set(serialised))}"
    )


def test_the_two_kinds_are_the_only_two_values_used() -> None:
    """A third label is not a refinement, it is a spelling mistake waiting to spread."""
    assert set(FIELD_KINDS.values()) <= {FACT, INFERENCE}
    assert FACT != INFERENCE


def test_a_count_is_a_fact_and_a_score_is_not(report: InspectionReport) -> None:
    """★ **The distinction, on a real document, by name.**

    ``count`` is what the scanner counted. ``score``, ``repeat_score`` and
    ``shape_consistency`` are what the ranking made of those counts. Both appear on the
    same candidate -- that is the whole reason the label is needed -- and the test says
    which is which without repeating the implementation's own comments.
    """
    assert report.candidates, "the fixture document should produce candidates"
    fields = FIELD_KINDS

    assert fields["count"] == FACT
    assert fields["path"] == FACT
    assert fields["depth"] == FACT

    assert fields["score"] == INFERENCE
    assert fields["repeat_score"] == INFERENCE
    assert fields["shape_consistency"] == INFERENCE


def test_the_json_carries_the_distinction_to_a_caller_that_never_reads_this_file(
    report: InspectionReport,
) -> None:
    """★ **Criterion C: a program reading ``--json`` can tell without reading the source.**

    The requirement is that the distinction is machine-readable *in the output*, not in
    a docstring. This asserts the mapping survives ``to_dict`` and then a JSON
    round-trip -- so it is genuinely in the bytes a tool receives, and not a Python
    attribute that stops existing the moment the report is serialised.
    """
    payload = json.loads(json.dumps(report.to_dict()))

    assert "field_kinds" in payload, "the JSON does not say which fields are judgements"
    kinds = payload["field_kinds"]

    for candidate in payload["candidates"]:
        for field in candidate:
            assert field in kinds, f"{field} appears in the JSON with no classification"
            assert kinds[field] in {FACT, INFERENCE}

    # And the claim is non-vacuous: a candidate really does carry both kinds at once.
    candidate = payload["candidates"][0]
    assert kinds["count"] == FACT
    assert kinds["score"] == INFERENCE
    assert isinstance(candidate["count"], int)
    assert isinstance(candidate["score"], float)


def test_a_candidate_count_is_an_integer_the_scan_produced_not_a_rounded_score(
    report: InspectionReport,
) -> None:
    """The label is only worth something if it matches what the values actually are.

    A field marked ``fact`` that carries a heuristic would make the label worse than
    useless -- a consumer would trust it. ``count`` is an ``int`` off the counter; the
    score is a ``float`` the ranking computed.
    """
    for candidate in report.candidates:
        assert isinstance(candidate.count, int)
        assert isinstance(candidate.score, float)
        assert 0.0 <= candidate.repeat_score <= 1.0
        assert 0.0 <= candidate.shape_consistency <= 1.0


# --- the human half: an inference must not read like a measurement ---


def test_the_text_output_says_score_and_says_occurrences(report: InspectionReport) -> None:
    """★ **Criterion C's other half: the wording never turns a guess into a statement.**

    The human output is allowed to show both, and does. What it must not do is present
    the judgement as though it were measured -- "record path: /catalog/products/product,
    count: 3" with no marker would read as a finding. It says ``score`` for the
    judgement and ``occurrences`` for the count, and the heading says "candidates",
    not "record path".
    """
    text = report.to_text()

    assert "record candidates" in text, "the heading stopped marking these as candidates"
    assert "score" in text
    assert "occurrences" in text
    # The strongest available negative: nothing presents a candidate as *the* record.
    assert "the record path is" not in text.lower()
    assert "likely" not in text.lower() or "candidate" in text.lower(), (
        "if the wording marks a guess, it must do so alongside the word candidate"
    )


def test_the_text_output_never_asserts_a_score_is_a_probability(report: InspectionReport) -> None:
    """``score 0.98`` is a ranking term, not a 98% chance.

    A number rendered as ``confidence 0.98`` invites a caller to treat it as a
    probability and threshold on it. The word is ``score``, and it stays ``score``.
    """
    text = report.to_text().lower()
    assert "confidence" not in text, (
        "the report started calling the ranking a confidence; a score is not a probability"
    )


# --- the code half: which module a value comes from ---


def test_the_scanner_calls_the_ranking_through_one_named_function() -> None:
    """★ **The seam is a function, so the boundary is checkable rather than intended.**

    ``scanner`` measures and ``candidates`` judges. The walk reaches the ranking through
    ``score_candidates`` and nothing else, which is what lets the next test assert that
    the scanner contains no scoring at all.
    """
    from gigaxml.inspection import scanner

    source = Path(scanner.__file__).read_text(encoding="utf-8")

    assert "score_candidates(" in source, "the scanner no longer calls the ranking"
    # The private helpers must not be reachable from the fact layer: calling them
    # directly would work identically and would put the judgement back in the scanner.
    assert "_score_candidates(" not in source
    assert "_with_namespaces(" not in source
