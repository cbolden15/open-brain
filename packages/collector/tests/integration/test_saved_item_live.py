from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain_collector import saved_item_folder, saved_item_live
from open_brain_collector.capture_policy import revision_store_dirname
from open_brain_collector.live_capture import LiveCaptureService
from open_brain_collector.live_manager import PriorityRuntime
from open_brain_collector.saved_item_folder import name_hash, select_folder
from open_brain_collector.saved_item_index import index_name, load_index
from open_brain_collector.saved_item_live import (
    MAX_PAGE_OUTCOMES,
    PAGE_BYTES,
    PAGE_INTAKES,
    SavedItemLiveSource,
)
from open_brain_connectors.runtime.live_common import LiveBatch, LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import SavedItemSourceAdapter, encoded_intake_size
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def _record(number: int, **fields: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "url": f"https://example.com/post/{number}",
        "why": f"Synthetic reason {number}",
        "received_at": "2026-10-26T14:00:00Z",
        "fetch_status": "not_attempted",
        **fields,
    }


def _write(folder: Path, name: str, value: dict[str, object]) -> None:
    (folder / name).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _setup(
    tmp_path: Path,
) -> tuple[SavedItemLiveSource, SourceResourceSelection, dict[str, object], Path]:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    source = SavedItemLiveSource(tmp_path / "live" / "saved_items")
    return source, selection, {"folder_path": str(resolved)}, resolved


def _drain(
    source: SavedItemLiveSource, selection: SourceResourceSelection, options: dict[str, object]
) -> list[LiveBatch]:
    batches: list[LiveBatch] = []
    checkpoint: dict[str, object] | None = None
    while True:
        batch = source.fetch(selection, options, checkpoint)
        source.acknowledge(selection, batch.checkpoint)
        batches.append(batch)
        checkpoint = batch.checkpoint
        if not batch.has_more:
            return batches


def _reason(intake: SourceRecordIntake) -> str:
    return intake.text.splitlines()[1]


def _intake_size(selection: SourceResourceSelection, record: dict[str, object]) -> int:
    adapter = SavedItemSourceAdapter(selection)
    parsed = adapter.record_from_bytes(json.dumps(record).encode())
    return encoded_intake_size(adapter.intake(parsed))


