from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest

from open_brain.services.brief_contracts import (
    BRIEF_LOG_MAX_BRIEFS,
    BRIEF_LOG_MAX_FILE_BYTES,
    BRIEF_LOG_MAX_KEYS,
)
from open_brain.services.brief_log import (
    BRIEF_LOG_PROTOCOL,
    LOG_ITEM_KEYS,
    BriefLogEntry,
    BriefLogStore,
    FamilyFingerprint,
)

KEY = "a" * 64
OTHER_KEY = "b" * 64
FROZEN = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
COVERAGE: dict[str, object] = {
    "families_checked": 2,
    "families_stale": 1,
    "captures_unavailable": 0,
    "followups_scanned": 3,
    "followups_truncated": False,
    "followups": "read",
    "freshness": "read",
    "collector": "not_read",
    "hidden_dropped": 0,
    "log": {"entries": 0, "bytes": 0, "unavailable": False},
}


class _Clock:
    def __init__(self, now: datetime = FROZEN) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def tick(self) -> None:
        self.now += timedelta(minutes=1)


def _item(rank: int, *, evidence: int = 1) -> dict[str, object]:
    return {
        "kind": "followup_due",
        "ref": f"followup:followup_3e6e8e2c-e638-47c6-8195-4bd6f306d6{rank:02d}",
        "reasons": ["overdue"],
        "evidence": [
            f"evidence_3e6e8e2c-e638-47c6-8195-4bd6f3{index:06d}" for index in range(evidence)
        ],
        "changed": "new",
        "rank": rank,
    }


def _entry(generated_at: str, *, items: int = 1, evidence: int = 1) -> BriefLogEntry:
    return BriefLogEntry(
        generated_at=generated_at,
        items=tuple(_item(rank, evidence=evidence) for rank in range(1, items + 1)),
        coverage=COVERAGE,
    )


def _store(tmp_path: Path, clock: _Clock | None = None) -> tuple[BriefLogStore, Path]:
    state = tmp_path / "brain" / ".open-brain" / "state"
    state.mkdir(parents=True)
    return BriefLogStore(state, clock=clock or _Clock()), state


def test_missing_key_reads_empty_and_last_is_none(tmp_path: Path) -> None:
    store, _state = _store(tmp_path)
    read = store.read(KEY)
    assert read.file is None and read.unavailable is False and (read.entries, read.bytes) == (0, 0)
    assert store.last(KEY) is None
    with pytest.raises(ValueError):
        store.read("not-a-key")


