"""G8: the batch panel's memory must not grow with the length of the queue.

**Why this needed new work.** ``tests/integration/test_gui_batch.py`` already proves the
scheduling half -- three documents, three processes, no argument list naming two documents
-- but it proves it with ``_FakeProcess``, a stub that allocates nothing. A stub cannot
speak to the question this file asks, which is what happens to the *window's* memory as the
queue gets longer. So everything here is real: real documents on disk, a real
:class:`CliProcess` per job, real child processes, and RSS sampled from outside.

**The comparison is additive, never a ratio.** Twelve documents do cost more than three --
each one is a row in a table, a :class:`Job` and a finished result. The question is whether
that cost is *the queue's* or the *documents'*, and a ratio cannot tell them apart, because a
ratio is dominated by whatever the baseline happened to be. ``12 <= 3 + 8 MiB`` can. This is
the 4b rule: a small constant is an additive property, and asserting on a ratio would let a
linear implementation through.
"""

from __future__ import annotations

import pathlib
import shutil
import threading
import time
from typing import Final

import psutil
import pytest

pytest.importorskip("PySide6")

from gigaxml.gui.panels.batch import BatchPanel

#: How much more memory a twelve-long queue may cost than a three-long one. Rows and job
#: objects are small; a leak that keeps every finished result, every child handle or every
#: parsed document would be far larger than this.
QUEUE_GROWTH_BUDGET_MB: Final = 8.0

#: How often the sampler looks while the queue drains.
_SAMPLE_INTERVAL_S: Final = 0.02

#: A ceiling on the whole thing, so a hung child fails the test instead of the session.
_DRAIN_TIMEOUT_S: Final = 900.0

#: One record, without an XML declaration. The declaration belongs to the document, not to
#: every record -- repeating it produces a document that is not well-formed, which is a
#: mistake in the test rather than anything about the panel.
_RECORD: Final = (
    '    <product id="{index}" type="physical">\n'
    "      <name>Widget {index}</name>\n"
    "      <price>{price}</price>\n"
    "    </product>\n"
)

_DOCUMENT: Final = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<catalog generated-by="gigaxml" seed="0">\n'
    "  <products>\n"
    "{records}"
    "  </products>\n"
    "</catalog>\n"
)

_CONFIG: Final = """
record: /catalog/products/product
fields:
  product_id:
    path: "@id"
  name:
    path: name
"""


def _write_documents(directory: pathlib.Path, count: int, records_each: int) -> list[pathlib.Path]:
    """Write ``count`` small but genuinely distinct documents.

    Distinct on purpose: twelve copies of one file would let the OS share a page cache and
    the child's work would be unrealistically cheap, which is the sort of thing that makes
    a memory test pass for the wrong reason.
    """
    directory.mkdir(parents=True, exist_ok=True)
    written: list[pathlib.Path] = []
    for index in range(count):
        body = "".join(
            _RECORD.format(index=f"{index}-{record}", price=f"{record}.50")
            for record in range(records_each)
        )
        target = directory / f"doc{index:02d}.xml"
        target.write_text(_DOCUMENT.format(records=body), encoding="utf-8")
        written.append(target)
    return written


def _drain(panel: BatchPanel, *, timeout_s: float = _DRAIN_TIMEOUT_S) -> bool:
    """Turn the event loop until the queue is done, sampling the window's RSS as it goes.

    The panel advances on a timer, so the loop has to keep turning for anything to happen.
    This is the same loop a user's window runs, and the panel is the shipped one -- the only
    concession to testability is that we add documents through the public method rather than
    through the file dialog.
    """
    from PySide6.QtWidgets import QApplication

    application = QApplication.instance()
    if application is None:  # pragma: no cover - the qapp fixture builds one
        raise RuntimeError("no QApplication")
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        application.processEvents()
        if panel.queue().done and not panel.is_running():
            application.processEvents()
            return True
        time.sleep(_SAMPLE_INTERVAL_S)
    return False


