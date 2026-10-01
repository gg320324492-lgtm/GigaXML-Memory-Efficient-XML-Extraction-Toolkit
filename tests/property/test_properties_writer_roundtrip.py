"""Property tests: a value that goes out comes back the same.

**The invariant.** For any scalar this tool supports, writing it through a
:class:`RowWriter` and reading the file back with the same field type returns **the same
value** -- or refuses with a :class:`FieldTypeError`. It does not return a *different*
value, and it does not raise something a caller cannot catch.

★ **Why this is the round trip worth automating.** ``CsvWriter._render`` turns a Python
object into text and ``coerce_value`` turns text back into a Python object. They are two
functions in two modules, they were written for different purposes, and **nothing asserts
that they agree**. The unit tests for each pass, and a value that renders one way and
coerces another is invisible until a user sums a column and gets a different total.

The cases below are chosen because they are exactly where a pair of independent
conversions disagrees: values whose text form is not obviously their own (floats and
``Decimal``), values with more than one spelling (``bool``), values containing the
characters CSV has to quote, and the value that is not a value at all (``None``).
"""

from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import pathlib
import tempfile

from hypothesis import assume, given
from hypothesis import strategies as st

from gigaxml.config import parse_config
from gigaxml.errors import FieldTypeError, WriterError
from gigaxml.fields import FieldType, coerce_value
from gigaxml.writers import WriterFormat, create_writer

from .conftest import FILE_PROPERTY_SETTINGS, accepts_or_refuses

#: One family for the writer: either it round-trips or it refuses with this.
WRITER_ERRORS: tuple[type[BaseException], ...] = (FieldTypeError, WriterError)

CONFIG_TEMPLATE = "record: /r\nfields:\n  value:\n    path: p\n    type: {type_name}\n"


def config_for(type_: FieldType) -> object:
    """A one-field config of the given type, built through the public parser."""
    return parse_config(
        {
            "record": "/r",
            "fields": {"value": {"path": "p", "type": type_.value}},
        }
    )


# --- the scalars, per declared type --------------------------------------------------

STRINGS = st.text(max_size=40)

#: Characters a CSV cell has to quote, plus the line terminators that a reader opened
#: with the wrong ``newline`` would silently rewrite.
NASTY_STRINGS = st.sampled_from(
    [
        "",
        " ",
        "a,b",
        'quote"inside',
        "line\nbreak",
        "carriage\rreturn",
        "crlf\r\npair",
        "trailing\r",
        "trailing\n",
        " leading and trailing ",
        "null\x00byte",
        "tab\there",
        "emoji \U0001f600 and accents éü",
        "\x0b\x0c",
        ",,,",
        '""',
    ]
)

#: Finite floats only. ``nan`` and ``inf`` are refused by ``_NUMERIC_LITERAL`` on purpose --
#: the module says so -- and a refusal is a different property, checked below.
FLOATS = st.floats(allow_nan=False, allow_infinity=False, width=64)

DECIMALS = st.decimals(
    allow_nan=False,
    allow_infinity=False,
    places=None,
    min_value=decimal.Decimal("-1e12"),
    max_value=decimal.Decimal("1e12"),
)

DATES = st.dates(min_value=datetime.date(1, 1, 1), max_value=datetime.date(9999, 12, 31))

BOOLS = st.booleans()

INTS = st.integers(min_value=-(2**128), max_value=2**128)

BY_TYPE: dict[FieldType, st.SearchStrategy[object]] = {
    FieldType.STRING: st.one_of(STRINGS, NASTY_STRINGS),
    FieldType.INT: INTS,
    FieldType.FLOAT: st.one_of(FLOATS, st.sampled_from([0.0, -0.0, 1e-300, 1e300, 0.1, -0.5])),
    FieldType.DECIMAL: st.one_of(
        DECIMALS,
        st.sampled_from(
            [
                decimal.Decimal("0"),
                decimal.Decimal("1.50"),
                decimal.Decimal("-0.001"),
                decimal.Decimal("1E+3"),
            ]
        ),
    ),
    FieldType.BOOL: BOOLS,
    FieldType.DATE: DATES,
}


