"""Canonical Portable evidence for saved-item source identity."""

from collections.abc import Mapping
from dataclasses import asdict

from open_brain_engine.engine.saved_item_identity import SavedItemSource

from .v1 import PortableValidationError, _portable_record

SAVED_ITEM_SOURCES_PATH = "history/saved-item-sources/sources-v1.json"
SAVED_ITEM_SOURCES_CONTRACT = "open-brain-saved-item-sources.v1"


def saved_item_sources_payload(sources: tuple[SavedItemSource, ...]) -> dict[str, object]:
    return {
        "contract": SAVED_ITEM_SOURCES_CONTRACT,
        "sources": [asdict(source) for source in sources],
    }


def validate_saved_item_sources(files: Mapping[str, bytes]) -> tuple[SavedItemSource, ...]:
    raw = files.get(SAVED_ITEM_SOURCES_PATH, b"")
    try:
        value = _portable_record(raw, "saved-item sources")
    except RecursionError:
        raise PortableValidationError("invalid saved-item source evidence") from None
    sources = value.get("sources")
    if (
        set(value) != {"contract", "sources"}
        or value["contract"] != SAVED_ITEM_SOURCES_CONTRACT
        or not isinstance(sources, list)
    ):
        raise PortableValidationError("invalid saved-item source evidence")
    validated: list[SavedItemSource] = []
    for source in sources:
        if not isinstance(source, dict) or set(source) != {
            "connector_name", "connection_id", "resource_id"
        }:
            raise PortableValidationError("invalid saved-item source fields")
        try:
            identity = SavedItemSource(
                source["connector_name"], source["connection_id"], source["resource_id"]
            )
        except ValueError:
            raise PortableValidationError("invalid saved-item source identity") from None
        if validated and identity <= validated[-1]:
            raise PortableValidationError("duplicate or unordered saved-item source identity")
        validated.append(identity)
    return tuple(validated)
