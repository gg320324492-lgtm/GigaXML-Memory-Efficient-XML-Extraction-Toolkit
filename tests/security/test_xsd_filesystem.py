"""Every other way a schema can name a file it should not be allowed to open.

``../`` is the obvious escape and is covered where the directory crossing itself is
tested. This file covers the spellings that get past a naive string check: an absolute
path, a Windows path with a drive letter, and a ``file://`` URL. None of them need a
``..`` at all -- they simply name the file outright -- so a guard that only looked for
parent-directory references would miss all three.

The symlink case is last and is written to *admit when it cannot be built*. Windows
creates a real symlink only with a privilege this machine may not have, and a test that
quietly skipped itself would read as coverage it does not provide.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

from gigaxml.errors import GigaXMLError
from gigaxml.xsd import record_field_types
from tests.security.conftest import (
    ESCAPED_ELEMENT,
    INCLUDED_ELEMENT,
    INCLUDED_FIELD,
    declared_element,
    schema_document,
)

POLICY_MESSAGE = "refused by the schema security policy"


def _build_escape(tmp_path: Path, location: str) -> Path:
    """A schema outside whose directory a real secret lives, including ``location``.

    The target is written for every case, so no test below can pass by naming a path
    that does not exist -- the failure this file exists to prevent. ``exist_ok`` because
    one test builds several spellings of the same escape into one tree.
    """
    (tmp_path / "outside").mkdir(exist_ok=True)
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir(exist_ok=True)
    (tmp_path / "outside" / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, "leakField")),
        encoding="utf-8",
    )
    root = sandbox / "root.xsd"
    root.write_text(
        schema_document(f'<xs:include schemaLocation="{location}"/>'),
        encoding="utf-8",
    )
    return root


def test_an_absolute_path_outside_the_sandbox_is_refused(tmp_path: Path) -> None:
    """A POSIX absolute path is named outright and still refused."""
    location = (tmp_path / "outside" / "secret.xsd").as_posix()
    root = _build_escape(tmp_path, location)

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_windows_path_with_a_drive_letter_is_refused(tmp_path: Path) -> None:
    """A drive-letter path, which no ``..`` check would ever match.

    Both spellings are tried -- ``C:\\...`` and its forward-slash form -- because a
    resolver that normalises one and not the other would let the other through.
    """
    secret = tmp_path / "outside" / "secret.xsd"
    for location in (str(secret), secret.as_posix()):
        root = _build_escape(tmp_path, location)

        with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
            record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_file_url_outside_the_sandbox_is_refused(tmp_path: Path) -> None:
    """``file:///`` is a URL spelling of the same escape."""
    secret = tmp_path / "outside" / "secret.xsd"
    root = _build_escape(tmp_path, f"file:///{secret.as_posix()}")

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_uri_with_a_different_scheme_is_refused(tmp_path: Path) -> None:
    """A scheme the sandbox does not recognise is refused rather than resolved.

    ``sandbox`` answers "may this location be read?" with no for anything that is not
    demonstrably inside the directory, so an unfamiliar scheme has nowhere to pass
    through to.
    """
    root = _build_escape(tmp_path, "data:text/plain,not-a-schema")

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_symlink_that_points_outside_the_sandbox_is_refused(tmp_path: Path) -> None:
    """A link *inside* the sandbox whose target is outside it is refused.

    **This one carries a history worth keeping in the docstring.** It used to be an
    expected failure: ``allow='sandbox'`` compares the *literal* path, and a symlink
    whose literal path sits inside the sandbox therefore passed the check while the
    kernel followed it to a file outside. The ``'../'`` case beside this has always
    been blocked, which is what proved the difference was the link and not the target.

    It now passes because the resource is resolved *before* it is compared, so the
    question answered is "which file", never "which name". The target above is a real
    file carrying a marker element -- without that, an absent path would fail under any
    setting and the test would distinguish nothing.

    Windows creates a real symlink only with a privilege this machine may not have, so
    a failure to *build* the link is reported as unverified rather than skipped
    quietly -- a silent skip would be indistinguishable from a pass.
    """
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, "leakField")),
        encoding="utf-8",
    )

    link = sandbox / "link.xsd"
    try:
        link.symlink_to(outside / "secret.xsd")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(
            f"unverified on this machine: cannot create a symlink to test with ({exc}); "
            "the path escape above is verified, the symlink variant is not"
        )

    root = sandbox / "root.xsd"
    root.write_text(
        schema_document('<xs:include schemaLocation="link.xsd"/>'),
        encoding="utf-8",
    )

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_symlink_that_stays_inside_the_sandbox_is_not_refused(tmp_path: Path) -> None:
    """A link inside the sandbox pointing at a file inside it works.

    **The case easiest to get wrong in the other direction.** Resolving before
    comparing could have been written as "refuse anything that is a link", which would
    pass every rejection test here and break a user who keeps one schema under two
    names. A link is not suspicious; only where it *goes* is. This is the assertion
    that keeps the fix from becoming a different bug.
    """
    from gigaxml.fields import FieldType

    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    (sandbox / "real.xsd").write_text(
        schema_document(declared_element(INCLUDED_ELEMENT, INCLUDED_FIELD)),
        encoding="utf-8",
    )
    link = sandbox / "alias.xsd"
    try:
        link.symlink_to(sandbox / "real.xsd")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified on this machine: cannot create a symlink ({exc})")

    root = sandbox / "root.xsd"
    root.write_text(
        schema_document('<xs:include schemaLocation="alias.xsd"/>'),
        encoding="utf-8",
    )

    assert record_field_types(root, f"/{INCLUDED_ELEMENT}") == {INCLUDED_FIELD: FieldType.STRING}


