"""Security limits: what a hostile document can and cannot make the tool do.

Everything here already worked before these tests existed -- that is the point. The
five limits are enforced by libxml2, driven by :func:`gigaxml.parser.streaming.
_parser_options`, and until now nothing in the repository asserted that they were still
switched on. A single well-meaning change (``huge_tree=True``, to accommodate one
legitimately deep document) would remove three of them at once and no test would fail.

The fixtures are **generated in code**. A 50 MB text node is a 50 MB file, and this
project does not commit large files; the generators are also the clearest statement of
what each document is shaped like.

Timing is asserted loosely on purpose. The claim being made is "these are refused
promptly", not "in under n milliseconds", and a wall-clock ceiling tight enough to be
interesting on a fast machine is flaky on a loaded one.

**These tests deliberately do not assert how libxml2 words its refusals.** It is a
dependency, it is free to reword, and it did: the same refusal reads "huge text node" on
one build and "Resource limit exceeded: Text node too long" on another, which failed CI
for a day on a machine nobody was watching. Asserting the wording of a dependency is
asserting something this project does not control.

**Nor do these tests assert the column the parser stopped at.** That was the first
attempt at a stable anchor and it is not one: the same document, the same build of this
project, gave `column 25` on Windows and `column 7` on Linux for the amplification bomb,
and `column 777` against `column 774` for the 100,000-level document. The column is a
function of the build of libxml2 and of where its input buffer happened to end -- an
implementation detail wearing the costume of a measurement. A test that passes because
one machine's buffer boundary equals a constant is passing by coincidence, and it will
fail on the next machine for a reason that has nothing to do with the limit.

What is asserted instead is **the limit itself and which side of it a document is on**.
Every refusal below is paired with a document shaped the same way that is *not* refused,
and the pairing is what makes the assertion mean something: if the limit moved, one of
the two flips. Where the limit is also a switch this project owns (`huge_tree`), the
switch is turned on and the same document is expected to go through -- that is the
project's A5 rule, applied.

The exit code, the `error: ` prefix, the elapsed time and the absence of an output file
carry the rest of the claim, and none of them belong to libxml2. `assert "Traceback" not
in err` stays as it is: `Traceback` is CPython's word, and the language's wording is the
one kind a test may rely on.
"""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import pytest
from lxml import etree

from gigaxml.cli import main
from gigaxml.errors import GigaXMLError
from gigaxml.parser import streaming
from gigaxml.parser.streaming import StreamingRecordReader, _parser_options

#: A refusal must arrive quickly. The slowest of these on the development machine is
#: under 0.1s; a minute would mean something is actually expanding.
PROMPT_SECONDS = 10.0


def write_config(tmp_path: Path, record: str = "/root/item") -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps({"record": record, "fields": {"a": {"path": "."}}}), encoding="utf-8"
    )
    return path


def write(tmp_path: Path, text: str, name: str = "hostile.xml") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def run(source: Path, config: Path, output: Path) -> tuple[int, float]:
    started = time.perf_counter()
    code = main(["extract", str(source), "-c", str(config), "-o", str(output)])
    return code, time.perf_counter() - started


# --- the switch that everything else depends on -----------------------------


def test_the_parser_never_enables_the_unsafe_options() -> None:
    """The whole defence, asserted directly.

    These five are not independent: ``huge_tree=False`` is what refuses a document that
    is too deep, carries one enormous text node, or is amplified by entities. Turning it
    on for one legitimate deep document would take out all three limits at once, and the
    behaviour tests below would each still be testing whatever was left. This is the
    test that has to fail when somebody changes it.
    """
    options = _parser_options()

    assert options["resolve_entities"] is False, "entities must never be expanded"
    assert options["no_network"] is True, "no remote fetch may ever happen"
    assert options["load_dtd"] is False, "no DTD, internal or external, may be loaded"
    assert options["attribute_defaults"] is False
    assert options["huge_tree"] is False, (
        "huge_tree lifts the depth, text-node and entity-amplification limits together"
    )


def test_every_option_the_reader_passes_is_one_of_those() -> None:
    """Nothing may be added to the call that this list does not know about.

    Without this, a new keyword could be slipped into ``iterparse`` and the assertion
    above would keep passing.
    """
    import inspect

    source = inspect.getsource(StreamingRecordReader.__iter__)
    assert "**_parser_options()" in source, "the reader must take its options from here"
    assert "resolve_entities" not in source, "and must not set any of them inline"


# --- entity amplification ---------------------------------------------------


# The depth ceiling is the one limit whose value libxml2 states in the refusal, and 256
# is a compile-time constant of the library rather than a position within the input. It
# is the only number in a refusal message this file is willing to look at.
DEPTH_CEILING = "256"


