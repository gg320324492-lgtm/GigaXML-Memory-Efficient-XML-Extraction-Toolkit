"""The interface-string checker runs as part of the suite, not by being remembered.

``tools/check_i18n_keys.py`` guards three things -- every ``tr()`` literal is in the
table, every module-level string constant is accounted for, and every ``count_of`` noun
has both of its keys -- and none of that is worth anything if the check only runs when
someone thinks of it. Running it here means it executes on every developer machine that
runs pytest and on every CI leg, with a red test where a red explanation would be.

The checker is deliberately invoked through its own ``main()`` rather than re-implemented:
a second implementation of the scan would drift from the standalone script the same way
an untranslated copy of a string drifts from its original.
"""

from __future__ import annotations

import ast
import textwrap

import pytest
from tools.check_i18n_keys import main


def test_every_interface_string_is_accounted_for(capsys: pytest.CaptureFixture[str]) -> None:
    """All three scans pass. On failure the output names every offender."""
    assert main() == 0, capsys.readouterr().out


def test_count_of_nouns_reads_keyword_forms_and_reports_the_unreadable() -> None:
    """``count_of``'s noun is read in both spellings; unreadable nouns are reported.

    Synthetic source through :func:`ast.parse` so each spelling's behaviour is pinned
    directly -- a keyword literal is collected, a keyword variable and a missing noun
    both land in ``dynamic`` -- without planting call sites in a real panel. The blind
    spot this pins was real: the scan used to read positional arguments only, and
    ``count_of(1, noun="gadget")`` passed in silence.
    """
    from tools.check_i18n_keys import _count_of_nouns

    source = textwrap.dedent(
        """
        def count_of(number: int, noun: str) -> str:
            ...

        count_of(1, noun="gadget")
        count_of(2, noun=x)
        count_of(3)
        count_of(4, "row")
        """
    )
    nouns, dynamic, sites = _count_of_nouns(ast.parse(source), "synthetic")
    lines = source.splitlines()
    unreadable = [
        f"synthetic:{number}"
        for number, line in enumerate(lines, 1)
        if line.startswith("count_of(2") or line.startswith("count_of(3")
    ]
    keyword_literal = (
        f"synthetic:{next(n for n, ln in enumerate(lines, 1) if ln.startswith('count_of(1'))}"
    )
    positional = (
        f"synthetic:{next(n for n, ln in enumerate(lines, 1) if ln.startswith('count_of(4'))}"
    )

    assert nouns == {"gadget", "row"}
    assert dynamic == unreadable
    assert sites["gadget"] == [keyword_literal]
    assert sites["row"] == [positional]
