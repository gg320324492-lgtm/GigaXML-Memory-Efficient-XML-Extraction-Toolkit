"""Reading what ``sample`` wrote.

The formats are exercised from files written by hand rather than by running the CLI: what
is being tested here is the reading, and a test that ran ``sample`` to check a CSV parser
would fail for the wrong reason when the CLI's summary changed.
"""

from __future__ import annotations

import json
import pathlib

from gigaxml.gui.sampling import (
    discard_run_directory,
    generate_config_args,
    make_run_directory,
    read_rejected,
    read_table,
    sample_args,
    table_from,
)


def write(path: pathlib.Path, text: str) -> pathlib.Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- the command lines --------------------------------------------------------


def test_the_sample_command_line_carries_the_limit_and_the_output() -> None:
    """``-n`` and ``-o`` are both required, and both come from the caller."""
    args = sample_args("doc.xml", "cfg.yaml", 7, "out.csv")

    assert args[0] == "sample"
    assert args[args.index("-n") + 1] == "7"
    assert args[args.index("-o") + 1] == "out.csv"
    assert args[args.index("-c") + 1] == "cfg.yaml"


def test_the_generate_config_command_line_uses_a_one_based_candidate() -> None:
    """Matching what ``inspect`` prints and what the candidate table shows."""
    args = generate_config_args("doc.xml", "cfg.yaml", 3)

    assert args[0] == "inspect"
    assert args[args.index("--candidate") + 1] == "3"
    assert args[args.index("--generate-config") + 1] == "cfg.yaml"


def test_each_run_gets_its_own_directory() -> None:
    """Reused, a previous run's rejected.jsonl would be read as if it were this one's."""
    first = make_run_directory("gigaxml-probe-")
    second = make_run_directory("gigaxml-probe-")

    assert first != second
    assert first.is_dir() and second.is_dir()
    discard_run_directory(first)
    discard_run_directory(second)


# --- cleaning up after a run --------------------------------------------------


def test_a_run_directory_can_be_discarded() -> None:
    directory = make_run_directory("gigaxml-probe-")

    assert discard_run_directory(directory) is True

    assert not directory.exists()


def test_discarding_refuses_a_directory_that_is_not_ours(tmp_path: pathlib.Path) -> None:
    """**The guard is the point.** This runs unattended, once per preview, and a cleanup
    helper that deletes whatever it is handed is one bad argument away from deleting
    something that was never its to delete."""
    foreign = tmp_path / "important"
    foreign.mkdir()
    (foreign / "data.txt").write_text("keep me", encoding="utf-8")

    assert discard_run_directory(foreign) is False
    assert (foreign / "data.txt").is_file()


def test_discarding_nothing_is_harmless() -> None:
    assert discard_run_directory(None) is False


def test_discarding_a_path_that_is_not_there_is_harmless(tmp_path: pathlib.Path) -> None:
    assert discard_run_directory(tmp_path / "gigaxml-not-created") is False


def test_discarding_a_file_that_looks_like_one_of_ours_is_refused(
    tmp_path: pathlib.Path,
) -> None:
    """Only directories are removed, even when the name matches."""
    impostor = tmp_path / "gigaxml-preview-not-a-directory"
    impostor.write_text("not a directory", encoding="utf-8")

    assert discard_run_directory(impostor) is False
    assert impostor.is_file()


# --- reading the table --------------------------------------------------------


