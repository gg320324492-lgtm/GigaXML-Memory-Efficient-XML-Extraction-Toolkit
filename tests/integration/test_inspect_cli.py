"""Integration tests: ``inspect``, ``sample`` and the chain between them.

The chain is the point of the phase: point the tool at a document nobody has a
schema for, get a config, run it. So most of these tests drive the commands the way
a user would and check the numbers line up end to end, rather than poking at the
library.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gigaxml.cli import main

TWO_RECORDS = "two_records.xml"


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inspect_json(source: Path, *extra: str) -> dict:
    """Run ``inspect --json`` through the CLI and return the parsed report."""
    import io
    from contextlib import redirect_stdout

    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert main(["inspect", str(source), "--json", *extra]) == 0
    return json.loads(buffer.getvalue())


# --- Gate 3: the reported count is the extracted count ----------------------


def test_the_reported_record_count_equals_the_extracted_row_count(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    source = fixtures_dir / TWO_RECORDS
    report = inspect_json(source)
    config = tmp_path / "auto.yaml"

    assert main(["inspect", str(source), "--generate-config", str(config)]) == 0
    output = tmp_path / "out.parquet"
    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    import pyarrow.parquet as parquet

    assert parquet.read_table(output).num_rows == report["candidates"][0]["count"] == 3


def test_the_reported_count_equals_the_extracted_count_on_a_real_dataset(
    s10_path: Path, tmp_path: Path
) -> None:
    report = inspect_json(s10_path)
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(s10_path), "--generate-config", str(config)]) == 0

    output = tmp_path / "out.parquet"
    assert main(["extract", str(s10_path), "-c", str(config), "-o", str(output)]) == 0

    import pyarrow.parquet as parquet

    assert report["candidates"][0]["count"] == 29_120
    assert parquet.read_table(output).num_rows == 29_120


# --- Gate 4: several candidates, all with evidence --------------------------


def test_two_record_kinds_are_both_reported_with_evidence(
    fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS)]) == 0
    text = capsys.readouterr().out

    assert "/catalog/products/product" in text
    assert "/catalog/orders/order" in text
    assert "3 occurrences" in text
    assert "2 occurrences" in text
    assert "sibling structure consistency" in text
    assert "score" in text


def test_the_json_report_carries_the_same_evidence(fixtures_dir: Path) -> None:
    report = inspect_json(fixtures_dir / TWO_RECORDS)
    by_path = {candidate["path"]: candidate for candidate in report["candidates"]}

    assert by_path["/catalog/products/product"]["count"] == 3
    assert by_path["/catalog/products/product"]["shape_consistency"] == 1.0
    assert "3 occurrences" in by_path["/catalog/products/product"]["evidence"]
    assert by_path["/catalog/orders/order"]["count"] == 2


# --- Gate 5: the path table reports its own truncation ----------------------


def test_a_truncated_path_table_says_so(
    fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS), "--max-paths", "4"]) == 0
    text = capsys.readouterr().out

    assert "WARNING" in text
    assert "path table is full" in text
    assert "not listed" in text


def test_an_untruncated_path_table_does_not_warn(
    fixtures_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS)]) == 0
    assert "path table is full" not in capsys.readouterr().out


# --- Gate 6 and 7: the generated config actually runs -----------------------


@pytest.mark.parametrize(
    "fixture",
    ["extract_default_ns.xml", "extract_prefixed_ns.xml", "two_records.xml"],
)
def test_a_generated_config_round_trips_through_extract(
    fixtures_dir: Path, tmp_path: Path, fixture: str
) -> None:
    source = fixtures_dir / fixture
    report = inspect_json(source)
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(source), "--generate-config", str(config)]) == 0

    output = tmp_path / "out.parquet"
    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 0

    import pyarrow.parquet as parquet

    table = parquet.read_table(output)
    assert table.num_rows == report["candidates"][0]["count"]
    assert table.column_names, "the generated config declares at least one field"


def test_a_default_namespaced_document_reports_its_uri(fixtures_dir: Path) -> None:
    """Without the URI the generated paths would look right and match nothing."""
    report = inspect_json(fixtures_dir / "extract_default_ns.xml")
    assert report["namespaces"] == {"": "urn:example:shop"}
    assert report["candidates"][0]["path"] == "/catalog/products/product"


def test_a_prefixed_document_reports_its_uri_and_prefixed_paths(fixtures_dir: Path) -> None:
    report = inspect_json(fixtures_dir / "extract_prefixed_ns.xml")
    assert report["namespaces"] == {"s": "urn:example:shop"}
    assert report["candidates"][0]["path"].startswith("/s:catalog/")


def test_the_generated_config_writes_a_message_to_stderr_not_stdout(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--json`` on stdout has to stay parseable."""
    config = tmp_path / "auto.yaml"
    assert (
        main(
            ["inspect", str(fixtures_dir / TWO_RECORDS), "--json", "--generate-config", str(config)]
        )
        == 0
    )
    captured = capsys.readouterr()

    json.loads(captured.out)
    assert "wrote a config" in captured.err
    assert config.exists()


