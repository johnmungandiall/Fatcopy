# Fatcopy architecture

One engine, two front ends. The engine never touches a user interface and the
front ends never touch the filesystem themselves.

```
                fatcopy_gui.py            fatcopy_cli.py
                       |                        |
                  fatcopy/gui.py           fatcopy/cli.py
                       \                       /
                        \                     /
                         fatcopy/core.py  (the engine)
```

## The pieces
- `fatcopy/core.py` — the whole engine: path helpers, the scanner/planner, the
  copy primitives, verification, metadata, the progress counters, and the
  settings file. It is the only module that opens a file for writing.
- `fatcopy/cli.py` — argument parsing, the live progress line, JSON summaries
  and exit codes. Calls `core.run_copy` on a worker thread so Ctrl+C can set
  the cancel event. See [[features/cli]].
- `fatcopy/gui.py` — the tkinter window. It runs a copy on a worker thread and
  receives updates only through a `queue.Queue`; no widget is ever touched off
  the main thread. See [[features/gui]].
- `fatcopy_gui.py`, `fatcopy_cli.py` — the two entry scripts. Each is a thin
  wrapper that calls `multiprocessing.freeze_support()` first and then hands
  over to `fatcopy/gui.py#main` or `fatcopy/cli.py#main`; they exist so the build
  has one obvious entry point each.
- `fatcopy/__main__.py` — makes `python -m fatcopy …` work (it forwards to
  `fatcopy.cli.main`), and with no arguments at all the CLI opens the window
  (`fatcopy/cli.py#main`).
- `build.py` — builds both executables, generating the icon and the Windows
  version details. See [[features/build]].
- `tests/test_core.py` — the suite; `conftest.py` puts the project root on
  `sys.path` so `import fatcopy` works under a bare `pytest`.

## The three interfaces between them
- `CopyOptions` — a dataclass of tunables. `CopyOptions.normalized` bounds every
  field, so a bad value from a config file or a command line becomes a sensible
  one instead of a crash. This is the ONLY way an option reaches the engine.
- callbacks — `run_copy(..., on_file=, on_error=, on_log=, on_plan=)`. The CLI
  prints from them; the GUI pushes onto its queue from them. Both front ends
  therefore use the same reporting path.
- `CopyStats` — lock-protected counters with `snapshot()` and `summary()`; a
  caller may poll `snapshot()` at any rate from any thread, which is what lets
  the GUI paint a progress bar 150 ms at a time without disturbing the copy.
