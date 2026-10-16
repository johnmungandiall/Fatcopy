# The window (`fatcopy/gui.py`)

A tkinter front end whose only job is to collect options, show progress and let
the user stop. Built and shown by `fatcopy/gui.py#main`, run by
`fatcopy/gui.py#FatcopyApp`.

## The threading rule that matters
The copy runs on a worker thread (`FatcopyApp._work`) and **never touches a
widget**. Workers push tuples onto `FatcopyApp.events`; the main thread drains
that queue in `FatcopyApp._pump`, which re-schedules itself with `after(150, …)`.
That is the only correct way to drive tkinter from a background thread, and
breaking it produces random crashes that are very hard to trace.

## Layout
- **What to copy where** — source and destination entries with folder/file
  pickers and a Swap button.
- **Options** — move instead of copy, leave hidden/system files alone, keep
  timestamps and attributes, overlap read and write for big files, preview only,
  the check level, the overwrite policy, files at once, block size, and an
  exclude list (comma separated).
- **Buttons** — Start copy, Cancel, Verify last copy, Open destination, Save
  log. Verify is enabled only while a plan from the last run is still held.
- **Progress** — bar, percentage, a detail line (bytes, files, speed, time left)
  and the file being worked on now.
- **Activity** — a timestamped scrolling log, capped at `_LOG_LINES` lines.

## Details worth keeping
- Options are validated *before* the run starts: a missing source, source equal
  to destination, or an unsafe plan produces one message box instead of a
  half-finished copy. The plan is built on the main thread
  (`FatcopyApp._start`), so a `CopyPlanError` is reported before any worker
  starts.
- *Move* asks for confirmation first, because it deletes source files.
- *Verify last copy* calls `fatcopy/core.py#verify_plan` — it re-reads both sides
  and compares every byte, which is the check to run before deleting an original.
- Closing the window during a copy asks first, then sets the cancel event and
  waits (bounded, 20 s) before destroying the window, so files are not killed
  mid-write.
- Settings are written by `FatcopyApp._save_settings` through
  `fatcopy/core.py#save_config`, which writes a temporary file and replaces it
  atomically, keeping one `.bak`. The one user-facing string that names a
  failure inside the copy says "Copying…" / "Copied…" — never a word that could
  be read as "deleting".
- If tkinter cannot open a display, `fatcopy/gui.py#main` prints the command
  line form instead of raising.