def test_thirty_new_files_arrive_in_two_pages(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(30):
        _write(folder, f"item-{number}.json", _record(number))
    batches = _drain(source, selection, options)
    assert [len(batch.intakes) for batch in batches] == [PAGE_INTAKES, 5]
    assert [batch.has_more for batch in batches] == [True, False]
    assert "saved_item_count:delivered:25" in batches[0].notices
    assert len({item.key.external_id for batch in batches for item in batch.intakes}) == 30
    assert source.fetch(selection, options, batches[-1].checkpoint).intakes == ()


def test_large_non_latin_records_split_under_the_page_budget(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(20):
        _write(folder, f"wide-{number}.json", _record(number, body="東" * 30_000))
    batches = _drain(source, selection, options)
    sizes = [sum(encoded_intake_size(item) for item in batch.intakes) for batch in batches]
    assert len(batches) >= 2 and len(batches[0].intakes) < 20
    assert all(size <= PAGE_BYTES for size in sizes)
    assert sum(len(batch.intakes) for batch in batches) == 20


def test_first_save_wins_across_and_within_pages(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    earliest = "Earliest reason"
    _write(folder, "later.json", _record(1, received_at="2026-10-26T15:00:00Z"))
    _write(folder, "earliest.json", _record(1, why=earliest, received_at="2026-10-26T13:00:00.5Z"))
    _write(folder, "middle.json", _record(1, received_at="2026-10-26T14:00:00Z"))
    _write(folder, "copy.json", _record(1, why=earliest, received_at="2026-10-26T16:00:00Z"))
    first = source.fetch(selection, options, None)
    assert [_reason(item) for item in first.intakes] == ["Earliest reason"]
    assert first.has_more and "saved_item_count:deferred_duplicate:3" in first.notices
    source.acknowledge(selection, first.checkpoint)
    second = source.fetch(selection, options, first.checkpoint)
    assert second.intakes == () and not second.has_more
    assert "saved_item_count:skipped:1" in second.notices
    assert "saved_item_count:already_captured:2" in second.notices
    assert {notice for notice in second.notices if notice.startswith("saved_item_refused:")} == {
        f"saved_item_refused:already_captured:{name_hash(name)[:12]}"
        for name in ("later.json", "middle.json")
    }


def test_same_url_with_equal_times_keeps_the_lowest_name_hash(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    names = ("one.json", "two.json")
    for name in names:
        _write(folder, name, _record(7, why=f"Reason from {name}"))
    batch = source.fetch(selection, options, None)
    assert [_reason(item) for item in batch.intakes] == [f"Reason from {min(names, key=name_hash)}"]


def test_unchanged_files_are_not_opened_on_a_second_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(501):
        _write(folder, f"r{number}.json", _record(number))
    batches = _drain(source, selection, options)
    assert sum(len(batch.intakes) for batch in batches) == 501
    opened: list[str] = []
    real_open = saved_item_folder._open_entry

    def spy(name: str, folder_fd: int) -> int:
        opened.append(name)
        return real_open(name, folder_fd)

    monkeypatch.setattr(saved_item_folder, "_open_entry", spy)
    again = source.fetch(selection, options, batches[-1].checkpoint)
    assert again.intakes == () and not again.has_more and opened == []
    assert "saved_item_count:unchanged:501" in again.notices


def test_more_than_ten_thousand_files_refuse_the_scan(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    for number in range(10_001):
        (folder / f"{number}.json").write_bytes(b"{}")
    with pytest.raises(LiveSourceError, match="folder_over_limit"):
        source.fetch(selection, options, None)
    assert not (tmp_path / "live" / "saved_items" / index_name(selection)).exists()


def test_named_pipe_and_entry_symlink_are_refused_without_blocking(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    os.mkfifo(folder / "pipe.json")
    _write(folder, "real.json", _record(1))
    (folder / "link.json").symlink_to(folder / "real.json")
    batch = source.fetch(selection, options, None)
    assert len(batch.intakes) == 1
    assert "saved_item_count:not_regular_file:2" in batch.notices
    assert {
        f"saved_item_refused:not_regular_file:{name_hash(name)[:12]}"
        for name in ("pipe.json", "link.json")
    } <= set(batch.notices)


def test_dataless_and_icloud_entries_are_skipped_without_opening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    _write(folder, "cloud.json", _record(1))
    (folder / ".evicted.json.icloud").write_bytes(b"placeholder")
    _write(folder, "local.json", _record(2))
    inode = (folder / "cloud.json").lstat().st_ino

    def dataless(info: os.stat_result) -> int:
        return saved_item_folder.SF_DATALESS if info.st_ino == inode else 0

    def downloaded(info: os.stat_result) -> int:
        return 0

    opened: list[str] = []
    real_open = saved_item_folder._open_entry

    def spy(name: str, folder_fd: int) -> int:
        opened.append(name)
        return real_open(name, folder_fd)

    monkeypatch.setattr(saved_item_folder, "_entry_flags", dataless)
    monkeypatch.setattr(saved_item_folder, "_open_entry", spy)
    batch = source.fetch(selection, options, None)
    assert opened == ["local.json"]
    assert len(batch.intakes) == 1 and "saved_item_count:placeholder:2" in batch.notices
    source.acknowledge(selection, batch.checkpoint)
    monkeypatch.setattr(saved_item_folder, "_entry_flags", downloaded)
    later = source.fetch(selection, options, batch.checkpoint)
    assert [_reason(item) for item in later.intakes] == ["Synthetic reason 1"]


def test_refusal_report_names_entries_only_by_hash_prefix(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    name = "bad\nname\x07.json"
    (folder / name).write_bytes(b"{not json")
    batch = source.fetch(selection, options, None)
    assert batch.notices == (
        "saved_item_count:invalid_json:1",
        f"saved_item_refused:invalid_json:{name_hash(name)[:12]}",
    )
    assert all("bad" not in notice and "\n" not in notice for notice in batch.notices)


def test_index_advances_only_after_acknowledge_and_prunes_vanished_files(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    _write(folder, "a.json", _record(1))
    first = source.fetch(selection, options, None)
    again = source.fetch(selection, options, None)
    assert [item.key for item in again.intakes] == [item.key for item in first.intakes]
    source.acknowledge(selection, first.checkpoint)
    source.acknowledge(selection, first.checkpoint)
    store = PrivateJsonStore(tmp_path / "live" / "saved_items")
    index = load_index(store, index_name(selection))
    assert index.entries[name_hash("a.json")].endswith(":delivered")
    assert source.fetch(selection, options, first.checkpoint).intakes == ()
    (folder / "a.json").unlink()
    _drain(source, selection, options)
    index = load_index(store, index_name(selection))
    assert name_hash("a.json") not in index.entries and len(index.delivered) == 1


def test_local_outcomes_past_the_page_cap_continue_on_the_next_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_live, "MAX_PAGE_OUTCOMES", 2)
    source, selection, options, folder = _setup(tmp_path)
    for number in range(5):
        (folder / f"bad-{number}.json").write_bytes(b"{")
    batches = _drain(source, selection, options)
    assert [batch.has_more for batch in batches] == [True, True, False]
    index = load_index(PrivateJsonStore(tmp_path / "live" / "saved_items"), index_name(selection))
    assert sorted(value.partition(":")[2] for value in index.entries.values()) == [
        "invalid_json"
    ] * 5


def test_page_outcome_cap_keeps_the_checkpoint_bounded() -> None:
    refused = ["a" * 32, "b" * 24, "payload_too_large", "", ""]
    delivered = ["a" * 32, "b" * 24, "delivered", "c" * 32, "d" * 32]
    worst = {
        "complete": False,
        "folder_path": "/" + "p" * 4096,
        "outcomes": [refused] * MAX_PAGE_OUTCOMES + [delivered] * PAGE_INTAKES,
        "schema_version": 1,
    }
    assert len(bounded_json(worst)) < 131_072


def test_unsafe_replaced_or_misconfigured_folder_refuses_the_scan(tmp_path: Path) -> None:
    source, selection, options, folder = _setup(tmp_path)
    bad_options: tuple[dict[str, object], ...] = (
        {"folder_path": "relative"},
        {**options, "extra": 1},
    )
    for bad in bad_options:
        with pytest.raises(LiveSourceError, match="source_invalid_options"):
            source.fetch(selection, bad, None)
    folder.chmod(0o777)
    try:
        with pytest.raises(LiveSourceError, match="folder_unsafe"):
            source.fetch(selection, options, None)
    finally:
        folder.chmod(0o700)
    folder.rename(tmp_path / "moved")
    folder.mkdir(mode=0o700)
    with pytest.raises(LiveSourceError, match="source_selection_mismatch"):
        source.fetch(selection, options, None)


def test_priority_runtime_routes_saved_items_on_the_plain_capture_path(tmp_path: Path) -> None:
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    _write(resolved, "a.json", _record(1))
    runtime = PriorityRuntime(tmp_path / "live")
    batch = runtime.fetch(selection, {"folder_path": str(resolved)}, None)
    assert len(batch.intakes) == 1
    runtime.acknowledge(selection, batch.checkpoint)
    assert (runtime.root / "saved_items" / index_name(selection)).is_file()
    assert revision_store_dirname("saved_item") is None


def test_failed_delivery_is_redelivered_and_the_index_follows_the_commit(tmp_path: Path) -> None:
    compile_single_user_local(tmp_path / "brain")
    folder = tmp_path / "inbox"
    folder.mkdir(mode=0o700)
    selection, resolved = select_folder(str(folder))
    _write(resolved, "a.json", _record(1))
    runtime = PriorityRuntime(tmp_path / "live")
    delivered: list[SourceRecordIntake] = []
    fail = True

    def sink(intake: SourceRecordIntake) -> None:
        if fail:
            raise RuntimeError("synthetic delivery failure")
        delivered.append(intake)

    service = LiveCaptureService(
        tmp_path / "state", tmp_path / "brain", runtime=runtime, sink=sink
    )
    service.configure("saved", selection, {"folder_path": str(resolved), "privacy_tier": "work"})
    preview = service.preview("saved")
    with pytest.raises(RuntimeError, match="synthetic delivery failure"):
        service.apply("saved", cast(str, preview["preview_id"]))
    store = PrivateJsonStore(runtime.root / "saved_items")
    assert load_index(store, index_name(selection)).entries == {}
    fail = False
    again = service.preview("saved")
    assert again["count"] == 1
    service.apply("saved", cast(str, again["preview_id"]))
    assert len(delivered) == 1 and delivered[0].privacy.tier.value == "work"
    assert load_index(store, index_name(selection)).entries[name_hash("a.json")].endswith(
        ":delivered"
    )
    assert service.preview("saved")["count"] == 0


def test_earliest_duplicate_after_a_full_page_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_live, "PAGE_INTAKES", 2)
    source, selection, options, folder = _setup(tmp_path)
    names = sorted((f"item-{number}.json" for number in range(4)), key=name_hash)
    _write(folder, names[0], _record(1, why="Later reason"))
    _write(folder, names[1], _record(2))
    _write(folder, names[2], _record(3))
    _write(folder, names[3], _record(1, why="Earliest reason", received_at="2026-10-26T13:00:00Z"))
    batches = _drain(source, selection, options)
    assert [_reason(item) for item in batches[0].intakes] == [
        "Earliest reason",
        "Synthetic reason 2",
    ]
    assert sum(len(batch.intakes) for batch in batches) == 3
    assert all(len(batch.intakes) <= 2 for batch in batches)


def test_larger_earliest_duplicate_wins_under_byte_pressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    names = sorted((f"item-{number}.json" for number in range(3)), key=name_hash)
    earlier = _record(
        1,
        why="Earliest reason",
        body="Synthetic paragraph. " * 200,
        received_at="2026-10-26T13:00:00Z",
    )
    budget = _intake_size(selection, earlier)
    assert (
        _intake_size(selection, _record(1, why="Later reason"))
        + _intake_size(selection, _record(2))
        <= budget
    )
    monkeypatch.setattr(saved_item_live, "PAGE_BYTES", budget)
    _write(folder, names[0], _record(1, why="Later reason"))
    _write(folder, names[1], _record(2))
    _write(folder, names[2], earlier)
    batches = _drain(source, selection, options)
    assert [_reason(item) for item in batches[0].intakes] == ["Earliest reason"]
    assert sum(len(batch.intakes) for batch in batches) == 2
    assert all(
        sum(encoded_intake_size(item) for item in batch.intakes) <= budget for batch in batches
    )


@pytest.mark.parametrize("received_at", ["2026-10-26T13:00:00Z", "2026-10-26T14:00:00Z"])
def test_a_deferred_winner_cannot_be_replaced_by_a_smaller_loser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, received_at: str
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    names = sorted((f"item-{number}.json" for number in range(3)), key=name_hash)
    winner = _record(
        1, why="Winning reason", body="Synthetic paragraph. " * 200, received_at=received_at
    )
    budget = _intake_size(selection, winner)
    assert (
        _intake_size(selection, _record(2))
        + _intake_size(selection, _record(1, why="Losing reason"))
        <= budget
    )
    monkeypatch.setattr(saved_item_live, "PAGE_BYTES", budget)
    _write(folder, names[0], _record(2))
    _write(folder, names[1], winner)
    _write(folder, names[2], _record(1, why="Losing reason"))
    batches = _drain(source, selection, options)
    assert [_reason(item) for batch in batches for item in batch.intakes] == [
        "Synthetic reason 2",
        "Winning reason",
    ]
    assert all(
        sum(encoded_intake_size(item) for item in batch.intakes) <= budget for batch in batches
    )


def test_earliest_duplicate_after_the_local_outcome_cap_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_live, "MAX_PAGE_OUTCOMES", 1)
    source, selection, options, folder = _setup(tmp_path)
    names = sorted((f"item-{number}.json" for number in range(4)), key=name_hash)
    _write(folder, names[0], _record(1, why="Later reason"))
    for name in names[1:3]:
        (folder / name).write_bytes(b"{")
    _write(folder, names[3], _record(1, why="Earliest reason", received_at="2026-10-26T13:00:00Z"))
    batches = _drain(source, selection, options)
    assert [_reason(item) for batch in batches for item in batch.intakes] == ["Earliest reason"]
    assert all(
        len(cast(list[object], batch.checkpoint["outcomes"])) <= 1 + PAGE_INTAKES
        for batch in batches
    )


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_checkpoint_schema_version_requires_an_integer(
    tmp_path: Path, schema_version: object
) -> None:
    source, selection, options, folder = _setup(tmp_path)
    checkpoint = {
        "complete": True,
        "folder_path": str(folder),
        "outcomes": [],
        "schema_version": schema_version,
    }
    with pytest.raises(LiveSourceError, match="saved_item_invalid_checkpoint"):
        source.fetch(selection, options, checkpoint)
    with pytest.raises(LiveSourceError, match="saved_item_invalid_checkpoint"):
        source.acknowledge(selection, checkpoint)
