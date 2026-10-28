from __future__ import annotations

import os
import re
import stat
from pathlib import Path

import pytest

from open_brain_collector import saved_item_folder
from open_brain_collector.saved_item_folder import (
    MAX_RECORD_BYTES,
    FolderListing,
    list_records,
    name_hash,
    open_folder,
    read_entry,
    select_folder,
    stat_fingerprint,
)
from open_brain_connectors.runtime.live_common import LiveSourceError


def _folder(tmp_path: Path) -> Path:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    return folder.resolve()


def _write(folder: Path, name: str, data: bytes = b'{"x": 1}') -> Path:
    path = folder / name
    path.write_bytes(data)
    return path


def _refuse_open(name: str, folder_fd: int) -> int:
    raise AssertionError(f"opened {name!r}")


def test_select_folder_hashes_the_resolved_path_and_inode(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    selection, resolved = select_folder(str(folder))
    assert resolved == folder
    assert (selection.connector_name, selection.resource_type) == ("saved_item", "local_folder")
    assert re.fullmatch(r"folder:[0-9a-f]{32}", selection.connection_id)
    assert re.fullmatch(r"items:[0-9a-f]{32}", selection.resource_id)
    assert select_folder(str(folder))[0] == selection
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    assert select_folder(str(other))[0] != selection
    with open_folder(folder) as opened:
        assert opened.selection == selection and opened.path == folder


def test_folder_must_exist_be_owner_private_and_not_a_symlink(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    link = folder.parent / "link"
    link.symlink_to(folder, target_is_directory=True)
    plain_file = _write(folder.parent, "plain")
    with pytest.raises(LiveSourceError, match="folder_missing"):
        select_folder(str(folder.parent / "absent"))
    for raw in (str(link), "relative/inbox", ""):
        with pytest.raises(LiveSourceError, match="folder_unsafe"):
            select_folder(raw)
    for path in (link, plain_file):
        with pytest.raises(LiveSourceError, match="folder_unsafe"), open_folder(path):
            pass
    folder.chmod(0o770)
    try:
        with pytest.raises(LiveSourceError, match="folder_unsafe"), open_folder(folder):
            pass
    finally:
        folder.chmod(0o700)


def test_list_records_skips_hidden_cloud_and_partial_names(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    for name in (
        "b.json",
        "a.json",
        ".hidden.json",
        ".a.json.icloud",
        "c.json.tmp",
        "d.json.partial",
        "notes.txt",
    ):
        _write(folder, name, b"{}")
    with open_folder(folder) as opened:
        listing = list_records(opened)
    assert listing == FolderListing(tuple(sorted(("a.json", "b.json"), key=name_hash)), 4)


def test_more_json_entries_than_the_limit_refuses_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_folder, "MAX_RECORD_FILES", 3)
    folder = _folder(tmp_path)
    for number in range(3):
        _write(folder, f"{number}.json", b"{}")
    with open_folder(folder) as opened:
        assert len(list_records(opened).records) == 3
    _write(folder, ".3.json", b"{}")
    with pytest.raises(LiveSourceError, match="folder_over_limit"), open_folder(folder) as opened:
        list_records(opened)


def test_regular_entry_is_read_with_a_stable_fingerprint(tmp_path: Path) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", None)
    assert (entry.status, entry.data, entry.code) == ("read", b'{"x": 1}', "")
    assert entry.name_hash == name_hash("a.json")
    assert entry.fingerprint == stat_fingerprint(path.lstat())
    assert re.fullmatch(r"[0-9a-f]{24}", entry.fingerprint)


def test_known_fingerprint_is_skipped_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    monkeypatch.setattr(saved_item_folder, "_open_entry", _refuse_open)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", stat_fingerprint(path.lstat()))
    assert entry.status == "unchanged"


def test_pipe_symlink_directory_and_oversize_entries_are_refused_without_blocking(
    tmp_path: Path,
) -> None:
    folder = _folder(tmp_path)
    os.mkfifo(folder / "pipe.json")
    target = _write(folder, "target.json")
    (folder / "link.json").symlink_to(target)
    (folder / "dir.json").mkdir()
    _write(folder, "big.json", b" " * (MAX_RECORD_BYTES + 1))
    with open_folder(folder) as opened:
        results = {
            name: (read_entry(opened, name, None).status, read_entry(opened, name, None).code)
            for name in ("pipe.json", "link.json", "dir.json", "big.json")
        }
        fd = saved_item_folder._open_entry("pipe.json", opened.fd)
        try:
            assert stat.S_ISFIFO(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        with pytest.raises(OSError):
            saved_item_folder._open_entry("link.json", opened.fd)
    assert results == {
        "pipe.json": ("refused", "not_regular_file"),
        "link.json": ("refused", "not_regular_file"),
        "dir.json": ("refused", "not_regular_file"),
        "big.json": ("refused", "file_too_large"),
    }


def test_dataless_entry_is_deferred_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    inode = _write(folder, "cloud.json").lstat().st_ino

    def flags(info: os.stat_result) -> int:
        return saved_item_folder.SF_DATALESS if info.st_ino == inode else 0

    monkeypatch.setattr(saved_item_folder, "_entry_flags", flags)
    monkeypatch.setattr(saved_item_folder, "_open_entry", _refuse_open)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "cloud.json", None)
    assert (entry.status, entry.code) == ("deferred", "placeholder")


def test_entry_changed_during_the_read_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    path = _write(folder, "a.json")
    real_open = saved_item_folder._open_entry

    def grow(name: str, folder_fd: int) -> int:
        fd = real_open(name, folder_fd)
        with path.open("ab") as handle:
            handle.write(b" ")
        return fd

    monkeypatch.setattr(saved_item_folder, "_open_entry", grow)
    with open_folder(folder) as opened:
        entry = read_entry(opened, "a.json", None)
    assert (entry.status, entry.code) == ("deferred", "unstable")


def test_name_hash_is_fixed_size_for_any_name() -> None:
    for name in ("a.json", "bad\nname\x07.json", "é.json"):
        assert re.fullmatch(r"[0-9a-f]{32}", name_hash(name))


def test_entry_stat_error_does_not_expose_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _folder(tmp_path)
    name = "synthetic-private-entry.json"
    _write(folder, name)
    with open_folder(folder) as opened:
        def deny_stat(*args: object, **kwargs: object) -> os.stat_result:
            raise PermissionError(13, "Permission denied", name)

        monkeypatch.setattr(os, "stat", deny_stat)
        with pytest.raises(LiveSourceError, match="folder_unsafe") as exc:
            read_entry(opened, name, None)
    assert name not in str(exc.value)
    assert exc.value.__suppress_context__