def test_a_csv_sample_reads_back_as_headers_and_rows(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.csv", "id,name\n1,Alpha Lamp\n2,Beta Suite\n")

    headers, rows = read_table(source)

    assert headers == ("id", "name")
    assert rows == (("1", "Alpha Lamp"), ("2", "Beta Suite"))


def test_a_csv_sample_with_a_comma_in_a_value_keeps_its_columns(
    tmp_path: pathlib.Path,
) -> None:
    source = write(tmp_path / "s.csv", 'id,name\n1,"Lamp, desk"\n')

    headers, rows = read_table(source)

    assert headers == ("id", "name")
    assert rows == (("1", "Lamp, desk"),)


def test_a_jsonl_sample_reads_back_with_the_headers_in_first_seen_order(
    tmp_path: pathlib.Path,
) -> None:
    source = write(tmp_path / "s.jsonl", '{"id": 1, "name": "A"}\n{"id": 2, "name": "B"}\n')

    headers, rows = read_table(source)

    assert headers == ("id", "name")
    assert rows == (("1", "A"), ("2", "B"))


def test_a_jsonl_sample_that_omits_a_key_shows_it_as_empty(
    tmp_path: pathlib.Path,
) -> None:
    source = write(tmp_path / "s.jsonl", '{"id": 1, "name": "A"}\n{"id": 2}\n')

    headers, rows = read_table(source)

    assert headers == ("id", "name")
    assert rows[1] == ("2", "")


def test_a_damaged_jsonl_line_is_skipped_not_fatal(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.jsonl", '{"id": 1}\nnot json at all\n{"id": 2}\n')

    headers, rows = read_table(source)

    assert headers == ("id",)
    assert rows == (("1",), ("2",))


def test_a_jsonl_line_that_is_not_an_object_is_skipped(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.jsonl", '{"id": 1}\n[1, 2]\n"a string"\n{"id": 2}\n')

    headers, rows = read_table(source)

    assert headers == ("id",)
    assert rows == (("1",), ("2",))


def test_blank_lines_in_a_jsonl_sample_are_ignored(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.jsonl", '\n{"id": 1}\n\n   \n')

    headers, rows = read_table(source)

    assert headers == ("id",)
    assert rows == (("1",),)


def test_a_table_that_is_not_there_reads_as_nothing(tmp_path: pathlib.Path) -> None:
    assert read_table(tmp_path / "nope.csv") == ((), ())


def test_an_empty_file_reads_as_nothing(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.csv", "")

    assert read_table(source) == ((), ())


def test_a_header_with_no_rows_reads_as_headers_alone(tmp_path: pathlib.Path) -> None:
    source = write(tmp_path / "s.csv", "id,name\n")

    headers, rows = read_table(source)

    assert headers == ("id", "name")
    assert rows == ()


# --- reading the rejections ---------------------------------------------------


def test_rejected_records_are_read_as_written(tmp_path: pathlib.Path) -> None:
    entry = {
        "index": 3,
        "record_path": "/catalog/products/product",
        "error": "FieldTypeError",
        "message": "not convertible",
        "field": "qty",
        "raw": "oops",
    }
    source = write(tmp_path / "rejected.jsonl", json.dumps(entry) + "\n")

    entries = read_rejected(source)

    assert len(entries) == 1
    assert entries[0]["field"] == "qty"
    assert entries[0]["raw"] == "oops"
    assert entries[0]["index"] == "3", "every cell is text; the panel shows what it says"


def test_no_rejected_path_reads_as_no_rejections() -> None:
    assert read_rejected(None) == ()


def test_a_rejected_file_that_is_not_there_reads_as_no_rejections(
    tmp_path: pathlib.Path,
) -> None:
    assert read_rejected(tmp_path / "rejected.jsonl") == ()


def test_a_damaged_rejected_line_is_skipped_not_fatal(tmp_path: pathlib.Path) -> None:
    source = write(
        tmp_path / "rejected.jsonl",
        '{"index": 1, "field": "a"}\nnot json at all\n{"index": 2, "field": "b"}\n',
    )

    entries = read_rejected(source)

    assert [entry["field"] for entry in entries] == ["a", "b"]


# --- the whole picture --------------------------------------------------------


def test_the_counts_come_from_the_summary_not_from_counting_rows(
    tmp_path: pathlib.Path,
) -> None:
    """If the file and the summary disagree, the disagreement is worth seeing."""
    source = write(tmp_path / "s.csv", "id\n1\n2\n")
    summary = {"requested": 5, "written": 2, "rejected": 1, "short_of_request": True}

    table = table_from(summary, source)

    assert table.row_count == 2
    assert table.requested == 5
    assert table.written == 2
    assert table.rejected == 1
    assert table.short_of_request is True


def test_a_summary_without_a_rejected_path_leaves_it_unset(tmp_path: pathlib.Path) -> None:
    table = table_from({}, write(tmp_path / "s.csv", "id\n1\n"))

    assert table.rejected_path is None
    assert table.rejected == 0


def test_a_missing_summary_still_reads_the_file(tmp_path: pathlib.Path) -> None:
    """The rows are the point; a summary that did not arrive should not hide them."""
    table = table_from(None, write(tmp_path / "s.csv", "id\n1\n"))

    assert table.headers == ("id",)
    assert table.row_count == 1


def test_the_summary_is_kept_in_full(tmp_path: pathlib.Path) -> None:
    summary = {"requested": 1, "note": "something the panel does not show"}

    table = table_from(summary, write(tmp_path / "s.csv", "id\n1\n"))

    assert table.summary["note"] == "something the panel does not show"
