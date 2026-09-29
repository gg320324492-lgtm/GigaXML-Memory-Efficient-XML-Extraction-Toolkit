"""Extraction via the GigaXML CLI.

The task the other scripts in this suite perform by hand is, in GigaXML, a configuration
file and one command. The config below is the whole of the field specification -- the
same six fields, the same order -- and the CLI handles streaming, CSV writing, the
run report, and the atomic output move.

**The CLI runs in this process, not as a child, and that is the whole point of this
file's shape.** The comparison runner samples the peak working set of the process it
launches; a child process's memory is not in its parent's counter, and a grandchild's
certainly is not. An earlier version of this script shelled out with
``subprocess.run``, so the extraction happened one process further down than the
sampler could see. The number it reported was the wrapper's own overhead -- 19.4 MB at
100 MB of input, at 1 GB, and at 4 GB, all of it equal to what an empty script through
the same wrapper costs. A figure that flat, and that exactly equal to the harness's own
overhead, is not a measurement of the extraction; it is the measurement apparatus
describing itself. So the CLI is executed here, in the sampled process, via
``runpy.run_module`` with ``run_name="__main__"`` -- the same module the console script
and ``python -m gigaxml.cli`` run, entered through its own ``__main__`` guard, with
``sys.argv`` set to what a user would type.

What this costs: the ~0.3 s of interpreter start-up a real ``gigaxml extract``
invocation pays, which the report had to list as an unfairness. What it buys: the
memory column means something. The two costs are not comparable -- one is a wall-clock
convenience, the other is the entire point of the tool.

Usage::

    python benchmarks/compare/via_gigaxml.py <input.xml> <output.csv>
"""

from __future__ import annotations

import pathlib
import runpy
import sys
import tempfile

#: The whole field specification, in GigaXML's config format. Switching the extraction to
#: different fields, types or output formats is an edit to *this text*, never to code.
CONFIG = """\
record: /catalog/products/product
fields:
  id:           {path: '@id'}
  type:         {path: '@type'}
  name:         {path: name}
  category:     {path: category}
  price:        {path: price, type: float}
  manufacturer: {path: manufacturer/name}
"""


def cli_argv(input_path: str, output_path: str, config_path: str) -> list[str]:
    """The arguments ``gigaxml extract`` parses -- everything after the program name."""
    return ["extract", input_path, "-c", config_path, "-o", output_path]


def run_cli(arguments: list[str]) -> int:
    """Run the CLI in this process and return its exit code.

    ``gigaxml.cli`` ends with ``raise SystemExit(main())`` under its ``__main__`` guard,
    so entering it as ``__main__`` gives the same behaviour as running it as a program,
    including the exit code -- caught here and returned rather than propagated, because
    this is a function the caller expects to come back from.
    """
    saved_argv = sys.argv
    sys.argv = ["gigaxml", *arguments]
    # runpy re-executes the module rather than returning a cached one; without this it
    # finds any earlier import and warns instead of running the code again.
    sys.modules.pop("gigaxml.cli", None)
    try:
        runpy.run_module("gigaxml.cli", run_name="__main__")
    except SystemExit as exit_exc:
        return exit_exc.code or 0
    finally:
        sys.argv = saved_argv
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: {argv[0]} <input.xml> <output.csv>", file=sys.stderr)
        return 2
    repo_config = (
        pathlib.Path(__file__).resolve().parents[2]
        / "benchmarks"
        / "compare"
        / "gigaxml-config.yaml"
    )
    with tempfile.TemporaryDirectory() as work:
        config_path = pathlib.Path(work) / "config.yaml"
        config_path.write_text(CONFIG, encoding="utf-8")
        if repo_config.is_file():  # the committed config is the one the report describes
            config_path.write_text(repo_config.read_text(encoding="utf-8"), encoding="utf-8")
        return run_cli(cli_argv(argv[1], argv[2], str(config_path)))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