def test_append_is_compare_and_swap_and_last_returns_newest(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    first = _entry("2026-10-23T09:00:00Z")
    assert store.append(KEY, first, expected_sha256=None) is True
    path = state / "briefs" / f"{KEY}.json"
    digest = sha256(path.read_bytes()).hexdigest()
    read = store.read(KEY)
    assert read.file is not None and read.file.sha256 == digest
    assert read.entries == 1 and read.bytes == path.stat().st_size
    assert read.file.briefs == (first,)
    payload = json.loads(path.read_bytes())
    assert payload["protocol"] == BRIEF_LOG_PROTOCOL and payload["key"] == KEY
    assert payload["written_at"] == "2026-10-24T09:00:00Z"

    second = _entry("2026-10-24T09:00:00Z")
    assert store.append(KEY, second, expected_sha256=None) is False, "stale: file now exists"
    assert store.append(KEY, second, expected_sha256="0" * 64) is False, "stale digest"
    assert store.append(KEY, second, expected_sha256=digest) is True
    assert store.last(KEY) == second
    assert store.read(KEY).entries == 2


def test_file_keeps_the_last_thirty_briefs(tmp_path: Path) -> None:
    store, _state = _store(tmp_path)
    for minute in range(BRIEF_LOG_MAX_BRIEFS + 1):
        current = store.read(KEY)
        expected = None if current.file is None else current.file.sha256
        assert store.append(
            KEY, _entry(f"2026-10-24T09:{minute:02d}:00Z"), expected_sha256=expected
        )
    read = store.read(KEY)
    assert read.file is not None and read.entries == BRIEF_LOG_MAX_BRIEFS
    assert read.file.briefs[0].generated_at == "2026-10-24T09:01:00Z"
    assert read.file.briefs[-1].generated_at == f"2026-10-24T09:{BRIEF_LOG_MAX_BRIEFS:02d}:00Z"


def test_file_cap_drops_the_oldest_brief_first(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    # Five items with 50 evidence IDs each is roughly 13 KB per entry; six entries exceed 64 KB.
    for index in range(6):
        current = store.read(KEY)
        expected = None if current.file is None else current.file.sha256
        assert store.append(
            KEY,
            _entry(f"2026-10-24T{index:02d}:00:00Z", items=5, evidence=50),
            expected_sha256=expected,
        )
    path = state / "briefs" / f"{KEY}.json"
    assert path.stat().st_size <= BRIEF_LOG_MAX_FILE_BYTES
    read = store.read(KEY)
    assert read.file is not None and 1 <= read.entries < 6
    assert read.file.briefs[-1].generated_at == "2026-10-24T05:00:00Z"
    assert read.file.briefs[0].generated_at != "2026-10-24T00:00:00Z"


def test_sixteen_keys_evict_the_least_recently_written(tmp_path: Path) -> None:
    clock = _Clock()
    store, state = _store(tmp_path, clock)
    keys = [
        sha256(str(index).encode("utf-8")).hexdigest() for index in range(BRIEF_LOG_MAX_KEYS + 1)
    ]
    for key in keys[:BRIEF_LOG_MAX_KEYS]:
        clock.tick()
        assert store.append(key, _entry("2026-10-24T09:00:00Z"), expected_sha256=None)
    # Touch the first key again so it is no longer the least recently written.
    clock.tick()
    first = store.read(keys[0])
    assert first.file is not None
    assert store.append(keys[0], _entry("2026-10-24T10:00:00Z"), expected_sha256=first.file.sha256)
    clock.tick()
    assert store.append(
        keys[BRIEF_LOG_MAX_KEYS], _entry("2026-10-24T11:00:00Z"), expected_sha256=None
    )
    present = {path.stem for path in (state / "briefs").glob("*.json")}
    assert len(present) == BRIEF_LOG_MAX_KEYS
    assert keys[1] not in present, "second key was the least recently written"
    assert keys[0] in present and keys[BRIEF_LOG_MAX_KEYS] in present


def test_corrupt_file_is_set_aside_and_other_keys_are_unaffected(tmp_path: Path) -> None:
    store, state = _store(tmp_path)
    assert store.append(OTHER_KEY, _entry("2026-10-23T09:00:00Z"), expected_sha256=None)
    briefs = state / "briefs"
    (briefs / f"{KEY}.json").write_bytes(b"{not json")
    read = store.read(KEY)
    assert read.file is None and read.unavailable is True and (read.entries, read.bytes) == (0, 0)
    assert not (briefs / f"{KEY}.json").exists()
    assert (briefs / f"{KEY}.json.aside").read_bytes() == b"{not json"
    assert store.read(OTHER_KEY).entries == 1

    # A second corruption replaces the earlier aside copy, and a fresh append starts over.
    (briefs / f"{KEY}.json").write_bytes(b'{"protocol": "other"}')
    assert store.read(KEY).unavailable is True
    assert (briefs / f"{KEY}.json.aside").read_bytes() == b'{"protocol": "other"}'
    assert store.append(KEY, _entry("2026-10-24T09:00:00Z"), expected_sha256=None) is True
    assert store.read(KEY).entries == 1

    # Validation failures count as corruption too: a wrong key and an oversized file.
    good = (briefs / f"{KEY}.json").read_bytes()
    (briefs / f"{OTHER_KEY}.json").write_bytes(good)
    assert store.read(OTHER_KEY).unavailable is True
    (briefs / f"{KEY}.json").write_bytes(b" " * (BRIEF_LOG_MAX_FILE_BYTES + 1))
    assert store.read(KEY).unavailable is True


def test_ack_records_fingerprints_under_compare_and_swap(tmp_path: Path) -> None:
    store, _state = _store(tmp_path)
    fingerprint = FamilyFingerprint("2026-10-20T08:00:00Z", 1)
    assert store.ack(KEY, "family:consumer:synthetic:space_1", fingerprint, expected_sha256=None)
    read = store.read(KEY)
    assert read.file is not None and read.file.acks == {
        "family:consumer:synthetic:space_1": fingerprint
    }
    assert read.file.briefs == () and store.last(KEY) is None
    assert store.ack(KEY, "family:markdown:x", fingerprint, expected_sha256=None) is False
    newer = FamilyFingerprint("2026-10-21T08:00:00Z", 0)
    assert store.ack(
        KEY, "family:consumer:synthetic:space_1", newer, expected_sha256=read.file.sha256
    )
    again = store.read(KEY)
    assert again.file is not None and again.file.acks["family:consumer:synthetic:space_1"] == newer
    assert store.append(KEY, _entry("2026-10-24T09:00:00Z"), expected_sha256=again.file.sha256)
    final = store.read(KEY)
    assert final.file is not None and final.file.acks == again.file.acks and final.entries == 1
    with pytest.raises(ValueError):
        store.ack(KEY, "followup:x", newer, expected_sha256=final.file.sha256)


def test_entries_never_carry_text_fields(tmp_path: Path) -> None:
    store, _state = _store(tmp_path)
    assert {"changed", "evidence", "kind", "rank", "ref", "reasons"} == LOG_ITEM_KEYS
    with_text = BriefLogEntry(
        "2026-10-24T09:00:00Z", ({**_item(1), "reason_text": "Due today."},), COVERAGE
    )
    with pytest.raises(ValueError):
        store.append(KEY, with_text, expected_sha256=None)
    with_summary = BriefLogEntry(
        "2026-10-24T09:00:00Z", ({**_item(1), "summary": "Send the deck"},), COVERAGE
    )
    with pytest.raises(ValueError):
        store.append(KEY, with_summary, expected_sha256=None)
    text_coverage = BriefLogEntry(
        "2026-10-24T09:00:00Z", (_item(1),), {**COVERAGE, "followups": "Send the deck"}
    )
    with pytest.raises(ValueError):
        store.append(KEY, text_coverage, expected_sha256=None)
    assert store.read(KEY).file is None


def test_refs_follow_the_contract_bound_exactly(tmp_path: Path) -> None:
    store, _state = _store(tmp_path)
    fingerprint = FamilyFingerprint("2026-10-20T08:00:00Z", 0)
    longest = "family:consumer:" + "x" * 2200
    assert store.ack(KEY, longest, fingerprint, expected_sha256=None)
    for ref in ("family:x", "family:consumer:", "family:consumer:" + "x" * 2201, "family:f-a"):
        with pytest.raises(ValueError):
            store.ack(KEY, ref, fingerprint, expected_sha256=None)


def test_owner_files_are_exempt_from_key_eviction(tmp_path: Path) -> None:
    clock = _Clock()
    store, state = _store(tmp_path, clock)
    owner_key = "c" * 64
    fingerprint = FamilyFingerprint("2026-10-20T08:00:00Z", 0)
    assert store.ack(
        owner_key, "family:markdown:space_1", fingerprint, expected_sha256=None, owner=True
    )
    current = store.read(owner_key)
    assert current.file is not None and current.file.owner is True
    assert store.append(
        owner_key, _entry("2026-10-24T09:00:00Z"), expected_sha256=current.file.sha256, owner=True
    )
    owner_path = state / "briefs" / f"{owner_key}.json"
    owner_bytes = owner_path.read_bytes()
    assert json.loads(owner_bytes)["owner"] is True
    clients = [sha256(f"client-{index}".encode()).hexdigest() for index in range(20)]
    for key in clients:
        clock.tick()
        assert store.append(key, _entry("2026-10-24T10:00:00Z"), expected_sha256=None)
    assert owner_path.read_bytes() == owner_bytes, "the owner file is never a victim"
    owner = store.read(owner_key)
    assert owner.file is not None and owner.file.acks == {"family:markdown:space_1": fingerprint}
    present = {path.stem for path in (state / "briefs").glob("*.json")}
    assert owner_key in present and len(present - {owner_key}) == BRIEF_LOG_MAX_KEYS
    assert set(clients[-BRIEF_LOG_MAX_KEYS:]) <= present, (
        "client files evict each other, oldest first"
    )
    assert json.loads((state / "briefs" / f"{clients[-1]}.json").read_bytes())["owner"] is False
    # A file whose owner flag is missing or not a boolean is corrupt.
    raw = json.loads(owner_bytes)
    del raw["owner"]
    owner_path.write_bytes(json.dumps(raw, sort_keys=True, separators=(",", ":")).encode())
    assert store.read(owner_key).unavailable is True
