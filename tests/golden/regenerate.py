"""Rewrite the golden expectations from the CLI's current behaviour.

A golden suite that cannot be regenerated is a golden suite that gets deleted the first
time a test fails for an unrelated reason. This is the tool that keeps the expectations
honest: it re-runs each case and rewrites the file, so a deliberate behaviour change is a
one-line command and a reviewable diff rather than a hand edit that silently weakens an
assertion.

Requires ``--update``. Without it the script does the work and prints what would change,
so "let me just re-record everything" is never a single keystroke away -- re-recording
destroys the only evidence that behaviour changed, and that is exactly the moment to want
to read the diff first.

Mirrors :mod:`tests.golden.test_cli_golden` case for case. If a test's steps change and
this does not, the tests fail -- which is the intended direction for the failure to go.

Usage::

    python tests/golden/regenerate.py             # report what would change
    python tests/golden/regenerate.py --update    # rewrite the expectations
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: A producer runs one case in a prepared directory and returns its exit code and the
#: text to freeze. Returning the code rather than assuming it is what keeps a refusal
#: from being recorded as a success.
Producer = Callable[[Path], "tuple[int, str]"]


def _cases() -> list[tuple[str, str, Producer]]:
    """Every case, as ``(golden filename, stream, producer)``.

    A producer takes the run directory and returns the stream's text. Declaring them in
    one list is what makes ``--update`` a complete operation rather than a collection of
    steps that can be half-remembered.
    """
    from tests.golden.conftest import normalize, run_cli

    def expect(result: subprocess.CompletedProcess[str], code: int, name: str) -> None:
        """Refuse to record a case whose exit code has moved.

        Re-recording is for a *deliberate* change. If the command now exits differently
        from what the test asserts, that is the change under review, not a detail to
        absorb -- so this stops and says so instead of writing the new answer over the
        old one.
        """
        if result.returncode != code:
            raise SystemExit(
                f"refusing to re-record {name}: it now exits {result.returncode}, where "
                f"the test asserts {code}.\n"
                "That is the change this suite exists to catch. Read it, then update the "
                "test's expected value and its golden file together."
            )

    def inspect_text(directory: Path) -> tuple[int, str]:
        result = run_cli("inspect", str(directory / "src.xml"), cwd=directory)
        expect(result, 0, "inspect_basic.txt")
        return result.returncode, normalize(result.stdout, tmp_path=directory)

    def inspect_namespace(directory: Path) -> tuple[int, str]:
        result = run_cli("inspect", str(directory / "ns.xml"), "--json", cwd=directory)
        expect(result, 0, "inspect_namespace.json")
        return result.returncode, normalize(result.stdout, tmp_path=directory)

    def extract_success(directory: Path) -> tuple[int, str]:
        result = run_cli(
            "extract",
            str(directory / "src.xml"),
            "-c",
            str(directory / "config.yaml"),
            "-o",
            str(directory / "out.csv"),
            cwd=directory,
        )
        expect(result, 0, "extract_success.json")
        return result.returncode, normalize(result.stdout, tmp_path=directory)

    def extract_failure(directory: Path) -> tuple[int, str]:
        result = run_cli(
            "extract",
            str(directory / "src.xml"),
            "-c",
            str(directory / "unmatched.yaml"),
            "-o",
            str(directory / "out.csv"),
            cwd=directory,
        )
        expect(result, 1, "extract_failure.txt")
        return result.returncode, normalize(result.stderr, tmp_path=directory)

    def sample_artifact(directory: Path) -> tuple[int, str]:
        output = directory / "sample.csv"
        result = run_cli(
            "sample",
            str(directory / "src.xml"),
            "-c",
            str(directory / "config.yaml"),
            "-n",
            "2",
            "-o",
            str(output),
            cwd=directory,
        )
        expect(result, 0, "sample.csv")
        # Not normalised: the artefact holds no path, and its CRLF endings are part of
        # what a caller opens.
        return result.returncode, output.read_bytes().decode("utf-8")

    def resume_error(directory: Path) -> tuple[int, str]:
        parts = directory / "parts"
        first = run_cli(
            "extract",
            str(directory / "src.xml"),
            "-c",
            str(directory / "config.yaml"),
            "-o",
            str(parts),
            "--checkpoint-every",
            "2",
            cwd=directory,
        )
        expect(first, 0, "resume_error.txt")
        manifest_path = parts / "checkpoint.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = 99
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        result = run_cli(
            "extract",
            str(directory / "src.xml"),
            "-c",
            str(directory / "config.yaml"),
            "-o",
            str(parts),
            "--checkpoint-every",
            "2",
            "--resume",
            cwd=directory,
        )
        expect(result, 1, "resume_error.txt")
        return result.returncode, normalize(result.stderr, tmp_path=directory)

    return [
        ("inspect_basic.txt", "stdout", inspect_text),
        ("inspect_namespace.json", "stdout", inspect_namespace),
        ("extract_success.json", "stdout", extract_success),
        ("extract_failure.txt", "stderr", extract_failure),
        ("sample.csv", "artifact", sample_artifact),
        ("resume_error.txt", "stderr", resume_error),
    ]


def main(argv: list[str] | None = None) -> int:
    """Re-record every expectation, or report what would change."""
    sys.path.insert(0, str(REPO_ROOT))
    from tests.golden.conftest import EXPECTED_DIR, build_inputs

    parser = argparse.ArgumentParser(description="Re-record the golden expectations.")
    parser.add_argument(
        "--update",
        action="store_true",
        help="write the expectations; without it, only report the differences",
    )
    args = parser.parse_args(argv)

    changed: list[str] = []
    for name, stream, produce in _cases():
        directory = Path(tempfile.mkdtemp(prefix="gigaxml-golden-"))
        try:
            build_inputs(directory)
            code, body = produce(directory)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

        text = f"# exit_code={code} stream={stream}\n{body}"
        target = EXPECTED_DIR / name
        existing = target.read_bytes().decode("utf-8") if target.is_file() else None
        if existing == text:
            continue
        changed.append(name)
        if args.update:
            target.parent.mkdir(parents=True, exist_ok=True)
            # newline="" keeps the CRLF of a sample artefact intact; the default would
            # rewrite every LF to this platform's separator and corrupt it.
            target.write_text(text, encoding="utf-8", newline="")

    if not changed:
        print("every expectation already matches the current behaviour")
        return 0
    verb = "rewrote" if args.update else "would rewrite"
    for name in changed:
        print(f"{verb} {name}")
    if not args.update:
        print("\nre-run with --update to record these")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
