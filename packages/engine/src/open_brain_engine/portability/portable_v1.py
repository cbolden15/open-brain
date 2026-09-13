"""Decode one validated Portable Brain v1 snapshot into shared values."""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from hashlib import sha256
from typing import cast

from open_brain_engine.portable.v1 import PortableSnapshot
from open_brain_engine.storage.markdown import parse_markdown

from .model import (
    FAMILY_SCHEMA_URI,
    PortabilityMappingError,
    SharedAttachment,
    SharedBlob,
    SharedBrain,
    SharedFamily,
    SharedImportEvidence,
    SharedRecord,
)

_JSON_FAMILIES: tuple[tuple[str, SharedFamily, str], ...] = (
    ("sources/captures/", "capture", "capture_id"),
    ("history/proposals/", "proposal", "proposal_id"),
    ("history/decisions/", "decision", "decision_id"),
    ("history/publications/", "publication", "publication_id"),
    ("history/actions/", "action", "action_id"),
    ("history/routes/", "route", "route_id"),
)

_TIMESTAMP_FIELDS: Mapping[SharedFamily, str] = {
    "action": "recorded_at",
    "capture": "accepted_at",
    "decision": "recorded_at",
    "event": "recorded_at",
    "measurement": "recorded_at",
    "page": "modified_at",
    "proposal": "recorded_at",
    "publication": "recorded_at",
    "route": "recorded_at",
}


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PortabilityMappingError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortabilityMappingError(f"{label} must be a non-empty string")
    return value


def _json_object(payload: bytes, label: str) -> dict[str, object]:
    try:
        return _object(json.loads(payload), label)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PortabilityMappingError(f"{label} is invalid JSON") from error


def _record(
    *,
    family: SharedFamily,
    semantic_id: str,
    path: str,
    ordinal: int | None,
    payload: bytes,
    actor_id: str,
    source_timestamp: str | None,
    space_id: str | None,
    provenance_ids: tuple[str, ...],
) -> SharedRecord:
    return SharedRecord(
        family=family,
        semantic_id=semantic_id,
        schema_uri=FAMILY_SCHEMA_URI[family],
        source_path=path,
        source_ordinal=ordinal,
        source_bytes=payload,
        source_sha256=sha256(payload).hexdigest(),
        actor_id=actor_id,
        source_timestamp=source_timestamp,
        space_id=space_id,
        provenance_ids=provenance_ids,
    )


def _provenance_ids(family: SharedFamily, value: Mapping[str, object]) -> tuple[str, ...]:
    if family in {"brain", "space"}:
        return ()
    if family in {"event", "measurement"}:
        supersedes = value.get("supersedes")
        return () if supersedes is None else (_string(supersedes, "supersedes"),)
    if family == "capture":
        binding = _object(value.get("payload_binding"), "capture payload binding")
        if binding.get("kind") == "batch":
            return (_string(binding.get("record_id"), "capture batch record"),)
        return ()
    if family == "page":
        values = value.get("provenance")
        if not isinstance(values, list):
            raise PortabilityMappingError("page provenance must be a list")
        return tuple(_string(item, "page provenance") for item in values)
    if family == "proposal":
        values = value.get("capture_ids")
        if not isinstance(values, list):
            raise PortabilityMappingError("proposal capture_ids must be a list")
        return tuple(_string(item, "proposal capture") for item in values)
    if family == "decision":
        return (_string(value.get("proposal_id"), "decision proposal"),)
    if family == "publication":
        return (
            _string(value.get("decision_id"), "publication decision"),
            _string(value.get("page_id"), "publication page"),
        )
    if family == "action":
        return (
            _string(value.get("proposal_id"), "action proposal"),
            _string(value.get("decision_id"), "action decision"),
        )
    if family == "route":
        sources = [_string(value.get("capture_id"), "route capture")]
        if value.get("supersedes") is not None:
            sources.append(_string(value.get("supersedes"), "superseded route"))
        return tuple(sources)
    raise PortabilityMappingError("unsupported provenance family")


def _semantic_record(
    family: SharedFamily,
    identity_field: str,
    path: str,
    payload: bytes,
) -> SharedRecord:
    value = _json_object(payload, family)
    timestamp_field = _TIMESTAMP_FIELDS[family]
    space_value = value.get("space_id")
    return _record(
        family=family,
        semantic_id=_string(value.get(identity_field), f"{family} identity"),
        path=path,
        ordinal=None,
        payload=payload,
        actor_id=_string(value.get("actor_id"), f"{family} actor"),
        source_timestamp=_string(value.get(timestamp_field), f"{family} timestamp"),
        space_id=None if space_value is None else _string(space_value, f"{family} space"),
        provenance_ids=_provenance_ids(family, value),
    )


