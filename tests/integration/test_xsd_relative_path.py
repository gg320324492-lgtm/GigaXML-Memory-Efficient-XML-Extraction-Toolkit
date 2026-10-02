"""A ``schema:`` written as a relative path names a real file, and this is what pins that.

**The defect.** ``schema: schemas/catalog.xsd`` is the way anybody writes a config next to
the data it describes, and it did not work: the run failed with ``can't access to
resource '.../schemas/schemas/catalog.xsd'`` -- the directory named once in the config
and once more inside the error. Not since a regression: the schema feature has handed a
relative string straight to the library since its first commit, so every config written
this way has been broken for the whole life of the feature.

**Where the doubling came from, measured rather than assumed.** It is not that the
library dislikes relative paths. ``normalize_url('schemas/catalog.xsd')`` alone produces
the correct absolute URL against the working directory. The doubling needs one more
thing, and it is the setting this project chose for itself: under ``allow='sandbox'`` with
no explicit ``base_url``, xmlschema derives the base from the source it was handed --
``base_url = os.path.dirname(normalize_url(source))`` at
``resources/xml_resource.py:166`` -- and the access that follows then resolves *that same
relative source* against the parent it was just cut out of. One step reads the source as
a path from the working directory; the next reads it as a location inside its own parent.
An absolute source cannot show the disagreement, because resolving an absolute path
against a ``base_url`` is a no-op. That is the whole reason this survived since ``72a5e33``:
every test that ever exercised it named an absolute path.

**So the invariant pinned here is not "relative paths work".** It is the narrower and
durable one: **gigaxml never hands the library a relative source.** That stays true after
the library fixes its own half of this, and it is the half gigaxml is responsible for.
What the library does with the string it is given is recorded in
:func:`test_what_the_library_does_with_a_relative_source_of_its_own`, which asserts our
contract rather than the library's bug -- so an upgrade that fixes the doubling upstream
shows up there as a fact that changed, not as a mysterious reason the workaround stopped
being needed.

**Why the end-to-end test asserts a column type and not just an exit code.** A run whose
``schema:`` key is present but unreadable fails rather than degrading -- ``_open_schema``
raises, and that is the behaviour the previous milestone installed on purpose -- so
``rc == 0`` already proves the schema was found. It does not prove the schema was *used*.
The config below therefore declares **no types at all**, and the field the XSD types as an
integer is read back out of a Parquet file written by a real child process. Only a schema
that was both located and applied can put an integer there.

**The refusal tests are here too, and they are the ones that matter for the milestone
that fixes this.** Resolving a path is the kind of change that quietly moves a boundary:
resolve wrongly and the sandbox is measured from somewhere new. So the escaping-include
case is re-tested with a **real** file outside the directory -- an absent file fails under
every policy, so a test built on one would report a refusal for the wrong reason and stay
green with the policy removed. The harmless include beside the schema is tested alongside
it, because a policy that refuses everything passes every refusal test and breaks every
user.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

import xmlschema

from gigaxml.checkpoint import schema_identity
from gigaxml.errors import SecurityError
from gigaxml.fields import FieldType
from gigaxml.xsd import record_field_types
from tests._interpreter import gigaxml_script

#: What every policy refusal says, one sentence a user can act on.
POLICY_MESSAGE = "refused by the schema security policy"

_XML_DECL = '<?xml version="1.0" encoding="UTF-8"?>\n'
_SCHEMA_OPEN = '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema">\n'


def _element(name: str, body: str) -> str:
    return f'<xs:element name="{name}">{body}</xs:element>'


def _complex(*children: str) -> str:
    return "<xs:complexType><xs:sequence>" + "".join(children) + "</xs:sequence></xs:complexType>"


#: ``catalog`` is the top-level element because the record path is ``/catalog/item``, and
#: ``qty`` is an ``xs:int`` because that is the assertion the Parquet file has to support.
CATALOG_XSD = (
    _XML_DECL
    + _SCHEMA_OPEN
    + _element(
        "catalog",
        _complex(
            _element(
                "item",
                _complex(
                    '<xs:element name="sku" type="xs:string"/>',
                    '<xs:element name="qty" type="xs:int"/>',
                ),
            )
        ),
    )
    + "</xs:schema>\n"
)

CATALOG_XML = (
    "<catalog><item><sku>A1</sku><qty>7</qty></item>"
    "<item><sku>B2</sku><qty>9</qty></item></catalog>\n"
)

#: No ``type:`` on either field. The types can only come from the XSD, which is what makes
#: the artefact assertion below mean something rather than restating the config.
CONFIG = """\
record: /catalog/item
fields:
  sku:
    path: sku
  qty:
    path: qty
