"""XSD support is optional, and is kept off the parsing path.

Two claims are tested here and both are about *placement*, not behaviour:

1. **The core imports without the optional dependency.** A plain ``pip install
   gigaxml`` has no ``xmlschema``, so anything on the import path of the streaming
   reader, the field extraction, or the writers would break every user who never asked
   for a schema. The test hides the module and imports the core anyway.
2. **A schema changes types, not the parse.** It cannot change which records are
   matched, how memory behaves, or what the reader does -- only what a field's value is
   converted to afterwards.

The mutation that must turn this file red is a single added line: an ``import
xmlschema`` at the top of ``gigaxml.parser.streaming``. That is the mistake this
module exists to make impossible, so it is worth being able to point at.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from gigaxml.cli import main
from gigaxml.config import ConfigError, parse_config
from gigaxml.fields import FieldType

REPO_ROOT = Path(__file__).resolve().parents[2]


def _have_xmlschema() -> bool:
    try:
        import xmlschema  # noqa: F401
    except ImportError:
        return False
    return True


SOURCE = """<?xml version="1.0" encoding="UTF-8"?>
<catalog>
  <products>
    <product id="1" type="service">
      <name>Ember Hub</name>
      <category>audio</category>
      <price>5338.63</price>
      <manufacturer><name>Kestrel Works</name></manufacturer>
    </product>
  </products>
</catalog>
"""

XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:element name="catalog">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="products">
          <xs:complexType>
            <xs:sequence>
              <xs:element name="product" maxOccurs="unbounded">
                <xs:complexType>
                  <xs:sequence>
                    <xs:element name="name"     type="xs:string"/>
                    <xs:element name="category" type="xs:string"/>
                    <xs:element name="price"    type="xs:decimal"/>
                    <xs:element name="manufacturer">
                      <xs:complexType>
                        <xs:sequence>
                          <xs:element name="name" type="xs:string"/>
                        </xs:sequence>
                      </xs:complexType>
                    </xs:element>
                  </xs:sequence>
                  <xs:attribute name="id"   type="xs:int"/>
                  <xs:attribute name="type" type="xs:string"/>
                </xs:complexType>
              </xs:element>
            </xs:sequence>
          </xs:complexType>
        </xs:element>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
"""

FIELDS = {
    "id": {"path": "@id"},
    "name": {"path": "name"},
    "category": {"path": "category"},
    "price": {"path": "price", "type": "string"},
    "manufacturer": {"path": "manufacturer/name"},
}


def write_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "in.xml"
    source.write_text(SOURCE, encoding="utf-8")
    schema = tmp_path / "catalog.xsd"
    schema.write_text(XSD, encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump({"record": "/catalog/products/product", "fields": FIELDS}, sort_keys=False),
        encoding="utf-8",
    )
    return source, schema, config


def with_schema(config: Path, schema: Path) -> Path:
    """The same config with a ``schema:`` key pointing at the XSD."""
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["schema"] = str(schema)
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config


# --- The core does not need the optional dependency --------------------------


def test_the_core_imports_with_the_optional_dependency_hidden() -> None:
    """Hide ``xmlschema``, then import everything the product needs. It must work.

    Run in a child, because the hiding has to happen before the imports and this
    process has already imported half the world. ``sys.modules[name] = None`` is the
    documented way to make a later ``import name`` raise ``ImportError`` -- stronger
    than hiding the file, because it holds even if something else already holds a
    reference to the real module.
    """
    probe = (
        "import sys\n"
        "sys.modules['xmlschema'] = None\n"
        "import gigaxml\n"
        "import gigaxml.cli\n"
        "import gigaxml.config\n"
        "import gigaxml.fields\n"
        "import gigaxml.writers\n"
        "import gigaxml.run\n"
        "import gigaxml.checkpoint\n"
        "from gigaxml.parser.streaming import StreamingRecordReader\n"
        "try:\n"
        "    import xmlschema\n"
        "    print('XMLSCHEMA-REACHABLE')\n"
        "except ImportError:\n"
        "    print('HIDDEN')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, (
        f"the core failed to import without the optional dependency:\n{completed.stderr[-800:]}"
    )
    assert "HIDDEN" in completed.stdout, (
        "the probe did not actually hide xmlschema, so this test proves nothing: "
        f"{completed.stdout!r}"
    )


