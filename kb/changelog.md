# Changelog

Newest first. This is the only place change history lives.

## 1.0.0 — 2026-10-16
First version, written in one session on request ("a Windows software to copy
fast, in Python").

- `fatcopy/core.py` — the engine: scanning and planning (`build_plan`), a
  parallel worker pool, an overlapped reader/writer pipeline for files at or
  above `pipeline_min_bytes` (`_copy_pipelined`), verification (none / size /
  content), metadata preservation with attributes applied last, `move` that only
  deletes a source after its copy passed, extended-length paths throughout,
  lock-protected progress counters, and an atomic settings file.
- `fatcopy/cli.py` — full option set with human byte sizes, a live progress
  line that only draws on a real terminal, a JSON summary, four exit codes, and
  Ctrl+C that cancels cleanly.
- `fatcopy/gui.py` — the tkinter window: file and folder pickers, all engine
  options, live progress with speed and time left, a timestamped activity log,
  cancel that names the files left half-written, a byte-for-byte "Verify last
  copy", and remembered settings.
- `build.py` — builds `Fatcopy.exe` (window) and `fatcopy-cli.exe` (console)
  with a generated multi-size icon and Windows version details, with `--tests`
  as a build gate.
- `tests/test_core.py` — 46 tests: planning and its safety checks, every
  overwrite policy, parallel and pipelined copies compared byte for byte, unicode
  and long paths, read-only destinations, cancellation, move, the damaged-copy
  case for verification, and the settings file's corruption recovery.
