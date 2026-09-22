"""The advice a failure gets, and the reading it is based on.

Qt-free, and that is deliberate: which advice a given ``error.type`` gets is the part that
would silently rot, and it can be pinned without a window.
"""

from __future__ import annotations

import json
import pathlib

from gigaxml.errors import (
    ConfigError,
    FieldPathError,
    FieldTypeError,
    MissingRequiredFieldError,
    RecordPathError,
    WriterError,
)
from gigaxml.gui.error_advice import (
    KIND_CHECK_CONFIG,
    KIND_FREE_TARGET,
    KIND_NAMESPACES,
    KIND_QUARANTINE,
    advice_for,
)
from gigaxml.gui.run_report import failure_from_stderr, read_failure, report_path_for

# --- advice -------------------------------------------------------------------


def test_the_types_the_extractor_calls_quarantinable_get_the_quarantine_advice() -> None:
    """Derived from the extractor's own list rather than typed out again.

    ``FieldTypeError`` and ``MissingRequiredFieldError`` are what ``QUARANTINABLE`` holds,
    so both get it -- and if the extractor decides another error is skippable, this follows
    without anybody remembering to come back here.
    """
    for cls in (FieldTypeError, MissingRequiredFieldError):
        advice = advice_for(cls.__name__)
        assert advice.kind == KIND_QUARANTINE, cls.__name__


def test_a_writer_error_gets_the_free_the_target_advice() -> None:
    advice = advice_for(WriterError.__name__)
    assert advice.kind == KIND_FREE_TARGET
    assert ".tmp" in advice.detail, "the partial output is the thing to point at"


def test_a_field_path_error_gets_the_namespace_advice() -> None:
    """A path naming a prefix the map does not declare.

    **This is the one that does not reach the CLI at all.** The loader raises it in this
    process, so there is no run report -- the kind has to be carried alongside the message
    by whoever caught it, which is what ``start_failed``'s second argument is for.
    """
    advice = advice_for(FieldPathError.__name__)
    assert advice.kind == KIND_NAMESPACES
    assert "namespace" in advice.detail.lower()


def test_a_record_path_error_is_not_mistaken_for_a_namespace_one() -> None:
    """Close enough to be worth pinning apart: both mean "the config does not describe
    this document", but only one of them is about prefixes."""
    assert advice_for(RecordPathError.__name__).kind == KIND_CHECK_CONFIG


def test_no_type_at_all_gets_the_config_advice() -> None:
    """The config that will not load: no report, so nothing to classify."""
    assert advice_for(None).kind == KIND_CHECK_CONFIG


def test_a_type_this_build_does_not_know_gets_the_config_advice() -> None:
    """**Not an error.** An unknown type is the same situation as no type: there is nothing
    to dispatch on, and guessing from the message is what this module refuses to do."""
    assert advice_for("SomeErrorFromAFutureVersion").kind == KIND_CHECK_CONFIG


def test_the_advice_never_mentions_the_message() -> None:
    """The structural half of "dispatch on the type, not the wording".

    ``advice_for`` takes the type and nothing else, so no message can reach it. That is
    worth stating as a test because the tempting fix -- "also check whether the message
    says namespace" -- would need a second parameter, and this would have to be deleted.
    """
    import inspect

    parameters = list(inspect.signature(advice_for).parameters)
    assert parameters == ["error_type"]


def test_every_advice_says_what_to_do_not_just_what_happened() -> None:
    for error_type in (FieldTypeError.__name__, WriterError.__name__, None):
        advice = advice_for(error_type)
        assert advice.headline and advice.detail
        assert len(advice.detail) > 40, "a headline with no detail is not actionable"


# --- where the report is ------------------------------------------------------


def test_the_report_sits_beside_a_file_output(tmp_path: pathlib.Path) -> None:
    assert report_path_for(tmp_path / "out.csv") == tmp_path / "run-report.json"


