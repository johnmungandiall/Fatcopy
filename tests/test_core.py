"""Tests for the Fatcopy engine, the CLI helpers and the settings file.

Run them with:  python -m pytest -q   (from the project root)

The suite is deliberately small in data (a few megabytes) but wide in shape:
folders, single files, zero-byte files, unicode names, very long paths,
read-only destinations, cancellation, every overwrite/verify policy, and the
settings file's corruption path.
"""

from __future__ import annotations

import argparse
import os
import stat
import threading
import time

import pytest

from fatcopy import core
from fatcopy.cli import parse_size
from fatcopy.core import (
    CopyOptions,
    CopyPlanError,
    CopyStats,
    build_plan,
    extended_path,
    human_bytes,
    human_time,
    load_config,
    plain_path,
    run_copy,
    save_config,
    verify_plan,
)

WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def make_tree(root: str, files: dict) -> None:
    for relative, payload in files.items():
        target = os.path.join(root, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(payload)


def read_bytes(path: str) -> bytes:
    with open(extended_path(path), "rb") as handle:
        return handle.read()


def collect(root: str) -> dict:
    """Map of relative path -> bytes for every file under *root*.

    Keys always use forward slashes, so a test can compare against the dict it
    wrote whatever separator the platform uses.
    """
    found = {}
    for base, _dirs, names in os.walk(extended_path(root)):
        for name in names:
            full = os.path.join(base, name)
            with open(full, "rb") as handle:
                key = os.path.relpath(plain_path(full), root).replace(os.sep, "/")
                found[key] = handle.read()
    return found


# ---------------------------------------------------------------------------
# size / time formatting and option clamping
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("4M", 4 * 1024 * 1024),
        ("4MB", 4 * 1024 * 1024),
        ("512kb", 512 * 1024),
        ("1.5G", int(1.5 * 1024 ** 3)),
        ("1048576", 1048576),
        ("64 KiB", 64 * 1024),
        ("0", 0),
    ],
)
def test_parse_size_accepts_human_units(text, expected):
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "abc", "M", "4X", "1MB2", "-4M"])
def test_parse_size_rejects_garbage(text):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_size(text)


def test_human_bytes_and_time_readability():
    assert human_bytes(0) == "0 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024 ** 3) == "5.0 GB"
    assert human_time(9) == "9s"
    assert human_time(125) == "2m 05s"
    assert human_time(7325) == "2h 02m"


def test_options_are_bounded_and_repaired():
    options = CopyOptions(threads=9999, buffer_bytes=1, verify="bogus", overwrite="nonsense",
                          pipeline_depth=500, exclude="*.tmp, ,Thumbs.db").normalized()
    assert options.threads == core.MAX_THREADS
    assert options.buffer_bytes == core.MIN_BUFFER
    assert options.verify == "size"
    assert options.overwrite == "overwrite"
    assert options.pipeline_depth == core.MAX_PIPELINE_DEPTH
    assert options.exclude == ("*.tmp", "Thumbs.db")


def test_options_auto_threads_and_move_safety():
    auto = CopyOptions(threads=0).normalized()
    assert 2 <= auto.threads <= 16
    moved = CopyOptions(move=True, verify="none").normalized()
    assert moved.verify == "size"  # a move may never delete an unchecked source
    assert CopyOptions(threads="abc").normalized().threads >= 2
    assert CopyOptions(threads=float("nan")).normalized().threads >= 2
    assert CopyOptions(threads=float("inf")).normalized().threads == core.MAX_THREADS


def test_extended_and_plain_paths_round_trip(tmp_path):
    plain = str(tmp_path / "a" / "b.txt")
    if WINDOWS:
        assert extended_path(plain).startswith("\\\\?\\")
        assert plain_path(extended_path(plain)) == os.path.abspath(plain)
        assert extended_path("\\\\server\\share\\x").startswith("\\\\?\\UNC\\")
    else:
        assert extended_path(plain) == plain


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------
def test_plan_directory_counts_files_folders_and_bytes(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"a" * 10, "sub/b.txt": b"b" * 20, "sub/deep/c.bin": b"c" * 5})
    plan = build_plan(str(source), str(tmp_path / "dst"))
    assert plan.files_total == 3
    assert plan.dirs_total == 2
    assert plan.bytes_total == 35
    assert plan.bytes_needed == 35


