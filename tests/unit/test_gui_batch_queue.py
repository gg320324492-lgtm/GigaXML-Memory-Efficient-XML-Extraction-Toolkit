"""The batch queue's state machine.

**Why these are unit tests.** `batch_queue` does not import PySide6 and does not start
processes -- it is a list and a state machine, which is exactly the part that is worth
testing and exactly the part a display cannot check. The panel's half, spawning one
`CliProcess` per job, is checked by `test_gui_batch.py` against the panel's own source.
"""

from __future__ import annotations

import pathlib

from gigaxml.gui.batch_queue import BatchQueue, JobState


def test_a_new_job_waits(tmp_path: pathlib.Path) -> None:
    queue = BatchQueue()

    job = queue.add(tmp_path / "a.xml", tmp_path / "out-a")

    assert job.state is JobState.WAITING


def test_the_next_job_is_handed_out_once(tmp_path: pathlib.Path) -> None:
    """A second call while one is running returns nothing.

    This is the guard against running the same file twice: the state moves to RUNNING here
    rather than when the caller gets round to saying it started.
    """
    queue = BatchQueue()
    queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    queue.add(tmp_path / "b.xml", tmp_path / "out-b")

    first = queue.next_job()

    assert first is not None
    assert queue.next_job() is None


def test_the_queue_moves_on_when_a_job_ends(tmp_path: pathlib.Path) -> None:
    queue = BatchQueue()
    first = queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    second = queue.add(tmp_path / "b.xml", tmp_path / "out-b")

    queue.note_finished(queue.next_job(), ok=True)

    assert queue.next_job() is second
    assert first.state is JobState.DONE


def test_a_failed_job_does_not_stop_the_queue(tmp_path: pathlib.Path) -> None:
    """One bad document is not a reason to abandon the other nine."""
    queue = BatchQueue()
    bad = queue.add(tmp_path / "bad.xml", tmp_path / "out-bad")
    good = queue.add(tmp_path / "good.xml", tmp_path / "out-good")

    queue.note_finished(queue.next_job(), ok=False, detail="exit 1")

    assert bad.state is JobState.FAILED
    assert bad.detail == "exit 1"
    assert queue.next_job() is good


def test_the_queue_reports_itself_done_only_when_everything_has_stopped(tmp_path: pathlib.Path) -> None:
    queue = BatchQueue()
    queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    queue.add(tmp_path / "b.xml", tmp_path / "out-b")

    queue.note_finished(queue.next_job(), ok=True)
    assert queue.done is False

    queue.note_finished(queue.next_job(), ok=True)
    assert queue.done is True


def test_a_skipped_job_counts_as_stopped(tmp_path: pathlib.Path) -> None:
    """A source that is gone has stopped waiting. Counting it as pending would hang the queue."""
    queue = BatchQueue()
    job = queue.add(tmp_path / "gone.xml", tmp_path / "out")
    queue.skip(job, "source is gone")

    assert queue.done is True
    assert job.state is JobState.SKIPPED


def test_an_empty_queue_is_done(tmp_path: pathlib.Path) -> None:
    assert BatchQueue().done is True
    assert BatchQueue().next_job() is None


def test_cancelling_leaves_the_running_job_alone(tmp_path: pathlib.Path) -> None:
    """The running job is the panel's to stop -- it owns the process, this module does not."""
    queue = BatchQueue()
    running = queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    queue.add(tmp_path / "b.xml", tmp_path / "out-b")
    queue.add(tmp_path / "c.xml", tmp_path / "out-c")
    queue.next_job()

    queue.stop_after_current()

    assert running.state is JobState.RUNNING
    assert [job.state for job in queue.jobs[1:]] == [JobState.SKIPPED, JobState.SKIPPED]


def test_clearing_finished_keeps_what_failed(tmp_path: pathlib.Path) -> None:
    """A queue that tidied its failures away would take away the row anybody wants to see."""
    queue = BatchQueue()
    queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    bad = queue.add(tmp_path / "b.xml", tmp_path / "out-b")
    queue.note_finished(queue.next_job(), ok=True)
    queue.note_finished(queue.next_job(), ok=False, detail="boom")

    removed = queue.clear_finished()

    assert removed == 1
    assert [job.state for job in queue.jobs] == [JobState.FAILED]
    assert queue.jobs[0] is bad


def test_reset_puts_everything_back_to_waiting(tmp_path: pathlib.Path) -> None:
    queue = BatchQueue()
    queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    queue.add(tmp_path / "b.xml", tmp_path / "out-b")
    queue.note_finished(queue.next_job(), ok=False, detail="boom")

    queue.reset()

    assert [job.state for job in queue.jobs] == [JobState.WAITING, JobState.WAITING]
    assert all(job.detail == "" for job in queue.jobs)


def test_counts_include_states_with_none(tmp_path: pathlib.Path) -> None:
    """A missing key would be ambiguous between "none" and "nobody counted"."""
    queue = BatchQueue()
    queue.add(tmp_path / "a.xml", tmp_path / "out-a")

    tally = queue.counts()

    assert set(tally) == {state.value for state in JobState}
    assert tally["waiting"] == 1
    assert tally["running"] == 0


def test_a_row_is_what_a_table_shows(tmp_path: pathlib.Path) -> None:
    queue = BatchQueue()
    job = queue.add(tmp_path / "a.xml", tmp_path / "out-a")
    queue.note_finished(queue.next_job(), ok=False, detail="exit 1")

    assert job.as_row() == ("a.xml", "failed", "exit 1")
    assert queue.rows() == [("a.xml", "failed", "exit 1")]
