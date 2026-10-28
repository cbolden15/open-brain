"""Fixed-size known-file index for one selected saved-item folder.

The index lives in its own private store file, not in the 131,072-byte checkpoint. It is
written only from ``acknowledge``, after the collector commits the checkpoint.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_item import RECORD_REFUSAL_CODES
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

INDEX_MAX_ENTRIES = 10_000
INDEX_MAX_DELIVERED = 10_000
# Half of PrivateJsonStore's 4 MiB file bound.
_STORE_MAX_BYTES = 4_194_304
INDEX_SIZE_LIMIT = _STORE_MAX_BYTES // 2
TERMINAL_STATES = frozenset(
    {"already_captured", "delivered", "not_regular_file", "skipped", *RECORD_REFUSAL_CODES}
)
_HASH = re.compile(r"[0-9a-f]{32}")
_FINGERPRINT = re.compile(r"[0-9a-f]{24}")


def _invalid() -> LiveSourceError:
    return LiveSourceError("saved_item_invalid_index")


@dataclass(slots=True)
class SavedItemIndex:
    entries: dict[str, str] = field(default_factory=dict)
    delivered: dict[str, str] = field(default_factory=dict)

    def fingerprint(self, name_hash: str) -> str | None:
        value = self.entries.get(name_hash)
        return None if value is None else value.partition(":")[0]

    def record(
        self,
        name_hash: str,
        fingerprint: str,
        state: str,
        item_key: str = "",
        content: str = "",
    ) -> None:
        delivered = state == "delivered"
        if (
            _HASH.fullmatch(name_hash) is None
            or _FINGERPRINT.fullmatch(fingerprint) is None
            or state not in TERMINAL_STATES
            or (
                delivered
                and (_HASH.fullmatch(item_key) is None or _HASH.fullmatch(content) is None)
            )
            or (not delivered and (item_key or content))
        ):
            raise _invalid()
        self.entries.pop(name_hash, None)
        self.entries[name_hash] = f"{fingerprint}:{state}"
        while len(self.entries) > INDEX_MAX_ENTRIES:
            del self.entries[next(iter(self.entries))]
        if delivered:
            self.delivered.pop(item_key, None)
            self.delivered[item_key] = content
            # An evicted key falls back to custody dedupe, as after a lost index.
            while len(self.delivered) > INDEX_MAX_DELIVERED:
                del self.delivered[next(iter(self.delivered))]

    def prune(self, present: Collection[str]) -> None:
        self.entries = {key: value for key, value in self.entries.items() if key in present}

    def to_dict(self) -> dict[str, object]:
        return {
            "delivered": dict(self.delivered),
            "entries": dict(self.entries),
            "schema_version": 1,
        }

    @classmethod
    def from_dict(cls, value: object) -> SavedItemIndex:
        if not isinstance(value, dict) or set(value) != {"delivered", "entries", "schema_version"}:
            raise _invalid()
        entries, delivered = value["entries"], value["delivered"]
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(entries, dict)
            or not isinstance(delivered, dict)
            or len(entries) > INDEX_MAX_ENTRIES
            or len(delivered) > INDEX_MAX_DELIVERED
        ):
            raise _invalid()
        index = cls()
        for key, item in cast(dict[object, object], entries).items():
            if type(key) is not str or type(item) is not str:
                raise _invalid()
            fingerprint, _, state = item.partition(":")
            if (
                _HASH.fullmatch(key) is None
                or _FINGERPRINT.fullmatch(fingerprint) is None
                or state not in TERMINAL_STATES
            ):
                raise _invalid()
            index.entries[key] = item
        for key, item in cast(dict[object, object], delivered).items():
            if type(key) is not str or type(item) is not str:
                raise _invalid()
            if _HASH.fullmatch(key) is None or _HASH.fullmatch(item) is None:
                raise _invalid()
            index.delivered[key] = item
        return index


def index_name(selection: SourceResourceSelection) -> str:
    return "index-" + hashlib.sha256(selection.resource_id.encode()).hexdigest()[:32] + ".json"


def load_index(store: PrivateJsonStore, name: str) -> SavedItemIndex:
    value = store.read(name)
    return SavedItemIndex() if value is None else SavedItemIndex.from_dict(value)


def save_index(store: PrivateJsonStore, name: str, index: SavedItemIndex) -> None:
    value = index.to_dict()
    try:
        bounded_json(value, INDEX_SIZE_LIMIT, preserve_order=True)
    except LiveSourceError as error:
        if error.code == "source_state_too_large":
            raise LiveSourceError("saved_item_index_too_large") from None
        raise
    store.write(name, value, preserve_order=True)
