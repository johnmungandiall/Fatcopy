"""Build Fatcopy into standalone Windows executables.

    python build.py              # build both executables (this is the normal one)
    python build.py --target gui # only the window application
    python build.py --target cli # only the command line tool
    python build.py --onedir     # a folder instead of a single file (starts faster)
    python build.py --tests      # run the test suite first, stop if it fails
    python build.py --no-icon    # skip the generated icon
    python build.py --clean      # ignore PyInstaller's cache

Two executables are produced because one file cannot be both: a window
application has no console to print to, and a console application shows a
black window behind its dialogs.  ``Fatcopy.exe`` is the window application
(this is the one to double-click) and ``fatcopy-cli.exe`` is the console tool
for scripts and the command prompt.

"""

from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
DIST = ROOT / "dist"
VERSION = (1, 0, 0, 0)
VERSION_TEXT = ".".join(str(part) for part in VERSION[:3])

TARGETS = {
    # name -> (entry script, window application?)
    "gui": ("Fatcopy", "fatcopy_gui.py", True),
    "cli": ("fatcopy-cli", "fatcopy_cli.py", False),
}

# Modules Fatcopy never uses; leaving them out keeps the executables smaller.
EXCLUDES = (
    "pytest",
    "unittest",
    "_pytest",
    "pydoc_data",
    "test",
    "distutils",
    "lib2to3",
    "xmlrpc",
    "sqlite3",
    "logging.handlers",
)


