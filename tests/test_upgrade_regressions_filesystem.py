"""Fault-injection regressions for the filesystem upgrade tracks."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from ares.tools import filesystem
from ares.tools.filesystem import (
    copy_file,
    disk_usage,
    find_duplicates,
    get_file_info,
    read_file,
    search_files_async,
)
from ares.tools.filesystem_write import batch_edit, edit_file, write_file


def test_batch_edit_no_match_is_a_failed_transaction(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("before", encoding="utf-8")

    result = batch_edit([{"action": "edit", "path": str(target), "old_text": "missing", "new_text": "after"}])

    assert "failed and rolled back" in result.lower()
    assert target.read_text(encoding="utf-8") == "before"


def test_batch_edit_restores_moved_directory_and_backup_tree(tmp_path):
    source = tmp_path / "source"
    (source / "nested").mkdir(parents=True)
    (source / "nested" / "payload.txt").write_bytes(b"original\x00bytes")
    destination = tmp_path / "destination"

    result = batch_edit(
        [
            {"action": "move", "source": str(source), "destination": str(destination)},
            {"action": "edit", "path": str(destination / "nested" / "payload.txt"), "old_text": "not present", "new_text": "x"},
        ],
        confirm=True,
    )

    assert "failed and rolled back" in result.lower()
    assert (source / "nested" / "payload.txt").read_bytes() == b"original\x00bytes"
    assert not destination.exists()
    assert not (tmp_path / ".ares_backups").exists()


def test_batch_edit_rolls_back_backup_file_and_index_records(tmp_path):
    edited = tmp_path / "edited.txt"
    failing = tmp_path / "failing.txt"
    edited.write_text("before", encoding="utf-8")
    failing.write_text("unchanged", encoding="utf-8")
    result = batch_edit(
        [
            {"action": "edit", "path": str(edited), "old_text": "before", "new_text": "after"},
            {"action": "edit", "path": str(failing), "old_text": "missing", "new_text": "x"},
        ]
    )
    assert "rolled back" in result.lower()
    assert edited.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / ".ares_backups").exists()


def test_edit_preserves_crlf_and_failed_backup_publishes_no_index(tmp_path, monkeypatch):
    target = tmp_path / "windows.txt"
    target.write_bytes(b"alpha\r\nbeta\r\n")

    result = edit_file(str(target), "alpha\nbeta", "alpha\ngamma")

    assert "Edited" in result
    assert target.read_bytes() == b"alpha\r\ngamma\r\n"

    backup_target = tmp_path / "backup-fails.txt"
    backup_target.write_text("one", encoding="utf-8")
    backup_root = tmp_path / ".ares_backups"
    index_before = (backup_root / "backup_index.json").read_bytes()
    backups_before = sorted(path.name for path in backup_root.glob("backup-fails.txt.*.bak"))
    monkeypatch.setattr("ares.tools.filesystem_write._record_backup", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("index failure")))
    with pytest.raises(OSError, match="index failure"):
        edit_file(str(backup_target), "one", "two")
    assert (backup_root / "backup_index.json").read_bytes() == index_before
    assert sorted(path.name for path in backup_root.glob("backup-fails.txt.*.bak")) == backups_before


def test_search_keeps_multiple_hits_and_async_execution_does_not_block(tmp_path, monkeypatch):
    target = tmp_path / "many.txt"
    target.write_text("needle one\nneedle two\n", encoding="utf-8")
    result = asyncio.run(search_files_async("needle", str(tmp_path), max_results=10))
    assert ":1" in result and ":2" in result
    assert "2 matched line(s)" in result

    async def slow_content(*args, **kwargs):
        await asyncio.sleep(0.05)
        return []

    monkeypatch.setattr(filesystem, "_content_search", slow_content)

    async def heartbeat_check():
        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        beat = asyncio.create_task(heartbeat())
        try:
            await search_files_async("needle", str(tmp_path))
        finally:
            beat.cancel()
        return ticks

    # A synchronous wrapper that joins its worker would leave this at zero.
    assert asyncio.run(heartbeat_check()) >= 1


def test_read_file_large_window_symlink_identity_and_disk_file_input(tmp_path):
    large = tmp_path / "large.log"
    large.write_text("".join(f"line-{index}\n" for index in range(1, 5001)), encoding="utf-8")
    shown = read_file(str(large), start_line=4000, num_lines=200)
    assert "(5000 lines total)" in shown
    assert "  4000\tline-4000" in shown
    assert "  4199\tline-4199" in shown
    assert "  4200\t" not in shown
    assert "File:" in disk_usage(str(large))

    link = tmp_path / "large-link"
    try:
        os.symlink(large, link)
    except OSError:
        pytest.skip("symlinks are unavailable for this Windows test process")
    info = get_file_info(str(link))
    assert "Type: symlink" in info
    assert "Target status: exists" in info


def test_duplicates_use_shared_ignore_and_verified_copy_never_replaces_old_destination(tmp_path, monkeypatch):
    payload = b"same payload" * 200
    first = tmp_path / "one.bin"
    second = tmp_path / "two.bin"
    first.write_bytes(payload)
    second.write_bytes(payload)
    ignored = tmp_path / "ignored"
    ignored.mkdir()
    (ignored / "third.bin").write_bytes(payload)
    (tmp_path / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    duplicates = find_duplicates(str(tmp_path), min_size=1)
    assert "sha256:" in duplicates
    assert "one.bin" in duplicates and "two.bin" in duplicates
    assert "third.bin" not in duplicates

    destination = tmp_path / "destination.bin"
    destination.write_bytes(b"old destination")
    original_fsync = filesystem.os.fsync

    def fail_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(filesystem.os, "fsync", fail_fsync)
    failed = copy_file(str(first), str(destination), overwrite=True)
    assert "Error copying" in failed
    assert destination.read_bytes() == b"old destination"
    monkeypatch.setattr(filesystem.os, "fsync", original_fsync)

    copied = copy_file(str(first), str(destination), overwrite=True)
    assert "verified SHA-256" in copied
    assert destination.read_bytes() == payload


def test_write_file_reports_utf8_byte_count_and_shared_confirmation_contract(tmp_path):
    target = tmp_path / "unicode.txt"
    assert "Created" in write_file(str(target), "é")
    assert "2 bytes" in write_file(str(target), "é", dry_run=True)
    blocked = write_file(str(target), "changed")
    assert "CONFIRM REQUIRED" in blocked
    assert target.read_text(encoding="utf-8") == "é"
    assert "Overwrote" in write_file(str(target), "changed", confirm=True)
