"""Fatcopy core engine — fast, parallel, verified file copying for Windows.

How the speed is obtained
-------------------------
* **Many files at once.** A pool of worker threads copies whole files in
  parallel (``CopyOptions.threads``).  For a real folder full of files this is
  where almost all of the speed-up comes from — the threads hide the per-file
  round trips that a plain sequential copy wastes.
* **Overlapped read/write for one big file.** A file at or above
  ``pipeline_min_bytes`` is read by a reader thread into a bounded queue of
  buffers while the writer thread writes the buffers out in order, so the read
  engine and the write engine are busy at the same time.  The destination is
  written strictly sequentially, so nothing is ever written out of order.
* **Long paths just work.** Every path handed to the OS goes through
  :func:`extended_path` (``\\\\?\\`` / ``\\\\?\\UNC\\``), so paths longer than
  260 characters, reserved device names and UNC shares work with no registry
  change.

What makes it trustworthy
-------------------------
* ``verify`` (none / size / content) runs after every newly written file;
  ``move`` is only allowed to delete a source file once verification passed.
* Attributes, timestamps and read-only flags travel with the file and are
  applied last, so a read-only destination never blocks its own write.
* Errors are collected per file and reported — one bad file never stops the
  rest of the copy.
* Cancel is cooperative through a :class:`threading.Event`; files that were
  half-written are named in ``stats.partial`` instead of being silently left.

Progress is exposed through :class:`CopyStats`, a lock-protected counter set
that a caller (the GUI) may poll as often as it likes; per-file and per-error
callbacks are optional.

"""

from __future__ import annotations

import ctypes
import fnmatch
import os
import queue
import shutil
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Callable, Sequence

IS_WINDOWS = os.name == "nt"

# ---------------------------------------------------------------------------
# Windows file attribute bits (only meaningful on Windows).
# ---------------------------------------------------------------------------
FILE_ATTRIBUTE_READONLY = 0x0001
FILE_ATTRIBUTE_HIDDEN = 0x0002
FILE_ATTRIBUTE_SYSTEM = 0x0004
FILE_ATTRIBUTE_DIRECTORY = 0x0010
FILE_ATTRIBUTE_ARCHIVE = 0x0020
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400

_CARRIED_ATTRIBUTES = (
    FILE_ATTRIBUTE_READONLY
    | FILE_ATTRIBUTE_HIDDEN
    | FILE_ATTRIBUTE_SYSTEM
    | FILE_ATTRIBUTE_ARCHIVE
)

# ---------------------------------------------------------------------------
# Bounds — every user-supplied number is clamped into these.
# ---------------------------------------------------------------------------
MIN_BUFFER = 64 * 1024
MAX_BUFFER = 64 * 1024 * 1024
MAX_THREADS = 64
MAX_PIPELINE_DEPTH = 16
MAX_PIPELINE_MIN = 64 * 1024 * 1024 * 1024

OVERWRITE_POLICIES = ("overwrite", "skip", "newer")
VERIFY_MODES = ("none", "size", "content")
LINK_MODES = ("follow", "copy", "skip")


class Cancelled(Exception):
    """Raised inside a worker when the caller asked the copy to stop."""


class CopyPlanError(Exception):
    """User-facing planning error (bad source, bad destination, ...)."""


class CopyMismatch(Exception):
    """The written file does not match the source."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------
def extended_path(path: str) -> str:
    """Return a Windows extended-length path (``\\\\?\\...``).

    ``\\\\?\\`` disables Win32 path parsing, which is what lets paths longer
    than MAX_PATH work and lets reserved device names (CON, NUL, ...) be used
    as ordinary file names.  On POSIX the path is returned unchanged.
    """
    if not IS_WINDOWS:
        return path
    if not path:
        return path
    if path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\"):  # UNC share
        return "\\\\?\\UNC\\" + absolute[2:]
    return "\\\\?\\" + absolute


def plain_path(path: str) -> str:
    """Inverse of :func:`extended_path`, for display and for libraries that
    do not accept the extended prefix."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def human_bytes(n: float) -> str:
    """Format a byte count the way a file manager would."""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{n:.0f} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float) -> str:
    """Format a duration as ``1h 02m`` / ``2m 05s`` / ``7s``."""
    seconds = max(0.0, float(seconds))
    if seconds >= 3600:
        return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60):02d}m"
    if seconds >= 60:
        return f"{int(seconds // 60)}m {int(seconds % 60):02d}s"
    return f"{seconds:.0f}s"


