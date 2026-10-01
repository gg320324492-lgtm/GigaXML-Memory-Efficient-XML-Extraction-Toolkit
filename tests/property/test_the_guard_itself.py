"""Property tests: the exception boundary, before anything else.

★ **This file exists because the guard the other properties rely on is the part that can
be wrong in a way nothing else notices.** ``accepts_or_refuses`` is what makes "either a
valid result or a domain error" mean anything; if it accepts a ``KeyError`` it makes every
property that uses it certify a leak, and the whole suite stays green.

So the guard is tested first, against inputs it must reject -- including the widened
error family that criterion F's first mutation consists of.
"""

from __future__ import annotations

import pytest

from gigaxml.errors import ConfigError, FieldPathError, GigaXMLError

from .conftest import accepts_or_refuses

#: A narrow family, the shape every real property uses.
PROMISED: tuple[type[BaseException], ...] = (ConfigError,)


def _raise(exc: BaseException) -> None:
    raise exc


def _refuses_as_promised() -> None:
    raise ConfigError("the config is wrong")


@pytest.mark.parametrize(
    "leaker",
    [KeyError("records_consumed"), TypeError("int is not subscriptable"), ValueError("nope")],
    ids=["KeyError", "TypeError", "ValueError"],
)
def test_a_leaked_exception_is_reported_rather_than_swallowed(leaker: object) -> None:
    """A ``KeyError`` out of a parse is a crash, not a refusal, and must be named as one.

    These three are exactly what the milestone's invariant forbids leaking: ``KeyError``,
    ``TypeError`` and ``ValueError`` are on the list because every "crash" defect this
    project has had has been one of them arriving from an input nobody wrote down.
    """
    with pytest.raises(AssertionError, match="LEAKED"):
        accepts_or_refuses(lambda: _raise(leaker), expected=PROMISED, label="the parse")


def test_a_promised_error_is_a_refusal_and_returns_none() -> None:
    assert accepts_or_refuses(_refuses_as_promised, expected=PROMISED, label="the parse") is None


def test_a_value_passes_through_untouched() -> None:
    sentinel = object()
    assert accepts_or_refuses(lambda: sentinel, expected=PROMISED, label="the parse") is sentinel


def test_an_unpromised_domain_error_is_reported_under_a_narrow_family() -> None:
    """The baseline: a gigaxml error the call site did not promise is still a defect."""
    with pytest.raises(AssertionError, match="promised"):
        accepts_or_refuses(
            lambda: _raise(FieldPathError("bad field path")), expected=PROMISED, label="the parse"
        )


def test_widening_the_family_to_exception_does_not_make_the_guard_permissive() -> None:
    """★ Criterion F, first end. This test is what the mutation breaks.

    The obvious guard is ``except EXPECTED_ERRORS: ... except Exception: ...``, and
    widening ``EXPECTED_ERRORS`` to ``Exception`` makes the first branch swallow
    everything while the second never runs -- a suite that stays green having certified
    nothing. This guard catches ``GigaXMLError`` first and *then* asks whether the type
    was promised, so the widened family fails the membership check instead of being
    absorbed.

    The assertion is not "an exception happened": it is that widening the declared family
    does not widen what counts as a valid refusal.
    """
    with pytest.raises(AssertionError, match="promised"):
        accepts_or_refuses(
            lambda: _raise(FieldPathError("bad field path")),
            expected=(Exception,),
            label="the parse",
        )


def test_the_base_class_itself_is_not_accepted_where_subclasses_are_promised() -> None:
    """``GigaXMLError`` is the family; a bare instance of it names no specific refusal.

    Every deliberate refusal in this project raises a subclass, so a bare base instance
    means something reached a catch-all. Treating it as a valid refusal would be exactly
    the "matches a message instead of a type" mistake ``run_report.py`` calls worse than
    not classifying.
    """
    with pytest.raises(AssertionError, match="promised"):
        accepts_or_refuses(
            lambda: _raise(GigaXMLError("raised directly, not by a subclass")),
            expected=(ConfigError, FieldPathError),
            label="the parse",
        )
