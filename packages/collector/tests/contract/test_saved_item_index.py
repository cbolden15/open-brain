from __future__ import annotations

import re
from pathlib import Path

import pytest

from open_brain_collector import saved_item_index
from open_brain_collector.saved_item_index import (
    INDEX_MAX_DELIVERED,
    INDEX_MAX_ENTRIES,
    INDEX_SIZE_LIMIT,
    TERMINAL_STATES,
    SavedItemIndex,
    index_name,
    load_index,
    save_index,
)
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

NAME = "a" * 32
FINGERPRINT = "b" * 24
KEY = "c" * 32
CONTENT = "d" * 32


def test_record_tracks_terminal_states_and_delivered_items() -> None:
    index = SavedItemIndex()
    assert index.fingerprint(NAME) is None
    index.record(NAME, FINGERPRINT, "delivered", KEY, CONTENT)
    index.record("e" * 32, FINGERPRINT, "invalid_json")
    assert index.fingerprint(NAME) == FINGERPRINT
    assert index.entries == {
        NAME: FINGERPRINT + ":delivered",
        "e" * 32: FINGERPRINT + ":invalid_json",
    }
    assert index.delivered == {KEY: CONTENT}
    assert SavedItemIndex.from_dict(index.to_dict()) == index
    index.prune({NAME})
    assert set(index.entries) == {NAME} and index.delivered == {KEY: CONTENT}


def test_terminal_states_cover_every_local_outcome() -> None:
    assert frozenset(
        {"already_captured", "delivered", "not_regular_file", "skipped", *RECORD_REFUSAL_CODES}
    ) == TERMINAL_STATES


@pytest.mark.parametrize(
    "arguments",
    [
        ("x", FINGERPRINT, "skipped", "", ""),
        (NAME, "short", "skipped", "", ""),
        (NAME, FINGERPRINT, "placeholder", "", ""),
        (NAME, FINGERPRINT, "delivered", "", ""),
        (NAME, FINGERPRINT, "skipped", KEY, CONTENT),
    ],
)
def test_record_refuses_malformed_or_nonterminal_values(
    arguments: tuple[str, str, str, str, str],
) -> None:
    with pytest.raises(LiveSourceError, match="saved_item_invalid_index"):
        SavedItemIndex().record(*arguments)


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"schema_version": 1},
        {"schema_version": 2, "entries": {}, "delivered": {}},
        {"schema_version": True, "entries": {}, "delivered": {}},
        {"schema_version": 1.0, "entries": {}, "delivered": {}},
        {"schema_version": 1, "entries": {NAME: "zz:skipped"}, "delivered": {}},
        {"schema_version": 1, "entries": {NAME: FINGERPRINT + ":placeholder"}, "delivered": {}},
        {"schema_version": 1, "entries": {}, "delivered": {KEY: 5}},
    ],
)
def test_from_dict_refuses_foreign_shapes(value: object) -> None:
    with pytest.raises(LiveSourceError, match="saved_item_invalid_index"):
        SavedItemIndex.from_dict(value)


def test_oldest_entries_and_delivered_items_are_evicted_past_the_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_ENTRIES", 2)
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_DELIVERED", 2)
    index = SavedItemIndex()
    for number in range(3):
        index.record(f"{number:032x}", FINGERPRINT, "delivered", f"{number + 10:032x}", CONTENT)
    assert list(index.entries) == [f"{1:032x}", f"{2:032x}"]
    assert list(index.delivered) == [f"{11:032x}", f"{12:032x}"]


def test_index_at_full_capacity_stays_under_half_the_store_bound(tmp_path: Path) -> None:
    longest = max(TERMINAL_STATES, key=len)
    index = SavedItemIndex(
        entries={f"{n:032x}": "f" * 24 + ":" + longest for n in range(INDEX_MAX_ENTRIES)},
        delivered={f"{n:032x}": "e" * 32 for n in range(INDEX_MAX_DELIVERED)},
    )
    assert INDEX_MAX_ENTRIES == INDEX_MAX_DELIVERED == 10_000
    assert INDEX_SIZE_LIMIT == 4_194_304 // 2
    assert len(bounded_json(index.to_dict(), 4_194_304)) < INDEX_SIZE_LIMIT
    store = PrivateJsonStore(tmp_path / "saved_items")
    save_index(store, "index-test.json", index)
    assert load_index(store, "index-test.json") == index


def test_oversized_index_is_refused_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_index, "INDEX_SIZE_LIMIT", 64)
    index = SavedItemIndex()
    index.record(NAME, FINGERPRINT, "delivered", KEY, CONTENT)
    store = PrivateJsonStore(tmp_path / "saved_items")
    with pytest.raises(LiveSourceError, match="saved_item_index_too_large"):
        save_index(store, "index-test.json", index)
    assert store.read("index-test.json") is None


def test_lost_index_loads_empty_and_names_are_per_selection(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "saved_items")
    first = SourceResourceSelection(
        "saved_item", "folder:" + "1" * 32, "items:" + "2" * 32, "local_folder"
    )
    second = SourceResourceSelection(
        "saved_item", "folder:" + "1" * 32, "items:" + "3" * 32, "local_folder"
    )
    assert load_index(store, index_name(first)) == SavedItemIndex()
    assert re.fullmatch(r"index-[0-9a-f]{32}\.json", index_name(first))
    assert index_name(first) != index_name(second)


def test_eviction_order_survives_reload_and_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_ENTRIES", 2)
    monkeypatch.setattr(saved_item_index, "INDEX_MAX_DELIVERED", 2)
    store = PrivateJsonStore(tmp_path / "state")
    index = SavedItemIndex()
    for key in ("f" * 32, "1" * 32):
        index.record(key, FINGERPRINT, "delivered", key, CONTENT)
    save_index(store, "index.json", index)
    index = load_index(store, "index.json")
    index.record("e" * 32, FINGERPRINT, "delivered", "e" * 32, CONTENT)
    assert list(index.entries) == list(index.delivered) == ["1" * 32, "e" * 32]
    index.record("1" * 32, FINGERPRINT, "delivered", "1" * 32, CONTENT)
    save_index(store, "index.json", index)
    index = load_index(store, "index.json")
    index.record("0" * 32, FINGERPRINT, "delivered", "0" * 32, CONTENT)
    assert list(index.entries) == list(index.delivered) == ["1" * 32, "0" * 32]


def test_index_over_store_limit_uses_index_error(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "state")
    index = SavedItemIndex(entries={NAME: "x" * 4_194_305})
    with pytest.raises(LiveSourceError, match="^saved_item_index_too_large$"):
        save_index(store, "index.json", index)
    assert store.read("index.json") is None


def test_index_does_not_translate_invalid_json_error(tmp_path: Path) -> None:
    store = PrivateJsonStore(tmp_path / "state")
    index = SavedItemIndex(entries={NAME: "x"})
    index.delivered[KEY] = float("nan")  # type: ignore[assignment]
    with pytest.raises(LiveSourceError, match="^source_invalid_state$"):
        save_index(store, "index.json", index)
