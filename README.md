# Fatcopy — fast copying for Windows

Fatcopy copies files and folders as fast as the disk allows, then checks that
what arrived is what was sent. It comes as a window application you can
double-click and as a command line tool you can script.

- **Many files at once.** A pool of worker threads copies whole files in
  parallel, which is where most of the speed comes from on a folder full of
  files.
- **One big file? Read and write at the same time.** Files at or above 16 MB
  are read ahead into buffers while the previous buffers are being written, so
  the source disk and the destination disk both stay busy.
- **Verified, not hoped.** Every new file is checked (size by default, or byte
  for byte), and a *move* only deletes the source once its copy has passed.
- **Long paths and unicode just work.** Paths past 260 characters, reserved
  names and network shares are handled without any registry change.
- **Nothing is hidden.** Files that were skipped, files that failed, and files
  left half-written are all named at the end of the run.

---

## 1. Run it from the source (no building needed)

You need Python 3.10 or newer with tkinter (included in the normal Windows
installer).

    python -m fatcopy --gui                       # open the window
    python -m fatcopy "D:\Photos" "E:\Backup"     # copy a folder
    python -m fatcopy "C:\big.iso" "E:\"          # copy one file

## 2. Build the Windows executables

    python build.py --tests

That produces two standalone executables in `dist\`:

| File | What it is |
|---|---|
| `dist\Fatcopy.exe` | The window application — double-click it. |
| `dist\fatcopy-cli.exe` | The command line tool for scripts and the command prompt. |

Both need no Python installed on the target machine. `python build.py --onedir`
builds a folder instead of a single file (starts faster, but must be kept
together); `python build.py --target gui` builds only one of them.

## 3. Using the window

1. **From** — pick the folder or file to copy.
2. **To** — pick where it should go.
3. Choose the options, then press **Start copy**.

The window shows a live progress bar, the current speed, how long is left, and
an activity list. **Cancel** stops cleanly: files already finished stay, files
still being written are listed so you know exactly what to re-copy.
**Verify last copy** re-reads both sides and compares every byte, which is the
check to run before deleting an original.

Your choices are remembered in `%APPDATA%\Fatcopy\settings.json`.

## 4. Using the command line

    fatcopy-cli.exe <source> <destination> [options]

| Option | What it does |
|---|---|
| `-t, --threads N` | Files copied at once. `0` (default) picks automatically. |
| `-b, --buffer SIZE` | Block size, e.g. `4M`. Default `4M`. |
| `--pipeline-min SIZE` | Files this size and larger overlap reading and writing. Default `16M`. |
| `--no-pipeline` | Never overlap; plain single-threaded copy per file. |
| `--verify none\|size\|content` | How each new copy is checked. Default `size`. |
| `--overwrite overwrite\|skip\|newer` | What to do when the file already exists. Default `overwrite`. |
| `--move` | Delete each source file after its copy has been verified. |
| `--skip-hidden` | Leave hidden and system files alone. |
| `--links follow\|copy\|skip` | What to do with symbolic links. Default `follow`. |
| `--exclude PATTERN` | Skip names matching a glob like `*.tmp`. Repeatable. |
| `--basename` | Put the source folder *inside* the destination folder. |
| `--dry-run` | Show what would be copied and write nothing. |
| `--json` | Print a machine-readable summary (for scripts). |
| `--force` | Continue past the safety checks (see below). |
| `--quiet`, `--no-progress` | Less output. |
| `--gui` | Open the window instead. |

Exit codes: `0` all good, `1` finished but something failed, `2` the request
itself was wrong, `3` cancelled.

Examples

    fatcopy-cli.exe D:\Photos E:\Backup --verify content --exclude "*.tmp"
    fatcopy-cli.exe C:\Videos\film.mkv "\\nas\media" --threads 4
    fatcopy-cli.exe E:\OldDrive E:\NewDrive --move --json > report.json

## 5. The safety checks (and how to override them)

Fatcopy stops before writing anything when it would do something surprising:

- the source and the destination are the same path;
- the destination is **inside** the source folder, so the copy would copy
  itself;
- a *move* was asked for without any checking.

It also warns (and, in the window, simply proceeds) when the destination does
not clearly have enough free space. `--force` on the command line overrides the
first two. Nothing is ever written before the size of the job — number of
files, folders and bytes — has been worked out and shown to you.

## 6. Running the tests

    python -m pytest -q

The suite covers the plan/scanner, the parallel and overlapped copy paths,
verification, move, cancellation, long paths, unicode names, read-only
destinations and the settings file.

## 7. If something goes wrong

**Windows shows "Windows protected your PC".** That is SmartScreen reacting to
any program it has not seen before: *More info → Run anyway*. Building it
yourself from this source removes the question entirely.

**Antivirus is slow or flags it.** A freshly built single-file executable is
extracted to a temporary folder on every start, and some scanners inspect that.
Use `python build.py --onedir` and run the executable in its own folder — it
starts faster too.

**A symlink could not be recreated.** Windows needs Developer Mode or
administrator rights to make symbolic links. Fatcopy falls back to copying the
target's contents so nothing is lost.

**A file is "still being written".** That file was being copied when the run
was cancelled — re-copy it, or use *Only copy when the file changed*.

## 8. Where the code lives

| File | Role |
|---|---|
| `fatcopy/core.py` | The engine: scanning, planning, parallel copying, overlapping read/write, verification, metadata, progress counters, settings file. |
| `fatcopy/cli.py` | Command line, argument parsing and the live progress line. |
| `fatcopy/gui.py` | The tkinter window; talks to the engine only through a queue. |
| `fatcopy_gui.py`, `fatcopy_cli.py` | The two entry points the build uses. |
| `build.py` | Builds the executables, generates the icon and the version details. |
| `tests/test_core.py` | The test suite. |
