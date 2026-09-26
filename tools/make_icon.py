"""Draw the application icon, at every size the three platforms ask for.

**Why this is a script and not a checked-in binary.** An icon is a build input, and a
build input that only one machine has is a build that only one machine can run -- the
macOS and Linux runners would each need the same ``.icns`` and ``.png`` produced here on
a Windows box. Generating them means every platform's icon is reproducible from the same
source, and the source is readable.

**What the mark says.** The two brackets are XML, and the three rules under them are what
an extraction turns it into: a document above, a table below. It is drawn from primitives
rather than composed from a font so that it stays sharp at 16 px, where a glyph drawn at
1024 and scaled down turns to mush.

Run::

    python -m tools.make_icon            # writes assets/gigaxml.{png,ico,icns}
    python -m tools.make_icon --check    # verify they exist and are current

Requires Pillow, which is a build-time dependency only -- nothing here is imported at
runtime, and nothing here is packaged into the application.
"""

from __future__ import annotations

import argparse
import pathlib
import struct
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover - a build-time tool, absent in a clean env
    print("This script needs Pillow: pip install pillow", file=sys.stderr)
    raise SystemExit(2) from None

ASSETS = pathlib.Path(__file__).resolve().parent.parent / "assets"

#: Drawn once at this size and downscaled. Big enough that a 16 px result has real edges.
MASTER = 1024

#: The sizes Windows wants in a .ico. Including 256 because that is what Explorer shows
#: at large sizes, and it is the one people screenshot.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)

#: The sizes the macOS icon set is built from. icns wants the square ones; the retina
#: variants are the same images at 2x, which icns encodes by pixel dimension.
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)

# A deep blue that stays legible on both light and dark taskbars, and a warm accent for
# the table rules so the two halves of the mark separate at small sizes.
INK = (17, 24, 39, 255)
ACCENT = (56, 148, 232, 255)
PAPER = (255, 255, 255, 255)

RADIUS_RATIO = 0.22


