"""A schema never opens a network connection.

The claim "gigaxml does not touch the network" is checked here at the level where it
would be false. A remote ``schemaLocation`` is the only route a schema has to reach out
of the machine, and before the policy it was taken: ``allow='all'`` issued a real TCP
connection attempt, and when the connection failed the failure surfaced as a *warning*
while the schema compiled anyway.

Two things are asserted, and the second is the one that matters. The error has to be a
policy refusal rather than a DNS or connect failure -- that proves the block happened
before the socket did. And the socket itself is watched directly, so a policy that
blocked for a different reason, or a compiler that opened a connection for an unrelated
purpose, would still be caught.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

pytest.importorskip("xmlschema", reason="the 'xsd' extra is optional: pip install 'gigaxml[xsd]'")

from gigaxml.errors import GigaXMLError
from gigaxml.xsd import record_field_types
from tests.security.conftest import schema_document

#: RFC 6761 reserves ``.invalid`` so it never resolves. Used deliberately: if the
#: policy were ever removed, a test built on a real host would attempt to reach it.
#: This one fails at DNS instead -- which is still a failure, and is why the socket
#: spy below is the assertion that actually carries the claim.
NEVER_RESOLVES = "https://unreachable-according-to-rfc6761.invalid/schema.xsd"

POLICY_MESSAGE = "refused by the schema security policy"


def _remote_schema(tmp_path: Path) -> Path:
    """A schema whose only content is a remote include."""
    root = tmp_path / "root.xsd"
    root.write_text(
        schema_document(f'<xs:include schemaLocation="{NEVER_RESOLVES}"/>'),
        encoding="utf-8",
    )
    return root


def test_a_remote_include_is_refused_as_a_policy_decision(tmp_path: Path) -> None:
    """The refusal says the policy said no -- not "connection failed".

    A DNS or connect error would mean the request was attempted and merely did not
    succeed, which is the behaviour before the policy: the connection went out and the
    schema compiled anyway, with the failure demoted to a warning.
    """
    root = _remote_schema(tmp_path)

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE) as refused:
        record_field_types(root, "/anything")

    cause = str(refused.value.__cause__)
    assert "block access to remote resource" in cause
    for failure_name in ("NameResolutionError", "gaierror", "ConnectionRefused", "WinError"):
        assert failure_name not in cause


def test_a_remote_include_opens_no_socket_at_all(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No connection is attempted: the spy sees zero calls.

    Direct evidence rather than inference. "It was blocked before connecting" is only
    shown by watching the connecting, so the assertion is on the call itself.

    The refusal is recorded rather than asserted first, deliberately. Under ``pytest.raises``
    a test that stops being refused never reaches the spy's assertion, so it would go
    red on "did not raise" while the claim it exists to make -- *no socket was opened* --
    was never checked. Checking the spy first means that when the policy is removed,
    this test fails on the connection it caught rather than on the missing refusal,
    which is the fact worth having.
    """
    attempts: list[object] = []
    real_connect = socket.socket.connect

    def spy(self: socket.socket, address: object) -> object:
        attempts.append(address)
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", spy)

    root = _remote_schema(tmp_path)
    refused: GigaXMLError | None = None
    try:
        record_field_types(root, "/anything")
    except GigaXMLError as exc:
        refused = exc

    assert attempts == [], (
        f"a schema opened {len(attempts)} network connection(s) while compiling: "
        f"{attempts!r}; the policy is supposed to stop this before any socket is made"
    )
    assert refused is not None, "the remote include was not refused at all"
    assert POLICY_MESSAGE in str(refused)


def test_the_remote_case_is_fast_because_nothing_is_waited_on(tmp_path: Path) -> None:
    """A blocked request returns immediately.

    Before the policy this path reached the operating system's connect machinery and
    took whatever the OS gives it; with the policy it never gets there. The bound here
    is a sanity check against a block that quietly became a timeout rather than an
    immediate refusal -- it is generous, because the claim is "no waiting", not "fast".
    """
    import time

    root = _remote_schema(tmp_path)
    started = time.perf_counter()

    with pytest.raises(GigaXMLError, match=POLICY_MESSAGE):
        record_field_types(root, "/anything")

    assert time.perf_counter() - started < 5.0
