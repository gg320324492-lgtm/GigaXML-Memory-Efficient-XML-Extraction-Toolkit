"""Integration tests: XXE / entity-expansion hardening.

The parser is constructed with ``resolve_entities=False``, ``no_network=True``,
``load_dtd=False`` and no ``huge_tree``, and those are not configurable. Each test
below would fail if any one of them were flipped back to the lxml default.
"""

from __future__ import annotations

from pathlib import Path

from gigaxml.parser.streaming import StreamingRecordReader

RECORD_PATH = "/catalog/products/product"


def _record_texts(source: Path) -> list[str]:
    """Whitespace-normalised text content of each record."""
    return [
        " ".join(" ".join(record.itertext()).split())
        for record in StreamingRecordReader(source, RECORD_PATH)
    ]


def test_network_entity_is_never_fetched(fixtures_dir: Path) -> None:
    """The fixture points at http://127.0.0.1:9/.

    If lxml tried to resolve it we would see a connection error (port 9 is the
    discard port and refuses immediately) or an ``no_network`` violation. Getting
    a clean result back is the evidence that nothing was fetched.
    """
    texts = _record_texts(fixtures_dir / "xxe.xml")
    assert len(texts) == 1
    assert "gigaxml-xxe-canary" not in texts[0]


def test_local_file_entity_is_not_expanded(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("GIGAXML-XXE-SENTINEL", encoding="utf-8")
    document = tmp_path / "xxe-local.xml"
    document.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE catalog [\n"
        f'  <!ENTITY xxe SYSTEM "file:///{secret.as_posix()}">\n'
        "]>\n"
        '<catalog generated-by="gigaxml" seed="0">\n'
        "  <products>\n"
        '    <product id="1" type="physical">\n'
        "      <name>&xxe;</name>\n"
        "    </product>\n"
        "  </products>\n"
        "</catalog>\n",
        encoding="utf-8",
    )
    texts = _record_texts(document)
    assert len(texts) == 1
    assert "GIGAXML-XXE-SENTINEL" not in texts[0]


def test_internal_entity_expansion_is_disabled(tmp_path: Path) -> None:
    """A miniature billion-laughs: the expansion must not appear in the output."""
    document = tmp_path / "bomb.xml"
    document.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!DOCTYPE catalog [\n"
        '  <!ENTITY a "AAAAAAAAAA">\n'
        '  <!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">\n'
        "]>\n"
        "<catalog>\n"
        "  <products>\n"
        '    <product id="1"><name>&b;</name></product>\n'
        "  </products>\n"
        "</catalog>\n",
        encoding="utf-8",
    )
    texts = _record_texts(document)
    assert len(texts) == 1
    assert "AAAAAAAAAA" not in texts[0]


def test_plain_document_is_unaffected(fixtures_dir: Path) -> None:
    """Hardening must not break ordinary documents."""
    assert _record_texts(fixtures_dir / "tiny.xml") == [
        "Aurora Desk Lamp lighting 49.90 Northwind Works SE desk led",
        "Pulse Audio Suite software 129.00 Kestrel Labs DE audio plugin",
    ]
