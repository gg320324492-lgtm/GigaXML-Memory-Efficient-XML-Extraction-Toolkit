"""Write the Windows version resource, read from the one place the version is defined.

PyInstaller's ``version=`` takes a compiled resource script, and that file has to name the
version number as a literal. Writing it by hand is a second copy of a number that will rot
silently: the package says 0.9.0, the file properties say 0.1.0, and Gate 6 fails on a
machine nobody looked at until a user reports the wrong version in About.

So it is generated, and the generator is what the build runs. Same reasoning as the icon:
a build input that exists on exactly one machine is a build that only runs on that machine.

Run::

    python -m tools.make_version_info
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
TARGET = ASSETS / "gigaxml-version-info.txt"

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'040904B0',
        [StringStruct(u'CompanyName', u'GigaXML'),
         StringStruct(u'FileDescription', u'GigaXML - memory-efficient XML extraction'),
         StringStruct(u'FileVersion', u'{version}'),
         StringStruct(u'InternalName', u'gigaxml-gui'),
         StringStruct(u'LegalCopyright', u'MIT licensed.'),
         StringStruct(u'OriginalFilename', u'gigaxml-gui.exe'),
         StringStruct(u'ProductName', u'GigaXML'),
         StringStruct(u'ProductVersion', u'{version}')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [1033, 1200])])
  ]
)
"""


def version_info_version(version: str) -> str:
    """``version`` reduced to the digits-and-dots form a Windows version number takes.

    **Measured, not assumed.** ISCC 6 was run against a script carrying this project's
    own ``[Setup]`` shape, once per candidate, on 2026-10-03::

        VersionInfoVersion=2.0.0rc1    rc=2  "Value of [Setup] section directive
                                              VersionInfoVersion is invalid."
        VersionInfoVersion=2.0.0.rc1   rc=2  (the same error)
        VersionInfoVersion=2.0.0-rc1   rc=2  (the same error)
        VersionInfoVersion=2.0.0       rc=0  Successful compile
        VersionInfoVersion=2.0.0.0     rc=0  Successful compile
        VersionInfoVersion=2.0.0.1     rc=0  Successful compile

    So the accepted grammar is one to four components of digits separated by dots, and
    nothing else. At 2.0.0rc1 the packaging job does not build a wrong installer, it
    **fails to build one at all** -- which is the good failure, and only because the
    number happened to be pre-release. The first plain release would have compiled with
    a version nobody chose, which is the failure this exists to stop.

    This is the same reduction :func:`main` has always applied to build the resource
    quad, lifted into a function so the installer's ``VersionInfoVersion`` and the exe's
    resource come from one rule rather than two.
    """
    parts = [int(part) for part in version.split(".") if part.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    return ".".join(str(part) for part in parts[:4])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # The packaging workflow needs the digits-only form to hand ISCC, and it must not
    # re-derive it inside a shell string: a second copy of this rule is a second thing
    # to rot, and the YAML one would rot where no test can see it.
    parser.add_argument(
        "--print-version-info-version",
        action="store_true",
        help="print the digits-only version and exit, without writing any file",
    )
    args = parser.parse_args(argv)

    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(metadata["project"]["version"])

    if args.print_version_info_version:
        print(version_info_version(version))
        return 0

    # Four parts, because the resource block wants a quad. "0.9.0" becomes 0.9.0.0.
    quad = tuple(int(part) for part in version_info_version(version).split("."))

    ASSETS.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(TEMPLATE.format(quad=quad, version=version), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} (version {version}, quad {quad})")

    if version != metadata["project"]["version"]:  # pragma: no cover - defensive
        print("version mismatch", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
