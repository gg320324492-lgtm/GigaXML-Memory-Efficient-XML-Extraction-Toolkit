"""Extraction via the GigaXML CLI.

The task the other scripts in this suite perform by hand is, in GigaXML, a configuration
file and one command. The config below is the whole of the field specification -- the
same six fields, the same order -- and the CLI handles streaming, CSV writing, the
run report, and the atomic output move.

Usage::

    python benchmarks/compare/via_gigaxml.py <input.xml> <output.csv>

The script locates the installed ``gigaxml`` CLI the same way a user would: on PATH if
it is installed, otherwise the source checkout's own package (``python -m gigaxml.cli``).
"""

from __future__ import annotations

import pathlib
import subprocess
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


def gigaxml_command(input_path: str, output_path: str, config_path: str) -> list[str]:
    """The CLI invocation, preferring an installed ``gigaxml`` over the source tree."""
    from shutil import which

    installed = which("gigaxml")
    if installed:
        return [installed, "extract", input_path, "-c", config_path, "-o", output_path]
    return [
        sys.executable,
        "-m",
        "gigaxml.cli",
        "extract",
        input_path,
        "-c",
        config_path,
        "-o",
        output_path,
    ]


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
        completed = subprocess.run(
            gigaxml_command(argv[1], argv[2], str(config_path)),
            capture_output=True,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr)
        return completed.returncode or 1
    print(completed.stdout.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