# --- Gate 8: types ----------------------------------------------------------


def test_the_default_config_is_all_string(fixtures_dir: Path, tmp_path: Path) -> None:
    """Asserted on the parsed YAML, not on the text.

    The fixture has an attribute called ``type``, so a field is legitimately *named*
    ``type`` and the raw text contains ``type:`` as a key. Parsing removes the
    ambiguity between a field's name and its declared type.
    """
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS), "--generate-config", str(config)]) == 0

    payload = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert payload["fields"], "the config declares fields"
    assert all("type" not in definition for definition in payload["fields"].values())
    assert "Every type is `string`" in config.read_text(encoding="utf-8")


def test_inferred_config_never_uses_decimal(fixtures_dir: Path, tmp_path: Path) -> None:
    config = tmp_path / "auto.yaml"
    assert (
        main(
            [
                "inspect",
                str(fixtures_dir / TWO_RECORDS),
                "--generate-config",
                str(config),
                "--infer-types",
            ]
        )
        == 0
    )
    text = config.read_text(encoding="utf-8")
    payload = yaml.safe_load(text)

    declared = {definition.get("type") for definition in payload["fields"].values()}
    assert "decimal" not in declared, "decimal is never inferred"
    assert "float" in declared, "the money-looking field is inferred as float, and says so"
    assert "INFERRED FROM A SAMPLE" in text
    assert "`decimal` by hand" in text, "and the reader is warned what that costs"


def test_inferred_config_lists_evidence(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(
        "<root>" + "".join(f'<i n="{n}"><d>2024-01-0{n}</d></i>' for n in (1, 2, 3)) + "</root>",
        encoding="utf-8",
    )
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(source), "--generate-config", str(config), "--infer-types"]) == 0
    text = config.read_text(encoding="utf-8")

    assert "n: int -- every sampled value is a whole number" in text
    assert "d: date -- every sampled value parses as an ISO date" in text
    assert "distinct value(s)" in text


def test_inferred_config_warns_about_float_for_money(tmp_path: Path) -> None:
    source = tmp_path / "doc.xml"
    source.write_text(
        "<root>" + "".join(f"<i><p>{n}.50</p></i>" for n in (1, 2)) + "</root>",
        encoding="utf-8",
    )
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(source), "--generate-config", str(config), "--infer-types"]) == 0
    assert "`decimal` by hand" in config.read_text(encoding="utf-8")


# --- Gate 9: sample ---------------------------------------------------------


def test_sampling_twice_gives_identical_bytes(fixtures_dir: Path, tmp_path: Path) -> None:
    source = fixtures_dir / TWO_RECORDS
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(source), "--generate-config", str(config)]) == 0

    first = tmp_path / "first.jsonl"
    second = tmp_path / "second.jsonl"
    for target in (first, second):
        assert main(["sample", str(source), "-c", str(config), "-n", "2", "-o", str(target)]) == 0

    assert sha256_of(first) == sha256_of(second)
    assert len(first.read_text(encoding="utf-8").splitlines()) == 2