def roundtrip(target: pathlib.Path, type_: FieldType, value: object) -> object:
    """Write one row, publish, read the cell back, and coerce it to ``type_``.

    Every step is the public one: ``create_writer``, the atomic publish, ``csv.reader``
    opened with ``newline=""`` the way the golden tests do, and ``coerce_value``. A
    round trip that shortcuts any of them would be testing the shortcut.
    """
    config = config_for(type_)
    writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
    writer.write({"value": value})
    writer.close()

    # ``newline=""`` is what tells csv.reader the file's line endings belong to the file,
    # so a value containing "\\r\\n" survives instead of being rewritten to "\\n".
    with target.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle, delimiter=",", quotechar='"'))
    cell = rows[1][0]
    return coerce_value(cell, type_, "value")


@FILE_PROPERTY_SETTINGS
@given(
    st.sampled_from(sorted(BY_TYPE, key=lambda t: t.value)),
    st.data(),
    st.sampled_from([WriterFormat.CSV, WriterFormat.JSONL]),
)
def test_a_supported_scalar_comes_back_as_the_same_value(
    type_: FieldType, data: st.DataObject, fmt: WriterFormat
) -> None:
    """Criterion B's fifth row, over every declared type and both text formats."""
    value = data.draw(BY_TYPE[type_], label=f"{type_.value} value")
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"

        if fmt is WriterFormat.CSV:
            assert roundtrip(target, type_, value) == value, (
                f"{type_.value}: {value!r} came back as something else"
            )
            return

        # JSONL has no header and one object per line, so the same claim by another route.
        config = config_for(type_)
        writer = create_writer(target, config.fields, output_format=fmt)
        writer.write({"value": value})
        writer.close()
        payload = json.loads(target.read_text(encoding="utf-8").strip())
        text = payload["value"] if isinstance(payload["value"], str) else str(payload["value"])
        assert coerce_value(text, type_, "value") == value


@FILE_PROPERTY_SETTINGS
@given(st.sampled_from(sorted(BY_TYPE, key=lambda t: t.value)), st.data())
def test_a_value_the_type_cannot_hold_is_refused_not_silently_changed(
    type_: FieldType, data: st.DataObject
) -> None:
    """★ The refusal half, so the round trip above cannot be satisfied by accepting anything.

    Text that is not a number, in a config that declares a number, must raise
    ``FieldTypeError`` -- never come back as ``0``, never truncate, never be stored as text.
    """
    assume(type_ is not FieldType.STRING)
    text = data.draw(st.sampled_from(["", " ", "abc", "1,5", "1.2.3", "--1", "1e", "0x10", "nan"]))
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(type_)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        writer.write({"value": text})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    accepts_or_refuses(
        lambda: coerce_value(rows[1][0], type_, "value"),
        expected=(FieldTypeError,),
        label=f"coerce_value({rows[1][0]!r}, {type_.value})",
    )


@FILE_PROPERTY_SETTINGS
@given(st.sampled_from(sorted(BY_TYPE, key=lambda t: t.value)))
def test_a_missing_value_is_refused_by_every_type_except_string(type_: FieldType) -> None:
    """★ GAP FOUND BY THIS MILESTONE, pinned and not fixed.

    ``writers.py`` says "``None`` becomes an empty field", which is true and is the whole
    mechanism. **What it does not say is what that costs**, and the cost is a silent one:

    - a missing value and an empty string are **the same two characters in the file**, so a
      STRING column cannot tell them apart afterwards;
    - every other type reads the empty cell back as a **refusal**
      (:class:`FieldTypeError`), never as a zero.

    The second half is the good news and is asserted strictly: a missing ``int`` must not
    come back as ``0``. The first half is the gap -- it cannot be fixed without changing
    the format, so it is recorded here instead, by measurement, with the exact behaviour
    this build has.

    Criterion D: reported, not repaired. Making a missing string distinguishable from an
    empty one is a change to the output format and to every reader of it, which is a
    different milestone with its own review.
    """
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(type_)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        writer.write({"value": None})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))

    assert rows[1] == [""], f"a missing value rendered as {rows[1]!r}, not as an empty cell"

    if type_ is FieldType.STRING:
        # The gap: "" is a legal string, so the missing value and the empty string are
        # indistinguishable here, and this is the behaviour being recorded rather than fixed.
        assert coerce_value("", FieldType.STRING, "value") == ""
        assert coerce_value("", FieldType.STRING, "value") == coerce_value(
            rows[1][0], FieldType.STRING, "value"
        ), "the recorded gap is that these two are equal; if that changed, close the entry"
        return

    try:
        coerce_value(rows[1][0], type_, "value")
    except FieldTypeError:
        return
    raise AssertionError(
        f"a missing {type_.value} came back as a value instead of being refused -- that is "
        "the silent zero this property exists to prevent"
    )