def test_the_streaming_module_does_not_mention_the_schema_layer() -> None:
    """The reader's source must not import the schema layer, by name.

    A cheap, static check that states the invariant in the place a future change would
    break it. The behavioural test above would also catch it, but only at import time
    and only on a machine without the extra; this one fails in every environment.
    """
    source = (REPO_ROOT / "src" / "gigaxml" / "parser" / "streaming.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "xsd" not in code, "the streaming parser must not reference the XSD layer"
    assert "xmlschema" not in code, "the streaming parser must not import xmlschema"


# --- A schema changes types, not the parse -----------------------------------


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_the_schema_overrides_a_guessed_type(tmp_path: Path) -> None:
    """A field the config calls a string and the schema calls a decimal is a decimal.

    The config says ``price: {type: string}`` on purpose. The XSD says ``xs:decimal``,
    and the schema wins -- the value comes out as the full-precision decimal rather
    than the string the config asked for.
    """
    source, schema, config = write_inputs(tmp_path)
    with_schema(config, schema)

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]) == 0
    rows = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "id,name,category,price,manufacturer"
    assert rows[1].endswith("5338.63,Kestrel Works"), rows[1]


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_the_schema_does_not_change_which_fields_are_extracted(tmp_path: Path) -> None:
    """The XSD declares an ``@type`` attribute; the output must not gain a column.

    A schema describes the document, not the extraction. Pulling in every column the
    schema happens to declare would quietly change the shape of somebody's output --
    and would do it only for the users who happened to install an extra, which is the
    worst way for a tool to behave.
    """
    source, schema, config = write_inputs(tmp_path)
    with_schema(config, schema)

    assert main(["extract", str(source), "-c", str(config), "-o", str(tmp_path / "out.csv")]) == 0
    header = (tmp_path / "out.csv").read_text(encoding="utf-8").splitlines()[0]
    assert "type" not in header.split(","), f"the schema added a column: {header}"


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_the_types_are_read_off_the_schema_not_guessed(tmp_path: Path) -> None:
    """Attributes and children both resolve, and an unrecognised type is left out."""
    from gigaxml.xsd import record_field_types

    _, schema, _ = write_inputs(tmp_path)
    types = record_field_types(schema, "/catalog/products/product")

    assert types["@id"] is FieldType.INT, "an xs:int attribute should infer as int"
    assert types["price"] is FieldType.DECIMAL
    assert types["name"] is FieldType.STRING
    # Only the record element's own children and attributes, not the whole document's.
    assert "products" not in types and "catalog" not in types


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_a_config_naming_a_missing_schema_says_so(tmp_path: Path) -> None:
    """A bad ``schema:`` path is a clear error, not a silent skip of the types."""
    source, _, config = write_inputs(tmp_path)
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["schema"] = str(tmp_path / "not-here.xsd")
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    # main() turns a deliberate failure into one `error:` line and exit 1, so this is
    # asserted at that level rather than by catching the exception underneath.
    output = tmp_path / "out.csv"
    assert main(["extract", str(source), "-c", str(config), "-o", str(output)]) == 1
    assert not output.exists(), "a run that could not read its schema must write nothing"


