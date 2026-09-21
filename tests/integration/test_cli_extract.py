"""Integration tests: the ``gigaxml extract`` command, end to end.

Most tests call :func:`gigaxml.cli.main` directly -- same code path, no process
start-up -- and one test runs the installed console script so the
``[project.scripts]`` wiring is covered too.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gigaxml.cli import main
from gigaxml.generate import Manifest, manifest_path_for
from tests._interpreter import gigaxml_script

CONFIG = """\
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
    type: string
  name:
    path: name
  category:
    path: category
  price:
    path: price
    type: decimal
    required: true
  currency:
    path: price/@currency
  manufacturer:
    path: manufacturer/name
  country:
    path: manufacturer/country
  first_tag:
    path: tags/tag
"""


def write_config(tmp_path: Path, text: str = CONFIG) -> Path:
    path = tmp_path / "extraction.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def run_extract(source: Path, config: Path, output: Path, *extra: str) -> int:
    return main(["extract", str(source), "--config", str(config), "--output", str(output), *extra])


# --- the three formats ------------------------------------------------------


def test_extract_to_parquet_from_a_ten_megabyte_file(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Gate 8: one command, a 10MB XML in, a Parquet file out."""
    manifest = Manifest.read(manifest_path_for(s10_path))
    config = write_config(tmp_path)
    output = tmp_path / "products.parquet"

    exit_code = run_extract(s10_path, config, output)

    assert exit_code == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == manifest.record_count
    assert summary["format"] == "parquet"
    assert summary["record_path"] == "/catalog/products/product"
    assert summary["fields"][0] == "product_id"

    import pyarrow.parquet as parquet

    table = parquet.read_table(output)
    assert table.num_rows == manifest.record_count
    assert table.column_names == summary["fields"]

    first = table.to_pydict()
    assert first["product_id"][0] == "1"
    assert first["country"][0] == "SE"
    assert isinstance(first["price"][0], str)
    assert first["name"][0]


def test_extract_to_csv(s10_path: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    output = tmp_path / "products.csv"

    assert run_extract(s10_path, config, output) == 0

    lines = output.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ("product_id,name,category,price,currency,manufacturer,country,first_tag")
    assert lines[1].startswith("1,")
    assert len(lines) == 29121, "29120 records plus the header"


def test_extract_to_jsonl(s10_path: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    output = tmp_path / "products.jsonl"

    assert run_extract(s10_path, config, output) == 0

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 29120
    first = json.loads(lines[0])
    assert first["product_id"] == "1"
    assert isinstance(first["price"], str)


def test_an_explicit_format_overrides_the_extension(s10_path: Path, tmp_path: Path) -> None:
    config = write_config(tmp_path)
    output = tmp_path / "products.data"

    assert run_extract(s10_path, config, output, "--format", "csv") == 0
    assert output.read_text(encoding="utf-8").splitlines()[0].startswith("product_id,")


def test_an_explicit_format_that_contradicts_the_extension_warns(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``-o out.csv --format parquet`` works, but it should not do so silently."""
    config = write_config(tmp_path)
    output = tmp_path / "products.csv"

    assert run_extract(s10_path, config, output, "--format", "jsonl") == 0

    captured = capsys.readouterr()
    assert "warning:" in captured.err
    assert "'jsonl'" in captured.err
    assert json.loads(captured.out)["format"] == "jsonl"
    # And the file really is JSONL, despite the name.
    assert json.loads(output.read_text(encoding="utf-8").splitlines()[0])["product_id"] == "1"


def test_no_warning_when_the_format_matches_the_extension(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    assert run_extract(s10_path, config, tmp_path / "products.csv", "--format", "csv") == 0
    assert "warning:" not in capsys.readouterr().err


def test_batch_size_controls_the_row_groups(s10_path: Path, tmp_path: Path) -> None:
    import pyarrow.parquet as parquet

    config = write_config(tmp_path)
    output = tmp_path / "products.parquet"

    assert run_extract(s10_path, config, output, "--batch-size", "10000") == 0

    metadata = parquet.ParquetFile(output).metadata
    assert metadata.num_rows == 29120
    assert metadata.num_row_groups == 3, "29120 rows at 10000 per batch is 3 row groups"


# --- error paths ------------------------------------------------------------


def test_a_bad_config_is_reported_on_one_line(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path, "record: /a\nfields:\n  x:\n    path: N\n    typo: 1\n")
    exit_code = run_extract(s10_path, config, tmp_path / "out.csv")

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err.startswith("error: ")
    assert "typo" in captured.err


def test_an_unsupported_type_is_reported(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(
        tmp_path, "record: /a\nfields:\n  price:\n    path: P\n    type: deciaml\n"
    )
    assert run_extract(s10_path, config, tmp_path / "out.csv") == 1
    assert "unsupported type" in capsys.readouterr().err


def test_a_missing_input_file_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    exit_code = run_extract(tmp_path / "nope.xml", config, tmp_path / "out.csv")

    assert exit_code == 1
    assert "nope.xml" in capsys.readouterr().err


def test_an_uninferable_output_format_is_reported(
    s10_path: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = write_config(tmp_path)
    assert run_extract(s10_path, config, tmp_path / "out.xml") == 1
    assert "cannot infer an output format" in capsys.readouterr().err


def test_a_record_path_that_matches_nothing_is_reported(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "small.xml"
    source.write_text("<root><other/></root>", encoding="utf-8")
    config = write_config(
        tmp_path, "record: /catalog/products/product\nfields:\n  x:\n    path: N\n"
    )

    assert run_extract(source, config, tmp_path / "out.csv") == 1
    assert "matched 0 elements" in capsys.readouterr().err


# --- the rest of the CLI still works ----------------------------------------


def test_generate_is_unaffected(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output = tmp_path / "small.xml"
    assert main(["generate", "--size", "64KB", "--seed", "7", "-o", str(output)]) == 0

    manifest = json.loads(capsys.readouterr().out)
    assert manifest["seed"] == 7
    assert output.exists()


def test_the_console_script_is_wired_up(s10_path: Path, tmp_path: Path) -> None:
    """The one test that goes through a real process, to cover [project.scripts]."""
    script = gigaxml_script()

    config = write_config(tmp_path)
    output = tmp_path / "products.csv"

    completed = subprocess.run(
        [str(script), "extract", str(s10_path), "-c", str(config), "-o", str(output)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["rows"] == 29120
    assert output.read_text(encoding="utf-8").splitlines()[0].startswith("product_id,")