def test_plan_exclude_patterns(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"keep.txt": b"1", "drop.tmp": b"2", "sub/also.tmp": b"3"})
    plan = build_plan(str(source), str(tmp_path / "dst"), CopyOptions(exclude=("*.tmp",)))
    assert plan.files_total == 1
    assert {reason for _path, reason in plan.skipped} == {"excluded"}


def test_plan_basename_puts_the_source_folder_inside_the_destination(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"1"})
    plan = build_plan(str(source), str(tmp_path / "dst"), CopyOptions(basename=True))
    assert plan.dst == str(tmp_path / "dst" / "src")
    assert plan.files[0].display_dst.endswith(os.path.join("src", "a.txt"))


def test_plan_rejects_missing_source(tmp_path):
    with pytest.raises(CopyPlanError):
        build_plan(str(tmp_path / "nope"), str(tmp_path / "dst"))


def test_plan_rejects_same_source_and_destination(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"1"})
    with pytest.raises(CopyPlanError):
        build_plan(str(source), str(source))


def test_plan_rejects_destination_inside_source(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"1"})
    with pytest.raises(CopyPlanError):
        build_plan(str(source), str(source / "inner"))


def test_plan_allows_destination_inside_source_when_forced(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"1"})
    plan = build_plan(str(source), str(source / "inner"), CopyOptions(force=True))
    assert plan.files_total == 1


def test_plan_single_file_into_a_folder_and_under_a_new_name(tmp_path):
    source = tmp_path / "one.bin"
    source.write_bytes(b"payload")
    folder = tmp_path / "folder"
    folder.mkdir()
    assert build_plan(str(source), str(folder)).files[0].display_dst == str(folder / "one.bin")
    assert build_plan(str(source), str(tmp_path / "renamed.bin")).files[0].display_dst == str(
        tmp_path / "renamed.bin"
    )
    # A destination that does not exist yet is used as the new file name.
    assert build_plan(str(source), str(tmp_path / "fresh.bin")).files[0].display_dst == str(
        tmp_path / "fresh.bin"
    )


def test_plan_reports_a_folder_sitting_where_a_file_goes(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"clash.txt": b"payload"})
    destination = tmp_path / "dst"
    (destination / "clash.txt").mkdir(parents=True)
    plan = build_plan(str(source), str(destination))
    assert plan.files_total == 0
    assert plan.skipped and plan.skipped[0][1] == "destination is a folder"


def test_plan_overwrite_policies(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "same.txt").write_bytes(b"old")
    destination = tmp_path / "dst"
    destination.mkdir()
    (destination / "same.txt").write_bytes(b"old")

    skipping = build_plan(str(source), str(destination), CopyOptions(overwrite="skip"))
    assert skipping.files_total == 0

    newer = build_plan(str(source), str(destination), CopyOptions(overwrite="newer"))
    assert newer.files_total == 0

    replacing = build_plan(str(source), str(destination), CopyOptions(overwrite="overwrite"))
    assert replacing.files_total == 1
    assert replacing.bytes_needed == 0  # same size, so no new space is needed


# ---------------------------------------------------------------------------
# copying
# ---------------------------------------------------------------------------
def test_copy_folder_round_trip_with_content_verification(tmp_path):
    source = tmp_path / "src"
    payloads = {"a.txt": b"a" * 1000, "sub/b.bin": b"b" * 200000, "sub/deep/c.dat": b""}
    make_tree(str(source), payloads)
    stats = run_copy(str(source), str(tmp_path / "dst"), CopyOptions(verify="content"))
    assert stats.errors == []
    assert stats.files_done == 3
    assert collect(str(tmp_path / "dst")) == payloads
    summary = stats.summary()
    assert summary["files_copied"] == 3
    assert summary["bytes_copied"] == sum(len(v) for v in payloads.values())
    assert summary["elapsed_seconds"] >= 0
    assert summary["partial_files"] == []


def test_copy_single_file_and_zero_byte_file(tmp_path):
    source = tmp_path / "one.bin"
    source.write_bytes(b"x" * 4096)
    destination = tmp_path / "out"
    destination.mkdir()
    stats = run_copy(str(source), str(destination), CopyOptions(verify="content"))
    assert stats.errors == []
    assert (destination / "one.bin").read_bytes() == b"x" * 4096

    empty = tmp_path / "empty.bin"
    empty.write_bytes(b"")
    empty_out = tmp_path / "empty-out"
    empty_out.mkdir()
    stats = run_copy(str(empty), str(empty_out), CopyOptions(verify="content"))
    assert stats.errors == []
    assert (empty_out / "empty.bin").read_bytes() == b""


