"""Every run report says which format it is, not just what the tool was.

``schema_version`` is the report's own shape marker, distinct from ``tool_version``:
a patch release changes one and not the other, and a consumer who wants to know what
fields to expect should read the former rather than parse version strings.

The assertion is on the **value**, not on "there is one more key". A report carrying
``schema_version: 7`` would be present and useless. A run that writes a report without
it would be worse -- a consumer would have no way to tell an old shape from a new one.

The interrupted case is asserted in ``test_interrupt_report.py``, beside the rest of
that test's evidence: it already builds a stopped run, and splitting the assertion out
would mean building a second one for no reason. All four are asserted somewhere; this
file covers the three that end without a signal.
"""

from __future__ import annotations

import json
from pathlib import Path

from gigaxml.cli import main

CONFIG = """\
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""

#: The value every report this build writes must carry.
EXPECTED_SCHEMA_VERSION = 1

SOURCE_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    "<catalog><products>"
    '<product id="1"><name>Alpha</name></product>'
    '<product id="2"><name>Beta</name></product>'
    '<product id="3"><name>Gamma</name></product>'
    "</products></catalog>"
)


def write_run(tmp_path: Path) -> tuple[Path, Path]:
    """A source document and a config, laid out for one extract."""
    source = tmp_path / "src.xml"
    source.write_text(SOURCE_XML, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(CONFIG, encoding="utf-8")
    return source, config


def report_keys(report_path: Path) -> dict[str, object]:
    return json.loads(report_path.read_text(encoding="utf-8"))


def test_a_successful_run_declares_the_format_it_wrote(tmp_path: Path) -> None:
    """The plain case: a run that finished, with its report beside the output."""
    source, config = write_run(tmp_path)

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]) == 0

    report = report_keys(tmp_path / "run-report.json")
    assert report["schema_version"] == EXPECTED_SCHEMA_VERSION


def test_a_failed_run_declares_the_format_it_wrote(tmp_path: Path) -> None:
    """A run that refused before writing anything still reports its own shape.

    A failure is exactly when a consumer most needs to know what it is looking at: the
    report is how it finds out the run failed. The shape marker belongs there too.
    """
    source, _config = write_run(tmp_path)
    bad_config = tmp_path / "unmatched.yaml"
    bad_config.write_text(
        'record: /nope/nothing\nfields:\n  product_id:\n    path: "@id"\n', encoding="utf-8"
    )

    assert (
        main(["extract", str(source), "-c", str(bad_config), "-o", str(tmp_path / "out.csv")]) != 0
    )

    report = report_keys(tmp_path / "run-report.json")
    assert report["status"] == "failed"
    assert report["schema_version"] == EXPECTED_SCHEMA_VERSION


def test_a_checkpointed_run_declares_the_format_it_wrote(tmp_path: Path) -> None:
    """The checkpoint block is additive: the new field sits alongside it, not instead of it.

    A checkpointed report carries one more key than an ordinary one (``checkpoint``),
    and the format marker appears in both -- otherwise the field would itself be
    conditional, and a consumer would have to guess which reports declare a version.
    """
    source, config = write_run(tmp_path)
    parts = tmp_path / "parts"

    assert (
        main(
            [
                "extract",
                str(source),
                "-c",
                str(config),
                "-o",
                str(parts),
                "--checkpoint-every",
                "2",
            ]
        )
        == 0
    )

    report = report_keys(parts / "run-report.json")
    assert "checkpoint" in report, "this case exists to cover a report that has the block"
    assert report["schema_version"] == EXPECTED_SCHEMA_VERSION


def test_a_sample_reports_the_format_too(tmp_path: Path) -> None:
    """``sample`` writes the same report as ``extract``, so the marker is not extract-only."""
    source, config = write_run(tmp_path)

    assert (
        main(["sample", str(source), "-c", str(config), "-n", "1", "-o", str(tmp_path / "s.csv")])
        == 0
    )

    report = report_keys(tmp_path / "run-report.json")
    assert report["schema_version"] == EXPECTED_SCHEMA_VERSION


def test_the_marker_is_distinct_from_the_tool_version(tmp_path: Path) -> None:
    """Both appear, and they are separate keys carrying separate meanings.

    Asserted so the two can never be merged "because both are versions": a patch release
    moves ``tool_version`` without moving ``schema_version``, and that difference is the
    reason the second field exists.
    """
    source, config = write_run(tmp_path)
    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]) == 0

    report = report_keys(tmp_path / "run-report.json")
    assert report["schema_version"] == EXPECTED_SCHEMA_VERSION
    # Two keys, two purposes: the tool's version is a string about the build, the
    # format's version is an integer about the shape of this file.
    assert isinstance(report["tool_version"], str)
    assert isinstance(report["schema_version"], int)
    assert {"schema_version", "tool_version"} <= set(report)
