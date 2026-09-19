"""Integration tests: the three writers, end to end from XML.

Assertions are per value rather than per row count. A writer that emits the right
number of rows with the columns shifted by one looks perfectly healthy to a row
count.
"""

from __future__ import annotations

import csv
import datetime
import decimal
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

import pytest

from gigaxml.config import ExtractionConfig, parse_config
from gigaxml.errors import WriterError
from gigaxml.fields import extract_record
from gigaxml.parser.streaming import StreamingRecordReader
from gigaxml.writers import (
    DEFAULT_BATCH_SIZE,
    CsvWriter,
    JsonlWriter,
    ParquetWriter,
    WriterFormat,
    create_writer,
)

URI = "urn:example:shop"
DEFAULT_NS_FIXTURE = "extract_default_ns.xml"

FIELDS = {
    "product_id": {"path": "@id"},
    "active": {"path": "@active", "type": "bool"},
    "name": {"path": "name"},
    "price": {"path": "price", "type": "decimal", "required": True},
    "currency": {"path": "price/@currency"},
    "stock": {"path": "stock", "type": "int"},
    "released": {"path": "released", "type": "date"},
    "manufacturer": {"path": "manufacturer/name"},
    "first_tag": {"path": "tags/tag"},
    "warranty": {"path": "warranty"},
}

EXPECTED_ROWS: list[dict[str, object]] = [
    {
        "product_id": "1",
        "active": True,
        "name": "Aurora Desk Lamp",
        "price": decimal.Decimal("49.90"),
        "currency": "USD",
        "stock": 12,
        "released": datetime.date(2023, 11, 14),
        "manufacturer": "Northwind Works",
        "first_tag": "desk",
        "warranty": None,
    },
    {
        "product_id": "2",
        "active": False,
        "name": "Pulse Audio Suite",
        "price": decimal.Decimal("129.00"),
        "currency": "EUR",
        "stock": 0,
        "released": datetime.date(2024, 2, 29),
        "manufacturer": "Kestrel Labs",
        "first_tag": "audio",
        "warranty": None,
    },
]


def build_config(
    fields: dict | None = None,
    *,
    namespaces: dict[str, str] | None = None,
    record: str = "/catalog/products/product",
) -> ExtractionConfig:
    return parse_config(
        {
            "record": record,
            "namespaces": namespaces if namespaces is not None else {"": URI},
            "fields": fields or FIELDS,
        }
    )


def rows_from_fixture(fixture: Path, config: ExtractionConfig) -> list[Mapping[str, object]]:
    reader = StreamingRecordReader(fixture, config.record_path, config.namespaces or None)
    return [extract_record(record, config.fields).values for record in reader]


def write_rows(
    path: Path,
    config: ExtractionConfig,
    rows: Iterable[Mapping[str, object]],
    **kwargs: object,
) -> object:
    with create_writer(path, config.fields, **kwargs) as writer:  # type: ignore[arg-type]
        for row in rows:
            writer.write(row)
    return writer


# --- CSV --------------------------------------------------------------------


