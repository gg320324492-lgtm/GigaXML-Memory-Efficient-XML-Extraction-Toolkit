"""The command-line behaviour Stage 3 must not change, frozen before the refactoring.

This package holds the evidence that nineteen milestones of restructuring did not change
what the tool does. It is a *characterisation* suite: the expectations were recorded from
the implementation as it stood at 1.2.1, not from what anybody would have written by
reading the source, so a golden test that passes is a statement about observed behaviour.

Two rules make it survive the refactoring it exists to police:

**Assert behaviour, never implementation.** Nothing here names a private function or
counts calls. A test that did would be rewritten by the first milestone that moved code,
and a rewritten test proves nothing.

**Normalise only what the machine decides.** The run directory and the line endings; see
:func:`tests.golden.conftest.normalize`. Everything else -- a score, a row order, a
message -- is compared as it came out.

The three defects confirmed before this milestone are pinned in
:mod:`tests.golden.test_known_defects`, which asserts today's wrong behaviour on purpose
so that a fix has something to point at.
"""
