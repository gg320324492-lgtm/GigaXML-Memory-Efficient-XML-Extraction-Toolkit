"""The batch queue: which files are waiting, which are running, and how each one ended.

**What this module is not.** It does not start processes, and it does not know what a
process is. It is a list of jobs and their states -- that is all. The panel reads the next
job from here, starts a `CliProcess` for it, and tells this module how it went.

**Why the split matters.** "Batch" must not become "one process doing many files". The
CLI's memory guarantees are per-run: a run holds one document's parsed records in memory
and releases them as it writes. Feeding it several documents would either hold them all at
once -- which is the thing this project exists to avoid -- or require a second extraction
loop inside the interface, which would be a second implementation of the thing being
tested. So the queue is bookkeeping and the panel is the thing that spawns, once per file,
and that is visible in the panel's own code.

**Nothing here may import PySide6.** The state machine -- what happens when a job fails,
what the next job is, whether the queue is done -- is the part worth testing, and none of
it needs a display.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from enum import Enum

__all__ = [
    "JobState",
    "Job",
    "BatchQueue",
]


class JobState(str, Enum):
    """Where a job has got to.

    A string enum so the state can go straight into a table cell or a JSON file without a
    translation table in between, which is one more place for the two to disagree.
    """

    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Job:
    """One file to process, and what became of it.

    Args:
        source: the document to extract from.
        output: where its records go. Each job has its own, because two jobs writing the
            same directory would have their manifests and partial files interleave.
        state: starts waiting; only :class:`BatchQueue` moves it.
        detail: a short human-readable note -- a failure message, or why it was skipped.
    """

    source: pathlib.Path
    output: pathlib.Path
    state: JobState = JobState.WAITING
    detail: str = ""

    @property
    def name(self) -> str:
        return self.source.name

    @property
    def finished(self) -> bool:
        """Whether this job has stopped occupying the queue.

        ``SKIPPED`` counts: a job that was never going to run has stopped waiting, and a
        queue that kept counting it would never report itself done.
        """
        return self.state in (JobState.DONE, JobState.FAILED, JobState.SKIPPED)

    def as_row(self) -> tuple[str, str, str]:
        """``(name, state, detail)`` -- what a table shows for this job."""
        return (self.name, self.state.value, self.detail)


@dataclass
class BatchQueue:
    """An ordered list of jobs, at most one of which is running.

    Args:
        jobs: the jobs, in the order they were added.
    """

    jobs: list[Job] = field(default_factory=list)

    # -- building ----------------------------------------------------------

    def add(self, source: pathlib.Path | str, output: pathlib.Path | str) -> Job:
        """Append a job and return it, so the caller can keep a handle on the row it added."""
        job = Job(source=pathlib.Path(source), output=pathlib.Path(output))
        self.jobs.append(job)
        return job

    def clear_finished(self) -> int:
        """Drop every job that *succeeded*, and say how many went.

        Only the user asks for this, and only the successes go: a queue that tidied its
        failures away would take away the record of what went wrong, which is the one row
        anybody wants to look at afterwards. A job that was skipped is kept for the same
        reason -- it says why it never ran.
        """
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.state is not JobState.DONE]
        return before - len(self.jobs)

    def reset(self) -> None:
        """Put everything back to waiting, so the same list can be run again."""
        for job in self.jobs:
            job.state = JobState.WAITING
            job.detail = ""

    # -- driving -----------------------------------------------------------

    def next_job(self) -> Job | None:
        """The next job to run, or ``None`` when there is nothing to do.

        At most one job is handed out at a time, and it is marked ``RUNNING`` here rather
        than by the caller -- so a panel that forgets to say "I started it" cannot end up
        running the same file twice.
        """
        if self.running is not None:
            return None
        for job in self.jobs:
            if job.state is JobState.WAITING:
                job.state = JobState.RUNNING
                return job
        return None

    def note_finished(self, job: Job, *, ok: bool, detail: str = "") -> None:
        """Record how a job ended. The panel calls this when its child process exits."""
        job.state = JobState.DONE if ok else JobState.FAILED
        job.detail = detail

    def skip(self, job: Job, detail: str = "") -> None:
        """Record that a job was not run -- a source that is gone, say."""
        job.state = JobState.SKIPPED
        job.detail = detail

    def stop_after_current(self) -> None:
        """Cancel everything that has not started, leaving the running job alone.

        The running job is left because stopping it is the panel's business -- it owns the
        process, and this module does not know there is one.
        """
        for job in self.jobs:
            if job.state is JobState.WAITING:
                job.state = JobState.SKIPPED
                job.detail = "cancelled"

    # -- reading -----------------------------------------------------------

    @property
    def running(self) -> Job | None:
        """The job in flight, if any."""
        return next((job for job in self.jobs if job.state is JobState.RUNNING), None)

    @property
    def done(self) -> bool:
        """Whether every job has stopped, one way or another."""
        return all(job.finished for job in self.jobs)

    def counts(self) -> dict[str, int]:
        """How many jobs are in each state, including states with none.

        Every state is present, so a caller building a summary does not have to guess
        whether a missing key means zero or means nobody counted.
        """
        tally = {state.value: 0 for state in JobState}
        for job in self.jobs:
            tally[job.state.value] += 1
        return tally

    def rows(self) -> list[tuple[str, str, str]]:
        """Every job as a table row, in queue order."""
        return [job.as_row() for job in self.jobs]

    def __len__(self) -> int:
        return len(self.jobs)