def free_space_for(path: str) -> int | None:
    """Free bytes available on the volume holding *path*, or ``None`` when it
    cannot be determined (network share, unusual device)."""
    probe = os.path.abspath(path or ".") if not path.startswith("\\\\?\\") else path
    while probe and not os.path.exists(plain_path(probe)):
        parent = os.path.dirname(probe)
        if parent == probe:
            probe = ""
            break
        probe = parent
    if not probe:
        return None
    try:
        return shutil.disk_usage(plain_path(probe)).free
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Windows attribute helpers
# ---------------------------------------------------------------------------
def _set_file_attributes(path: str, attributes: int) -> bool:
    """Apply raw Windows attributes to *path* (extended path accepted)."""
    if not IS_WINDOWS:
        return False
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
        kernel32.SetFileAttributesW.restype = ctypes.c_int
        return bool(kernel32.SetFileAttributesW(path, int(attributes)))
    except Exception:
        return False


def _clear_readonly(path: str, attributes: int) -> None:
    """Drop the read-only bit so an existing destination can be overwritten."""
    if not attributes & FILE_ATTRIBUTE_READONLY:
        return
    if IS_WINDOWS:
        _set_file_attributes(path, attributes & ~FILE_ATTRIBUTE_READONLY)
    else:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass


def _attributes_of(path: str) -> int:
    if not IS_WINDOWS:
        return 0
    try:
        return getattr(os.stat(path, follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
def _clamp_int(value, low: int, high: int, default: int) -> int:
    """Coerce *value* to a bounded int.

    NaN, infinities and anything unparseable are handled explicitly — ``int()``
    raises ``OverflowError`` on infinity, so a settings file or command line
    holding ``inf`` must never crash the copy.
    """
    try:
        if isinstance(value, bool):
            value = int(value)
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if number != number:  # NaN never equals itself
        return default
    if number == float("inf"):
        return high
    if number == float("-inf"):
        return low
    try:
        number = int(number)
    except (ValueError, OverflowError):
        return default
    return max(low, min(high, number))


def _one_of(value, allowed: Sequence[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


@dataclass(slots=True)
class CopyOptions:
    """Tunables for one copy run.  ``normalized()`` bounds every field."""

    threads: int = 0  # 0 = auto (8, or 2x CPU count capped at 16)
    buffer_bytes: int = 4 * 1024 * 1024
    pipeline: bool = True
    pipeline_min_bytes: int = 16 * 1024 * 1024
    pipeline_depth: int = 4
    verify: str = "size"
    overwrite: str = "overwrite"
    preserve: bool = True
    move: bool = False
    skip_hidden: bool = False
    links: str = "follow"
    exclude: tuple = ()
    basename: bool = False
    dry_run: bool = False
    force: bool = False

    def normalized(self) -> "CopyOptions":
        threads = _clamp_int(self.threads, 0, MAX_THREADS, 0)
        if threads == 0:
            threads = max(2, min(16, (os.cpu_count() or 4) * 2))
        exclude = self.exclude
        if isinstance(exclude, str):
            exclude = tuple(part.strip() for part in exclude.replace(";", ",").split(","))
        clean_exclude = tuple(str(p).strip() for p in (exclude or ()) if str(p).strip())
        verify = _one_of(self.verify, VERIFY_MODES, "size")
        move = bool(self.move)
        if move and verify == "none":
            # Moving deletes the source: never do that without at least a size check.
            verify = "size"
        return CopyOptions(
            threads=threads,
            buffer_bytes=_clamp_int(self.buffer_bytes, MIN_BUFFER, MAX_BUFFER, 4 * 1024 * 1024),
            pipeline=bool(self.pipeline),
            pipeline_min_bytes=_clamp_int(
                self.pipeline_min_bytes, 0, MAX_PIPELINE_MIN, 16 * 1024 * 1024
            ),
            pipeline_depth=_clamp_int(self.pipeline_depth, 1, MAX_PIPELINE_DEPTH, 4),
            verify=verify,
            overwrite=_one_of(self.overwrite, OVERWRITE_POLICIES, "overwrite"),
            preserve=bool(self.preserve),
            move=move,
            skip_hidden=bool(self.skip_hidden),
            links=_one_of(self.links, LINK_MODES, "follow"),
            exclude=clean_exclude,
            basename=bool(self.basename),
            dry_run=bool(self.dry_run),
            force=bool(self.force),
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class Job:
    """One unit of work: a file (or link) to write."""

    src: str  # extended path
    dst: str  # extended path
    size: int
    mode: int = 0o666
    atime_ns: int = 0
    mtime_ns: int = 0
    attrs: int = 0
    kind: str = "file"  # file | link
    link_target: str = ""
    display_src: str = ""
    display_dst: str = ""


@dataclass
class Plan:
    """Everything the scan decided, before a single byte is written."""

    src: str
    dst: str
    files: list = field(default_factory=list)
    dirs: list = field(default_factory=list)
    links: list = field(default_factory=list)
    skipped: list = field(default_factory=list)  # (display path, reason)
    bytes_total: int = 0
    bytes_needed: int = 0
    dirs_total: int = 0

    @property
    def files_total(self) -> int:
        return len(self.files)

    @property
    def items_total(self) -> int:
        return len(self.files) + len(self.links)


def _matches_exclude(display_path: str, name: str, patterns: Sequence[str]) -> bool:
    rel = display_path.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.normpath(a)) == os.path.normcase(os.path.normpath(b))


def _is_inside(child: str, parent: str) -> bool:
    child = os.path.normcase(os.path.normpath(child))
    parent = os.path.normcase(os.path.normpath(parent))
    return child.startswith(parent.rstrip("\\/") + os.sep)


def build_plan(
    src: str,
    dst: str,
    options: CopyOptions | None = None,
    on_error: Callable[[str], None] | None = None,
) -> Plan:
    """Scan *src* and decide exactly what will be written where.

    Raises :class:`CopyPlanError` for a bad source or an unsafe destination,
    when the copy would overwrite the source or recurse into itself.
    """
    opts = (options or CopyOptions()).normalized()
    src_abs = os.path.abspath(os.path.expanduser(src))
    dst_abs = os.path.abspath(os.path.expanduser(dst))
    # Every filesystem probe goes through the extended form, so a source or
    # destination that is itself longer than 260 characters still works.
    src_ext = extended_path(src_abs)
    dst_ext = extended_path(dst_abs)

    if not os.path.lexists(src_ext):
        raise CopyPlanError(f"source does not exist: {src_abs}")
    if _same_path(src_abs, dst_abs):
        raise CopyPlanError("source and destination are the same path")

    def report(message: str) -> None:
        if on_error:
            on_error(message)

    src_is_dir = os.path.isdir(src_ext) and not os.path.islink(src_ext)

    if src_is_dir:
        dst_root = dst_abs
        if opts.basename:
            dst_root = os.path.join(dst_abs, os.path.basename(os.path.normpath(src_abs)))
        if os.path.isfile(extended_path(dst_root)):
            raise CopyPlanError(f"destination is a file, not a folder: {dst_root}")
        if _is_inside(dst_root, src_abs) and not opts.force:
            raise CopyPlanError(
                "destination is inside the source folder — that would copy the "
                "copy; pass --force if you really mean it"
            )
        plan = Plan(src=src_abs, dst=dst_root)
        plan.dirs.append(dst_root)
        stack = [(src_abs, dst_root)]
        while stack:
            cur_src, cur_dst = stack.pop()
            try:
                entries = list(os.scandir(extended_path(cur_src)))
            except OSError as exc:
                report(f"cannot read folder {cur_src}: {exc.strerror or exc}")
                continue
            for entry in entries:
                child_src = os.path.join(cur_src, entry.name)
                child_dst = os.path.join(cur_dst, entry.name)
                display_src = plain_path(child_src)
                if _matches_exclude(display_src, entry.name, opts.exclude):
                    plan.skipped.append((display_src, "excluded"))
                    continue
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    report(f"cannot read {display_src}: {exc.strerror or exc}")
                    continue
                attrs = getattr(st, "st_file_attributes", 0)
                if opts.skip_hidden and attrs & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM):
                    plan.skipped.append((display_src, "hidden/system"))
                    continue
                is_link = entry.is_symlink()
                if is_link and opts.links == "skip":
                    plan.skipped.append((display_src, "symlink"))
                    continue
                if is_link and opts.links == "copy":
                    plan.links.append(
                        Job(
                            src=extended_path(child_src),
                            dst=extended_path(child_dst),
                            size=0,
                            kind="link",
                            link_target=os.readlink(plain_path(child_src)),
                            display_src=display_src,
                            display_dst=plain_path(child_dst),
                        )
                    )
                    continue
                if is_link:  # follow: use the target's own stat data
                    try:
                        st = entry.stat(follow_symlinks=True)
                        attrs = getattr(st, "st_file_attributes", attrs)
                    except OSError as exc:
                        report(f"cannot follow link {display_src}: {exc.strerror or exc}")
                        continue
                if stat.S_ISDIR(st.st_mode):
                    plan.dirs.append(child_dst)
                    stack.append((child_src, child_dst))
                    continue
                if not stat.S_ISREG(st.st_mode):
                    plan.skipped.append((display_src, "not a regular file"))
                    continue
                job = Job(
                    src=extended_path(child_src),
                    dst=extended_path(child_dst),
                    size=int(st.st_size),
                    mode=st.st_mode,
                    atime_ns=int(getattr(st, "st_atime_ns", 0)),
                    mtime_ns=int(getattr(st, "st_mtime_ns", 0)),
                    attrs=attrs,
                    display_src=display_src,
                    display_dst=plain_path(child_dst),
                )
                _apply_overwrite_policy(job, opts, plan, report)
        plan.dirs_total = max(0, len(plan.dirs) - 1)
        return plan

    # ---- single file -----------------------------------------------------
    dst_file = dst_abs
    if os.path.isdir(dst_ext) and not os.path.islink(dst_ext):
        dst_file = os.path.join(dst_abs, os.path.basename(src_abs))
    elif dst_abs.endswith(("\\", "/")):
        dst_file = os.path.join(dst_abs, os.path.basename(src_abs))
    plan = Plan(src=src_abs, dst=dst_file)
    try:
        st = os.stat(extended_path(src_abs))
    except OSError as exc:
        raise CopyPlanError(f"cannot read source: {exc.strerror or exc}") from exc
    if stat.S_ISDIR(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise CopyPlanError("source is not a regular file")
    job = Job(
        src=extended_path(src_abs),
        dst=extended_path(dst_file),
        size=int(st.st_size),
        mode=st.st_mode,
        atime_ns=int(getattr(st, "st_atime_ns", 0)),
        mtime_ns=int(getattr(st, "st_mtime_ns", 0)),
        attrs=getattr(st, "st_file_attributes", 0),
        display_src=src_abs,
        display_dst=dst_file,
    )
    _apply_overwrite_policy(job, opts, plan, report)
    return plan


def _apply_overwrite_policy(job: Job, opts: CopyOptions, plan: Plan, report) -> None:
    """Decide whether *job* is copied, and keep the running totals."""
    existing = None
    try:
        existing = os.stat(job.dst)
    except OSError:
        existing = None
    if existing is not None:
        if stat.S_ISDIR(existing.st_mode):
            report(f"destination is a folder where a file is expected: {job.display_dst}")
            plan.skipped.append((job.display_src, "destination is a folder"))
            return
        if opts.overwrite == "skip":
            plan.skipped.append((job.display_src, "destination exists"))
            return
        if opts.overwrite == "newer":
            same_or_newer = int(existing.st_mtime_ns) >= job.mtime_ns
            same_size = int(existing.st_size) == job.size
            if same_or_newer and same_size:
                plan.skipped.append((job.display_src, "destination is up to date"))
                return
    plan.files.append(job)
    plan.bytes_total += job.size
    if existing is None or int(existing.st_size) != job.size:
        plan.bytes_needed += job.size


# ---------------------------------------------------------------------------
# Progress
# ---------------------------------------------------------------------------
class CopyStats:
    """Lock-protected counters.  Safe to read from another thread at any time."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.files_total = 0
        self.dirs_total = 0
        self.bytes_total = 0
        self.files_done = 0
        self.dirs_done = 0
        self.bytes_done = 0
        self.errors: list = []
        self.skipped: list = []
        self.active: dict = {}
        self.partial: list = []
        self.started_at = 0.0
        self.finished_at = 0.0
        self.cancelled = False
        self.plan = None
        self._samples: deque = deque(maxlen=256)

    # -- mutation ----------------------------------------------------------
    def begin(self, plan: Plan) -> None:
        with self._lock:
            self.plan = plan
            self.files_total = len(plan.files) + len(plan.links)
            self.dirs_total = plan.dirs_total
            self.bytes_total = plan.bytes_total
            self.skipped = list(plan.skipped)
            self.started_at = time.monotonic()
            self._samples.clear()
            self._samples.append((self.started_at, 0))

    def add_bytes(self, count: int) -> None:
        if count <= 0:
            return
        now = time.monotonic()
        with self._lock:
            self.bytes_done += count
            last_t = self._samples[-1][0] if self._samples else 0.0
            if now - last_t >= 0.25:
                self._samples.append((now, self.bytes_done))

    def file_started(self, name: str) -> None:
        with self._lock:
            self.active[name] = time.monotonic()

    def file_done(self, name: str) -> None:
        with self._lock:
            self.active.pop(name, None)
            self.files_done += 1

    def add_dir_done(self, count: int = 1) -> None:
        with self._lock:
            self.dirs_done += count

    def add_error(self, message: str) -> None:
        with self._lock:
            self.errors.append(message)
            if len(self.errors) > 500:
                del self.errors[:100]

    def add_skip(self, path: str, reason: str) -> None:
        with self._lock:
            self.skipped.append((path, reason))

    def mark_partial(self, name: str) -> None:
        """Remember a destination that was left not matching the source."""
        with self._lock:
            if name not in self.partial:
                self.partial.append(name)

    def finish(self) -> None:
        with self._lock:
            self.finished_at = time.monotonic()
            for name in self.active:
                if name not in self.partial:
                    self.partial.append(name)
            self.partial.sort()

    # -- reading -----------------------------------------------------------
    def _instant_speed_locked(self, now: float) -> float:
        if len(self._samples) < 2:
            return 0.0
        window_start = now - 2.5
        first = None
        for stamp, total in self._samples:
            if stamp >= window_start:
                first = (stamp, total)
                break
        if first is None:
            first = self._samples[0]
        last = self._samples[-1]
        span = last[0] - first[0]
        if span <= 0.05:
            return 0.0
        return max(0.0, (last[1] - first[1]) / span)

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            start = self.started_at or now
            end = self.finished_at or now
            elapsed = max(0.0, end - start)
            done = self.finished_at > 0
            bytes_done = self.bytes_done
            total = self.bytes_total
            percent = (bytes_done / total * 100.0) if total else (100.0 if done else 0.0)
            # A short copy still deserves a real speed figure — the earlier
            # half-second gate reported "0 B/s" for a fast 40 MB copy. Below
            # ~50 ms the measurement is noise, so report nothing instead of a spike.
            average = (bytes_done / elapsed) if elapsed > 0.05 else 0.0
            instant = 0.0 if done else self._instant_speed_locked(now)
            speed = instant or average
            remaining = max(0, total - bytes_done)
            eta = (remaining / speed) if (speed > 0 and not done and remaining) else 0.0
            return {
                "files_total": self.files_total,
                "files_done": self.files_done,
                "dirs_total": self.dirs_total,
                "bytes_total": total,
                "bytes_done": bytes_done,
                "percent": min(100.0, percent),
                "elapsed": elapsed,
                "speed": speed,
                "speed_average": average,
                "eta": eta,
                "errors": list(self.errors),
                "skipped": list(self.skipped),
                "active": sorted(self.active),
                "partial": list(self.partial),
                "cancelled": self.cancelled,
                "done": done,
            }

    def summary(self) -> dict:
        snap = self.snapshot()
        return {
            "files_copied": snap["files_done"],
            "files_planned": snap["files_total"],
            "bytes_copied": snap["bytes_done"],
            "bytes_planned": snap["bytes_total"],
            "skipped": len(self.skipped),
            "errors": len(self.errors),
            "error_messages": list(self.errors[:50]),
            "skipped_items": list(self.skipped[:50]),
            "elapsed_seconds": round(snap["elapsed"], 3),
            "average_bytes_per_second": round(snap["speed_average"], 1),
            "cancelled": snap["cancelled"],
            "partial_files": list(snap["partial"]),
        }


# ---------------------------------------------------------------------------
# Low-level copy primitives
# ---------------------------------------------------------------------------
def _write_all(handle, view: memoryview) -> None:
    """Write the whole memoryview — ``FileIO.write`` may write partially."""
    offset = 0
    length = len(view)
    while offset < length:
        written = handle.write(view[offset:])
        if written is None:  # a buffered object accepted everything
            return
        if written <= 0:
            raise OSError("the destination stopped accepting data")
        offset += written


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _prepare_destination(job: Job) -> None:
    """Make *job.dst* safe to open for writing."""
    try:
        st = os.stat(job.dst, follow_symlinks=False)
    except OSError:
        return
    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(f"destination is a folder: {plain_path(job.dst)}")
    if stat.S_ISLNK(st.st_mode) or stat.S_ISREG(st.st_mode):
        # Never write *through* a link; replace the link itself.
        if stat.S_ISLNK(st.st_mode):
            try:
                os.unlink(job.dst)
            except OSError:
                pass
        _clear_readonly(job.dst, _attributes_of(job.dst))


def _copy_sequential(job: Job, opts: CopyOptions, stats: CopyStats, cancel: threading.Event) -> None:
    """Plain buffered loop — the right choice for small and medium files."""
    buffer = bytearray(opts.buffer_bytes)
    with open(job.src, "rb", buffering=0) as fin:
        with open(job.dst, "wb", buffering=0) as fout:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                count = fin.readinto(buffer)
                if not count:
                    break
                _write_all(fout, memoryview(buffer)[:count])
                stats.add_bytes(count)


class _Slot:
    """One buffer travelling from the reader thread to the writer thread."""

    __slots__ = ("buffer", "count", "error")

    def __init__(self, buffer, count: int = 0, error: BaseException | None = None) -> None:
        self.buffer = buffer
        self.count = count
        self.error = error


_READER_SENTINEL = _Slot(None, 0)


def _put(slot_queue: "queue.Queue", item: _Slot, stop: threading.Event) -> bool:
    """Bounded put that gives up as soon as the writer asked us to stop."""
    while not stop.is_set():
        try:
            slot_queue.put(item, timeout=0.2)
            return True
        except queue.Full:
            continue
    return False


def _copy_pipelined(job: Job, opts: CopyOptions, stats: CopyStats, cancel: threading.Event) -> None:
    """Overlap reading and writing of one large file.

    The reader thread fills buffers; the writer thread drains them in order.
    The destination is therefore written strictly sequentially while the OS is
    already reading the next chunk.
    """
    depth = max(1, opts.pipeline_depth)
    slot_queue: "queue.Queue[_Slot]" = queue.Queue(maxsize=depth)
    free_buffers: "queue.Queue[bytearray]" = queue.Queue()
    for _ in range(depth):
        free_buffers.put(bytearray(opts.buffer_bytes))
    stop = threading.Event()

    def reader() -> None:
        try:
            with open(job.src, "rb", buffering=0) as fin:
                while not stop.is_set() and not cancel.is_set():
                    try:
                        buffer = free_buffers.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    try:
                        count = fin.readinto(buffer)
                    except BaseException as exc:  # surfaced to the writer thread
                        _put(slot_queue, _Slot(None, 0, exc), stop)
                        return
                    if not count:
                        free_buffers.put(buffer)
                        _put(slot_queue, _READER_SENTINEL, stop)
                        return
                    if not _put(slot_queue, _Slot(buffer, count), stop):
                        return
        except BaseException as exc:
            _put(slot_queue, _Slot(None, 0, exc), stop)

    thread = threading.Thread(target=reader, name="fatcopy-reader", daemon=True)
    thread.start()
    try:
        with open(job.dst, "wb", buffering=0) as fout:
            while True:
                if cancel.is_set():
                    raise Cancelled()
                slot = slot_queue.get()
                if slot is _READER_SENTINEL:
                    break
                if slot.error is not None:
                    raise slot.error
                _write_all(fout, memoryview(slot.buffer)[: slot.count])
                stats.add_bytes(slot.count)
                free_buffers.put(slot.buffer)
    finally:
        stop.set()
        thread.join(timeout=10)


def _verify_job(job: Job, opts: CopyOptions, cancel: threading.Event) -> None:
    """Raise when the written file does not match the source."""
    if opts.verify == "none":
        return
    try:
        written = os.path.getsize(job.dst)
    except OSError as exc:
        raise CopyMismatch(f"cannot read back the copy: {exc.strerror or exc}") from exc
    if written != job.size:
        raise CopyMismatch(f"size mismatch: source {job.size} bytes, copy {written} bytes")
    if opts.verify != "content":
        return
    buffer_a = bytearray(opts.buffer_bytes)
    buffer_b = bytearray(opts.buffer_bytes)
    with open(job.src, "rb", buffering=0) as fin:
        with open(job.dst, "rb", buffering=0) as fout:
            offset = 0
            while True:
                if cancel.is_set():
                    raise Cancelled()
                count_a = fin.readinto(buffer_a)
                count_b = fout.readinto(buffer_b)
                if count_a != count_b:
                    raise CopyMismatch(f"content mismatch at byte offset {offset}")
                if not count_a:
                    break
                if memoryview(buffer_a)[:count_a] != memoryview(buffer_b)[:count_b]:
                    raise CopyMismatch(f"content mismatch at byte offset {offset}")
                offset += count_a


def _apply_metadata(job: Job, opts: CopyOptions) -> None:
    """Timestamps first, attributes last (a read-only file cannot be utime'd)."""
    if not opts.preserve:
        return
    try:
        os.utime(job.dst, ns=(job.atime_ns, job.mtime_ns))
    except OSError:
        pass
    if IS_WINDOWS:
        carried = job.attrs & _CARRIED_ATTRIBUTES
        if carried:
            _set_file_attributes(job.dst, carried)
    else:
        try:
            os.chmod(job.dst, stat.S_IMODE(job.mode))
        except OSError:
            pass


def _note_incomplete(job: Job, stats: CopyStats) -> None:
    """Record a destination that exists but does not match the source size.

    Only files that were actually touched are named, so a permission error on
    the *source* never reports a phantom half-written destination.
    """
    try:
        written = os.path.getsize(job.dst)
    except OSError:
        return
    if written != job.size:
        stats.mark_partial(job.display_dst)


def _copy_one(job: Job, opts: CopyOptions, stats: CopyStats, cancel: threading.Event) -> None:
    """Copy a single planned job and everything that must follow it."""
    stats.file_started(job.display_dst)
    try:
        _ensure_parent(job.dst)
        _prepare_destination(job)
        if opts.pipeline and job.size >= opts.pipeline_min_bytes and job.size > 0:
            _copy_pipelined(job, opts, stats, cancel)
        else:
            _copy_sequential(job, opts, stats, cancel)
        _verify_job(job, opts, cancel)
        _apply_metadata(job, opts)
    except BaseException:
        # A cancelled or failed write must never be silent: name the file.
        _note_incomplete(job, stats)
        raise
    finally:
        stats.file_done(job.display_dst)
    if opts.move:
        try:
            os.remove(job.src)
        except OSError as exc:
            raise OSError(f"copy verified but the source could not be removed: {exc}") from exc


def _make_link(job: Job, opts: CopyOptions, cancel: threading.Event) -> None:
    """Recreate a symbolic link (``links='copy'``), falling back to a copy."""
    if cancel.is_set():
        raise Cancelled()
    _ensure_parent(job.dst)
    try:
        if os.path.lexists(job.dst):
            os.unlink(job.dst)
        os.symlink(job.link_target, job.dst, target_is_directory=os.path.isdir(job.src))
    except OSError:
        # Windows needs Developer Mode / elevation for symlinks — fall back to
        # copying the link target's contents so nothing is silently lost.
        if os.path.isfile(job.src):
            try:
                os.stat(job.src)
            except OSError:
                return
            shutil.copyfile(job.src, job.dst)
        else:
            os.makedirs(job.dst, exist_ok=True)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_copy(
    src: str,
    dst: str,
    options: CopyOptions | None = None,
    *,
    cancel: threading.Event | None = None,
    stats: CopyStats | None = None,
    on_file: Callable[[Job, str], None] | None = None,
    on_error: Callable[[str], None] | None = None,
    on_log: Callable[[str], None] | None = None,
    on_plan: Callable[[Plan], None] | None = None,
) -> CopyStats:
    """Copy *src* to *dst* and return the run's :class:`CopyStats`.

    ``on_file(job, phase)`` is called with phase ``start``/``done``;
    ``on_error(message)`` for anything that failed; ``on_log(message)`` for
    informational lines; ``on_plan(plan)`` once the scan is complete.
    """
    opts = (options or CopyOptions()).normalized()
    cancel = cancel or threading.Event()
    stats = stats or CopyStats()

    def log(message: str) -> None:
        if on_log:
            on_log(message)

    def error(message: str) -> None:
        stats.add_error(message)
        if on_error:
            on_error(message)

    plan = build_plan(src, dst, opts, on_error=error)
    stats.begin(plan)
    if on_plan:
        on_plan(plan)

    log(
        f"Plan: {len(plan.files)} files ({human_bytes(plan.bytes_total)}), "
        f"{plan.dirs_total} folders, {len(plan.skipped)} skipped"
    )
    if opts.dry_run:
        stats.finish()
        log("Dry run — nothing was written")
        return stats

    free = free_space_for(plain_path(plan.dst))
    if free is not None and plan.bytes_needed > free:
        message = (
            f"not enough free space on the destination: "
            f"{human_bytes(plan.bytes_needed)} needed, {human_bytes(free)} free"
        )
        if opts.force:
            log("Warning: " + message)
        else:
            error(message + " — free some space or pass --force")

    # Folders first, in plan order, so every parent exists before its files.
    for folder in plan.dirs:
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError as exc:
            error(f"cannot create folder {plain_path(folder)}: {exc.strerror or exc}")

    for job in plan.links:
        if cancel.is_set():
            break
        try:
            _make_link(job, opts, cancel)
            stats.file_done(job.display_dst)
        except Cancelled:
            break
        except OSError as exc:
            stats.file_done(job.display_dst)
            error(f"{job.display_src} -> {job.display_dst}: {exc.strerror or exc}")

    work: "queue.Queue[Job | None]" = queue.Queue(maxsize=max(8, opts.threads * 4))

    def worker() -> None:
        while True:
            job = work.get()
            try:
                if job is None:
                    return
                if cancel.is_set():
                    continue
                if on_file:
                    on_file(job, "start")
                _copy_one(job, opts, stats, cancel)
                if on_file:
                    on_file(job, "done")
            except Cancelled:
                pass
            except BaseException as exc:  # keep the rest of the copy running
                detail = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
                error(f"{job.display_src} -> {job.display_dst}: {type(exc).__name__}: {detail}")
            finally:
                work.task_done()

    workers = [
        threading.Thread(target=worker, name=f"fatcopy-worker-{i}", daemon=True)
        for i in range(opts.threads)
    ]
    for thread in workers:
        thread.start()
    try:
        for job in plan.files:
            if cancel.is_set():
                break
            work.put(job)
    finally:
        for _ in workers:
            work.put(None)
        for thread in workers:
            thread.join()

    if cancel.is_set():
        stats.cancelled = True

    # Folder timestamps last, so writing files inside does not reset them.
    if opts.preserve and not cancel.is_set():
        for folder in reversed(plan.dirs):
            try:
                relative = os.path.relpath(folder, plan.dst)
                source_folder = plan.src if relative == "." else os.path.join(plan.src, relative)
                source_st = os.stat(extended_path(source_folder))
                os.utime(folder, ns=(source_st.st_atime_ns, source_st.st_mtime_ns))
            except (OSError, ValueError):
                continue

    # A move only tidies up folders once every file arrived safely.
    if opts.move and not cancel.is_set() and not stats.errors:
        for folder in reversed(plan.dirs):
            try:
                relative = os.path.relpath(folder, plan.dst)
                source_folder = plan.src if relative == "." else os.path.join(plan.src, relative)
                os.rmdir(extended_path(source_folder))
            except (OSError, ValueError):
                continue

    stats.finish()
    if stats.cancelled:
        log("Cancelled — files still being written are listed as partial")
    elif stats.errors:
        log(f"Finished with {len(stats.errors)} problem(s)")
    else:
        log("Finished")
    return stats


def verify_plan(
    plan: Plan,
    options: CopyOptions | None = None,
    *,
    cancel: threading.Event | None = None,
    stats: CopyStats | None = None,
    on_error: Callable[[str], None] | None = None,
) -> CopyStats:
    """Re-check an already copied plan without writing anything."""
    opts = (options or CopyOptions()).normalized()
    check_opts = replace(opts, verify="content")
    cancel = cancel or threading.Event()
    stats = stats or CopyStats()
    stats.begin(plan)
    for job in plan.files:
        if cancel.is_set():
            stats.cancelled = True
            break
        try:
            _verify_job(job, check_opts, cancel)
            stats.file_done(job.display_dst)
        except Cancelled:
            stats.cancelled = True
            break
        except BaseException as exc:
            detail = getattr(exc, "strerror", None) or str(exc) or exc.__class__.__name__
            message = f"{job.display_src}: {detail}"
            stats.add_error(message)
            if on_error:
                on_error(message)
            stats.file_done(job.display_dst)
    stats.finish()
    return stats


# ---------------------------------------------------------------------------
# Settings file (used by the GUI)
# ---------------------------------------------------------------------------
def config_dir() -> str:
    """Per-user settings folder: ``%APPDATA%\\Fatcopy`` (or ``~/.fatcopy``)."""
    base = os.environ.get("APPDATA") or os.environ.get("XDG_CONFIG_HOME")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "Fatcopy")


def config_path() -> str:
    return os.path.join(config_dir(), "settings.json")


def load_config() -> dict:
    """Read the settings file, tolerating absence and corruption."""
    import json

    path = config_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        # Keep the damaged file, start clean.
        try:
            os.replace(path, path + ".bak")
        except OSError:
            pass
        return {}
    return data if isinstance(data, dict) else {}


def save_config(values: dict) -> bool:
    """Write the settings file atomically, keeping one ``.bak`` of the old one."""
    import json

    directory = config_dir()
    path = config_path()
    try:
        os.makedirs(directory, exist_ok=True)
        if os.path.isfile(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError:
                pass
        temporary = path + ".tmp"
        clean = {str(k): v for k, v in (values or {}).items()}
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=2, sort_keys=True)
        os.replace(temporary, path)
        return True
    except OSError:
        return False