def test_sampling_says_the_slice_is_biased(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS), "--generate-config", str(config)]) == 0
    capsys.readouterr()  # drop the inspect report; only the sample JSON is parsed

    assert (
        main(
            [
                "sample",
                str(fixtures_dir / TWO_RECORDS),
                "-c",
                str(config),
                "-n",
                "2",
                "-o",
                str(tmp_path / "out.jsonl"),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["written"] == 2
    assert payload["document_exhausted"] is False
    assert "BIASED" in payload["note"]
    assert "first 2" in payload["note"]


def test_asking_for_more_records_than_exist_says_why(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS), "--generate-config", str(config)]) == 0
    capsys.readouterr()  # drop the inspect report; only the sample JSON is parsed

    assert (
        main(
            [
                "sample",
                str(fixtures_dir / TWO_RECORDS),
                "-c",
                str(config),
                "-n",
                "50",
                "-o",
                str(tmp_path / "out.jsonl"),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["requested"] == 50
    assert payload["written"] == 3
    assert payload["short_of_request"] is True
    assert "holds only 3 record(s)" in payload["note"]
    assert "all of them were written" in payload["note"]


def test_sampling_rejects_a_non_positive_limit(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "auto.yaml"
    assert main(["inspect", str(fixtures_dir / TWO_RECORDS), "--generate-config", str(config)]) == 0

    with pytest.raises(SystemExit) as info:
        main(
            [
                "sample",
                str(fixtures_dir / TWO_RECORDS),
                "-c",
                str(config),
                "-n",
                "0",
                "-o",
                str(tmp_path / "out.jsonl"),
            ]
        )
    assert info.value.code == 2
    assert "at least 1" in capsys.readouterr().err


# --- Gate 10: the batch-size warning ---------------------------------------


def test_a_large_batch_size_warns_on_stderr(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "record: /catalog/products/product\nfields:\n  x:\n    path: '@id'\n", "utf-8"
    )

    assert (
        main(
            [
                "extract",
                str(s10_path),
                "-c",
                str(config),
                "-o",
                str(tmp_path / "out.csv"),
                "--batch-size",
                "1000000",
            ]
        )
        == 0
    )
    err = capsys.readouterr().err
    assert "warning:" in err
    assert "32 MiB" in err
    assert "1,000,000 rows" in err


def test_the_default_batch_size_does_not_warn(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "c.yaml"
    config.write_text(
        "record: /catalog/products/product\nfields:\n  x:\n    path: '@id'\n", "utf-8"
    )

    assert main(["extract", str(s10_path), "-c", str(config), "-o", str(tmp_path / "out.csv")]) == 0
    assert "warning:" not in capsys.readouterr().err


# --- Gate 11: the whole chain, and the error paths -------------------------


def test_inspect_generate_extract_sample_chain_on_a_ten_megabyte_document(
    s10_path: Path, tmp_path: Path
) -> None:
    config = tmp_path / "auto.yaml"
    rows = tmp_path / "rows.parquet"
    sample = tmp_path / "sample.jsonl"

    assert main(["inspect", str(s10_path), "--generate-config", str(config)]) == 0
    assert main(["extract", str(s10_path), "-c", str(config), "-o", str(rows)]) == 0
    assert main(["sample", str(s10_path), "-c", str(config), "-n", "5", "-o", str(sample)]) == 0

    import pyarrow.parquet as parquet

    assert parquet.read_table(rows).num_rows == 29_120
    assert len(sample.read_text(encoding="utf-8").splitlines()) == 5


def test_a_document_with_no_candidate_is_reported_not_crashed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "flat.xml"
    source.write_text("<root><a>1</a><b>2</b><c>3</c></root>", encoding="utf-8")

    assert main(["inspect", str(source), "--generate-config", str(tmp_path / "c.yaml")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "no record candidate" in err


def test_an_out_of_range_candidate_index_is_reported(
    fixtures_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(
        [
            "inspect",
            str(fixtures_dir / TWO_RECORDS),
            "--generate-config",
            str(tmp_path / "c.yaml"),
            "--candidate",
            "99",
        ]
    )
    assert exit_code == 1
    assert "does not exist" in capsys.readouterr().err


def test_malformed_xml_is_reported_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "broken.xml"
    source.write_text("<root><unclosed></root>", encoding="utf-8")

    assert main(["inspect", str(source)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "line" in err


def test_a_missing_input_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["inspect", str(tmp_path / "nope.xml")]) == 1
    assert "nope.xml" in capsys.readouterr().err


def test_the_console_script_runs_inspect(s10_path: Path) -> None:
    """The one test that goes through a real process for the new command."""
    script = Path(sys.executable).parent / ("gigaxml.exe" if sys.platform == "win32" else "gigaxml")
    if not script.exists():  # pragma: no cover - depends on the environment
        pytest.skip(f"console script not installed at {script}")

    completed = subprocess.run(
        [str(script), "inspect", str(s10_path), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["candidates"][0]["path"] == "/catalog/products/product"
    assert payload["candidates"][0]["count"] == 29_120
