"""Performance tests for gigaxml.

These are excluded from CI on purpose: wall-clock and RSS numbers are machine
dependent and would be flaky as a merge gate. They are still part of the default
local run, because the bounded-memory claim is the whole point of the project and
a claim that is never executed is not a claim.

See docs/ROADMAP.md revision A11.
"""