def test_csv_writes_the_header_in_config_order_and_every_value(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    config = build_config()
    output = tmp_path / "products.csv"
    write_rows(output, config, rows_from_fixture(fixtures_dir / DEFAULT_NS_FIXTURE, config))

    text = output.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert lines[0] == (
        "product_id,active,name,price,currency,stock,released,manufacturer,first_tag,warranty"
    )
    assert lines[1] == ("1,true,Aurora Desk Lamp,49.90,USD,12,2023-11-14,Northwind Works,desk,")
    assert lines[2] == "2,false,Pulse Audio Suite,129.00,EUR,0,2024-02-29,Kestrel Labs,audio,"
    assert len(lines) == 3


def test_csv_quotes_values_that_need_it(tmp_path: Path) -> None:
    """The csv module handles quoting; a hand-rolled join would not."""
    config = build_config({"name": {"path": "name"}, "note": {"path": "note"}})
    output = tmp_path / "quoted.csv"
    rows = [{"name": "a,b", "note": 'he said "hi"'}, {"name": "x\ny", "note": ""}]
    write_rows(output, config, rows)

    parsed = list(csv.reader(output.read_text(encoding="utf-8").splitlines(True)))
    assert parsed[0] == ["name", "note"]
    assert parsed[1] == ["a,b", 'he said "hi"']


def test_csv_round_trips_through_the_extractors_own_coercion(tmp_path: Path) -> None:
    """Text written by the CSV writer must re-coerce to the same Python values."""
    from gigaxml.fields import coerce_value

    config = build_config()
    output = tmp_path / "products.csv"
    write_rows(output, config, [EXPECTED_ROWS[0]])

    with output.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        row = next(reader)

    for field in config.fields:
        raw = row[field.name]
        if field.required or raw != "":
            assert coerce_value(raw, field.type, field.name) == EXPECTED_ROWS[0][field.name]


def test_csv_writes_an_empty_field_for_a_missing_optional_value(tmp_path: Path) -> None:
    config = build_config({"name": {"path": "name"}, "warranty": {"path": "warranty"}})
    output = tmp_path / "missing.csv"
    write_rows(output, config, [{"name": "x", "warranty": None}])

    assert output.read_text(encoding="utf-8").splitlines()[1] == "x,"


# --- JSONL ------------------------------------------------------------------


def test_jsonl_writes_one_object_per_line_with_every_value(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    config = build_config()
    output = tmp_path / "products.jsonl"
    write_rows(output, config, rows_from_fixture(fixtures_dir / DEFAULT_NS_FIXTURE, config))

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert list(first) == list(config.field_names), "keys follow the configured order"
    assert first["product_id"] == "1"
    assert first["active"] is True
    assert first["name"] == "Aurora Desk Lamp"
    assert first["price"] == "49.90"
    assert first["stock"] == 12
    assert first["released"] == "2023-11-14"
    assert first["manufacturer"] == "Northwind Works"
    assert first["warranty"] is None

    second = json.loads(lines[1])
    assert second["active"] is False
    assert second["price"] == "129.00"
    assert second["stock"] == 0


def test_jsonl_never_writes_a_decimal_as_a_json_number(tmp_path: Path) -> None:
    """A JSON number would come back as a float, losing the value's exactness."""
    config = build_config({"price": {"path": "price", "type": "decimal"}})
    output = tmp_path / "price.jsonl"
    write_rows(output, config, [{"price": decimal.Decimal("0.1")}])

    assert output.read_text(encoding="utf-8").strip() == '{"price": "0.1"}'


def test_jsonl_keeps_non_ascii_readable(tmp_path: Path) -> None:
    config = build_config({"name": {"path": "name"}})
    output = tmp_path / "utf8.jsonl"
    write_rows(output, config, [{"name": "台灯"}])

    assert output.read_text(encoding="utf-8").strip() == '{"name": "台灯"}'


# --- Parquet ----------------------------------------------------------------


def _read_parquet(path: Path) -> object:
    import pyarrow.parquet as parquet

    return parquet.read_table(path)


def test_parquet_round_trips_rows_columns_types_and_values(
    fixtures_dir: Path, tmp_path: Path
) -> None:
    import pyarrow as pa

    config = build_config()
    output = tmp_path / "products.parquet"
    write_rows(output, config, rows_from_fixture(fixtures_dir / DEFAULT_NS_FIXTURE, config))

    table = _read_parquet(output)

    assert table.num_rows == 2
    assert table.column_names == list(config.field_names)
    assert table.schema.field("product_id").type == pa.string()
    assert table.schema.field("active").type == pa.bool_()
    assert table.schema.field("stock").type == pa.int64()
    assert table.schema.field("released").type == pa.date32()
    assert table.schema.field("warranty").nullable is True

    column = table.to_pydict()
    assert column["product_id"] == ["1", "2"]
    assert column["active"] == [True, False]
    assert column["name"] == ["Aurora Desk Lamp", "Pulse Audio Suite"]
    assert column["stock"] == [12, 0]
    assert column["released"] == [datetime.date(2023, 11, 14), datetime.date(2024, 2, 29)]
    assert column["warranty"] == [None, None]


def test_parquet_decimal_is_exact_all_the_way_back(tmp_path: Path) -> None:
    """Gate 4: read back the value and compare it to the original ``Decimal``.

    Both comparisons are asserted on purpose. ``Decimal("49.90") == Decimal("49.9")``
    is *true* -- they are numerically equal -- so equality alone would not notice a
    writer that dropped the trailing zero. The ``str`` comparison is what pins the
    scale.
    """
    config = build_config({"price": {"path": "price", "type": "decimal"}})
    original = decimal.Decimal("49.90")
    output = tmp_path / "price.parquet"
    write_rows(output, config, [{"price": original}])

    table = _read_parquet(output)
    read_back = table.to_pydict()["price"][0]

    assert read_back == str(original)
    assert decimal.Decimal(read_back) == original
    assert str(decimal.Decimal(read_back)) == str(original)


def test_parquet_decimal_survives_a_value_decimal128_could_not_hold(tmp_path: Path) -> None:
    """40 significant digits: ``decimal128(38, s)`` would have to round this.

    Storing decimals as text means there is no precision to guess and no value
    that has to be silently rounded, which is why that representation was chosen
    over ``decimal128``.
    """
    digits = "12345678901234567890123456789012345678.90"
    original = decimal.Decimal(digits)
    assert len(original.as_tuple().digits) == 40

    config = build_config({"price": {"path": "price", "type": "decimal"}})
    output = tmp_path / "wide.parquet"
    write_rows(output, config, [{"price": original}])

    read_back = _read_parquet(output).to_pydict()["price"][0]
    assert str(decimal.Decimal(read_back)) == digits


def test_parquet_decimal_column_is_not_a_float(tmp_path: Path) -> None:
    import pyarrow as pa

    config = build_config({"price": {"path": "price", "type": "decimal"}})
    output = tmp_path / "price.parquet"
    write_rows(output, config, [{"price": decimal.Decimal("1.10")}])

    field_type = _read_parquet(output).schema.field("price").type
    assert field_type != pa.float64()
    assert field_type == pa.string()


def test_parquet_schema_comes_from_the_config_not_from_the_data(tmp_path: Path) -> None:
    """Gate 5: a first batch full of ``None`` must not decide the column type.

    Inferring the schema would make this file's ``stock`` column null-typed (or
    string-typed), and the second row's integer would then either fail or arrive
    as text.
    """
    import pyarrow as pa

    config = build_config(
        {
            "product_id": {"path": "@id"},
            "stock": {"path": "stock", "type": "int"},
            "released": {"path": "released", "type": "date"},
            "price": {"path": "price", "type": "decimal"},
        },
        namespaces={},
    )
    document = tmp_path / "mixed.xml"
    document.write_text(
        "<catalog><products>"
        '<product id="1"></product>'
        '<product id="2"><stock>7</stock><released>2024-01-02</released>'
        "<price>3.30</price></product>"
        "</products></catalog>",
        encoding="utf-8",
    )

    output = tmp_path / "mixed.parquet"
    write_rows(output, config, rows_from_fixture(document, config))

    table = _read_parquet(output)
    assert table.schema.field("stock").type == pa.int64()
    assert table.schema.field("released").type == pa.date32()
    assert table.schema.field("price").type == pa.string()
    assert table.to_pydict()["stock"] == [None, 7]
    assert table.to_pydict()["price"] == [None, "3.30"]


def test_parquet_writes_one_row_group_per_batch(tmp_path: Path) -> None:
    """The whole point of batching: nothing is collected and written at the end."""
    import pyarrow.parquet as parquet

    config = build_config({"name": {"path": "name"}})
    output = tmp_path / "batched.parquet"
    write_rows(
        output,
        config,
        [{"name": f"n{i}"} for i in range(7)],
        batch_size=2,
    )

    metadata = parquet.ParquetFile(output).metadata
    assert metadata.num_rows == 7
    assert metadata.num_row_groups == 4, "7 rows at 2 per batch is 4 row groups"


def test_parquet_output_does_not_depend_on_the_batch_size(tmp_path: Path) -> None:
    config = build_config()
    rows = EXPECTED_ROWS * 3

    small = tmp_path / "small.parquet"
    large = tmp_path / "large.parquet"
    write_rows(small, config, rows, batch_size=1)
    write_rows(large, config, rows, batch_size=DEFAULT_BATCH_SIZE)

    assert _read_parquet(small).to_pydict() == _read_parquet(large).to_pydict()


# --- shared behaviour -------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,expected",
    [
        (".csv", CsvWriter),
        (".jsonl", JsonlWriter),
        (".ndjson", JsonlWriter),
        (".parquet", ParquetWriter),
        (".pq", ParquetWriter),
    ],
)
def test_the_extension_selects_the_writer(tmp_path: Path, suffix: str, expected: type) -> None:
    config = build_config({"name": {"path": "name"}})
    writer = create_writer(tmp_path / f"out{suffix}", config.fields)
    try:
        assert isinstance(writer, expected)
    finally:
        writer.close()


def test_an_unknown_extension_is_rejected(tmp_path: Path) -> None:
    config = build_config({"name": {"path": "name"}})
    with pytest.raises(WriterError, match="cannot infer an output format"):
        create_writer(tmp_path / "out.xml", config.fields)


def test_an_explicit_format_overrides_the_extension(tmp_path: Path) -> None:
    config = build_config({"name": {"path": "name"}})
    output = tmp_path / "out.data"
    writer = create_writer(output, config.fields, output_format="csv")
    try:
        assert isinstance(writer, CsvWriter)
        writer.write({"name": "x"})
    finally:
        writer.close()
    assert output.read_text(encoding="utf-8").splitlines() == ["name", "x"]


def test_an_unknown_format_name_is_rejected(tmp_path: Path) -> None:
    config = build_config({"name": {"path": "name"}})
    with pytest.raises(WriterError, match="unknown output format"):
        create_writer(tmp_path / "out.csv", config.fields, output_format="xml")


@pytest.mark.parametrize("batch_size", [0, -1])
def test_a_non_positive_batch_size_is_rejected(tmp_path: Path, batch_size: int) -> None:
    config = build_config({"name": {"path": "name"}})
    with pytest.raises(WriterError, match="batch_size must be at least 1"):
        create_writer(tmp_path / "out.csv", config.fields, batch_size=batch_size)


def test_a_row_with_the_wrong_keys_is_rejected(tmp_path: Path) -> None:
    """A missing key would shift every later CSV column; that must not be silent."""
    config = build_config({"a": {"path": "a"}, "b": {"path": "b"}})
    with create_writer(tmp_path / "out.csv", config.fields, batch_size=1) as writer:
        writer.write({"a": "1", "b": "2"})
        with pytest.raises(WriterError) as info:
            writer.write({"a": "3"})

    message = str(info.value)
    assert "missing ['b']" in message
    assert "unexpected []" in message


def test_a_row_with_an_extra_key_is_rejected(tmp_path: Path) -> None:
    config = build_config({"a": {"path": "a"}})
    with (
        create_writer(tmp_path / "out.jsonl", config.fields, batch_size=1) as writer,
        pytest.raises(WriterError, match=r"unexpected \['b'\]"),
    ):
        writer.write({"a": "1", "b": "2"})


def test_writing_after_close_is_rejected(tmp_path: Path) -> None:
    config = build_config({"a": {"path": "a"}})
    writer = create_writer(tmp_path / "out.csv", config.fields)
    writer.close()
    with pytest.raises(WriterError, match="already closed"):
        writer.write({"a": "1"})


def test_close_is_idempotent_and_flushes_the_last_partial_batch(tmp_path: Path) -> None:
    config = build_config({"a": {"path": "a"}})
    writer = create_writer(tmp_path / "out.csv", config.fields, batch_size=10)
    writer.write({"a": "1"})
    assert writer.rows_written == 0, "still buffered"
    writer.close()
    writer.close()

    assert writer.rows_written == 1
    assert (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines() == ["a", "1"]


def test_write_all_reports_the_total_including_the_last_batch(tmp_path: Path) -> None:
    config = build_config({"a": {"path": "a"}})
    with create_writer(tmp_path / "out.csv", config.fields, batch_size=2) as writer:
        total = writer.write_all([{"a": str(i)} for i in range(5)])

    assert total == 4, "the 5th row is still in the buffer at that point"
    assert writer.rows_written == 5


def test_a_writer_needs_at_least_one_field(tmp_path: Path) -> None:
    with pytest.raises(WriterError, match="at least one field"):
        create_writer(tmp_path / "out.csv", [])


def test_every_writer_error_is_catchable_through_the_shared_base(tmp_path: Path) -> None:
    from gigaxml.errors import GigaXMLError

    config = build_config({"a": {"path": "a"}})
    with pytest.raises(GigaXMLError):
        create_writer(tmp_path / "out.xml", config.fields)


def test_format_names_round_trip() -> None:
    assert WriterFormat.from_name("CSV") is WriterFormat.CSV
    assert WriterFormat.from_path("x.NDJSON") is WriterFormat.JSONL
    assert WriterFormat.from_path("x.pq") is WriterFormat.PARQUET


def test_maybe_from_path_returns_none_for_an_unknown_extension() -> None:
    """An unknown extension is only an error when nothing else decides the format."""
    assert WriterFormat.maybe_from_path("x.csv") is WriterFormat.CSV
    assert WriterFormat.maybe_from_path("x.data") is None
    assert WriterFormat.maybe_from_path("x") is None


# --- pyarrow is optional ----------------------------------------------------


def test_csv_and_jsonl_work_without_pyarrow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``pyarrow`` is an extra, so only the Parquet path may need it.

    The import lives inside the Parquet writer for exactly this reason; if it ever
    moves to module scope, importing the writers would start requiring an optional
    dependency and this test is what notices.
    """
    _block_pyarrow(monkeypatch)
    config = build_config({"name": {"path": "name"}})

    with create_writer(tmp_path / "out.csv", config.fields) as writer:
        writer.write({"name": "csv"})
    with create_writer(tmp_path / "out.jsonl", config.fields) as writer:
        writer.write({"name": "jsonl"})

    assert (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines() == ["name", "csv"]
    assert (tmp_path / "out.jsonl").read_text(encoding="utf-8").strip() == '{"name": "jsonl"}'


def test_parquet_without_pyarrow_explains_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _block_pyarrow(monkeypatch)
    config = build_config({"name": {"path": "name"}})

    with pytest.raises(WriterError) as info:
        create_writer(tmp_path / "out.parquet", config.fields)

    message = str(info.value)
    assert "optional 'pyarrow' dependency" in message
    assert "gigaxml[parquet]" in message


def _block_pyarrow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import pyarrow`` fail, as it would on a CSV-only install."""
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: object, **kwargs: object) -> object:
        if name == "pyarrow" or name.startswith("pyarrow."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", blocked)
