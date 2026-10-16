"""Fatcopy command line.

Usage
-----
    fatcopy <source> <destination> [options]
    fatcopy --gui                       # open the window instead

Exit codes
----------
0  everything copied and verified
1  finished, but at least one file failed
2  the request itself was wrong (bad source, unsafe destination, bad option)
3  cancelled by the user

"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading

from . import __version__
from .core import (
    CopyOptions,
    CopyPlanError,
    CopyStats,
    human_bytes,
    human_time,
    run_copy,
)

EXIT_OK = 0
EXIT_ERRORS = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "kib": 1024,
    "m": 1024 ** 2,
    "mb": 1024 ** 2,
    "mib": 1024 ** 2,
    "g": 1024 ** 3,
    "gb": 1024 ** 3,
    "gib": 1024 ** 3,
    "t": 1024 ** 4,
    "tb": 1024 ** 4,
    "tib": 1024 ** 4,
}


def parse_size(text) -> int:
    """Turn ``4M`` / ``512KB`` / ``1048576`` into a byte count."""
    if isinstance(text, (int, float)):
        return int(text)
    raw = str(text).strip().lower().replace(",", "").replace("_", "").replace(" ", "")
    if not raw:
        raise argparse.ArgumentTypeError("empty size")
    number = ""
    suffix = ""
    for char in raw:
        if char.isdigit() or char == ".":
            if suffix:
                raise argparse.ArgumentTypeError(f"unit must come last: {text}")
            number += char
        else:
            suffix += char
    if not number:
        raise argparse.ArgumentTypeError(f"missing number in size: {text}")
    if suffix not in _SIZE_UNITS:
        raise argparse.ArgumentTypeError(f"unknown size unit '{suffix}' in {text}")
    try:
        value = float(number)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid size: {text}") from exc
    if value < 0:
        raise argparse.ArgumentTypeError(f"negative size: {text}")
    return int(value * _SIZE_UNITS[suffix])


def _enable_windows_console() -> None:
    """Turn on VT sequences and UTF-8 output on Windows, best effort."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL
    except Exception:
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fatcopy",
        description="Fast, parallel, verified copying for Windows.",
        epilog="Examples:\n"
        "  fatcopy D:\\Photos E:\\Backup\\Photos --verify content\n"
        "  fatcopy big.iso E:\\ --threads 4\n"
        "  fatcopy --gui",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("source", nargs="?", help="file or folder to copy")
    parser.add_argument("destination", nargs="?", help="where to put it")
    parser.add_argument("--gui", action="store_true", help="open the window instead of copying")
    parser.add_argument("-t", "--threads", type=int, default=0,
                        help="files copied at once (0 = automatic)")
    parser.add_argument("-b", "--buffer", type=parse_size, default=4 * 1024 * 1024,
                        metavar="SIZE", help="block size, e.g. 1M, 4M (default 4M)")
    parser.add_argument("--pipeline-min", type=parse_size, default=16 * 1024 * 1024,
                        metavar="SIZE",
                        help="files at or above this size overlap read and write (default 16M)")
    parser.add_argument("--pipeline-depth", type=int, default=4, metavar="N",
                        help="buffers in flight for a big file (1-16, default 4)")
    parser.add_argument("--no-pipeline", action="store_true",
                        help="never overlap the read and the write of one file")
    parser.add_argument("--verify", choices=("none", "size", "content"), default="size",
                        help="check each new copy (default size)")
    parser.add_argument("--overwrite", choices=("overwrite", "skip", "newer"), default="overwrite",
                        help="what to do when the destination file exists (default overwrite)")
    parser.add_argument("--no-preserve", action="store_true",
                        help="do not carry timestamps/attributes over")
    parser.add_argument("--move", action="store_true",
                        help="delete each source file after its copy is verified")
    parser.add_argument("--skip-hidden", action="store_true",
                        help="leave hidden and system files alone")
    parser.add_argument("--links", choices=("follow", "copy", "skip"), default="follow",
                        help="what to do with symbolic links (default follow)")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATTERN",
                        help="skip names matching this glob; repeatable")
    parser.add_argument("--basename", action="store_true",
                        help="put the source folder inside the destination folder")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be copied, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="continue past the safety checks (destination inside source, "
                             "not enough free space)")
    parser.add_argument("--json", action="store_true", help="print a JSON summary on stdout")
    parser.add_argument("--quiet", action="store_true", help="no progress, only problems")
    parser.add_argument("--no-progress", action="store_true", help="no live progress line")
    parser.add_argument("--version", action="version", version=f"Fatcopy {__version__}")
    return parser


