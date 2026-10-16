# The copy engine (`fatcopy/core.py`)

The only module that writes files. Everything else treats it as a black box
that reports progress through `CopyStats` and callbacks.

## The three stages
1. **Scan and plan** — `fatcopy/core.py#build_plan` walks the source and returns
   a `Plan` (files, folders, links, what was skipped, total and still-needed
   bytes). Nothing is written until the whole plan exists and its size has been
   reported, so the caller always knows the size of the job first.
2. **Create folders** — `fatcopy/core.py#run_copy` makes every planned folder
   in plan order before any file is written, so parents always exist.
3. **Copy** — worker threads pull `Job` objects from a queue and each calls
   `fatcopy/core.py#_copy_one`, which writes, verifies and only then applies
   metadata (and, for a move, deletes the source).

## Where the speed comes from
- **Whole files in parallel.** `CopyOptions.threads` worker threads
  (`fatcopy/core.py#run_copy`'s `worker`). Default is 8, or twice the CPU count
  capped at 16. For a folder of many files this is where nearly all the gain is.
- **Read and write at the same time for one big file.**
  `fatcopy/core.py#_copy_pipelined` runs a reader thread that fills buffers and
  the calling thread as the writer; a bounded queue of `_Slot` buffers paces
  them. Files at or above `CopyOptions.pipeline_min_bytes` (default 16 MB) take
  this path. The destination is still written strictly sequentially, in order.
- `fatcopy/core.py#_copy_sequential` is the plain buffered loop for everything
  else — small and medium files are dominated by per-file overhead, not by the
  read/write overlap, so paying for a second thread there would only add cost.

Buffer sizes and depths are clamped in `CopyOptions.normalized` (64 KB … 64 MB
per block, 1 … 16 buffers). `fatcopy/core.py#_clamp_int` deliberately handles
NaN, `inf` and `-inf` — `int(inf)` raises `OverflowError`, and a settings file
holding `inf` must not crash a copy.

## Correctness rules the code depends on
- **Long paths always.** Every path handed to the operating system goes through
  `fatcopy/core.py#extended_path` (`\\?\` / `\\?\UNC\`) — including the probes in
  `build_plan`. `fatcopy/core.py#plain_path` converts back for display, for
  `os.path.relpath` and for libraries that reject the prefix.
- **Never write *through* a link.** `fatcopy/core.py#_prepare_destination` removes
  a destination that is a symbolic link and clears the read-only bit
  (`fatcopy/core.py#_clear_readonly`) before opening the file for writing.
- **Attributes last.** `fatcopy/core.py#_apply_metadata` sets timestamps first and
  attributes afterwards — a read-only file cannot be `utime`d. Only the bits in
  `_CARRIED_ATTRIBUTES` (read-only, hidden, system, archive) travel.
- **A move only deletes what passed.** `CopyOptions.normalized` forces
  `verify="size"` when `move` is set, so `verify="none"` can never delete an
  unchecked source; source folders are removed only after every file arrived and
  only when `stats.errors` is empty.
- **Nothing disappears silently.** `fatcopy/core.py#_note_incomplete` records a
  destination that was left not matching the source size, so a cancelled or
  failed write is named in `summary()["partial_files"]`.
- **One bad file never stops the rest.** Each worker catches `BaseException`,
  records the message through `on_error`, and carries on.

## Planning safety checks (`build_plan` raises `CopyPlanError`)
- the source must exist;
- source and destination must not be the same path;
- the destination must not be inside the source folder (it would copy the copy)
  unless `force` is set;
- the destination must not be an existing *file* when a folder is expected.

`fatcopy/core.py#_apply_overwrite_policy` then applies the overwrite policy per
file and counts `bytes_needed` (bytes that actually have to be written, which is
less than the total when files are already up to date). `run_copy` compares that
against `fatcopy/core.py#free_space_for` and reports a shortfall unless `force`
is set.

## Cancellation
A `threading.Event` passed as `cancel=`. Workers check it between blocks; the
pipelined reader checks between buffers. On cancel the run stops, `stats.cancelled`
is set, and the files still in flight are reported — they are the ones to re-copy.

## Progress reporting (`CopyStats`)
`snapshot()` returns files, folders, bytes, percent, elapsed, speed, time left,
the files being worked on, anything left half-written and whether the run
finished — all under the lock, so any thread may call it at any rate.
`summary()` is the same information flattened for a final report or `--json`.

One subtlety: the *average* speed is only computed once at least ~50 ms have
passed. Gating it on a longer interval reported `0 B/s` for a 40 MB copy that
took a quarter of a second, which is both wrong and the most common case for a
small fast copy. `add_bytes` samples the total roughly four times a second and
`speed` prefers that recent figure while the copy is running, falling back to the
average — that is what stops the displayed speed from lurching about.
