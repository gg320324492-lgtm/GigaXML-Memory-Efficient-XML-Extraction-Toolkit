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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "KNOWN GAP, reported for ruling: allow='sandbox' checks the literal path, and a "
        "symlink inside the sandbox has a literal path inside the sandbox. The link is "
        "followed when the file is opened, so a target outside the sandbox is read. The "
        "'../' case beside this one is blocked, which is what makes the two differ. "
        "Fixing it means canonicalising before the check, which is a change to how the "
        "sandbox is implemented rather than to the parameters passed to it -- out of "
        "scope for the change that fixed the rest. strict=True: this fails the build "
        "the moment it starts passing, so it cannot be forgotten."
    ),
)
def test_a_symlink_that_points_outside_the_sandbox_is_refused(tmp_path: Path) -> None:
    """A link *inside* the sandbox whose target is outside it.

    **This does not pass today, and that is the finding.** The test states what should
    happen so the gap is visible and cannot drift out of mind; the ``xfail`` above
    records why. It is written as an expectation rather than an assertion of the hole,
    because recording that the hole works would read as an approval of it.

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
