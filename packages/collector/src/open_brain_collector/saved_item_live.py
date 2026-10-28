"""Selected saved-item folder capture on the live-source runtime.

One fetch is one page: at most 25 new items under a 1 MiB encoded budget. Proposed
outcomes ride in the checkpoint; the known-file index advances only in ``acknowledge``,
which the collector calls after it commits that checkpoint.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from open_brain_collector.saved_item_folder import (
    list_records,
    name_hash,
    open_folder,
    read_entry,
)
from open_brain_collector.saved_item_index import index_name, load_index, save_index
from open_brain_connectors.runtime.live_common import LiveBatch, LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import (
    SavedItemRefusal,
    SavedItemSourceAdapter,
    content_digest,
    encoded_intake_size,
)
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

PAGE_INTAKES = 25
PAGE_BYTES = 1_048_576
MAX_PAGE_OUTCOMES = 500
_NOTICE_LIMIT = 25
Outcome = tuple[str, str, str, str, str]


@dataclass(frozen=True, slots=True)
class _Candidate:
    received_at: datetime
    name_hash: str
    fingerprint: str
    item_key: str
    content: str
    size: int
    intake: SourceRecordIntake

    @property
    def order(self) -> tuple[datetime, str]:
        return (self.received_at, self.name_hash)


def _folder_path(value: object) -> Path:
    if type(value) is not str or not value or "\x00" in value or not Path(value).is_absolute():
        raise LiveSourceError("source_invalid_options")
    return Path(value)


def _options(options: object) -> Path:
    if not isinstance(options, dict) or set(options) != {"folder_path"}:
        raise LiveSourceError("source_invalid_options")
    return _folder_path(options["folder_path"])


def _checkpoint(value: object) -> tuple[Path, bool, tuple[Outcome, ...]]:
    if (
        not isinstance(value, dict)
        or set(value) != {"complete", "folder_path", "outcomes", "schema_version"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or type(value["complete"]) is not bool
        or not isinstance(value["outcomes"], list)
        or len(value["outcomes"]) > MAX_PAGE_OUTCOMES + PAGE_INTAKES
    ):
        raise LiveSourceError("saved_item_invalid_checkpoint")
    outcomes: list[Outcome] = []
    for item in cast(list[object], value["outcomes"]):
        if (
            not isinstance(item, list)
            or len(item) != 5
            or any(type(part) is not str for part in item)
        ):
            raise LiveSourceError("saved_item_invalid_checkpoint")
        outcomes.append(cast(Outcome, tuple(item)))
    try:
        folder = _folder_path(value["folder_path"])
    except LiveSourceError:
        raise LiveSourceError("saved_item_invalid_checkpoint") from None
    return folder, value["complete"], tuple(outcomes)


def _notices(counts: Counter[str], refused: list[tuple[str, str]]) -> tuple[str, ...]:
    notices = [f"saved_item_count:{outcome}:{counts[outcome]}" for outcome in sorted(counts)]
    # Other writers control names, so an entry is reported only by its name-hash prefix.
    notices.extend(f"saved_item_refused:{code}:{hashed[:12]}" for hashed, code in refused)
    return tuple(notices[:_NOTICE_LIMIT])


class SavedItemLiveSource:
    """One page of new saved items per fetch; the index advances only on acknowledge."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise LiveSourceError("source_invalid_value")
        self._root = root

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        folder_path = _options(options)
        if checkpoint is not None and _checkpoint(checkpoint)[0] != folder_path:
            raise LiveSourceError("source_selection_mismatch")
        store = PrivateJsonStore(self._root)
        with open_folder(folder_path) as folder:
            if folder.selection != selection:
                raise LiveSourceError("source_selection_mismatch")
            listing = list_records(folder)
            index = load_index(store, index_name(selection))
            adapter = SavedItemSourceAdapter(selection)
            counts: Counter[str] = Counter()
            if listing.placeholders:
                counts["placeholder"] = listing.placeholders
            outcomes: list[Outcome] = []
            refused: list[tuple[str, str]] = []
            page: dict[str, _Candidate] = {}
            deferred: set[str] = set()
            page_bytes = 0
            has_more = False
            for name in listing.records:
                entry = read_entry(folder, name, index.fingerprint(name_hash(name)))
                if entry.status != "read":
                    if entry.status == "refused":
                        if len(outcomes) >= MAX_PAGE_OUTCOMES:
                            has_more = True
                            continue
                        outcomes.append((entry.name_hash, entry.fingerprint, entry.code, "", ""))
                        refused.append((entry.name_hash, entry.code))
                    counts[entry.code or entry.status] += 1
                    continue
                try:
                    record = adapter.record_from_bytes(entry.data)
                    intake = adapter.intake(record)
                except SavedItemRefusal as refusal:
                    if len(outcomes) >= MAX_PAGE_OUTCOMES:
                        has_more = True
                        continue
                    counts[refusal.code] += 1
                    outcomes.append((entry.name_hash, entry.fingerprint, refusal.code, "", ""))
                    refused.append((entry.name_hash, refusal.code))
                    continue
                content = content_digest(intake)
                delivered = index.delivered.get(record.item_key)
                if delivered is not None:
                    if len(outcomes) >= MAX_PAGE_OUTCOMES:
                        has_more = True
                        continue
                    state = "skipped" if delivered == content else "already_captured"
                    counts[state] += 1
                    outcomes.append((entry.name_hash, entry.fingerprint, state, "", ""))
                    if state == "already_captured":
                        refused.append((entry.name_hash, state))
                    continue
                candidate = _Candidate(
                    record.received_at,
                    entry.name_hash,
                    entry.fingerprint,
                    record.item_key,
                    content,
                    encoded_intake_size(intake),
                    intake,
                )
                current = page.get(record.item_key)
                if current is not None:
                    # The loser stays unindexed; first-save-wins settles it next page.
                    counts["deferred_duplicate"] += 1
                    has_more = True
                    if candidate.order < current.order:
                        page_bytes += candidate.size - current.size
                        page[record.item_key] = candidate
                    continue
                if record.item_key in deferred:
                    counts["deferred_duplicate"] += 1
                    continue
                if len(page) == PAGE_INTAKES or page_bytes + candidate.size > PAGE_BYTES:
                    # Defer this identity for the whole page, including smaller later losers.
                    deferred.add(record.item_key)
                    has_more = True
                    continue
                page[record.item_key] = candidate
                page_bytes += candidate.size
        winners = sorted(page.values(), key=lambda item: item.order)
        # Resolve duplicate order before enforcing bytes: an earlier, larger record
        # must never lose to a smaller later record. Trim whole identities instead.
        while page_bytes > PAGE_BYTES:
            page_bytes -= winners.pop().size
            has_more = True
        if winners:
            counts["delivered"] = len(winners)
        outcomes.extend(
            (item.name_hash, item.fingerprint, "delivered", item.item_key, item.content)
            for item in winners
        )
        proposed: dict[str, object] = {
            "complete": not has_more,
            "folder_path": str(folder_path),
            "outcomes": [list(item) for item in outcomes],
            "schema_version": 1,
        }
        return LiveBatch(
            intakes=tuple(item.intake for item in winners),
            checkpoint=proposed,
            has_more=has_more,
            notices=_notices(counts, refused),
        )

    def acknowledge(
        self,
        selection: SourceResourceSelection,
        checkpoint: dict[str, object],
    ) -> None:
        """Record outcomes the caller's committed checkpoint proves final.

        Every intake of a committed page has a terminal custody receipt (captured,
        duplicate or quarantined); quarantine is owned by collector recovery.
        Repeating this call is safe.
        """
        folder_path, complete, outcomes = _checkpoint(checkpoint)
        store = PrivateJsonStore(self._root)
        name = index_name(selection)
        with store.lock(name.removesuffix(".json")):
            with open_folder(folder_path) as folder:
                if folder.selection != selection:
                    raise LiveSourceError("source_selection_mismatch")
                present = (
                    {name_hash(entry) for entry in list_records(folder).records}
                    if complete
                    else None
                )
            index = load_index(store, name)
            for hashed, fingerprint, state, item_key, content in outcomes:
                index.record(hashed, fingerprint, state, item_key, content)
            if present is not None:
                index.prune(present)
            save_index(store, name, index)
