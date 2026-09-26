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


def main() -> int:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(metadata["project"]["version"])

    # Four parts, because the resource block wants a quad. "0.9.0" becomes 0.9.0.0.
    parts = [int(part) for part in version.split(".") if part.isdigit()]
    while len(parts) < 4:
        parts.append(0)
    quad = tuple(parts[:4])

    ASSETS.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(TEMPLATE.format(quad=quad, version=version), encoding="utf-8")
    print(f"wrote {TARGET.relative_to(ROOT)} (version {version}, quad {quad})")

    if version != metadata["project"]["version"]:  # pragma: no cover - defensive
        print("version mismatch", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