# ---------------------------------------------------------------------------
# icon
# ---------------------------------------------------------------------------
def icon_pixels(size: int) -> list:
    """Draw the Fatcopy mark: two stacked pages, in BGRA rows, top first."""
    blue = (232, 115, 26, 255)      # #1a73e8
    light = (230, 169, 90, 255)     # #5aa9e6
    white = (255, 255, 255, 255)
    clear = (0, 0, 0, 0)
    grid = [[clear] * size for _ in range(size)]

    def fill(x0, y0, x1, y1, colour):
        for y in range(max(0, int(y0)), min(size, int(y1))):
            row = grid[y]
            for x in range(max(0, int(x0)), min(size, int(x1))):
                row[x] = colour

    def frame(x0, y0, x1, y1, colour, thickness):
        fill(x0, y0, x1, y0 + thickness, colour)
        fill(x0, y1 - thickness, x1, y1, colour)
        fill(x0, y0, x0 + thickness, y1, colour)
        fill(x1 - thickness, y0, x1, y1, colour)

    back = (0.10 * size, 0.06 * size, 0.70 * size, 0.66 * size)
    front = (0.30 * size, 0.34 * size, 0.92 * size, 0.96 * size)
    border = max(1, round(size * 0.075))

    fill(*back, light)
    fill(*front, white)
    frame(*front, blue, border)

    lines = max(1, size // 16)
    inner_x0 = front[0] + border + size * 0.10
    inner_x1 = front[2] - border - size * 0.10
    for step in range(3):
        y = front[1] + border + size * (0.16 + 0.20 * step)
        fill(inner_x0, y, inner_x1, y + lines, blue)

    # A thin white gap between the two pages so the overlap stays readable on
    # any desktop background.
    fill(back[0], back[1], back[2], back[1] + max(1, size // 32), clear)
    return grid


def icon_bitmap(size: int) -> bytes:
    """One ICO image: BITMAPINFOHEADER + BGRA pixels (bottom-up) + AND mask."""
    grid = icon_pixels(size)
    header = struct.pack(
        "<IiiHHIIiiII",
        40,           # header size
        size,         # width
        size * 2,     # height (colour + mask)
        1,            # planes
        32,           # bits per pixel
        0,            # BI_RGB, uncompressed
        size * size * 4,
        2835,         # ~72 dpi
        2835,
        0,
        0,
    )
    pixels = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up
        for x in range(size):
            b, g, r, a = grid[y][x]
            pixels += bytes((b, g, r, a))
    mask_row = ((size + 31) // 32) * 4
    return header + bytes(pixels) + b"\x00" * (mask_row * size)


def build_icon(path: Path, sizes=(16, 32, 48, 64, 128, 256)) -> bool:
    """Write a multi-size .ico and read it back to prove it is well formed."""
    images = [(size, icon_bitmap(size)) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries = bytearray()
    body = bytearray()
    for size, bitmap in images:
        dimension = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32,
                               len(bitmap), offset)
        offset += len(bitmap)
        body += bitmap
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header + bytes(entries) + bytes(body))
    except OSError as exc:
        print(f"  ! could not write the icon: {exc}")
        return False
    # Read it back: a malformed icon makes PyInstaller fail with a vague error.
    data = path.read_bytes()
    _reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    if kind != 1 or count != len(images) or len(data) != offset:
        print("  ! the generated icon did not verify, building without one")
        return False
    for index in range(count):
        entry = struct.unpack_from("<BBBBHHII", data, 6 + 16 * index)
        if entry[6] <= 0 or entry[7] + entry[6] > len(data):
            print("  ! the generated icon did not verify, building without one")
            return False
    return True


def build_version_file(path: Path) -> bool:
    """Windows file properties (right-click -> Details)."""
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={VERSION},
    prodvers={VERSION},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', 'Fatcopy'),
        StringStruct('FileDescription', 'Fatcopy - fast, parallel, verified file copying'),
        StringStruct('FileVersion', '{VERSION_TEXT}.0'),
        StringStruct('InternalName', 'Fatcopy'),
        StringStruct('OriginalFilename', 'Fatcopy.exe'),
        StringStruct('ProductName', 'Fatcopy'),
        StringStruct('ProductVersion', '{VERSION_TEXT}.0'),
        StringStruct('LegalCopyright', '')
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as exc:
        print(f"  ! could not write the version file: {exc}")
        return False


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
def check_pyinstaller() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--version"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    print(f"PyInstaller {result.stdout.strip()}")
    return True


def run_tests() -> bool:
    print("Running the test suite first...")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"], cwd=str(ROOT), text=True,
    )
    return result.returncode == 0


def build_one(name: str, script: str, windowed: bool, *, onefile: bool, icon: Path | None,
              version_file: Path | None, clean: bool) -> Path | None:
    print(f"\n=== Building {name} ({'window' if windowed else 'console'}) ===")
    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--log-level", "WARN",
        "--distpath", str(DIST),
        "--workpath", str(BUILD / "work" / name),
        "--specpath", str(BUILD / "spec"),
        "--name", name,
        "--noupx",
        "--onefile" if onefile else "--onedir",
    ]
    if windowed:
        command.append("--windowed")
    else:
        command.append("--console")
    if icon is not None:
        command += ["--icon", str(icon)]
    if version_file is not None:
        command += ["--version-file", str(version_file)]
    if clean:
        command.append("--clean")
    for module in EXCLUDES:
        command += ["--exclude-module", module]
    command.append(str(ROOT / script))

    result = subprocess.run(command, cwd=str(ROOT), text=True)
    if result.returncode != 0:
        print(f"  ! PyInstaller failed for {name} (exit {result.returncode})")
        return None

    produced = (DIST / f"{name}.exe") if onefile else (DIST / name / f"{name}.exe")
    if not produced.is_file():
        # A window application is not always suffixed .exe on other platforms.
        candidate = (DIST / name) if onefile else (DIST / name / name)
        produced = candidate if candidate.is_file() else produced
    if not produced.is_file():
        print(f"  ! expected {produced} but it is not there")
        return None
    print(f"  ok: {produced}  ({produced.stat().st_size / 1024 / 1024:.1f} MB)")
    return produced


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build the Fatcopy executables.")
    parser.add_argument("--target", choices=("all", "gui", "cli"), default="all")
    parser.add_argument("--onedir", action="store_true",
                        help="produce a folder instead of a single file (starts faster)")
    parser.add_argument("--no-icon", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--tests", action="store_true", help="run the test suite first")
    args = parser.parse_args(argv)

    if os.name != "nt":
        print("Note: this build is meant for Windows; the result here will be for "
              f"{sys.platform} only.")

    if args.tests and not run_tests():
        print("The test suite failed — not building.")
        return 1

    if not check_pyinstaller():
        print("PyInstaller is not available. Install it with:\n"
              f"  {Path(sys.executable).name} -m pip install pyinstaller")
        return 1

    for script in {entry[1] for entry in TARGETS.values()}:
        if not (ROOT / script).is_file():
            print(f"entry script missing: {script}")
            return 1

    icon = None
    if not args.no_icon:
        candidate = BUILD / "fatcopy.ico"
        icon = candidate if build_icon(candidate) else None
    version_file = None
    candidate = BUILD / "version.txt"
    if build_version_file(candidate):
        version_file = candidate

    wanted = list(TARGETS.values()) if args.target == "all" else [TARGETS[args.target]]
    produced = []
    for name, script, windowed in wanted:
        result = build_one(name, script, windowed, onefile=not args.onedir, icon=icon,
                           version_file=version_file, clean=args.clean)
        if result is None:
            return 1
        produced.append(result)

    print("\nBuilt:")
    for path in produced:
        print(f"  {path}")
    print("\nFatcopy.exe is the window application - double-click it, or make a "
          "shortcut to it.\nfatcopy-cli.exe is the command line tool: "
          "fatcopy-cli.exe <source> <destination>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