def test_copy_uses_the_overlapped_pipeline_and_still_matches_byte_for_byte(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"big.bin": bytes(range(256)) * 8192})  # 2 MiB, non-repeating pattern
    options = CopyOptions(verify="content", pipeline=True, pipeline_min_bytes=0, pipeline_depth=3)
    stats = run_copy(str(source), str(tmp_path / "dst"), options)
    assert stats.errors == []
    assert collect(str(tmp_path / "dst")) == collect(str(source))


def test_copy_many_files_in_parallel_matches_the_source(tmp_path):
    source = tmp_path / "src"
    payloads = {f"part-{i}.bin": bytes([i % 251]) * (5000 + i) for i in range(40)}
    make_tree(str(source), payloads)
    options = CopyOptions(threads=8, verify="size")
    stats = run_copy(str(source), str(tmp_path / "dst"), options)
    assert stats.errors == []
    assert stats.files_done == 40
    assert collect(str(tmp_path / "dst")) == payloads


def test_copy_handles_unicode_and_spaces_in_names(tmp_path):
    source = tmp_path / "src"
    payloads = {"héllo wörld.txt": b"unicode", "sub dir/naïve — file.bin": b"deep"}
    make_tree(str(source), payloads)
    stats = run_copy(str(source), str(tmp_path / "dst"), CopyOptions(verify="content"))
    assert stats.errors == []
    assert collect(str(tmp_path / "dst")) == payloads


def test_copy_preserves_timestamps(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"old.txt": b"data"})
    when = time.time() - 86400 * 30
    os.utime(str(source / "old.txt"), (when, when))
    run_copy(str(source), str(tmp_path / "dst"), CopyOptions(verify="size"))
    copied = tmp_path / "dst" / "old.txt"
    assert abs(copied.stat().st_mtime - when) < 2


@pytest.mark.skipif(not WINDOWS, reason="read-only file attribute is Windows-only")
def test_copy_replaces_a_read_only_destination(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"locked.txt": b"new-content"})
    destination = tmp_path / "dst"
    destination.mkdir()
    locked = destination / "locked.txt"
    locked.write_bytes(b"old")
    assert core._set_file_attributes(str(locked), core.FILE_ATTRIBUTE_READONLY)
    stats = run_copy(str(source), str(destination), CopyOptions(verify="content"))
    assert stats.errors == []
    assert locked.read_bytes() == b"new-content"
    attributes = core._attributes_of(str(locked))
    assert not attributes & core.FILE_ATTRIBUTE_READONLY


def test_copy_dry_run_writes_nothing(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"1" * 100})
    destination = tmp_path / "dst"
    stats = run_copy(str(source), str(destination), CopyOptions(dry_run=True))
    assert stats.files_done == 0
    assert not destination.exists()


def test_copy_move_deletes_sources_only_after_verification(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"a" * 100, "sub/b.txt": b"b" * 200})
    destination = tmp_path / "dst"
    stats = run_copy(str(source), str(destination), CopyOptions(move=True, verify="content"))
    assert stats.errors == []
    assert not source.exists()  # the whole source tree is tidied away
    assert (destination / "a.txt").read_bytes() == b"a" * 100


def test_copy_skip_policy_leaves_existing_files(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"new"})
    destination = tmp_path / "dst"
    make_tree(str(destination), {"a.txt": b"keep-me"})
    stats = run_copy(str(source), str(destination), CopyOptions(overwrite="skip"))
    assert stats.errors == []
    assert stats.files_done == 0
    assert (destination / "a.txt").read_bytes() == b"keep-me"
    assert stats.skipped[0][1] == "destination exists"


@pytest.mark.skipif(not WINDOWS, reason="long-path handling is Windows-only")
def test_copy_handles_paths_longer_than_260_characters(tmp_path):
    deep = str(tmp_path / "deep")
    index = 0
    while len(deep) < 260:  # grow the folder until its path passes the old limit
        deep = os.path.join(deep, "segment_" + str(index).zfill(3) + "x" * 10)
        index += 1
    os.makedirs(extended_path(deep), exist_ok=True)
    payload = deep + os.sep + "payload.bin"
    assert len(payload) > 260
    with open(extended_path(payload), "wb") as handle:
        handle.write(b"deep" * 512)
    # The destination is made deep as well, so both sides of the copy are past
    # the old 260-character limit.
    destination = str(tmp_path / "long-out")
    index = 0
    while len(destination) < 255:
        destination = os.path.join(destination, "out_" + str(index).zfill(3) + "y" * 10)
        index += 1
    os.makedirs(extended_path(destination), exist_ok=True)
    stats = run_copy(deep, destination, CopyOptions(verify="content"))
    assert stats.errors == []
    relative = os.path.relpath(payload, deep)
    copied = os.path.join(destination, relative)
    assert len(copied) > 260
    assert os.path.getsize(extended_path(copied)) == 2048
    with open(extended_path(copied), "rb") as handle:
        assert handle.read() == b"deep" * 512


