from __future__ import annotations

import errno
import os
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

import open_brain_engine.engine.markdown_import_fs as markdown_import_fs
import pytest
from open_brain_engine.engine.markdown_import_fs import (
    ImportInterrupted,
    ImportScanIncomplete,
    ScanInventory,
    ScanLimitExceeded,
    ScanLimits,
    enumerate_markdown,
    pin_import_root,
    read_markdown_candidate,
)


def _scan(
    root: Path,
    *,
    limits: ScanLimits,
    allow_large_vault: bool = False,
    interrupted: Callable[[], bool] | None = None,
) -> ScanInventory:
    with pin_import_root(str(root)) as pinned:
        return enumerate_markdown(
            pinned.descriptor,
            limits=limits,
            allow_large_vault=allow_large_vault,
            interrupted=(lambda: False) if interrupted is None else interrupted,
        )


def test_visited_entry_bound_passes_exactly_and_refuses_one_past(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("a.txt", "b.txt"):
        (vault / name).write_text("x", encoding="utf-8")
    limits = ScanLimits(visited_entries=2, selected_files=10, aggregate_bytes=10, file_bytes=10)

    assert _scan(vault, limits=limits).visited_entries == 2
    (vault / "c.txt").write_text("x", encoding="utf-8")

    with pytest.raises(ScanLimitExceeded) as failure:
        _scan(vault, limits=limits)
    assert failure.value.exceeded == ("visited_entries",)
    assert failure.value.visited_entries == 3
    assert failure.value.selected_files == 0
    assert failure.value.aggregate_bytes == 0


def test_selected_file_bound_passes_exactly_and_refuses_one_past(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text("x", encoding="utf-8")
    limits = ScanLimits(visited_entries=10, selected_files=2, aggregate_bytes=10, file_bytes=10)

    assert _scan(vault, limits=limits).selected_files == 2
    (vault / "c.md").write_text("x", encoding="utf-8")

    with pytest.raises(ScanLimitExceeded) as failure:
        _scan(vault, limits=limits)
    assert failure.value.exceeded == ("selected_markdown_files",)
    assert failure.value.selected_files == 3


def test_aggregate_bound_passes_exactly_and_refuses_one_past(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    limits = ScanLimits(visited_entries=10, selected_files=10, aggregate_bytes=5, file_bytes=10)
    note.write_bytes(b"12345")

    assert _scan(vault, limits=limits).aggregate_bytes == 5
    note.write_bytes(b"123456")

    with pytest.raises(ScanLimitExceeded) as failure:
        _scan(vault, limits=limits)
    assert failure.value.exceeded == ("aggregate_bytes",)
    assert failure.value.aggregate_bytes == 6


def test_total_limits_report_one_deterministic_classification_safe_point(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "a.md").write_bytes(b"123")
    (vault / "b.md").write_bytes(b"456")
    (vault / "z.txt").write_bytes(b"ignored")

    with pytest.raises(ScanLimitExceeded) as failure:
        _scan(
            vault,
            limits=ScanLimits(
                visited_entries=10,
                selected_files=1,
                aggregate_bytes=5,
                file_bytes=10,
            ),
        )

    assert failure.value.exceeded == ("aggregate_bytes", "selected_markdown_files")
    assert failure.value.visited_entries == 2
    assert failure.value.selected_files == 2
    assert failure.value.aggregate_bytes == 6


def test_per_file_bound_is_not_bypassed_by_large_vault_override(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "exact.md").write_bytes(b"12345")
    (vault / "oversized.md").write_bytes(b"123456")
    limits = ScanLimits(visited_entries=10, selected_files=10, aggregate_bytes=5, file_bytes=5)

    inventory = _scan(vault, limits=limits, allow_large_vault=True)

    assert [candidate.relative_path for candidate in inventory.candidates] == ["exact.md"]
    assert [(outcome.path, outcome.reason) for outcome in inventory.outcomes] == [
        ("oversized.md", "file_too_large")
    ]


def test_directory_collection_checks_interruption_incrementally(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    for index in range(10):
        (vault / f"asset-{index}.txt").write_text("x", encoding="utf-8")
    checks = 0

    def interrupted() -> bool:
        nonlocal checks
        checks += 1
        return True

    with pytest.raises(ImportInterrupted):
        _scan(
            vault,
            limits=ScanLimits(
                visited_entries=100,
                selected_files=100,
                aggregate_bytes=100,
                file_bytes=100,
            ),
            interrupted=interrupted,
        )
    assert checks == 1


def test_invalid_utf8_filename_is_rendered_as_opaque_token(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    descriptor = os.open(vault, os.O_RDONLY)
    try:
        try:
            file_descriptor = os.open(
                b"private-\xff.md",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=descriptor,
            )
        except OSError as error:
            if error.errno == errno.EILSEQ:
                pytest.skip("filesystem rejects non-UTF-8 filenames")
            raise
        try:
            os.write(file_descriptor, b"synthetic")
        finally:
            os.close(file_descriptor)
    finally:
        os.close(descriptor)

    inventory = _scan(
        vault,
        limits=ScanLimits(
            visited_entries=10,
            selected_files=10,
            aggregate_bytes=100,
            file_bytes=100,
        ),
    )

    assert inventory.candidates == ()
    assert [(item.path, item.reason) for item in inventory.outcomes] == [
        ("<invalid-path-000001>", "invalid_path")
    ]


def test_nfc_file_collision_overrides_per_file_failure(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    composed = "caf\u00e9.md"
    decomposed = unicodedata.normalize("NFD", composed)
    (vault / composed).write_bytes(b"x")
    (vault / decomposed).write_bytes(b"123456")
    if len(tuple(vault.iterdir())) != 2:
        pytest.skip("filesystem does not preserve distinct NFC and NFD names")

    inventory = _scan(
        vault,
        limits=ScanLimits(
            visited_entries=10,
            selected_files=10,
            aggregate_bytes=100,
            file_bytes=5,
        ),
    )

    assert inventory.candidates == ()
    assert [item.reason for item in inventory.outcomes] == ["path_collision", "path_collision"]
    assert {item.path for item in inventory.outcomes} == {composed}


@pytest.mark.parametrize("other_kind", ("symlink", "hardlink"))
def test_nfc_markdown_leaf_collision_overrides_link_outcome(
    tmp_path: Path,
    other_kind: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    composed = "caf\u00e9.md"
    decomposed = unicodedata.normalize("NFD", composed)
    (vault / composed).write_bytes(b"regular")
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    try:
        if other_kind == "symlink":
            (vault / decomposed).symlink_to(outside)
        else:
            os.link(outside, vault / decomposed)
    except FileExistsError:
        pytest.skip("filesystem does not preserve distinct NFC and NFD names")
    if len(tuple(vault.iterdir())) != 2:
        pytest.skip("filesystem does not preserve distinct NFC and NFD names")

    inventory = _scan(
        vault,
        limits=ScanLimits(
            visited_entries=10,
            selected_files=10,
            aggregate_bytes=100,
            file_bytes=100,
        ),
    )

    assert inventory.selected_files == 1
    assert inventory.candidates == ()
    assert [item.reason for item in inventory.outcomes] == ["path_collision", "path_collision"]
    assert {item.path for item in inventory.outcomes} == {composed}


def test_nfc_sibling_directory_collision_fails_complete_scan(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    composed = "caf\u00e9"
    decomposed = unicodedata.normalize("NFD", composed)
    (vault / composed).mkdir()
    try:
        (vault / decomposed).mkdir()
    except FileExistsError:
        pytest.skip("filesystem does not preserve distinct NFC and NFD names")
    if len(tuple(vault.iterdir())) != 2:
        pytest.skip("filesystem does not preserve distinct NFC and NFD names")

    with pytest.raises(ImportScanIncomplete):
        _scan(
            vault,
            limits=ScanLimits(
                visited_entries=10,
                selected_files=10,
                aggregate_bytes=100,
                file_bytes=100,
            ),
        )


def test_regular_file_swapped_to_fifo_is_rejected_without_blocking(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("synthetic", encoding="utf-8")
    limits = ScanLimits(visited_entries=10, selected_files=10, aggregate_bytes=100, file_bytes=100)

    with pin_import_root(str(vault)) as pinned:
        candidate = enumerate_markdown(
            pinned.descriptor,
            limits=limits,
            allow_large_vault=False,
            interrupted=lambda: False,
        ).candidates[0]
        note.unlink()
        os.mkfifo(note)
        started = time.monotonic()
        with pytest.raises(ValueError, match="file_changed"):
            read_markdown_candidate(pinned.descriptor, candidate, maximum_bytes=100)
        assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("replacement", ("symlink", "hardlink"))
def test_regular_file_swapped_to_link_is_rejected(
    tmp_path: Path,
    replacement: str,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_text("original", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("replacement", encoding="utf-8")
    limits = ScanLimits(visited_entries=10, selected_files=10, aggregate_bytes=100, file_bytes=100)

    with pin_import_root(str(vault)) as pinned:
        candidate = enumerate_markdown(
            pinned.descriptor,
            limits=limits,
            allow_large_vault=False,
            interrupted=lambda: False,
        ).candidates[0]
        note.unlink()
        if replacement == "symlink":
            note.symlink_to(outside)
        else:
            os.link(outside, note)

        with pytest.raises(ValueError, match="file_changed"):
            read_markdown_candidate(pinned.descriptor, candidate, maximum_bytes=100)


def test_file_mutated_after_first_read_chunk_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    note = vault / "note.md"
    note.write_bytes(b"x" * (128 * 1024))
    limits = ScanLimits(
        visited_entries=10,
        selected_files=10,
        aggregate_bytes=256 * 1024,
        file_bytes=256 * 1024,
    )

    with pin_import_root(str(vault)) as pinned:
        candidate = enumerate_markdown(
            pinned.descriptor,
            limits=limits,
            allow_large_vault=False,
            interrupted=lambda: False,
        ).candidates[0]
        original_read = os.read
        mutated = False

        def mutate_after_read(descriptor: int, count: int) -> bytes:
            nonlocal mutated
            chunk = original_read(descriptor, count)
            if chunk and not mutated:
                mutated = True
                with note.open("ab") as stream:
                    stream.write(b"y")
            return chunk

        monkeypatch.setattr(os, "read", mutate_after_read)
        with pytest.raises(ValueError, match="file_changed"):
            read_markdown_candidate(pinned.descriptor, candidate, maximum_bytes=256 * 1024)


def test_root_replacement_between_independent_opens_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    archived = tmp_path / "archived"
    original_resolve = markdown_import_fs._resolve_and_open
    descriptors: list[int] = []

    def replace_after_first_open(argument: str) -> tuple[int, markdown_import_fs.RootSnapshot]:
        descriptor, snapshot = original_resolve(argument)
        descriptors.append(descriptor)
        if len(descriptors) == 1:
            vault.rename(archived)
            vault.mkdir()
        return descriptor, snapshot

    monkeypatch.setattr(markdown_import_fs, "_resolve_and_open", replace_after_first_open)

    with pytest.raises(markdown_import_fs.ImportDirectoryUnavailable):
        pin_import_root(str(vault))
    assert len(descriptors) == 2
    for descriptor in descriptors:
        with pytest.raises(OSError) as closed:
            os.fstat(descriptor)
        assert closed.value.errno == errno.EBADF


def test_child_directory_replacement_between_stat_and_open_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "vault"
    nested = vault / "nested"
    nested.mkdir(parents=True)
    (nested / "note.md").write_text("original", encoding="utf-8")
    original_open = os.open
    replaced = False

    with pin_import_root(str(vault)) as pinned:

        def replace_before_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if path == b"nested" and not replaced:
                replaced = True
                nested.rename(vault / "moved")
                nested.mkdir()
            return original_open(path, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(os, "open", replace_before_open)
        with pytest.raises(ImportScanIncomplete):
            enumerate_markdown(
                pinned.descriptor,
                limits=ScanLimits(
                    visited_entries=10,
                    selected_files=10,
                    aggregate_bytes=100,
                    file_bytes=100,
                ),
                allow_large_vault=False,
                interrupted=lambda: False,
            )


def test_parent_directory_swap_is_rejected(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    nested = vault / "nested"
    nested.mkdir(parents=True)
    (nested / "note.md").write_text("original", encoding="utf-8")
    limits = ScanLimits(visited_entries=10, selected_files=10, aggregate_bytes=100, file_bytes=100)

    with pin_import_root(str(vault)) as pinned:
        candidate = enumerate_markdown(
            pinned.descriptor,
            limits=limits,
            allow_large_vault=False,
            interrupted=lambda: False,
        ).candidates[0]
        nested.rename(vault / "moved")
        nested.mkdir()
        (nested / "note.md").write_text("replacement", encoding="utf-8")

        with pytest.raises(ValueError, match="file_changed"):
            read_markdown_candidate(pinned.descriptor, candidate, maximum_bytes=100)


def test_inventory_order_is_deterministic(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "z.md").write_text("z", encoding="utf-8")
    (vault / "a.md").write_text("a", encoding="utf-8")
    (vault / "m.txt").write_text("m", encoding="utf-8")

    inventory = _scan(
        vault,
        limits=ScanLimits(
            visited_entries=10,
            selected_files=10,
            aggregate_bytes=100,
            file_bytes=100,
        ),
    )

    assert [candidate.relative_path for candidate in inventory.candidates] == ["a.md", "z.md"]
    assert [(item.path, item.reason) for item in inventory.outcomes] == [("m.txt", "non_markdown")]
