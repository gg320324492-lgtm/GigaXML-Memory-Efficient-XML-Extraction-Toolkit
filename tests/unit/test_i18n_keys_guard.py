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

import pytest
from tools.check_i18n_keys import main


def test_every_interface_string_is_accounted_for(capsys: pytest.CaptureFixture[str]) -> None:
    """All three scans pass. On failure the output names every offender."""
    assert main() == 0, capsys.readouterr().out
