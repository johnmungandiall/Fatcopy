# The command line (`fatcopy/cli.py`)

`fatcopy <source> <destination> [options]`, or `fatcopy --gui`, or no arguments
at all (which opens the window). Implemented by `fatcopy/cli.py#main`, with the
work in `fatcopy/cli.py#run_once` and the parser in
`fatcopy/cli.py#build_parser`.

## Options
`-t/--threads`, `-b/--buffer`, `--pipeline-min`, `--pipeline-depth`,
`--no-pipeline`, `--verify none|size|content`, `--overwrite overwrite|skip|newer`,
`--no-preserve`, `--move`, `--skip-hidden`, `--links follow|copy|skip`,
`--exclude PATTERN` (repeatable), `--basename`, `--dry-run`, `--force`,
`--json`, `--quiet`, `--no-progress`, `--version`. Human sizes such as `4M` or
`512kb` are accepted for the byte options by `fatcopy/cli.py#parse_size`.

## Exit codes
`0` everything copied, `1` finished but something failed, `2` the request itself
was wrong (bad source, unsafe destination, bad option), `3` cancelled. The
constants are `EXIT_OK`, `EXIT_ERRORS`, `EXIT_USAGE`, `EXIT_CANCELLED` at the
top of the module.

## How the progress line works
`fatcopy/cli.py#_live_progress` polls `CopyStats.snapshot()` about eight times a
second and redraws one line with a carriage return. It only draws when stdout is
a real terminal (`sys.stdout.isatty()`), so redirecting to a file gives clean
text rather than thousands of overwritten lines. Problems and log lines are
printed by clearing the line first, so a message never gets mangled into the
progress bar.

Ctrl+C sets the cancel event and waits for the workers to finish the files they
are in the middle of, so interrupting never leaves a file silently half-written.
`fatcopy/cli.py#_enable_windows_console` turns on ANSI output and UTF-8 on
Windows, best effort.