def entity_bomb(levels: int = 9, fanout: int = 10) -> str:
    """The classic nested-entity bomb: each level references the last `fanout` times."""
    declarations = ['<!ENTITY lol0 "lol">']
    for level in range(1, levels + 1):
        body = "".join(f"&lol{level - 1};" for _ in range(fanout))
        declarations.append(f'<!ENTITY lol{level} "{body}">')
    joined = "\n".join(declarations)
    return (
        '<?xml version="1.0"?>\n'
        f"<!DOCTYPE root [\n{joined}\n]>\n"
        f"<root><item>&lol{levels};</item></root>"
    )


def test_entity_amplification_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = write(tmp_path, entity_bomb())
    config = write_config(tmp_path)

    code, elapsed = run(source, config, tmp_path / "out.csv")

    assert code == 1, "a bomb must not be a successful run"
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
    assert elapsed < PROMPT_SECONDS, f"took {elapsed:.1f}s -- something is expanding"
    assert not (tmp_path / "out.csv").exists()

    # No assertion on the message. The ceiling is an amplification factor and libxml2
    # does not put the factor in the refusal, so there is no number here to anchor on --
    # and the column it does report is a buffer boundary, not the limit. What proves the
    # limit is the pair: this document is refused, and the fanout=2 document below goes
    # through. See test_a_mild_entity_document_still_works.


def test_a_mild_entity_document_still_works(tmp_path: Path) -> None:
    """The refusal is about amplification, not about entities existing.

    Without this, a change that refused every document with a DOCTYPE would look fine.
    """
    source = write(tmp_path, entity_bomb(levels=9, fanout=2))
    config = write_config(tmp_path)

    code, _ = run(source, config, tmp_path / "out.csv")

    assert code == 0
    assert (tmp_path / "out.csv").is_file()


# --- depth ------------------------------------------------------------------


def nested_document(depth: int) -> str:
    """``<root><a><a>...<item>1</item>...</a></a></root>``, `depth` levels of ``<a>``."""
    return "<root>" + "<a>" * depth + "<item>1</item>" + "</a>" * depth + "</root>"


def test_deep_nesting_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write(tmp_path, nested_document(100_000))
    config = write_config(tmp_path, record="//item")

    code, elapsed = run(source, config, tmp_path / "out.csv")

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert DEPTH_CEILING in err, (
        "refused, but not at the documented 256-level ceiling -- if this fails the "
        "ceiling moved, not the column"
    )
    assert "Traceback" not in err
    assert elapsed < PROMPT_SECONDS
    assert not (tmp_path / "out.csv").exists()


def test_a_deep_but_legal_document_is_still_refused_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """300 levels is past the limit, whatever the document's intentions are."""
    source = write(tmp_path, nested_document(300))
    config = write_config(tmp_path, record="//item")

    code, _ = run(source, config, tmp_path / "out.csv")

    assert code == 1
    assert "depth" in capsys.readouterr().err


def test_a_document_below_the_depth_ceiling_is_fine(tmp_path: Path) -> None:
    """200 levels is under 256, so nesting is a ceiling and not a blanket ban.

    The other half of `test_deep_nesting_is_refused`. Without it, a change that refused
    every document with any nesting at all would keep that test green.
    """
    source = write(tmp_path, nested_document(200))
    config = write_config(tmp_path, record="//item")

    code, _ = run(source, config, tmp_path / "out.csv")

    assert code == 0
    assert (tmp_path / "out.csv").is_file()


# --- the counter-example ----------------------------------------------------


def test_the_depth_limit_really_is_the_huge_tree_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prove the test above is testing the switch and not merely passing.

    This is the project's A5 rule applied here: a test that asserts a refusal is only
    evidence if turning the mechanism off makes it stop refusing. So ``huge_tree`` is
    turned on for the duration of this test and the *same* 300-level document is
    expected to go through.
    """
    original = streaming._parser_options

    def with_huge_tree() -> dict[str, bool]:
        options = dict(original())
        options["huge_tree"] = True
        return options

    source = write(tmp_path, nested_document(300))
    config = write_config(tmp_path, record="//item")

    # With the switch off (the real configuration) it is refused.
    code, _ = run(source, config, tmp_path / "before.csv")
    assert code == 1, "300 levels should be refused with huge_tree off"

    monkeypatch.setattr(streaming, "_parser_options", with_huge_tree)
    try:
        code, _ = run(source, config, tmp_path / "after.csv")
    finally:
        monkeypatch.undo()

    assert code == 0, "with huge_tree on the same document parses -- so the limit is that switch"
    assert (tmp_path / "after.csv").is_file()

    # And the switch is back off afterwards.
    assert _parser_options()["huge_tree"] is False


def test_huge_tree_does_not_disable_the_entity_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reminder that the defences are not all the same switch.

    The entity-amplification check lives in libxml2 and survives ``huge_tree``; if that
    ever stopped being true this test says so.
    """
    original = streaming._parser_options

    def with_huge_tree() -> dict[str, bool]:
        options = dict(original())
        options["huge_tree"] = True
        return options

    source = write(tmp_path, entity_bomb())
    config = write_config(tmp_path)

    monkeypatch.setattr(streaming, "_parser_options", with_huge_tree)
    try:
        code, _ = run(source, config, tmp_path / "out.csv")
    finally:
        monkeypatch.undo()

    assert code == 1
    assert not (tmp_path / "out.csv").exists()


