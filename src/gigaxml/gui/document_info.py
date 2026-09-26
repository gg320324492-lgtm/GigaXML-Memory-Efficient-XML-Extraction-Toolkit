"""What the window can say about a document before it reads it.

**Why the estimate exists at all.** ``inspect`` reads the whole document. On a small file
that is instant, and on a large one it is not, and a window that goes quiet for thirty
seconds without having said so looks broken. The number here is an estimate and is
labelled as one; what it must not be is a guess dressed up as a measurement.

**Where the rate comes from.** Measured on this development machine, 2026-09-22, by
timing ``inspect --json`` end to end through ``subprocess``:

===========================  ==========  =========  ============
document                     size        wall time  throughput
===========================  ==========  =========  ============
``data/s10.xml``             10.03 MiB   0.90 s     11.2 MiB/s
``data/s100.xml``            100.66 MiB  7.24 s     13.9 MiB/s
===========================  ==========  =========  ============

The larger run is the one used, because the smaller one is dominated by interpreter
start-up -- which is exactly the part that does not grow with the document. Both are kept
above so the choice can be checked rather than trusted.

**The rate is a property of this machine, not of the program.** It is a module constant
rather than a literal buried in a formula so that a caller who has measured their own
machine can pass it in, and so that a test can pin the arithmetic without asserting a
speed. Nothing may fail because a machine is slow.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from datetime import datetime

from gigaxml.gui.i18n import tr

__all__ = [
    "DOCUMENT_SUFFIXES",
    "INSPECT_MIB_PER_SECOND",
    "SLOW_ANALYSIS_SECONDS",
    "DocumentInfo",
    "describe",
    "looks_like_document",
]

#: What the file dialog offers and what a drop is allowed to be. ``.xml.gz`` is listed
#: separately from ``.xml`` because a suffix check on the last component alone would call
#: ``catalog.xml.gz`` a ``.gz`` and reject it.
DOCUMENT_SUFFIXES = (".xml", ".xml.gz")

#: Measured throughput of ``inspect`` on this machine; see the module docstring.
INSPECT_MIB_PER_SECOND = 13.9

#: Below this the estimate is not worth showing -- "this will take 0.4 s" is noise, and a
#: warning that appears for every file is a warning nobody reads.
SLOW_ANALYSIS_SECONDS = 10.0

_BYTES_PER_MIB = 1024 * 1024


@dataclass(frozen=True)
class DocumentInfo:
    """One document as the filesystem describes it, before anything reads it."""

    path: pathlib.Path
    size_bytes: int
    modified: datetime | None
    exists: bool

    @property
    def size_mib(self) -> float:
        return self.size_bytes / _BYTES_PER_MIB

    def estimated_seconds(self, *, rate: float = INSPECT_MIB_PER_SECOND) -> float:
        """How long ``inspect`` is likely to take, from the measured rate."""
        if rate <= 0:
            return 0.0
        return self.size_mib / rate

    def estimate_note(self, *, rate: float = INSPECT_MIB_PER_SECOND) -> str:
        """A sentence for the information bar, or ``""`` when there is nothing to say.

        Returned rather than written into the widget so that the wording is testable, and
        so the panel cannot invent a second, differently-worded version of it.
        """
        if not self.exists:
            return ""
        seconds = self.estimated_seconds(rate=rate)
        if seconds < SLOW_ANALYSIS_SECONDS:
            return ""
        if seconds >= 60:
            minutes = seconds / 60
            return tr(
                "analysis reads the whole document: expect roughly {} min "
                "({} MiB at about {} MiB/s)"
            ).format(f"{minutes:.0f}", f"{self.size_mib:.0f}", f"{rate:.0f}")
        return tr(
            "analysis reads the whole document: expect roughly {} s ({} MiB at about {} MiB/s)"
        ).format(f"{seconds:.0f}", f"{self.size_mib:.0f}", f"{rate:.0f}")


def describe(path: pathlib.Path | str) -> DocumentInfo:
    """Stat ``path`` without reading it.

    A path that is not there is described rather than raised: the recent-files list has
    to render an entry for a document on a drive that is currently unplugged, and that is
    a normal state, not an error.
    """
    target = pathlib.Path(path)
    try:
        stat = target.stat()
    except OSError:
        return DocumentInfo(path=target, size_bytes=0, modified=None, exists=False)
    return DocumentInfo(
        path=target,
        size_bytes=stat.st_size,
        modified=datetime.fromtimestamp(stat.st_mtime),
        exists=target.is_file(),
    )


def looks_like_document(path: pathlib.Path | str) -> bool:
    """Whether ``path`` is a readable document this window is willing to open.

    Checked here rather than in the drop handler so that the rule is the same one the
    file dialog's filter expresses, and so it can be tested without a window. A directory
    named ``things.xml`` is not a document, and neither is a file that is not there --
    both are things a user can drop by accident, and both have to be refused in a way the
    user can see.
    """
    target = pathlib.Path(path)
    lowered = target.name.lower()
    if not any(lowered.endswith(suffix) for suffix in DOCUMENT_SUFFIXES):
        return False
    return target.is_file()