schema: schemas/catalog.xsd
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A self-contained project whose config names its schema relatively.

    ``schemas/catalog.xsd`` beside ``catalog.xml`` and ``config.yaml`` -- the layout a
    person gets by putting the files in the right place, and the layout that did not work.
    """
    root = tmp_path / "project"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "catalog.xsd").write_text(CATALOG_XSD, encoding="utf-8")
    (root / "catalog.xml").write_text(CATALOG_XML, encoding="utf-8")
    (root / "config.yaml").write_text(CONFIG, encoding="utf-8")
    return root


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every source string handed to ``xmlschema.XMLSchema``, and compile as usual.

    Delegating to the real class rather than stubbing the compile is what keeps the
    recorded string trustworthy: the value observed is the one the library acted on, not
    the one a fake was handed.
    """
    seen: list[str] = []
    real = xmlschema.XMLSchema

    def recording(source: object = None, **kwargs: object) -> object:
        seen.append(source)  # type: ignore[arg-type]
        return real(source, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(xmlschema, "XMLSchema", recording)
    return seen


# --- the defect, end to end ------------------------------------------------


def test_a_relative_schema_path_works_end_to_end_through_the_console_script(
    project: Path,
) -> None:
    """Criterion A: a relative ``schema:`` runs to completion in a real process.

    ``cwd`` is the project directory, which is the whole point -- the defect only exists
    for a source that is relative to somewhere, and a test that runs from the repository
    root with a relative path would be testing a path that does not resolve to anything.
    """
    completed = subprocess.run(
        [
            str(gigaxml_script()),
            "extract",
            "catalog.xml",
            "-c",
            "config.yaml",
            "-o",
            "out.parquet",
            "--format",
            "parquet",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=project,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["rows"] == 2


def test_the_type_comes_from_the_schema_reached_by_a_relative_path(project: Path) -> None:
    """Criterion A's load-bearing half: the XSD was *applied*, not merely tolerated.

    The config declares no types, so an integer column in the artefact can only have come
    from ``<xs:element name="qty" type="xs:int"/>`` in a schema that was found, compiled
    and read. A run that ignored its ``schema:`` key would write two text columns and fail
    here rather than pass quietly.
    """
    pyarrow = pytest.importorskip("pyarrow", reason="the 'parquet' extra is optional")
    parquet = pytest.importorskip("pyarrow.parquet", reason="the 'parquet' extra is optional")

    completed = subprocess.run(
        [
            str(gigaxml_script()),
            "extract",
            "catalog.xml",
            "-c",
            "config.yaml",
            "-o",
            "out.parquet",
            "--format",
            "parquet",
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=project,
    )
    assert completed.returncode == 0, completed.stderr

    schema = parquet.read_schema(project / "out.parquet")
    assert pyarrow.types.is_integer(schema.field("qty").type)
    assert pyarrow.types.is_string(schema.field("sku").type) or pyarrow.types.is_large_string(
        schema.field("sku").type
    )


# --- the invariant, at the boundary ----------------------------------------


def test_a_relative_schema_path_never_reaches_the_library_as_written(
    project: Path, spy: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """gigaxml hands over an absolute source, and the one it hands over exists.

    Asserted as a property of *our* call rather than of the library's behaviour: this is
    the half gigaxml owns, and it keeps holding after the library's own half is fixed.
    The ``is_file()`` half is what distinguishes "resolved" from "made to look absolute" --
    the doubled path this replaces was a plausible-looking path to a file that was not
    there.
    """
    monkeypatch.chdir(project)

    types = record_field_types("schemas/catalog.xsd", "/catalog/item")

    assert types["qty"] is FieldType.INT
    assert spy, "the schema was compiled without the spy being called"
    (handed_over,) = spy
    assert Path(handed_over).is_absolute()
    assert Path(handed_over).is_file(), "the source handed over does not exist -- doubled?"
    assert Path(handed_over) == (project / "schemas" / "catalog.xsd").resolve()


def test_an_absolute_schema_path_reaches_the_library_byte_for_byte(
    tmp_path: Path, spy: list[str]
) -> None:
    """Criterion B: an absolute path is not rewritten, not even to tidy itself up.

    The path carries a ``schemas/../schemas`` segment on purpose. It names the same file,
    it is what a caller may legitimately write, and **any** normalisation -- ``Path``'s
    ``resolve()``, the library's ``normalize_url``, a tidy-up someone adds later -- would
    collapse it and change the string. So this test cannot tell "passed through" from
    "normalised" by accident: it is built so the two differ.
    """
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "catalog.xsd").write_text(CATALOG_XSD, encoding="utf-8")
    spelled_with_a_detour = tmp_path / "schemas" / ".." / "schemas" / "catalog.xsd"

    record_field_types(spelled_with_a_detour, "/catalog/item")

    (handed_over,) = spy
    assert handed_over == str(spelled_with_a_detour)


def test_what_the_library_does_with_a_relative_source_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Our contract is one thing; the library's behaviour is another, and both are pinned.

    The assertion is that **gigaxml resolves before the library sees the path**. The
    library's own half of the defect is not asserted here: a test that pins somebody
    else's bug goes red the day they fix it and reports nothing useful. Instead the fact
    is recorded here -- with ``allow='sandbox'`` a relative source resolves the directory
    into its own parent -- so that an upgrade which changes it can be read as a changed
    fact rather than as a mystery.

    The assertion that matters is the first one, and it is the one a regression in
    :func:`gigaxml.xsd._open_schema` would break.
    """
    schemas = tmp_path / "schemas"
    schemas.mkdir()
    (schemas / "catalog.xsd").write_text(CATALOG_XSD, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    # Ours: gigaxml hands over an absolute source, and the library's own sandbox then
    # measures from the schema's directory -- the same directory gigaxml measures from.
    schemas_dir = schemas.resolve()
    compiled = xmlschema.XMLSchema(
        str(schemas_dir / "catalog.xsd"), allow="sandbox", defuse="always"
    )
    assert compiled.base_url == schemas_dir.as_uri()

    # Theirs, recorded not asserted: with no base_url and allow='sandbox', the source the
    # library is given is normalised once to build the base and then normalised *again*
    # against it, and a relative source carries its directory in twice.
    try:
        xmlschema.XMLSchema("schemas/catalog.xsd", allow="sandbox", defuse="always")
    except Exception as exc:
        assert "schemas/schemas" in str(exc), (
            "the library no longer doubles a relative source under allow='sandbox'; "
            "gigaxml's resolution is now belt and braces rather than the fix, and the "
            "comment in _open_schema should say so"
        )
    else:  # pragma: no cover - the doubling is what xmlschema 4.3.2 does
        raise AssertionError(
            "xmlschema 4.3.2 was expected to double a relative source under "
            "allow='sandbox'; if it no longer does, the comment in _open_schema and the "
            "resolution there can be re-examined"
        )


# --- the boundary did not move ---------------------------------------------


@pytest.fixture
def escaping_project(tmp_path: Path) -> Path:
    """A schema under ``schemas/`` whose include escapes it, **into a file that exists**.

    escaping_project/
      outside/secret.xsd   real, declares ``leakField`` inside ``outsideMarker``
      schemas/root.xsd     includes ../outside/secret.xsd
      config.yaml          names it as schemas/root.xsd
    """
    root = tmp_path / "escaping"
    (root / "outside").mkdir(parents=True)
    (root / "schemas").mkdir()
    (root / "outside" / "secret.xsd").write_text(
        _XML_DECL
        + _SCHEMA_OPEN
        + _element("outsideMarker", _complex('<xs:element name="leakField" type="xs:string"/>'))
        + "</xs:schema>\n",
        encoding="utf-8",
    )
    (root / "schemas" / "root.xsd").write_text(
        _XML_DECL
        + _SCHEMA_OPEN
        + '<xs:include schemaLocation="../outside/secret.xsd"/>'
        + "</xs:schema>\n",
        encoding="utf-8",
    )
    (root / "config.yaml").write_text(
        "record: /outsideMarker\nfields:\n  leakField:\n    path: leakField\n"
        "schema: schemas/root.xsd\n",
        encoding="utf-8",
    )
    return root


def test_a_relative_schema_path_still_refuses_a_file_outside_its_directory(
    escaping_project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion C: resolving the source did not move the boundary that measures it.

    Refused **as a policy decision**, not as a missing file -- which is the whole
    distinction, because a refusal that is really the filesystem saying no stops nothing.
    """
    monkeypatch.chdir(escaping_project)

    with pytest.raises(SecurityError, match=POLICY_MESSAGE):
        record_field_types("schemas/root.xsd", "/outsideMarker")


def test_a_relative_schema_path_still_allows_a_file_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion C's other half, and the reason the test above can be trusted.

    A policy that refuses everything passes every refusal test. So the include that *is*
    beside the schema is read, by the same relative path, and its declarations arrive.
    """
    root = tmp_path / "beside"
    (root / "schemas").mkdir(parents=True)
    (root / "schemas" / "inner.xsd").write_text(
        _XML_DECL
        + _SCHEMA_OPEN
        + _element("insideMarker", _complex('<xs:element name="insideField" type="xs:string"/>'))
        + "</xs:schema>\n",
        encoding="utf-8",
    )
    (root / "schemas" / "root.xsd").write_text(
        _XML_DECL + _SCHEMA_OPEN + '<xs:include schemaLocation="inner.xsd"/>' + "</xs:schema>\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(root)

    types = record_field_types("schemas/root.xsd", "/insideMarker")

    assert "insideField" in types


def test_the_run_identity_does_not_depend_on_how_the_schema_was_named(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schema's identity is its content, so naming it differently is not a new run.

    This is why the fix is safe for the one compatibility promise 2.0 has to keep: a
    checkpoint written by 1.2.x must resume on 2.0. ``schema_identity`` hashes the file's
    **content** and not its path, so a config whose ``schema:`` is relative and one whose
    ``schema:`` is absolute identify the same run -- which is what lets a user upgrade
    without their in-flight runs becoming unresumable.

    A fix that rewrote the path into the identity would pass every test above and break
    this, which is why it is asserted rather than assumed.
    """
    monkeypatch.chdir(project)

    assert schema_identity("schemas/catalog.xsd") == schema_identity(
        str(project / "schemas" / "catalog.xsd")
    )