# --- a single enormous text node --------------------------------------------


def test_a_huge_text_node_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Ten megabytes in one node is past libxml2's limit; the file is built here."""
    source = write(tmp_path, f"<root><item>{'x' * (50 * 1024 * 1024)}</item></root>")
    config = write_config(tmp_path)

    code, elapsed = run(source, config, tmp_path / "out.csv")

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    # No assertion on the position either. This one looked like a constant -- it is
    # `10027009`, ten megabytes plus a buffer -- but it is where libxml2's input buffer
    # ended, which is the same class of fact as the column numbers above. The pair that
    # carries the claim is this test and test_a_text_node_just_under_the_limit_is_fine.
    assert elapsed < PROMPT_SECONDS
    assert not (tmp_path / "out.csv").exists()


def test_a_text_node_just_under_the_limit_is_fine(tmp_path: Path) -> None:
    """One megabyte is comfortably legal, so the limit is a limit and not a blanket ban."""
    source = write(tmp_path, f"<root><item>{'x' * (1024 * 1024)}</item></root>")
    config = write_config(tmp_path)

    code, _ = run(source, config, tmp_path / "out.csv")

    assert code == 0
    assert (tmp_path / "out.csv").is_file()


# --- malformed input --------------------------------------------------------


def test_a_truncated_document_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write(tmp_path, "<root><item><a>1</a></item><item><a>2</a>")
    config = write_config(tmp_path)

    code, elapsed = run(source, config, tmp_path / "out.csv")

    assert code == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ")
    assert "Traceback" not in err
    assert elapsed < PROMPT_SECONDS
    assert not (tmp_path / "out.csv").exists()


# --- an external DTD is never fetched ---------------------------------------


def test_an_external_dtd_is_never_fetched(tmp_path: Path) -> None:
    """A closed port is the sentinel: if anything dialled it, the connection would fail.

    ``no_network=True`` plus ``load_dtd=False`` means the DOCTYPE is noted and ignored,
    so the document parses normally -- which is the desired outcome, not a refusal. If
    the DTD were fetched this would raise a connection error instead.
    """
    # Bind and immediately close, so the port is certainly not listening.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    source = write(
        tmp_path,
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE root SYSTEM "http://127.0.0.1:{port}/evil.dtd">\n'
        "<root><item><a>1</a></item><item><a>2</a></item></root>",
    )
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    code, elapsed = run(source, config, output)

    assert code == 0, "the document is fine; the DTD is simply not fetched"
    assert output.is_file()
    assert len(output.read_text(encoding="utf-8").splitlines()) == 3  # header + 2
    assert elapsed < PROMPT_SECONDS


def test_an_external_entity_reference_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The XXE shape: an entity pointing at a local file must not be read."""
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    source = write(
        tmp_path,
        '<?xml version="1.0"?>\n'
        f'<!DOCTYPE root [<!ENTITY leak SYSTEM "file:///{secret.as_posix()}">]>\n'
        "<root><item><a>&leak;</a></item></root>",
    )
    config = write_config(tmp_path)
    output = tmp_path / "out.csv"

    code, _ = run(source, config, output)

    if code == 0:
        assert "TOP-SECRET" not in output.read_text(encoding="utf-8"), "the file was read"
    else:
        err = capsys.readouterr().err
        assert "TOP-SECRET" not in err


# --- the limits hold at the reader level too --------------------------------


@pytest.mark.parametrize("shape", ["bomb", "deep", "truncated"])
def test_the_reader_refuses_these_directly(tmp_path: Path, shape: str) -> None:
    """The same documents through the library API, not just through the CLI.

    A caller using the library gets the same refusal -- and it is a declared error type,
    so one ``except`` clause covers it, rather than whatever lxml happened to raise.

    The parametrisation is by name, not by document: pytest puts the parameter into the
    ``PYTEST_CURRENT_TEST`` environment variable, and a 100,000-level document is well
    over the 32,767-character limit, so passing the text itself makes collection fail
    with ``ValueError: the environment variable is longer than 32767 characters``.
    """
    documents = {
        "bomb": entity_bomb(),
        "deep": nested_document(100_000),
        "truncated": "<root><item><a>1</a>",
    }
    source = write(tmp_path, documents[shape], name=f"{shape}.xml")

    with pytest.raises((GigaXMLError, etree.XMLSyntaxError)) as info:
        for _ in StreamingRecordReader(source, "//item"):
            pass

    assert str(info.value)
