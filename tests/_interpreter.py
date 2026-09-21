"""Locating the console script that some tests have to run as a real process.

The layout of an install is not a test's business. The script may sit beside the
interpreter (a plain venv), in a user-level scripts directory (``pip install --user``),
or under a prefix a packaging tool picked. Only two mechanisms actually know where it
went -- ``PATH`` and ``sysconfig`` -- so those are the two this asks, in that order.

Raising when neither knows is deliberate. Falling back to a guessed path would turn
"the package is not installed here" into a later, more confusing failure, which reads
like a product defect rather than the environment problem it is.
"""

from __future__ import annotations

import pathlib
import shutil
import sys
import sysconfig


def gigaxml_script() -> pathlib.Path:
    """Locate the console script without assuming an install layout."""
    found = shutil.which("gigaxml")
    if found:
        return pathlib.Path(found)
    candidate = pathlib.Path(sysconfig.get_path("scripts")) / (
        "gigaxml.exe" if sys.platform == "win32" else "gigaxml"
    )
    if candidate.is_file():
        return candidate
    raise FileNotFoundError(
        "the gigaxml console script is not on PATH and not beside sysconfig's scripts "
        f"dir ({sysconfig.get_path('scripts')}); is the package installed?"
    )
