"""Shared helpers for the property tests.

**One guard, used by every property.** The invariant this milestone states is "any input
either produces a valid result or raises one of this project's own error types" -- and
the value of that sentence is entirely in the second half. A test that only checks the
happy path measures nothing; a test that catches ``Exception`` measures nothing either.
So the interesting code is here, and it is tested by
``tests/property/test_the_guard_itself.py``.

★ **Why the leak check is keyed on ``GigaXMLError`` and not on the expected family.**
The obvious implementation is ``except EXPECTED_ERRORS: ... except Exception: ...``, and
that one is defeatable in a way that matters: widen ``EXPECTED_ERRORS`` to ``Exception``
and every exception is caught by the first branch, the second branch never runs, and the
suite stays green having proved nothing. Criterion F asks for exactly that mutation, so
this guard catches ``GigaXMLError`` **first** and asks afterwards whether the type is one
this call site promised. Widening the family then fails the *membership* check rather
than being absorbed -- and it fails on the first domain error hypothesis finds, so no
leak has to be discovered for the mutation to bite.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from hypothesis import HealthCheck, settings

from gigaxml.errors import GigaXMLError

T = TypeVar("T")

#: Settings shared by every property here.
#:
#: ★ ``database=None`` is not optional. Hypothesis otherwise keeps a database of failing
#: examples in ``.hypothesis/`` under the pytest rootdir, which for this project is the
#: repository -- so the default setting writes into the working tree, untracked, and a
#: property run would show up in ``git status``. Criterion E says the file-writing
#: properties must not write into the repository, and this is the same rule one level
#: down: the framework must not either.
PROPERTY_SETTINGS = settings(
    max_examples=100,
    deadline=None,  # these are parsing functions; a slow CI box is not a bug in them
    database=None,
    suppress_health_check=[HealthCheck.too_slow],
)

#: The same, for the properties that touch the filesystem, which cost more per example.
FILE_PROPERTY_SETTINGS = settings(
    max_examples=60,
    deadline=None,
    database=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)


def accepts_or_refuses(
    operation: Callable[[], T],
    *,
    expected: tuple[type[BaseException], ...],
    label: str,
) -> T | None:
    """Return ``operation``'s value, or ``None`` if it refused for a stated reason.

    Args:
        operation: the parse, called with no arguments.
        expected: the exact error family this call site promises to raise. Something else
            that is still a :class:`GigaXMLError` is a **wrong domain error** -- the
            problem was classified, just not the way this caller expects -- and anything
            that is not one is a **leak**, which no caller of this library can catch.
        label: how a failure reads, so a shrunk example names one call site rather than
            saying "the operation".

    Raises:
        AssertionError: ``operation`` raised outside ``expected``.
    """
    try:
        return operation()
    except GigaXMLError as exc:
        actual = type(exc)
        assert actual in expected, (
            f"{label} raised {actual.__name__}: a domain error, but not one this call site "
            f"promised ({', '.join(cls.__name__ for cls in expected)}). {exc}"
        )
        return None
    except Exception as exc:
        raise AssertionError(
            f"{label} LEAKED {type(exc).__name__}, which no caller can catch as a gigaxml "
            f"error: {exc!r}"
        ) from exc