def _render(stats: CopyStats, width: int) -> str:
    snap = stats.snapshot()
    if snap["done"]:
        return ""
    if stats.plan is None:
        return "Scanning…"
    total = snap["bytes_total"]
    copied = snap["bytes_done"]
    parts = [f"{snap['percent']:5.1f}%"]
    if total:
        parts.append(f"{human_bytes(copied)} / {human_bytes(total)}")
    else:
        parts.append(human_bytes(copied))
    parts.append(f"{human_bytes(snap['speed'])}/s")
    if snap["eta"]:
        parts.append(f"left {human_time(snap['eta'])}")
    parts.append(f"{snap['files_done']}/{snap['files_total']} files")
    active = snap["active"]
    if active:
        name = os.path.basename(active[0])
        if len(active) > 1:
            name += f" (+{len(active) - 1})"
        parts.append(name[:40])
    line = "  ".join(parts)
    if len(line) < width:
        line = line.ljust(width)
    return line


def _live_progress(stats: CopyStats, done: threading.Event, *, quiet: bool, show: bool) -> None:
    width = 0
    if not show:
        return
    while not done.wait(0.12):
        line = _render(stats, width)
        if not line:
            continue
        width = max(width, len(line))
        sys.stdout.write("\r" + line)
        sys.stdout.flush()
    line = _render(stats, width)
    if width:
        sys.stdout.write("\r" + " " * width + "\r")
    if not quiet:
        snap = stats.snapshot()
        if snap["done"] or snap["bytes_done"]:
            sys.stdout.write(
                f"  {human_bytes(snap['bytes_done'])} in {human_time(snap['elapsed'])}"
                f"  ({human_bytes(snap['speed_average'])}/s)\n"
            )
    sys.stdout.flush()


def run_once(args) -> int:
    options = CopyOptions(
        threads=args.threads,
        buffer_bytes=args.buffer,
        pipeline=not args.no_pipeline,
        pipeline_min_bytes=args.pipeline_min,
        pipeline_depth=args.pipeline_depth,
        verify=args.verify,
        overwrite=args.overwrite,
        preserve=not args.no_preserve,
        move=args.move,
        skip_hidden=args.skip_hidden,
        links=args.links,
        exclude=tuple(args.exclude),
        basename=args.basename,
        dry_run=args.dry_run,
        force=args.force,
    )

    human = not args.quiet and not args.json
    cancel = threading.Event()
    stats = CopyStats()
    problems: list = []

    def note(message: str) -> None:
        problems.append(message)
        if human:
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.write(f"  ! {message}\n")
            sys.stdout.flush()

    def log(message: str) -> None:
        if human:
            sys.stdout.write("\r" + " " * 120 + "\r")
            sys.stdout.write(f"  {message}\n")
            sys.stdout.flush()

    finished = threading.Event()

    def work() -> None:
        try:
            run_copy(
                args.source,
                args.destination,
                options,
                cancel=cancel,
                stats=stats,
                on_error=note,
                on_log=log,
            )
        except BaseException as exc:  # surfaced through `failure`
            failure.append(exc)
        finally:
            finished.set()

    failure: list = []
    show_progress = human and not args.no_progress and sys.stdout.isatty()
    thread = threading.Thread(target=work, name="fatcopy-run", daemon=True)
    thread.start()

    try:
        _live_progress(stats, finished, quiet=args.quiet or args.json, show=show_progress)
    except KeyboardInterrupt:
        if human:
            sys.stdout.write("\n  Stopping — finishing the files already in flight…\n")
        cancel.set()
        while thread.is_alive():
            try:
                thread.join(timeout=0.2)
            except KeyboardInterrupt:
                continue
    thread.join(timeout=30)

    if failure:
        exc = failure[0]
        if isinstance(exc, CopyPlanError):
            sys.stderr.write(f"fatcopy: {exc}\n")
            return EXIT_USAGE
        sys.stderr.write(f"fatcopy: {type(exc).__name__}: {exc}\n")
        return EXIT_ERRORS

    summary = stats.summary()
    if args.json:
        sys.stdout.write(json.dumps(summary, indent=2) + "\n")
        return EXIT_CANCELLED if summary["cancelled"] else (
            EXIT_ERRORS if summary["errors"] else EXIT_OK
        )

    if human and not args.quiet:
        sys.stdout.write(
            f"  {summary['files_copied']} of {summary['files_planned']} files copied"
            f" ({human_bytes(summary['bytes_copied'])})\n"
        )
        if summary["skipped"]:
            sys.stdout.write(f"  {summary['skipped']} item(s) left alone\n")
        for line in summary["error_messages"][:10]:
            sys.stdout.write(f"  ! {line}\n")
        if summary["errors"] > len(summary["error_messages"]):
            sys.stdout.write(f"  … and {summary['errors'] - len(summary['error_messages'])} more\n")
        if summary["cancelled"]:
            sys.stdout.write("  Cancelled.\n")

    if summary["cancelled"]:
        return EXIT_CANCELLED
    return EXIT_ERRORS if summary["errors"] else EXIT_OK


def main(argv=None) -> int:
    _enable_windows_console()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.gui or (args.source is None and args.destination is None):
        from .gui import main as gui_main

        return gui_main([])

    if not args.source or not args.destination:
        parser.error("both a source and a destination are required")

    try:
        return run_once(args)
    except KeyboardInterrupt:
        sys.stderr.write("\nfatcopy: interrupted\n")
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