def test_cancel_stops_the_run_and_reports_it(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {f"f{i}.bin": b"z" * 65536 for i in range(60)})
    cancel = threading.Event()
    seen = []

    def on_file(job, phase):
        if phase == "start":
            seen.append(job.display_dst)
            if len(seen) == 3:
                cancel.set()

    stats = run_copy(
        str(source),
        str(tmp_path / "dst"),
        CopyOptions(threads=2, verify="size"),
        cancel=cancel,
        on_file=on_file,
    )
    assert stats.cancelled is True
    assert stats.files_done < 60
    assert stats.summary()["cancelled"] is True


def test_copy_reports_a_file_it_could_not_read(tmp_path):
    """One bad file must not stop the other files from arriving."""
    if os.name == "nt" or os.geteuid() == 0:  # pragma: no cover - environment dependent
        pytest.skip("chmod cannot make a file unreadable for this user")
    source = tmp_path / "src"
    make_tree(str(source), {"good.txt": b"ok", "bad.txt": b"nope"})
    os.chmod(str(source / "bad.txt"), 0)
    try:
        stats = run_copy(str(source), str(tmp_path / "dst"), CopyOptions(verify="size"))
    finally:
        os.chmod(str(source / "bad.txt"), stat.S_IRUSR | stat.S_IWUSR)
    assert stats.files_done >= 1
    assert stats.errors


def test_verify_plan_finds_a_damaged_copy(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"a" * 500, "b.txt": b"b" * 500})
    destination = tmp_path / "dst"
    run_copy(str(source), str(destination), CopyOptions(verify="size"))
    plan = build_plan(str(source), str(destination))
    with open(destination / "b.txt", "r+b") as handle:
        handle.seek(10)
        handle.write(b"X")
    stats = verify_plan(plan, CopyOptions())
    assert len(stats.errors) == 1
    assert "b.txt" in stats.errors[0]
    assert stats.files_done == 2  # both files were checked


def test_verify_plan_passes_for_a_faithful_copy(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"a" * 500})
    destination = tmp_path / "dst"
    run_copy(str(source), str(destination), CopyOptions(verify="content"))
    stats = verify_plan(build_plan(str(source), str(destination)))
    assert stats.errors == []


def test_stats_snapshot_shape(tmp_path):
    source = tmp_path / "src"
    make_tree(str(source), {"a.txt": b"a" * 100})
    stats = CopyStats()
    stats.begin(build_plan(str(source), str(tmp_path / "out")))
    stats.add_bytes(1024)
    snapshot = stats.snapshot()
    for key in ("percent", "speed", "eta", "elapsed", "active", "partial", "done"):
        assert key in snapshot
    assert snapshot["percent"] >= 0
    assert isinstance(snapshot["partial"], list)


def test_free_space_is_reported_for_a_real_folder(tmp_path):
    free = core.free_space_for(str(tmp_path))
    assert free is not None and free > 0
    # A path that does not exist yet still reports its nearest existing parent.
    missing = core.free_space_for(str(tmp_path / "does" / "not" / "exist"))
    assert missing is None or missing > 0


# ---------------------------------------------------------------------------
# settings file
# ---------------------------------------------------------------------------
def test_config_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_config() == {}
    assert save_config({"threads": 4, "verify": "content"}) is True
    assert load_config() == {"threads": 4, "verify": "content"}
    assert os.path.isfile(os.path.join(core.config_dir(), "settings.json"))
    assert save_config({"threads": 8}) is True  # second save keeps a .bak
    assert os.path.isfile(core.config_path() + ".bak")
    assert load_config() == {"threads": 8}


def test_config_recovers_from_a_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    os.makedirs(core.config_dir(), exist_ok=True)
    with open(core.config_path(), "w", encoding="utf-8") as handle:
        handle.write("{ not json at all")
    assert load_config() == {}
    assert os.path.isfile(core.config_path() + ".bak")
