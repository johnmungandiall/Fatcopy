# Building the executables (`build.py`)

```
python build.py              # both executables into dist\
python build.py --tests      # run the test suite first, stop if it fails
python build.py --target gui # only the window application
python build.py --target cli # only the console tool
python build.py --onedir     # a folder instead of a single file
python build.py --no-icon    # skip the generated icon
python build.py --clean      # ignore PyInstaller's cache
```

## Why two executables
One file cannot be both: a windowed program has no console to print to, and a
console program shows a black window behind its dialogs. So:

| Output | Entry script | Kind |
|---|---|---|
| `dist\Fatcopy.exe` | `fatcopy_gui.py` | window application (`--windowed`) |
| `dist\fatcopy-cli.exe` | `fatcopy_cli.py` | console (`--console`) |

The names come from `TARGETS` in `build.py`.

## What the script does beyond calling PyInstaller
- `build_icon` (in `build.py`) draws the mark in code — two stacked pages — and
  writes a multi-size `.ico` for 16…256 px with no image library, then reads the
  file back and checks the header, entry offsets and length. A malformed icon
  makes PyInstaller fail with a vague error, so it is verified first and simply
  skipped if it does not check out. `icon_pixels` returns BGRA rows (the ICO
  format stores blue, green, red, alpha); `icon_bitmap` wraps them in the
  BITMAPINFOHEADER and stores the rows bottom-up, plus the AND mask.
- `build_version_file` writes the version resource, which becomes the
  right-click → Details text on the executable.
- `EXCLUDES` drops modules Fatcopy never uses to keep the files smaller.
- `check_pyinstaller` reports the exact `python -m pip install pyinstaller`
  command when the tool is missing instead of failing inside a subprocess.
- `run_tests` (with `--tests`) is the gate: a red suite means no build.

## Known Windows behaviours
A freshly built single-file executable unpacks into a temporary folder on every
start, so the first launch is slow and some antivirus products inspect that
unpacking. `--onedir` starts faster and is the better shape for a folder that
stays put. SmartScreen may also warn about any executable it has not seen before
(*More info → Run anyway*); building it locally from this source avoids that.