def test_the_report_sits_inside_a_checkpointed_output_directory(tmp_path: pathlib.Path) -> None:
    """``--output`` names the parts directory in that mode, so the summary goes in it."""
    assert report_path_for(tmp_path / "parts", checkpointing=True) == (
        tmp_path / "parts" / "run-report.json"
    )


# --- reading it ---------------------------------------------------------------


def write_report(tmp_path: pathlib.Path, payload: object) -> pathlib.Path:
    path = tmp_path / "run-report.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_report_with_an_error_reads_back(tmp_path: pathlib.Path) -> None:
    write_report(
        tmp_path,
        {
            "status": "failed",
            "error": {"type": "FieldTypeError", "message": "field 'name' is not an int"},
            "output_complete": False,
            "partial_path": str(tmp_path / "out.csv.tmp"),
        },
    )

    failure = read_failure(tmp_path / "out.csv")

    assert failure is not None
    assert failure.error_type == "FieldTypeError"
    assert failure.message == "field 'name' is not an int"
    assert failure.output_complete is False
    assert failure.partial_path == tmp_path / "out.csv.tmp"
    assert failure.had_report is True


def test_a_report_with_no_error_reads_as_no_failure(tmp_path: pathlib.Path) -> None:
    write_report(tmp_path, {"status": "ok", "error": None, "output_complete": True})

    assert read_failure(tmp_path / "out.csv") is None


def test_no_report_reads_as_no_failure(tmp_path: pathlib.Path) -> None:
    """The caller decides what that means; this function does not invent a failure."""
    assert read_failure(tmp_path / "out.csv") is None


def test_a_report_that_is_not_json_does_not_raise(tmp_path: pathlib.Path) -> None:
    (tmp_path / "run-report.json").write_text("not json at all", encoding="utf-8")

    assert read_failure(tmp_path / "out.csv") is None


def test_an_error_without_a_type_still_reads(tmp_path: pathlib.Path) -> None:
    """A future version that writes a bare message should still be shown, not swallowed."""
    write_report(tmp_path, {"error": {"message": "something went wrong"}, "output_complete": False})

    failure = read_failure(tmp_path / "out.csv")

    assert failure is not None
    assert failure.error_type is None
    assert failure.message == "something went wrong"
    assert failure.had_report is True


# --- when there is no report at all -------------------------------------------


def test_a_failure_with_no_report_keeps_the_type_empty() -> None:
    """**No type is invented.** The message is prose; classifying it would be a guess that
    looks like knowledge, and it would break the first time the CLI reworded it."""
    failure = failure_from_stderr(
        ["error: config file 'x.yaml' is not valid YAML: mapping values are not allowed here"],
        1,
    )

    assert failure.error_type is None
    assert failure.had_report is False
    assert "not valid YAML" in failure.message


def test_a_type_the_caller_knows_is_carried_through() -> None:
    """The GUI catches the loader in-process, so it has the class even with no report."""
    failure = failure_from_stderr(
        ["field path segment 'zz:x' uses namespace prefix 'zz', which is not present"],
        1,
        error_type=FieldPathError.__name__,
    )

    assert failure.error_type == FieldPathError.__name__
    assert failure.had_report is False


def test_config_error_and_field_path_error_are_not_the_same_parent() -> None:
    """**Why the panel catches ``GigaXMLError`` and not ``ConfigError``.**

    The loader raises both, and they are siblings rather than parent and child -- so a
    handler written for ``ConfigError`` lets the other one through. This is the fact the
    crash rested on, and it is a property of the project's own error classes, so it is
    worth asserting here rather than only in the panel's test.
    """
    assert issubclass(ConfigError, Exception)
    assert not issubclass(FieldPathError, ConfigError)
    assert not issubclass(RecordPathError, ConfigError)


def test_a_failure_with_no_report_and_no_output_says_something() -> None:
    failure = failure_from_stderr([], 2)

    assert failure.message, "a blank message is worse than none"
    assert "2" in failure.message
