# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the desktop application.

Run from the repository root::

    python -m PyInstaller packaging/gigaxml.spec --noconfirm

**One executable, not two** (brief 1.1 option (a)). The window finds its CLI by asking
``sys.executable`` what it is, so there is no "the other binary is missing" failure mode and
no second path to get wrong on a machine where the install directory is read-only. The cost
is that :mod:`gigaxml.gui.app` has to dispatch -- ``gigaxml-gui.exe extract ...`` runs the
CLI, ``gigaxml-gui.exe`` opens the window -- which it does, and which the smoke test in the
pipeline exercises on every platform.

**onedir, not onefile.** A onefile build unpacks itself to a temporary directory on every
launch, which costs a second or two before a window appears and re-extracts per run. This is
an application somebody double-clicks while holding a drag, so the startup is the thing being
protected. The installer packages the directory; nothing has to extract at run time.

**The version in the Windows file properties is read from the package, not written here.**
Two hand-kept copies of a version number is one too many, and the one that would rot is the
one nobody looks at.
"""

import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

#: ``SPECPATH`` is the directory holding *this spec file*, not the repository root -- it is
#: ``packaging/``. Walking up one level is the whole of the difference, and getting it wrong
#: fails as a confusing ``G:\pyproject.toml`` rather than as an obvious complaint.
ROOT = Path(SPECPATH).resolve().parent
ASSETS = ROOT / "assets"

#: Read the version from the one place it is defined, so Gate 6 is true by construction
#: rather than by a second edit someone has to remember.
_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
VERSION = str(_version["project"]["version"])

# A four-part number, which is what the resource block needs. "0.1.0" becomes 0.1.0.0.
VERSION_TUPLE = tuple(int(part) for part in VERSION.split(".")[:4]) + (0,) * (
    4 - len(VERSION.split("."))
)

block_cipher = None

a = Analysis(
    # The single entry point: it is the window, the CLI, or whichever one the arguments
    # select. See gigaxml.gui.app.
    [str(ROOT / "src" / "gigaxml" / "gui" / "app.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    # PySide6 is a large package and its hooks pull in the modules that are actually
    # imported; collecting submodules as well is what keeps a lazily-imported panel from
    # turning into a "module not found" dialog on a machine with no source.
    hiddenimports=[
        *collect_submodules("gigaxml"),
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Never ship a test framework or a second Qt binding into a user's install
        # directory. PyQt5 and PySide2 are pulled in by some PySide6 hooks on Linux.
        "PyQt5",
        "PyQt6",
        "PySide2",
        "pytest",
        "pytestqt",
        "tkinter",
        "unittest",
    ],
    # ``--version`` is answered by our own parser, and this is the flag that would print
    # PyInstaller's note instead if it were ever passed through.
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="gigaxml-gui",
    debug=False,
    # Warnings are collected into the build log rather than guessed away; a missing module
    # here is exactly the thing that shows up as a broken window on somebody's machine.
    strip=False,
    upx=False,  # UPX-packed Qt DLLs are a known source of startup failures
    console=False,  # a window, not a console
    disable_windowed_traceback=False,
    icon=str(ASSETS / "gigaxml.ico"),
    version=str(ASSETS / "gigaxml-version-info.txt")
    if (ASSETS / "gigaxml-version-info.txt").is_file()
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="gigaxml-gui",
)