def _decode_snapshot(snapshot: PortableSnapshot) -> SharedBrain:
    if not isinstance(snapshot, PortableSnapshot):
        raise PortabilityMappingError("source must be a validated PortableSnapshot")
    source_files = dict(snapshot.files)
    if any(
        not isinstance(path, str) or not isinstance(payload, bytes)
        for path, payload in source_files.items()
    ):
        raise PortabilityMappingError("Portable snapshot files are invalid")
    manifest_bytes = source_files.get("portable-manifest.json")
    if manifest_bytes is None:
        raise PortabilityMappingError("Portable snapshot manifest is missing")
    manifest = _json_object(manifest_bytes, "Portable manifest")
    if manifest != snapshot.manifest:
        raise PortabilityMappingError("Portable snapshot manifest evidence differs")
    raw_entries = manifest.get("files")
    if not isinstance(raw_entries, list):
        raise PortabilityMappingError("Portable manifest inventory is invalid")
    declared = tuple(
        (
            _string(_object(entry, "manifest entry").get("path"), "manifest path"),
            _string(_object(entry, "manifest entry").get("sha256"), "manifest digest"),
        )
        for entry in raw_entries
    )
    evidence = SharedImportEvidence(
        manifest_bytes=manifest_bytes,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        export_id=_string(manifest.get("export_id"), "manifest export_id"),
        declared_files=declared,
    )
    if set(source_files) != {"portable-manifest.json", *(path for path, _ in declared)}:
        raise PortabilityMappingError("Portable snapshot and manifest inventories differ")

    try:
        profile = tomllib.loads(source_files["brain.toml"].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PortabilityMappingError("Portable Brain profile is invalid") from error
    profile = _object(profile, "Portable Brain profile")
    source_brain_id = _string(profile.get("tenant_id"), "Portable Brain identity")
    owner_actor_id = _string(profile.get("owner_actor_id"), "Portable owner identity")
    records: list[SharedRecord] = [
        _record(
            family="brain",
            semantic_id=source_brain_id,
            path="brain.toml",
            ordinal=None,
            payload=source_files["brain.toml"],
            actor_id=owner_actor_id,
            source_timestamp=None,
            space_id=None,
            provenance_ids=(),
        )
    ]
    blobs: list[SharedBlob] = []
    attachments: list[SharedAttachment] = []

    for path, declared_digest in declared:
        if path == "brain.toml":
            continue
        payload = source_files[path]
        if sha256(payload).hexdigest() != declared_digest:
            raise PortabilityMappingError("Portable source digest differs from its manifest")
        if path.startswith("sources/blobs/sha256/"):
            blobs.append(SharedBlob(path=path, sha256=declared_digest, data=payload))
            continue
        if path.endswith("/_space.md"):
            fields = dict(parse_markdown(payload).fields)
            records.append(
                _record(
                    family="space",
                    semantic_id=_string(fields.get("space_id"), "space identity"),
                    path=path,
                    ordinal=None,
                    payload=payload,
                    actor_id=_string(fields.get("actor_id"), "space actor"),
                    source_timestamp=None,
                    space_id=_string(fields.get("space_id"), "space identity"),
                    provenance_ids=(),
                )
            )
            continue
        if path.endswith(".md") and path.rsplit("/", 1)[-1].startswith("page_"):
            fields = dict(parse_markdown(payload).fields)
            records.append(
                _record(
                    family="page",
                    semantic_id=_string(fields.get("page_id"), "page identity"),
                    path=path,
                    ordinal=None,
                    payload=payload,
                    actor_id=_string(fields.get("actor_id"), "page actor"),
                    source_timestamp=_string(fields.get("modified_at"), "page timestamp"),
                    space_id=_string(fields.get("space_id"), "page space"),
                    provenance_ids=_provenance_ids("page", fields),
                )
            )
            continue
        if path.endswith(".md"):
            attachments.append(SharedAttachment(path=path, sha256=declared_digest, data=payload))
            continue
        if path.endswith(".jsonl"):
            for ordinal, line in enumerate(payload.splitlines()):
                value = _json_object(line, "Portable JSONL row")
                record_id = _string(value.get("record_id"), "batch record identity")
                family: SharedFamily
                if record_id.startswith("event_"):
                    family = "event"
                elif record_id.startswith("measurement_"):
                    family = "measurement"
                else:
                    raise PortabilityMappingError("batch row family is unsupported")
                records.append(
                    _record(
                        family=family,
                        semantic_id=record_id,
                        path=path,
                        ordinal=ordinal,
                        payload=line,
                        actor_id=_string(value.get("actor_id"), "batch row actor"),
                        source_timestamp=_string(value.get("recorded_at"), "batch row timestamp"),
                        space_id=None,
                        provenance_ids=_provenance_ids(family, value),
                    )
                )
            continue
        matched = next(
            (
                (family, identity_field)
                for prefix, family, identity_field in _JSON_FAMILIES
                if path.startswith(prefix) and path.endswith(".json")
            ),
            None,
        )
        if matched is None:
            raise PortabilityMappingError("Portable manifest contains an unmapped file")
        records.append(_semantic_record(matched[0], matched[1], path, payload))

    return SharedBrain(
        source_brain_id=source_brain_id,
        owner_actor_id=owner_actor_id,
        evidence=evidence,
        records=tuple(records),
        blobs=tuple(blobs),
        attachments=tuple(attachments),
    )


def shared_brain_from_snapshot(snapshot: PortableSnapshot) -> SharedBrain:
    """Map one immutable validated snapshot without reading its source path again."""
    try:
        return _decode_snapshot(snapshot)
    except PortabilityMappingError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise PortabilityMappingError("Portable snapshot cannot be mapped losslessly") from error


__all__ = ["shared_brain_from_snapshot"]