@FILE_PROPERTY_SETTINGS
@given(st.data())
def test_a_missing_value_never_leaks_a_foreign_error(data: st.DataObject) -> None:
    """The invariant form of the property above, over the whole type set."""
    type_ = data.draw(st.sampled_from(sorted(BY_TYPE, key=lambda t: t.value)), label="type")
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(type_)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        writer.write({"value": None})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    accepts_or_refuses(
        lambda: coerce_value(rows[1][0], type_, "value"),
        expected=WRITER_ERRORS,
        label=f"coerce_value(<empty cell>, {type_.value})",
    )


@FILE_PROPERTY_SETTINGS
@given(st.lists(NASTY_STRINGS, min_size=1, max_size=6))
def test_nasty_strings_survive_a_csv_round_trip_unchanged(values: list[str]) -> None:
    """★ Quoting, commas, newlines and NULs, in combination rather than one at a time.

    One nasty value at a time is what a hand-written test does. The combinations are
    where a quoting bug shows up: a comma next to a quote, a CRLF next to a trailing
    newline, a value that is itself the quote character.
    """
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(FieldType.STRING)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        for value in values:
            writer.write({"value": value})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    assert rows[0] == ["value"], f"the header changed: {rows[0]!r}"
    assert rows[1:] == [[value] for value in values], (
        f"quoting changed the data:\n  wrote  {values!r}\n  read   {rows[1:]!r}"
    )


@FILE_PROPERTY_SETTINGS
@given(
    st.lists(
        st.sampled_from([0.1, 1.5, -0.25, 1e-10, 1e10, 123456789.123456789]), min_size=1, max_size=8
    )
)
def test_floats_keep_their_exact_value_through_a_float_column(values: list[float]) -> None:
    """★ Precision, which is the reason ``decimal`` exists and the reason float must not drift.

    ``repr`` is exact for a Python float, so ``float(repr(x)) == x`` always holds -- and the
    risk is that the *file* takes a different route. This asserts the value after it has
    been through the writer, the file, the reader and the coercion.
    """
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(FieldType.FLOAT)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        for value in values:
            writer.write({"value": value})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    read_back = [coerce_value(row[0], FieldType.FLOAT, "value") for row in rows[1:]]
    assert read_back == values, f"floats drifted:\n  wrote {values!r}\n  read  {read_back!r}"


@FILE_PROPERTY_SETTINGS
@given(
    st.lists(
        st.sampled_from([decimal.Decimal("0"), decimal.Decimal("1.50"), decimal.Decimal("-0.001")]),
        min_size=1,
        max_size=6,
    )
)
def test_decimals_keep_their_scale_through_a_decimal_column(
    values: list[decimal.Decimal],
) -> None:
    """``Decimal("49.90")`` is not ``Decimal("49.9")``, and ``str`` is what preserves that.

    Trailing zeros are the whole reason the ``decimal`` field type exists; a conversion
    that normalised them would make a money column that adds up wrongly.
    """
    with tempfile.TemporaryDirectory() as raw:
        target = pathlib.Path(raw) / "out.csv"
        config = config_for(FieldType.DECIMAL)
        writer = create_writer(target, config.fields, output_format=WriterFormat.CSV)
        for value in values:
            writer.write({"value": value})
        writer.close()
        with target.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
    read_back = [coerce_value(row[0], FieldType.DECIMAL, "value") for row in rows[1:]]
    assert read_back == values, f"decimal scale drifted:\n  wrote {values!r}\n  read  {read_back!r}"
    for original, got in zip(values, read_back, strict=True):
        assert str(original) == str(got), f"{original!r} lost its scale: {got!r}"


def _csv_reader_is_the_golden_one() -> None:
    """A guard on the reader itself: the round trip above is only as good as this.

    ``csv.reader`` opened without ``newline=""`` rewrites ``\\r\\n`` inside a quoted cell to
    ``\\n``, which would make every string property in this file quietly weaker. Asserting
    it once here means a future change to how these tests read files cannot pass by
    weakening all of them at the same time.
    """
    handle = io.StringIO('"a\r\nb",c\r\n', newline="")
    rows = list(csv.reader(handle))
    assert rows == [["a\r\nb", "c"]], f"the reader is not preserving line endings: {rows!r}"


def test_the_csv_reader_preserves_line_endings_inside_a_quoted_cell() -> None:
    _csv_reader_is_the_golden_one()