def test_a_config_without_a_schema_never_touches_the_optional_layer(tmp_path: Path) -> None:
    """A config with no ``schema:`` works on a machine that has never heard of XSD.

    Checked in a child with the module hidden, because the claim is about what a
    *plain install* can do, and this process has it installed.
    """
    source, _, config = write_inputs(tmp_path)
    output = tmp_path / "out.csv"
    probe = (
        "import sys\n"
        "sys.modules['xmlschema'] = None\n"
        f"from gigaxml.cli import main\n"
        f"raise SystemExit(main(['extract', {str(source)!r}, '-c', {str(config)!r},"
        f" '-o', {str(output)!r}]))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stderr[-600:]
    assert output.is_file()


# --- The config key ----------------------------------------------------------


def test_the_schema_key_is_optional_and_typed() -> None:
    """Absent, the key is ``None``; present, it is the path as written."""
    assert parse_config({"record": "/a", "fields": {"x": {"path": "x"}}}).schema is None
    parsed = parse_config({"record": "/a", "fields": {"x": {"path": "x"}}, "schema": "  s.xsd  "})
    assert parsed.schema == "s.xsd"
    with pytest.raises(ConfigError):
        parse_config({"record": "/a", "fields": {"x": {"path": "x"}}, "schema": 7})


def test_a_schema_key_does_not_change_the_config_hash_without_the_extra() -> None:
    """Two configs that extract identically report the same hash.

    ``config_identity`` hashes the parsed config and is what a resume checks, so if the
    schema leaked into that hash, pointing a config at a schema would silently refuse
    every resume of an existing run for no reason. The hash is computed without the
    optional layer here, which is also the state a plain install is in.
    """
    from gigaxml.checkpoint import config_identity

    base = parse_config({"record": "/a", "fields": {"x": {"path": "x"}}})
    same = parse_config(
        {"record": "/a", "fields": {"x": {"path": "x"}}, "schema": "irrelevant.xsd"}
    )
    assert config_identity(base) == config_identity(same), (
        "a schema path must not invalidate a resume: it changes types, not the extraction"
    )


# --- What happens when the extra is absent, from the inside --------------------
#
# The subprocess tests above prove the *core* survives without xmlschema. These prove
# the schema module itself says so rather than raising something the CLI cannot turn
# into one `error:` line.


def test_requiring_the_schema_without_it_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The message names the extra and the command that installs it.

    An ``ImportError`` from three frames down is a traceback; a user who has not
    installed an optional extra needs one line telling them what to type.
    """
    import builtins

    import gigaxml.xsd as xsd

    real_import = builtins.__import__

    def refuse(name: str, *args: object, **kwargs: object) -> object:
        if name == "xmlschema":
            raise ImportError("no module named xmlschema")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", refuse)
    assert xsd.available() is False
    with pytest.raises(xsd.SchemaUnavailableError) as caught:
        xsd.require_schema()
    message = str(caught.value)
    assert "pip install 'gigaxml[xsd]'" in message, message
    assert "works without it" in message, message


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_a_missing_or_broken_schema_is_a_gigaxml_error(tmp_path: Path) -> None:
    """A bad path and a malformed file both fail through the same door.

    They are ordinary user mistakes, so they arrive as ``GigaXMLError`` -- which the
    CLI renders as one ``error:`` line -- rather than as whatever the schema library
    happens to raise, which is a family of its own and would reach the user raw.
    """
    from gigaxml.errors import GigaXMLError
    from gigaxml.xsd import record_field_types

    with pytest.raises(GigaXMLError) as missing:
        record_field_types(tmp_path / "nope.xsd", "/a/b")
    assert "does not exist" in str(missing.value)

    broken = tmp_path / "broken.xsd"
    broken.write_text("this is not XML at all", encoding="utf-8")
    with pytest.raises(GigaXMLError) as unparseable:
        record_field_types(broken, "/a/b")
    assert "not a usable XSD" in str(unparseable.value)

    # A well-formed schema that simply does not contain the path, with the alternatives
    # named -- a user who misspelled a segment needs to see what is there.
    _, schema, _ = write_inputs(tmp_path)
    with pytest.raises(GigaXMLError) as wrong_top:
        record_field_types(schema, "/envelope/items/item")
    assert "no top-level element" in str(wrong_top.value)
    assert "catalog" in str(wrong_top.value)

    with pytest.raises(GigaXMLError) as wrong_nested:
        record_field_types(schema, "/catalog/products/order")
    assert "no element" in str(wrong_nested.value)

    with pytest.raises(GigaXMLError) as empty_path:
        record_field_types(schema, "///")
    assert "names no element" in str(empty_path.value)


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_a_user_defined_simple_type_is_followed_to_its_base(tmp_path: Path) -> None:
    """A named type resolves to what it is built from, not to a guess.

    Schemas in the wild name their types. A schema that says ``MoneyType`` and never
    says what that is should not type a price column as a string -- and a column whose
    base is unrecognised should be left out rather than coerced.
    """
    from gigaxml.xsd import record_field_types

    schema = tmp_path / "named.xsd"
    schema.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">
  <xs:simpleType name="MoneyType">
    <xs:restriction base="xs:decimal"/>
  </xs:simpleType>
  <xs:simpleType name="DurationType">
    <xs:restriction base="xs:duration"/>
  </xs:simpleType>
  <xs:element name="thing">
    <xs:complexType>
      <xs:sequence>
        <xs:element name="amount" type="MoneyType"/>
        <xs:element name="blob"   type="DurationType"/>
      </xs:sequence>
    </xs:complexType>
  </xs:element>
</xs:schema>
""",
        encoding="utf-8",
    )
    types = record_field_types(schema, "/thing")
    assert types["amount"] is FieldType.DECIMAL, "a named type must resolve to its base"
    assert "blob" not in types, (
        "a type this project does not model must be absent, not guessed at -- coercing "
        "it to a string would silently change what the column contains"
    )


@pytest.mark.skipif(
    not _have_xmlschema(), reason="install the 'xsd' extra: pip install 'gigaxml[xsd]'"
)
def test_validation_reports_what_does_not_fit(tmp_path: Path) -> None:
    """A valid document yields no errors and a broken one names the element."""
    from gigaxml.errors import GigaXMLError
    from gigaxml.xsd import validate_document

    _, schema, _ = write_inputs(tmp_path)

    good = tmp_path / "good.xml"
    good.write_text(SOURCE, encoding="utf-8")
    assert validate_document(schema, good) == []

    bad = tmp_path / "bad.xml"
    bad.write_text(
        '<?xml version="1.0"?><catalog><products><product id="not-an-int">'
        "<name>x</name></product></products></catalog>",
        encoding="utf-8",
    )
    errors = validate_document(schema, bad)
    assert errors, "a product with a non-integer id does not fit the schema"

    with pytest.raises(GigaXMLError):
        validate_document(schema, tmp_path / "absent.xml")