def _draw_master() -> Image.Image:
    """The mark at :data:`MASTER` pixels square, with transparency outside the tile."""
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    inset = int(MASTER * 0.06)
    box = (inset, inset, MASTER - inset, MASTER - inset)
    draw.rounded_rectangle(
        box,
        radius=int(MASTER * RADIUS_RATIO),
        fill=INK,
    )

    # The document half: a pair of angle brackets, drawn as strokes so the corners stay
    # sharp. Each bracket is two lines meeting at a point, which is how a real `<` looks.
    top, bottom = int(MASTER * 0.26), int(MASTER * 0.56)
    left = int(MASTER * 0.34)
    depth = int(MASTER * 0.10)
    thickness = int(MASTER * 0.055)
    draw.line(
        [(left + depth, top), (left, (top + bottom) // 2), (left + depth, bottom)],
        fill=PAPER,
        width=thickness,
        joint="curve",
    )
    right = MASTER - left
    draw.line(
        [(right - depth, top), (right, (top + bottom) // 2), (right - depth, bottom)],
        fill=PAPER,
        width=thickness,
        joint="curve",
    )

    # The table half: three rules under the brackets, the middle one shorter, which is
    # what a row of extracted records looks like rather than a solid block.
    rule_top = int(MASTER * 0.66)
    gap = int(MASTER * 0.085)
    rule_x0 = int(MASTER * 0.28)
    rule_x1 = MASTER - rule_x0
    for index, fraction in enumerate((1.0, 0.72, 1.0)):
        y = rule_top + index * gap
        draw.line(
            [(rule_x0, y), (int(rule_x0 + (rule_x1 - rule_x0) * fraction), y)],
            fill=ACCENT if index != 1 else PAPER,
            width=int(MASTER * 0.042),
        )

    return image


def _downscale(master: Image.Image, size: int) -> Image.Image:
    """One size, with a floor so a 16 px icon does not turn to noise.

    LANCZOS rather than the default: at these sizes the difference between a good and a
    muddy downscale is visible in the taskbar, which is where people actually see it.
    """
    return master.resize((size, size), Image.LANCZOS)


def _write_ico(master: Image.Image, target: pathlib.Path) -> None:
    """A multi-resolution .ico.

    **One image and a ``sizes`` list, not ``append_images``.** ``append_images`` is for
    formats that accept a sequence of images; for .ico Pillow downsamples the single source
    into each size itself, and passing the pre-scaled images just produces a file whose
    only entry is the first one. Measured: an ``append_images`` version wrote a 747-byte
    .ico containing a single 16x16 image -- smaller than the 32x32 PNG beside it, which is
    how the mistake showed up. Saving from the master with ``sizes=`` is both simpler and
    the only thing that actually populates the table Explorer reads.
    """
    master.save(
        target,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
    )


def _write_pngs(master: Image.Image) -> list[pathlib.Path]:
    """The square sizes Linux and any other platform wants, named by pixel size."""
    written = []
    for size in ICNS_SIZES:
        target = ASSETS / f"gigaxml-{size}.png"
        _downscale(master, size).save(target, format="PNG")
        written.append(target)
    return written


def _write_icns(master: Image.Image, target: pathlib.Path) -> None:
    """An .icns, written by hand because Pillow has no .icns encoder.

    The container is a simple typed-chunk format: an ``icns`` magic, a total length, then
    per-entry a type code, a length and the raw PNG. ``ic07`` is 128x128 PNG, ``ic08`` is
    256, ``ic09`` 512, ``ic10`` 1024, ``ic11`` 32 (retina), ``ic12`` 64 (retina),
    ``ic13`` 256 (retina), ``ic14`` 512 (retina), and ``icp4``/``icp5`` 16/32. Writing it
    here rather than shelling out to ``iconutil`` means macOS icons are buildable from any
    platform, which is the same reason the whole file is a script.

    The 1024 entry is the ``ic10`` one; older tools read the largest they recognise, so the
    usual behaviour is that a 1024 source ends up displayed at whatever size is asked for.
    """
    by_type = {
        "icp4": 16,
        "icp5": 32,
        "ic11": 32,
        "ic12": 64,
        "ic07": 128,
        "ic08": 256,
        "ic09": 512,
        "ic10": 1024,
    }
    import io

    chunks = bytearray()
    for code, size in by_type.items():
        buffer = io.BytesIO()
        _downscale(master, size).save(buffer, format="PNG")
        data = buffer.getvalue()
        chunks += code.encode("ascii") + struct.pack(">I", len(data) + 8) + data

    target.write_bytes(b"icns" + struct.pack(">I", len(chunks) + 8) + bytes(chunks))


def build() -> list[pathlib.Path]:
    """Write every icon this project's three platforms need. Returns what was written."""
    ASSETS.mkdir(parents=True, exist_ok=True)
    master = _draw_master()

    written: list[pathlib.Path] = []

    png_master = ASSETS / "gigaxml.png"
    master.save(png_master, format="PNG")
    written.append(png_master)

    written += _write_pngs(master)

    ico = ASSETS / "gigaxml.ico"
    _write_ico(master, ico)
    written.append(ico)

    icns = ASSETS / "gigaxml.icns"
    _write_icns(master, icns)
    written.append(icns)

    return written


def check() -> int:
    """Report whether the icon files are present **and hold what they claim to**.

    Presence alone is not the check. A first version of this wrote a 747-byte .ico that
    existed, had the right name, and contained exactly one 16x16 image -- every "does the
    file exist" test passed while Explorer showed a 16 px icon scaled up to look broken. So
    the .ico's size table and the .icns's chunk list are both read back.
    """
    expected = [
        ASSETS / "gigaxml.png",
        ASSETS / "gigaxml.ico",
        ASSETS / "gigaxml.icns",
        *(ASSETS / f"gigaxml-{size}.png" for size in ICNS_SIZES),
    ]
    missing = [path for path in expected if not path.is_file()]
    for path in expected:
        mark = "missing" if not path.is_file() else f"{path.stat().st_size:>7} bytes"
        print(f"  {mark:<16} {path.name}")
    if missing:
        print(f"\n{len(missing)} icon file(s) missing; run: python -m tools.make_icon")
        return 1

    problems: list[str] = []

    from PIL import Image

    with Image.open(ASSETS / "gigaxml.ico") as ico:
        present = {size[0] for size in ico.info.get("sizes", [])}
    wanted = set(ICO_SIZES)
    if present != wanted:
        problems.append(f"gigaxml.ico holds sizes {sorted(present)}, expected {sorted(wanted)}")

    data = (ASSETS / "gigaxml.icns").read_bytes()
    if data[:4] != b"icns":
        problems.append("gigaxml.icns does not start with the icns magic")
    else:
        total = struct.unpack(">I", data[4:8])[0]
        if total != len(data):
            problems.append(f"gigaxml.icns declares {total} bytes but the file is {len(data)}")
        found: set[str] = set()
        offset = 8
        while offset + 8 <= len(data):
            code = data[offset : offset + 4].decode("ascii", "replace")
            length = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
            found.add(code)
            offset += length
        if not {"icp4", "ic07", "ic10"} <= found:
            problems.append(f"gigaxml.icns is missing the small or large chunk: {sorted(found)}")

    for problem in problems:
        print(f"\nPROBLEM: {problem}")
    if problems:
        print(
            "\nrun: python -m tools.make_icon   (the files exist but do not hold what they claim)"
        )
        return 1

    print("\nall icon files present, and the .ico and .icns hold the sizes they claim")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="only report whether the icon files are present",
    )
    args = parser.parse_args(argv)

    if args.check:
        return check()

    for path in build():
        print(f"  wrote {path.relative_to(ASSETS.parent)} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
