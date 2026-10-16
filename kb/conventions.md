# Conventions

## Code
- **Standard library only.** No third-party import may appear in `fatcopy/` —
  that is what keeps the built executables dependency-free. `build.py` and the
  tests may use PyInstaller and pytest, because they are development tools.
- **Type-annotated signatures** (`from __future__ import annotations`) and
  docstrings that say what a function is FOR, not what each line does.
- **Long, honest names.** `_note_incomplete`, `bytes_needed`,
  `pipeline_min_bytes` — the reader should not have to look anything up. The two
  deliberate exceptions are the two-letter `snap` for a stats snapshot and the
  short local `opts`/`st`.
- **Bounds live in one place.** Every user-supplied number is clamped inside
  `CopyOptions.normalized` via `_clamp_int`, so no other function has to defend
  itself against a silly value.
- **Errors are collected, not raised across a pool.** A worker records a
  message through `on_error` and continues; only a planning problem raises
  `CopyPlanError` to the caller.
- **Cancellation is a `threading.Event`**, never a flag or an exception across
  threads. Every wait uses a timeout so a cancel is noticed promptly.
- Private helpers are prefixed with `_`; the public surface is
  `fatcopy/core.py#build_plan`, `#run_copy`, `#verify_plan`, `CopyOptions`,
  `CopyStats`, `CopyPlanError` and the settings helpers.

## Notes in `kb/`
- One fact lives in one place; other notes point at it with `[[note-name]]`.
- **Never cite a line number.** Cite `path#symbol` (for example
  `fatcopy/core.py#_copy_pipelined`) so a moved function costs no note edit.
- A note starts with one line saying what it covers.
- Long topics get their own note under `kb/features/`.
- Release and change history lives only in [[changelog]].

## Tests
- `tests/test_core.py`, run with `python -m pytest -q` from the project root.
- Test names state the behaviour, not the function:
  `test_copy_move_deletes_sources_only_after_verification`.
- Data stays small (a few megabytes) but the *shapes* are wide: empty files,
  unicode and spaces in names, paths past 260 characters on the Windows side,
  read-only destinations, cancellation, and a damaged copy for the verifier.
- A platform-specific behaviour is skipped with a reason, never asserted loosely.
