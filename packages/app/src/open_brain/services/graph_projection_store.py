"""Atomic rebuildable cache for the managed structural graph projection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import ManagedGraphSnapshot, ManagedSuggestion
from open_brain_engine.storage.filesystem import (
    RootConfinementError,
    RootIdentity,
    StorageError,
    atomic_replace,
    read_confined,
)

from open_brain.services.graphify_projection import (
    MAX_GRAPHIFY_BYTES,
    GraphifyAdapter,
    GraphifyDiagnostic,
    GraphifyExtraction,
    GraphifyFailure,
)

PROJECTION_PROTOCOL = "open-brain-graph-projection-v1"
PROJECTION_PATH = ".open-brain/state/managed-graph-projection.json"
MAX_CANVAS_BYTES = 64 * 1024

_PAGE_ID = re.compile(r"^page_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_REVISION_ID = re.compile(
    r"^revision_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_GRAPH_ID = re.compile(r"^graph_[0-9a-f]{64}$")
_STORED_FAILURES = {
    "adapter_unavailable",
    "ambiguous_snapshot",
    "cancelled",
    "cleanup_failed",
    "component_mismatch",
    "input_invalid",
    "output_invalid",
    "timeout",
    "worker_failed",
}


@dataclass(frozen=True, slots=True)
class StructuralGraphLink:
    source_note_id: str
    source_revision_id: str
    target_note_id: str
    target_revision_id: str
    kind: str = "explicit_reference"


@dataclass(frozen=True, slots=True)
class StructuralGraphReceipt:
    workspace_id: str
    status: str
    generation_id: str | None
    snapshot_sha256: str | None
    adapter_identity: str | None
    links: tuple[StructuralGraphLink, ...]
    diagnostics: tuple[GraphifyDiagnostic, ...]
    failure: str | None

    def __post_init__(self) -> None:
        if (
            self.status not in {"failed", "fresh", "missing", "stale"}
            or self.status == "fresh" and self.failure is not None
            or self.status == "missing" and any(
                value is not None
                for value in (
                    self.generation_id,
                    self.snapshot_sha256,
                    self.adapter_identity,
                    self.failure,
                )
            )
        ):
            raise ValueError("invalid structural graph receipt")


class GraphProjectionStore:
    """Publish complete graph generations without touching managed Markdown."""

    def __init__(self, root: Path, root_identity: RootIdentity) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("invalid graph projection root")
        self._root = root
        self._root_identity = root_identity

    def refresh(
        self,
        snapshot: ManagedGraphSnapshot,
        adapter: GraphifyAdapter,
    ) -> StructuralGraphReceipt:
        if not isinstance(snapshot, ManagedGraphSnapshot) or not isinstance(
            adapter, GraphifyAdapter
        ):
            raise ValueError("invalid graph projection refresh")
        try:
            extraction = adapter.extract(snapshot)
            if extraction.status == "blocked":
                raise GraphifyFailure("ambiguous_snapshot")
            record = _successful_record(snapshot, adapter.identity, extraction)
            self._write(record)
        except GraphifyFailure as error:
            return self.record_failure(snapshot, error.code)
        return self.load(snapshot)

    def record_failure(
        self, snapshot: ManagedGraphSnapshot, failure: str
    ) -> StructuralGraphReceipt:
        if not isinstance(snapshot, ManagedGraphSnapshot) or failure not in _STORED_FAILURES:
            raise ValueError("invalid graph projection failure")
        existing = self._read_record()
        if existing is None:
            record: dict[str, object] = {
                "adapter_identity": None,
                "diagnostics": [],
                "failed_snapshot_sha256": snapshot.snapshot_sha256,
                "generation_id": None,
                "last_failure": failure,
                "links": [],
                "protocol": PROJECTION_PROTOCOL,
                "snapshot_sha256": None,
                "workspace_id": snapshot.workspace_id,
            }
        else:
            if existing["workspace_id"] != snapshot.workspace_id:
                raise GraphifyFailure("output_invalid")
            record = dict(existing)
            record["failed_snapshot_sha256"] = snapshot.snapshot_sha256
            record["last_failure"] = failure
        self._write(record)
        return self.load(snapshot)

    def load(self, snapshot: ManagedGraphSnapshot) -> StructuralGraphReceipt:
        if not isinstance(snapshot, ManagedGraphSnapshot):
            raise ValueError("invalid graph projection snapshot")
        record = self._read_record()
        if record is None:
            return StructuralGraphReceipt(
                workspace_id=snapshot.workspace_id,
                status="missing",
                generation_id=None,
                snapshot_sha256=None,
                adapter_identity=None,
                links=(),
                diagnostics=(),
                failure=None,
            )
        if record["workspace_id"] != snapshot.workspace_id:
            raise GraphifyFailure("output_invalid")
        generation_id = cast(str | None, record["generation_id"])
        recorded_snapshot = cast(str | None, record["snapshot_sha256"])
        failure = cast(str | None, record["last_failure"])
        if generation_id is None:
            status = "failed"
        elif failure is not None or recorded_snapshot != snapshot.snapshot_sha256:
            status = "stale"
            if failure is None:
                failure = "snapshot_changed"
        else:
            status = "fresh"
        return StructuralGraphReceipt(
            workspace_id=snapshot.workspace_id,
            status=status,
            generation_id=generation_id,
            snapshot_sha256=recorded_snapshot,
            adapter_identity=cast(str | None, record["adapter_identity"]),
            links=tuple(
                StructuralGraphLink(
                    source_note_id=cast(str, item["source_note_id"]),
                    source_revision_id=cast(str, item["source_revision_id"]),
                    target_note_id=cast(str, item["target_note_id"]),
                    target_revision_id=cast(str, item["target_revision_id"]),
                )
                for item in cast(list[dict[str, object]], record["links"])
            ),
            diagnostics=tuple(
                GraphifyDiagnostic(
                    code=cast(str, item["code"]),
                    source_note_id=cast(str | None, item["source_note_id"]),
                )
                for item in cast(list[dict[str, object]], record["diagnostics"])
            ),
            failure=failure,
        )

    def _read_record(self) -> dict[str, object] | None:
        try:
            payload = read_confined(
                root=self._root,
                relative=PROJECTION_PATH,
                expected_root_identity=self._root_identity,
                maximum_bytes=MAX_GRAPHIFY_BYTES,
            )
        except (RootConfinementError, StorageError):
            raise GraphifyFailure("output_invalid") from None
        if payload is None:
            return None
        return _validate_record(payload)

    def _write(self, record: dict[str, object]) -> None:
        payload = portable_canonical_json_bytes(record)
        if len(payload) > MAX_GRAPHIFY_BYTES:
            raise GraphifyFailure("output_invalid")
        try:
            existing = read_confined(
                root=self._root,
                relative=PROJECTION_PATH,
                expected_root_identity=self._root_identity,
                maximum_bytes=MAX_GRAPHIFY_BYTES,
            )
            if existing is not None:
                _validate_record(existing)
            atomic_replace(
                root=self._root,
                relative=PROJECTION_PATH,
                data=payload,
                require_existing=existing is not None,
                expected_existing_sha256=(
                    sha256(existing).hexdigest() if existing is not None else None
                ),
                expected_root_identity=self._root_identity,
            )
        except GraphifyFailure:
            raise
        except (RootConfinementError, StorageError):
            raise GraphifyFailure("worker_failed") from None


def _successful_record(
    snapshot: ManagedGraphSnapshot,
    adapter_identity: str,
    extraction: GraphifyExtraction,
) -> dict[str, object]:
    if extraction.snapshot_sha256 != snapshot.snapshot_sha256:
        raise GraphifyFailure("output_invalid")
    sources = {source.note_id: source for source in snapshot.sources}
    links = [
        {
            "kind": link.kind,
            "source_note_id": link.source_note_id,
            "source_revision_id": sources[link.source_note_id].revision_id,
            "target_note_id": link.target_note_id,
            "target_revision_id": sources[link.target_note_id].revision_id,
        }
        for link in extraction.links
    ]
    diagnostics = [
        {"code": item.code, "source_note_id": item.source_note_id}
        for item in extraction.diagnostics
    ]
    generation = {
        "adapter_identity": adapter_identity,
        "diagnostics": diagnostics,
        "links": links,
        "snapshot_sha256": snapshot.snapshot_sha256,
        "workspace_id": snapshot.workspace_id,
    }
    return {
        **generation,
        "failed_snapshot_sha256": None,
        "generation_id": "graph_" + sha256(portable_canonical_json_bytes(generation)).hexdigest(),
        "last_failure": None,
        "protocol": PROJECTION_PROTOCOL,
    }


def _validate_record(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise GraphifyFailure("output_invalid")
            result[key] = value
        return result

    try:
        value = json.loads(payload, object_pairs_hook=unique, parse_constant=lambda _x: _reject())
    except GraphifyFailure:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, RecursionError):
        raise GraphifyFailure("output_invalid") from None
    if (
        not isinstance(value, dict)
        or portable_canonical_json_bytes(value) != payload
        or set(value)
        != {
            "adapter_identity",
            "diagnostics",
            "failed_snapshot_sha256",
            "generation_id",
            "last_failure",
            "links",
            "protocol",
            "snapshot_sha256",
            "workspace_id",
        }
        or value.get("protocol") != PROJECTION_PROTOCOL
        or not isinstance(value.get("workspace_id"), str)
        or not isinstance(value.get("links"), list)
        or not isinstance(value.get("diagnostics"), list)
        or len(cast(list[object], value["links"])) > 64
        or len(cast(list[object], value["diagnostics"])) > 64
    ):
        raise GraphifyFailure("output_invalid")
    _validate_optional_string(value, "adapter_identity")
    _validate_optional_digest(value, "failed_snapshot_sha256")
    _validate_optional_digest(value, "snapshot_sha256")
    _validate_optional_string(value, "generation_id")
    _validate_optional_string(value, "last_failure")
    generation_id = value.get("generation_id")
    last_failure = value.get("last_failure")
    if last_failure is not None and last_failure not in _STORED_FAILURES:
        raise GraphifyFailure("output_invalid")
    if generation_id is None:
        if (
            value.get("adapter_identity") is not None
            or value.get("snapshot_sha256") is not None
            or value["links"]
            or value["diagnostics"]
        ):
            raise GraphifyFailure("output_invalid")
    elif not isinstance(generation_id, str) or _GRAPH_ID.fullmatch(generation_id) is None:
        raise GraphifyFailure("output_invalid")
    for item in cast(list[object], value["links"]):
        if (
            not isinstance(item, dict)
            or set(item)
            != {
                "kind",
                "source_note_id",
                "source_revision_id",
                "target_note_id",
                "target_revision_id",
            }
            or item.get("kind") != "explicit_reference"
            or not all(isinstance(child, str) for child in item.values())
            or _PAGE_ID.fullmatch(cast(str, item.get("source_note_id"))) is None
            or _PAGE_ID.fullmatch(cast(str, item.get("target_note_id"))) is None
            or _REVISION_ID.fullmatch(cast(str, item.get("source_revision_id"))) is None
            or _REVISION_ID.fullmatch(cast(str, item.get("target_revision_id"))) is None
            or item.get("source_note_id") == item.get("target_note_id")
        ):
            raise GraphifyFailure("output_invalid")
    for item in cast(list[object], value["diagnostics"]):
        if (
            not isinstance(item, dict)
            or set(item) != {"code", "source_note_id"}
            or item.get("code") not in {"ambiguous_snapshot", "unresolved_reference"}
            or item.get("source_note_id") is not None
            and (
                not isinstance(item.get("source_note_id"), str)
                or _PAGE_ID.fullmatch(cast(str, item.get("source_note_id"))) is None
            )
        ):
            raise GraphifyFailure("output_invalid")
    if generation_id is not None:
        generation = {
            "adapter_identity": value["adapter_identity"],
            "diagnostics": value["diagnostics"],
            "links": value["links"],
            "snapshot_sha256": value["snapshot_sha256"],
            "workspace_id": value["workspace_id"],
        }
        expected = "graph_" + sha256(portable_canonical_json_bytes(generation)).hexdigest()
        if generation_id != expected:
            raise GraphifyFailure("output_invalid")
    if (last_failure is None) is not (value.get("failed_snapshot_sha256") is None):
        raise GraphifyFailure("output_invalid")
    return value


def _validate_optional_string(value: dict[str, object], key: str) -> None:
    child = value.get(key)
    if child is not None and (not isinstance(child, str) or not child):
        raise GraphifyFailure("output_invalid")


def _validate_optional_digest(value: dict[str, object], key: str) -> None:
    child = value.get(key)
    if child is not None and (
        not isinstance(child, str)
        or len(child) != 64
        or any(character not in "0123456789abcdef" for character in child)
    ):
        raise GraphifyFailure("output_invalid")


def _reject() -> None:
    raise GraphifyFailure("output_invalid")


def projection_result(
    snapshot: ManagedGraphSnapshot,
    structural: StructuralGraphReceipt,
    suggestions: tuple[ManagedSuggestion, ...],
) -> dict[str, object]:
    """Render explicit and inferred relations as one path-free local presentation."""
    if structural.workspace_id != snapshot.workspace_id or any(
        suggestion.workspace_id != snapshot.workspace_id for suggestion in suggestions
    ):
        raise ValueError("graph projection workspace mismatch")
    revisions = {source.note_id: source.revision_id for source in snapshot.sources}
    return {
        "adapter_identity": structural.adapter_identity,
        "diagnostics": [
            {"code": item.code, "source_note_id": item.source_note_id}
            for item in structural.diagnostics
        ],
        "failure": structural.failure,
        "generation_id": structural.generation_id,
        "inferred_suggestions": [
            {
                "kind": "inferred_suggestion",
                "model": suggestion.model,
                "provider": suggestion.provider.value,
                "revision_status": _suggestion_revision_status(suggestion, revisions),
                "source_evidence": {
                    "note_id": suggestion.source_note_id,
                    "quote": suggestion.source_quote,
                },
                "suggestion_id": suggestion.suggestion_id,
                "target_evidence": {
                    "note_id": suggestion.target_note_id,
                    "quote": suggestion.target_quote,
                },
            }
            for suggestion in suggestions
        ],
        "snapshot_sha256": structural.snapshot_sha256,
        "status": structural.status,
        "structural_links": [
            {
                "kind": link.kind,
                "revision_status": (
                    "current"
                    if revisions.get(link.source_note_id) == link.source_revision_id
                    and revisions.get(link.target_note_id) == link.target_revision_id
                    else "stale"
                ),
                "source_note_id": link.source_note_id,
                "source_revision_id": link.source_revision_id,
                "target_note_id": link.target_note_id,
                "target_revision_id": link.target_revision_id,
            }
            for link in structural.links
        ],
        "workspace_id": snapshot.workspace_id,
    }


def canvas_result(
    snapshot: ManagedGraphSnapshot,
    structural: StructuralGraphReceipt,
    suggestions: tuple[ManagedSuggestion, ...],
) -> dict[str, object]:
    """Render a deterministic Obsidian Canvas with local file-node mappings."""
    if structural.workspace_id != snapshot.workspace_id or any(
        suggestion.workspace_id != snapshot.workspace_id for suggestion in suggestions
    ):
        raise ValueError("graph Canvas workspace mismatch")
    ordered_sources = sorted(
        snapshot.sources,
        key=lambda source: (source.relative_path.casefold(), source.relative_path, source.note_id),
    )
    revisions = {source.note_id: source.revision_id for source in ordered_sources}
    node_ids = {
        source.note_id: _canvas_id("note", source.note_id) for source in ordered_sources
    }
    stale_suggestions = sum(
        _suggestion_revision_status(suggestion, revisions) == "stale"
        for suggestion in suggestions
    )
    nodes: list[dict[str, object]] = [
        {
            "height": 160,
            "id": _canvas_id("status", snapshot.workspace_id),
            "text": (
                "# Open Brain graph\n\n"
                f"Status: {structural.status}\n\n"
                f"Explicit links: {len(structural.links)}  |  "
                f"Suggestions: {len(suggestions)}  |  "
                f"Stale suggestions: {stale_suggestions}"
            ),
            "type": "text",
            "width": 400,
            "x": 0,
            "y": -240,
        }
    ]
    for index, source in enumerate(ordered_sources):
        nodes.append(
            {
                "file": source.relative_path,
                "height": 240,
                "id": node_ids[source.note_id],
                "type": "file",
                "width": 360,
                "x": index % 4 * 440,
                "y": index // 4 * 320,
            }
        )
    edges: list[dict[str, object]] = []
    for link in structural.links:
        if link.source_note_id not in node_ids or link.target_note_id not in node_ids:
            continue
        current = (
            structural.status == "fresh"
            and revisions[link.source_note_id] == link.source_revision_id
            and revisions[link.target_note_id] == link.target_revision_id
        )
        edges.append(
            {
                "color": "4" if current else "1",
                "fromNode": node_ids[link.source_note_id],
                "id": _canvas_id(
                    "explicit", link.source_note_id, link.target_note_id
                ),
                "label": "Explicit link" if current else "Explicit link (stale)",
                "toEnd": "arrow",
                "toNode": node_ids[link.target_note_id],
            }
        )
    for suggestion in suggestions:
        if (
            suggestion.source_note_id not in node_ids
            or suggestion.target_note_id not in node_ids
        ):
            continue
        current = _suggestion_revision_status(suggestion, revisions) == "current"
        edges.append(
            {
                "color": "3" if current else "1",
                "fromNode": node_ids[suggestion.source_note_id],
                "id": _canvas_id("suggestion", suggestion.suggestion_id),
                "label": "Suggested" if current else "Suggested (stale)",
                "toEnd": "arrow",
                "toNode": node_ids[suggestion.target_note_id],
            }
        )
    result: dict[str, object] = {"edges": edges, "nodes": nodes}
    if len(portable_canonical_json_bytes(result)) > MAX_CANVAS_BYTES:
        raise ValueError("graph Canvas exceeds output limit")
    return result


def _suggestion_revision_status(
    suggestion: ManagedSuggestion, revisions: dict[str, str]
) -> str:
    return (
        "current"
        if revisions.get(suggestion.source_note_id) == suggestion.source_revision_id
        and revisions.get(suggestion.target_note_id) == suggestion.target_revision_id
        else "stale"
    )


def _canvas_id(kind: str, *parts: str) -> str:
    return sha256("\x00".join((kind, *parts)).encode("utf-8")).hexdigest()[:16]


__all__ = [
    "MAX_CANVAS_BYTES",
    "PROJECTION_PATH",
    "PROJECTION_PROTOCOL",
    "GraphProjectionStore",
    "StructuralGraphLink",
    "StructuralGraphReceipt",
    "canvas_result",
    "projection_result",
]
