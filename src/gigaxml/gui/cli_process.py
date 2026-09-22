"""Driving the CLI as a child process. No Qt, no window, no blocking.

**Why a child process at all.** The whole point of this project is that a multi-gigabyte
document is processed in bounded memory. Parsing it inside the window's process would put
the document in the window's memory and make that claim false the moment somebody opened
the GUI. So the window never sees the XML: it starts the CLI, reads what the CLI prints,
and shows that.

**Nothing in this module may import PySide6.** The plumbing here -- which command line,
how a line is split, what a non-zero exit means, whether the child really died -- is what
breaks in practice, and it can only be tested properly without a window in the way. The
same is true of :mod:`gigaxml.gui.progress`, which this module uses.

**Why threads rather than Qt's own machinery.** A Qt-free module cannot use signals, so
reading happens on a thread and results are handed back through callbacks. Those callbacks
run on the reader thread, not the UI thread; the Qt layer is responsible for hopping back.
That is stated here because getting it wrong is how a GUI freezes without anyone noticing
until a large file is opened.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gigaxml.gui.progress import Progress, parse_line


def cli_command(args: Sequence[str]) -> list[str]:
    """The command line that runs the CLI in this same interpreter.

    ``python -m gigaxml`` cannot work -- there is no package ``__main__`` -- and the
    console script is a file beside the interpreter that may not be on ``PATH``. Running
    ``gigaxml.cli`` as a module needs only the interpreter, which is what makes it keep
    working once this application is frozen into an executable: ``sys.executable`` is then
    the frozen binary, and a module launch is not available, so the frozen build passes
    its own path instead. See :func:`cli_command_for`.
    """
    return [sys.executable, "-m", "gigaxml.cli", *args]


def cli_command_for(executable: str, args: Sequence[str]) -> list[str]:
    """As :func:`cli_command`, but with an explicit interpreter.

    A frozen application has no separate CLI to invoke, so ``frozen`` selects the single
    executable and lets the CLI's own argument parsing sort out which subcommand was
    meant.
    """
    return [executable, *args]


@dataclass
class RunResult:
    """What a finished run produced.

    ``stderr_lines`` is kept in full, including the warnings that were not progress.
    Losing them is how a GUI ends up saying "it failed" with nothing to show the user,
    and the CLI's messages are written to be read.
    """

    exit_code: int
    summary: dict[str, object] | None = None
    stderr_lines: list[str] = field(default_factory=list)
    killed: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.killed

    @property
    def warnings(self) -> list[str]:
        """The stderr lines that were not progress: what the CLI wanted to say."""
        return [line for line in self.stderr_lines if parse_line(line) is None]


class CliProcess:
    """One CLI invocation, driven from a background thread.

    Start it, then wait for :meth:`join` or watch the callbacks. Nothing here blocks the
    caller's thread: :meth:`start` returns immediately and the reading happens elsewhere.

    Args:
        args: the CLI arguments, without the program name.
        on_progress: called for each progress line, from the reader thread.
        on_stderr: called for each stderr line that is not progress, from the reader
            thread. Warnings arrive here.
        on_finished: called once with the :class:`RunResult`, from the reader thread.
    """

    def __init__(
        self,
        args: Sequence[str],
        *,
        cwd: Path | str | None = None,
        on_progress: Callable[[Progress], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        on_finished: Callable[[RunResult], None] | None = None,
    ) -> None:
        self._args = list(args)
        self._cwd = str(cwd) if cwd is not None else None
        self._on_progress = on_progress
        self._on_stderr = on_stderr
        self._on_finished = on_finished
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._result: RunResult | None = None
        self._killed = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Launch the child and begin reading. Returns immediately."""
        if self._process is not None:
            raise RuntimeError("this process has already been started")
        self._process = subprocess.Popen(
            cli_command(self._args),
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # line buffered, so a progress line arrives when it is written
        )
        self._reader = threading.Thread(target=self._read, name="gigaxml-cli-reader", daemon=True)
        self._reader.start()

    def _read(self) -> None:
        """Read both pipes to the end. Runs on the reader thread."""
        assert self._process is not None
        process = self._process
        stderr_lines: list[str] = []

        # stdout first, on this thread, while stderr is drained on another: reading them
        # in sequence would deadlock as soon as one pipe filled up, and the summary is
        # small enough that holding it here costs nothing.
        def drain_stderr() -> None:
            assert process.stderr is not None
            for line in process.stderr:
                stderr_lines.append(line)
                progress = parse_line(line)
                if progress is not None:
                    if self._on_progress is not None:
                        self._on_progress(progress)
                elif self._on_stderr is not None:
                    self._on_stderr(line.rstrip("\n"))

        drainer = threading.Thread(target=drain_stderr, name="gigaxml-cli-stderr", daemon=True)
        drainer.start()

        assert process.stdout is not None
        summary_text = process.stdout.read()
        process.wait()
        drainer.join()

        summary = _parse_summary(summary_text)
        result = RunResult(
            exit_code=process.returncode,
            summary=summary,
            stderr_lines=stderr_lines,
            killed=self._killed.is_set(),
        )
        with self._lock:
            self._result = result
        if self._on_finished is not None:
            self._on_finished(result)

    def kill(self) -> None:
        """Stop the child and wait until nothing more will arrive from it.

        Waiting matters twice over, and the second one used to be missed.

        Returning while the child is still running would let the caller read the output
        directory before the CLI has finished tidying it, and would make "cancelled" mean
        "we stopped listening".

        And returning before the reader thread has finished would mean ``on_finished`` had
        not been called yet. **"The child is gone" and "the callback has been delivered" are
        different statements.** A caller that cancels one run and starts another needs the
        first one's callback to have happened by then; otherwise the two answers race for
        the same slot, and the stale one can arrive last and be the one that is kept. So the
        join is unconditional -- it does not depend on the child having been alive when this
        was called, which is what the old ``poll() is not None`` early return got wrong.

        That is safe only because **no ``on_finished`` callback calls this method**. The
        callback runs on the reader thread, and joining the current thread is not something
        Python will do: it raises ``RuntimeError: cannot join current thread`` rather than
        deadlocking. Every callback in ``gigaxml.gui`` records its result and sets a flag --
        see ``_note_finished`` in the execution, preview and structure panels -- and
        ``test_killing_from_the_callback_fails_loudly_rather_than_stalling`` pins that down.

        **Because the callback has run by the time this returns, a caller that cancels
        should clear its "handled" flag *after* calling this, not before.** Clearing it
        first is cleared again by the callback, and the line ends up reading as "forget
        the previous run" while doing nothing -- which is what the two ``_cancel_*``
        methods in the structure and field panels used to do.
        """
        self._killed.set()
        process = self._process
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if self._reader is not None:
            self._reader.join(timeout=10)

    def join(self, timeout: float | None = None) -> RunResult | None:
        """Wait for the reader thread. ``None`` if it is still going."""
        if self._reader is not None:
            self._reader.join(timeout=timeout)
        with self._lock:
            return self._result

    # -- state -------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    @property
    def returncode(self) -> int | None:
        """The child's exit code, or ``None`` while it is still running."""
        process = self._process
        return None if process is None else process.poll()

    @property
    def result(self) -> RunResult | None:
        with self._lock:
            return self._result

    @property
    def pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid


def _parse_summary(text: str) -> dict[str, object] | None:
    """The CLI's stdout is one JSON object. Anything else means it did not get that far."""
    stripped = text.strip()
    if not stripped:
        return None
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def run_to_completion(args: Sequence[str], *, cwd: Path | str | None = None) -> RunResult:
    """Run the CLI and wait. For short commands and for tests; never for the UI thread.

    The window uses :class:`CliProcess` instead. This exists because a lot of the CLI is
    fast enough that blocking is fine, and pretending otherwise would make the tests
    harder to read than the code they cover.
    """
    process = CliProcess(args, cwd=cwd)
    process.start()
    result = process.join()
    assert result is not None  # join() without a timeout always waits for the thread
    return result


def progress_lines(lines: Iterable[str]) -> list[Progress]:
    """Every progress line in ``lines``, in order. Convenience for tests and callers."""
    parsed = (parse_line(line) for line in lines)
    return [item for item in parsed if item is not None]
