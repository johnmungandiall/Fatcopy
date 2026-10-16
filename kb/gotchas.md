# Gotchas

Traps that have already cost time here, and the rule that prevents each.

## Long paths: use the extended form everywhere, not just when opening files
`file.copy` — no; **Fatcopy** probes the filesystem with `os.path.isdir`,
`os.path.isfile`, `os.stat`, `os.path.lexists` and `os.makedirs` as well as
opening files. On Windows each of those must be given
`fatcopy/core.py#extended_path` output, or a destination past 260 characters
fails some of the checks and passes others. `build_plan` therefore computes
`src_ext`/`dst_ext` once and uses them for every probe.

## `int(inf)` raises `OverflowError`, it does not return a huge number
`fatcopy/core.py#_clamp_int` converts to `float` first and handles NaN, `inf`
and `-inf` explicitly. A settings file or a command line holding `inf` used to
take the whole copy down at the point of parsing options.

## A read-only destination cannot receive its own metadata write
Windows refuses `os.utime` on a read-only file, so
`fatcopy/core.py#_apply_metadata` sets timestamps **first** and attributes
**last**, and `fatcopy/core.py#_prepare_destination` clears the read-only bit
before opening the file for writing. Reversing that order silently loses
timestamps.

## Do not write through a symbolic link
Opening a destination that is a symlink writes into whatever it points at.
`_prepare_destination` unlinks the link first and creates a real file.

## tkinter must only be touched from the main thread
All worker output goes through `FatcopyApp.events` and is applied in
`FatcopyApp._pump`. Editing a widget from a worker thread appears to work and
then crashes at random.

## A move may never delete an unchecked source
`CopyOptions.normalized` forces `verify="size"` whenever `move` is true. Removing
that "helpful" coercion would let `--move --verify none` delete files whose
copies were never confirmed.

## Byte comparisons must not rely on short reads
`fatcopy/core.py#_write_all` loops because `FileIO.write` may accept only part of
a buffer. The same care applies in `_verify_job`, which checks that the number
of bytes it just read from each side matches before comparing the blocks.

## PyInstaller and the two entry scripts
A frozen executable must call `multiprocessing.freeze_support()` at module level,
before anything else — that call is in `fatcopy_gui.py` and `fatcopy_cli.py`,
not in the modules they import. Without it a library that spawns a helper
process re-runs the whole program instead of the helper.

## The test suite pins one Windows-only behaviour
`test_copy_replaces_a_read_only_destination` and the long-path test are skipped
off Windows. Do not "fix" them by relaxing the assertions — the attribute bit
and the path length are the whole point of those tests.