def test_a_chain_of_symlinks_that_ends_outside_is_refused(tmp_path: Path) -> None:
    """Two links deep still refused, because resolving follows the whole chain.

    ``hop1.xsd`` is a link to ``hop2.xsd``, which is a link to the file outside. A check
    that stopped at the first hop would see a target inside the sandbox and pass it.
    Resolving the final target collapses the chain in one step, which is the reason the
    fix does it that way -- but that reasoning is only worth something if a test holds
    it, because nothing about "one hop" or "two hops" is written anywhere else.
    """
    outside = tmp_path / "outside"
    sandbox = tmp_path / "sandbox"
    outside.mkdir()
    sandbox.mkdir()
    (outside / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, "leakField")),
        encoding="utf-8",
    )
    hop1 = sandbox / "hop1.xsd"
    hop2 = sandbox / "hop2.xsd"
    try:
        hop2.symlink_to(outside / "secret.xsd")
        hop1.symlink_to(hop2)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified on this machine: cannot create symlinks ({exc})")

    root = sandbox / "root.xsd"
    root.write_text(
        schema_document('<xs:include schemaLocation="hop1.xsd"/>'),
        encoding="utf-8",
    )

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, f"/{ESCAPED_ELEMENT}")


def test_a_root_that_is_a_symlink_sets_the_boundary_where_it_lands(tmp_path: Path) -> None:
    """The root's own target decides the boundary, and it still constrains what follows.

    The user named ``entry.xsd``, which is a link into ``real/``. The boundary is
    therefore ``real/`` -- the user pointed there, so that is the directory they meant
    -- and everything reached *from* it is measured against it. A file inside ``real/``
    is allowed; a step outside it is not. Both halves are asserted, because the first
    without the second would be a boundary that expands to nothing.

    The paths are **absolute** here, and that is load-bearing: see
    :func:`test_a_relative_include_from_a_symlinked_root_is_refused` for why a relative
    spelling does not reach the file the user meant.
    """
    from gigaxml.fields import FieldType

    outside = tmp_path / "outside"
    real = tmp_path / "real"
    outside.mkdir()
    real.mkdir()
    (outside / "secret.xsd").write_text(
        schema_document(declared_element(ESCAPED_ELEMENT, "leakField")),
        encoding="utf-8",
    )
    (real / "sibling.xsd").write_text(
        schema_document(declared_element(INCLUDED_ELEMENT, INCLUDED_FIELD)),
        encoding="utf-8",
    )
    entry = tmp_path / "entry.xsd"

    # Inside the boundary: entry resolves into real/, and the sibling lives in real/.
    (real / "actual.xsd").write_text(
        schema_document(f'<xs:include schemaLocation="{(real / "sibling.xsd").as_posix()}"/>'),
        encoding="utf-8",
    )
    try:
        entry.symlink_to(real / "actual.xsd")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified on this machine: cannot create a symlink ({exc})")

    assert record_field_types(entry, f"/{INCLUDED_ELEMENT}") == {INCLUDED_FIELD: FieldType.STRING}

    # And from that same root, a step back out is still refused.
    (real / "actual.xsd").write_text(
        schema_document(f'<xs:include schemaLocation="{(outside / "secret.xsd").as_posix()}"/>'),
        encoding="utf-8",
    )
    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(entry, f"/{ESCAPED_ELEMENT}")


def test_a_relative_include_from_a_symlinked_root_is_refused(tmp_path: Path) -> None:
    """A relative include from a symlinked root does not resolve, and does not pass quietly.

    **This is a limit of the library's path resolution, not of the boundary.** The base
    it resolves against is the directory the *root argument* names -- ``tmp/`` when the
    argument is ``tmp/entry.xsd`` -- not the directory the root actually lives in after
    following the link. So ``sibling.xsd`` is looked up as ``tmp/sibling.xsd``, which is
    outside the boundary and also not a file that exists.

    Left to the library, this compiles with a warning and **no** elements: the include is
    dropped and the schema reports itself as built. Refused instead, because an include
    that vanished without failing is the same shape of problem as a remote resource that
    failed while the compile claimed success. The two paths through this file differ only
    in where they would have gone wrong, which is why both are asserted.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "sibling.xsd").write_text(
        schema_document(declared_element(INCLUDED_ELEMENT, INCLUDED_FIELD)),
        encoding="utf-8",
    )
    (real / "actual.xsd").write_text(
        schema_document('<xs:include schemaLocation="sibling.xsd"/>'),
        encoding="utf-8",
    )
    entry = tmp_path / "entry.xsd"
    try:
        entry.symlink_to(real / "actual.xsd")
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"unverified on this machine: cannot create a symlink ({exc})")

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(entry, f"/{INCLUDED_ELEMENT}")