def _measure_queue_size(
    tmp_path: pathlib.Path,
    *,
    documents: int,
    records_each: int = 200,
    label: str,
) -> tuple[float, list[int], int]:
    """Run a queue of ``documents`` real jobs and report the window's memory growth.

    Returns:
        ``(delta_mb, pids, rows_written)`` -- the RSS growth from just-before-``start`` to
        the peak seen while draining, the child pids in the order they were started, and the
        total rows the queue actually produced.
    """
    from PySide6.QtWidgets import QApplication

    work = tmp_path / label
    if work.exists():
        # Windows will not let a later run reuse a directory whose handles are still open,
        # and a leftover from a previous attempt is not this test's business to keep.
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    sources = _write_documents(work / "in", documents, records_each)
    config = work / "config.yaml"
    config.write_text(_CONFIG, encoding="utf-8")

    application = QApplication.instance()
    if application is None:  # pragma: no cover - the qapp fixture builds one
        raise RuntimeError("no QApplication")

    panel = BatchPanel()
    panel._config.setText(str(config))
    panel._output.setText(str(work / "out"))
    panel.add_documents(sources)

    baseline = psutil.Process().memory_info().rss / (1024 * 1024)
    peak = baseline

    # Pids are recorded by hooking the one method that creates a child, not by sampling on a
    # timer. A sampler can miss a job that lives inside one tick, and worse, a sampler that
    # raises takes the evidence with it -- which is exactly what happened the first time
    # this was written. Counting at the point of creation cannot miss anything.
    pids: list[int] = []
    original_start_next = panel._start_next

    def _record_pid(panel: BatchPanel = panel) -> None:
        original_start_next()
        child = panel._process
        if child is not None and child.pid is not None:
            pids.append(child.pid)

    panel._start_next = _record_pid  # type: ignore[method-assign]

    stop = threading.Event()

    def _sample() -> None:
        nonlocal peak
        process_handle = psutil.Process()
        while not stop.wait(_SAMPLE_INTERVAL_S):
            try:
                current = process_handle.memory_info().rss / (1024 * 1024)
            except psutil.Error:  # pragma: no cover
                return
            if current > peak:
                peak = current

    sampler = threading.Thread(target=_sample, daemon=True)
    sampler.start()
    try:
        panel.start()
        finished = _drain(panel)
    finally:
        stop.set()
        sampler.join(timeout=2.0)

    # The pids are recorded in start order, but they are OS-assigned and are deliberately
    # *not* compared for order: pid 4000 can easily be followed by pid 3100. The claim being
    # made is "one process per document, none reused", which is the distinctness check below.

    # Rows are counted from the output files, not from the table: the table shows state, and
    # "state is done" is not the same claim as "the bytes were written".
    written = 0
    out_root = work / "out"
    if out_root.exists():
        for csv in out_root.rglob("*.csv"):
            with csv.open(encoding="utf-8", newline="") as handle:
                written += max(0, sum(1 for _ in handle) - 1)

    # Two independent counts of "how many processes ran", because each alone has a way to be
    # wrong: the sampled pids can miss a job that lived inside one tick, and the queue's own
    # tally cannot tell a reused child from a fresh one. They have to agree, and the pids
    # have to be distinct.
    finished_jobs = panel.queue().counts()
    assert finished_jobs["done"] + finished_jobs["failed"] == documents, (
        f"queue reported {finished_jobs}, expected {documents} jobs accounted for"
    )
    assert len(pids) == documents, (
        f"sampled {len(pids)} distinct child pids, expected {documents}: {pids}"
    )
    assert len(set(pids)) == documents, f"a child was reused across jobs: {pids}"

    panel.shutdown()
    del panel
    application.processEvents()

    if not finished:  # pragma: no cover - reported as a failure below, not raised here
        pytest.fail(f"queue of {documents} did not finish within {_DRAIN_TIMEOUT_S}s")
    return peak - baseline, pids, written


@pytest.mark.performance
def test_queue_length_does_not_raise_window_memory(tmp_path: pathlib.Path, qapp) -> None:  # noqa: ANN001
    """Twelve documents must not cost meaningfully more memory than three.

    The two halves of the criterion are both here: the per-job process count (each pid
    distinct, so no child was reused or skipped) and the additive memory band.
    """
    # Requested for its side effect: pytest-qt builds the QApplication, and the panels below
    # are real widgets that need one. Nothing here calls into it.
    del qapp

    small_delta, small_pids, small_rows = _measure_queue_size(tmp_path, documents=3, label="small")
    big_delta, big_pids, big_rows = _measure_queue_size(tmp_path, documents=12, label="big")

    # The per-job process count and the row counts are asserted inside the helper, where the
    # queue's own tally and the sampled pids are cross-checked against each other. Here we
    # only restate them as the criterion, because this is the claim being made.

    # The queue really did the work; a queue that failed instantly would prove nothing.
    assert small_rows == 3 * 200, f"expected 600 rows from the small queue, wrote {small_rows}"
    assert big_rows == 12 * 200, f"expected 2400 rows from the big queue, wrote {big_rows}"

    # Additive band, never a ratio -- see the module docstring.
    assert big_delta <= small_delta + QUEUE_GROWTH_BUDGET_MB, (
        f"queue memory grew with length: 3 docs cost {small_delta:.2f} MiB, "
        f"12 docs cost {big_delta:.2f} MiB, budget {QUEUE_GROWTH_BUDGET_MB} MiB"
    )

    print(
        "\n".join(
            [
                "",
                "G8 queue memory (real CliProcess per job, real children):",
                f"  3 docs:  delta={small_delta:.2f} MiB  rows={small_rows}  pids={small_pids}",
                f"  12 docs: delta={big_delta:.2f} MiB  rows={big_rows}  pids={big_pids}",
                f"  growth = {big_delta - small_delta:.2f} MiB"
                f"  budget = {QUEUE_GROWTH_BUDGET_MB} MiB",
            ]
        )
    )


if __name__ == "__main__":  # pragma: no cover - manual run
    raise SystemExit(pytest.main([__file__, "-s", "-q"]))
