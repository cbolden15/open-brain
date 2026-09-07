"""Product-neutral values for lossless Portable Brain upgrades."""

from __future__ import annotations

import json
import re
import tomllib
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

from open_brain_engine.portable import (
    PORTABLE_V1_SCHEMA_CATALOG_DIGEST,
    portable_canonical_json_bytes,
    validate_portable_file_set,
)
from open_brain_engine.protocol import RESOURCE_LIMITS
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

type SharedFamily = Literal[
    "action",
    "brain",
    "capture",
    "decision",
    "event",
    "measurement",
    "page",
    "proposal",
    "publication",
    "route",
    "space",
]


class PortabilityMappingError(ValueError):
    """A validated export cannot be represented by the shared model."""


FAMILY_ID_PREFIX: Mapping[SharedFamily, str] = MappingProxyType(
    {
        "action": "action",
        "brain": "tenant",
        "capture": "capture",
        "decision": "decision",
        "event": "event",
        "measurement": "measurement",
        "page": "page",
        "proposal": "proposal",
        "publication": "publication",
        "route": "route",
        "space": "space",
    }
)

FAMILY_SCHEMA_URI: Mapping[SharedFamily, str] = MappingProxyType(
    {
        "action": "urn:open-brain:portable-brain:v1:action",
        "brain": "urn:open-brain:portable-brain:v1:brain-profile",
        "capture": "urn:open-brain:portable-brain:v1:capture",
        "decision": "urn:open-brain:portable-brain:v1:decision",
        "event": "urn:open-brain:portable-brain:v1:batch-row",
        "measurement": "urn:open-brain:portable-brain:v1:batch-row",
        "page": "urn:open-brain:portable-brain:v1:canonical-page-frontmatter",
        "proposal": "urn:open-brain:portable-brain:v1:proposal",
        "publication": "urn:open-brain:portable-brain:v1:publication",
        "route": "urn:open-brain:portable-brain:v1:routing",
        "space": "urn:open-brain:portable-brain:v1:space-frontmatter",
    }
)

_DIGEST = re.compile(r"[0-9a-f]{64}")
_PORTABLE_ID = re.compile(
    r"(?P<prefix>[a-z][a-z0-9_]*)_"
    r"(?P<uuid>[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})"
)
_PORTABLE_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z"
)
_OWNER_MARKDOWN = re.compile(
    r"content/spaces/[a-z0-9]+(?:-[a-z0-9]+)*/(?:notes|state)/"
    r"[a-z0-9]+(?:[-_][a-z0-9]+)*\.md"
)
_BLOB_PATH = re.compile(r"sources/blobs/sha256/[0-9a-f]{2}/[0-9a-f]{64}")

_JSON_SOURCE_PATHS: Mapping[SharedFamily, str] = MappingProxyType(
    {
        "action": "history/actions/",
        "capture": "sources/captures/",
        "decision": "history/decisions/",
        "proposal": "history/proposals/",
        "publication": "history/publications/",
        "route": "history/routes/",
    }
)

_IDENTITY_FIELDS: Mapping[SharedFamily, str] = MappingProxyType(
    {
        "action": "action_id",
        "brain": "tenant_id",
        "capture": "capture_id",
        "decision": "decision_id",
        "event": "record_id",
        "measurement": "record_id",
        "page": "page_id",
        "proposal": "proposal_id",
        "publication": "publication_id",
        "route": "route_id",
        "space": "space_id",
    }
)

_TIMESTAMP_FIELDS: Mapping[SharedFamily, str] = MappingProxyType(
    {
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
)


def validate_portable_identifier(value: str, prefix: str) -> str:
    if not isinstance(value, str) or (matched := _PORTABLE_ID.fullmatch(value)) is None:
        raise PortabilityMappingError(f"invalid Portable {prefix} identifier")
    if matched["prefix"] != prefix:
        raise PortabilityMappingError(f"invalid Portable {prefix} identifier")
    try:
        parsed = uuid.UUID(matched["uuid"])
    except ValueError as error:  # pragma: no cover - regex already narrows this.
        raise PortabilityMappingError(f"invalid Portable {prefix} identifier") from error
    if parsed.version != 4 or value != f"{prefix}_{parsed}":
        raise PortabilityMappingError(f"invalid Portable {prefix} identifier")
    return value


def validate_portable_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortabilityMappingError("unsafe or operational Portable path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or any(part in {"", ".", "..", ".open-brain"} for part in path.parts)
        or path.as_posix() != value
    ):
        raise PortabilityMappingError("unsafe or operational Portable path")
    return value


def _validate_digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise PortabilityMappingError(f"{label} must be lowercase SHA-256")
    return value


def _validate_portable_timestamp(value: str | None) -> None:
    if value is None:
        return
    if not isinstance(value, str) or _PORTABLE_TIMESTAMP.fullmatch(value) is None:
        raise PortabilityMappingError("source timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + "+00:00")
    except ValueError as error:
        raise PortabilityMappingError("source timestamp is invalid") from error
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise PortabilityMappingError("source timestamp is invalid")


def _source_object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PortabilityMappingError(f"{label} source bytes are invalid")
    return cast(dict[str, object], value)


def _source_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PortabilityMappingError(f"{label} source field is invalid")
    return value


def _source_strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise PortabilityMappingError(f"{label} source field is invalid")
    values = tuple(_source_string(item, label) for item in value)
    if len(values) != len(set(values)):
        raise PortabilityMappingError(f"{label} source field is invalid")
    return values


def _source_provenance(
    family: SharedFamily, value: Mapping[str, object]
) -> tuple[str, ...]:
    if family in {"brain", "space"}:
        return ()
    if family in {"event", "measurement"}:
        supersedes = value.get("supersedes")
        return () if supersedes is None else (_source_string(supersedes, "supersedes"),)
    if family == "capture":
        binding = _source_object(value.get("payload_binding"), "capture payload binding")
        if binding.get("kind") == "batch":
            return (_source_string(binding.get("record_id"), "capture batch record"),)
        return ()
    if family == "page":
        return _source_strings(value.get("provenance"), "page provenance")
    if family == "proposal":
        return _source_strings(value.get("capture_ids"), "proposal capture_ids")
    if family == "decision":
        return (_source_string(value.get("proposal_id"), "decision proposal"),)
    if family == "publication":
        return (
            _source_string(value.get("decision_id"), "publication decision"),
            _source_string(value.get("page_id"), "publication page"),
        )
    if family == "action":
        return (
            _source_string(value.get("proposal_id"), "action proposal"),
            _source_string(value.get("decision_id"), "action decision"),
        )
    if family == "route":
        sources = [_source_string(value.get("capture_id"), "route capture")]
        if value.get("supersedes") is not None:
            sources.append(_source_string(value.get("supersedes"), "superseded route"))
        return tuple(sources)
    raise PortabilityMappingError("unsupported provenance family")


def _validate_source_binding(
    *,
    family: SharedFamily,
    semantic_id: str,
    source_path: str,
    source_bytes: bytes,
    actor_id: str,
    source_timestamp: str | None,
    space_id: str | None,
    provenance_ids: tuple[str, ...],
) -> None:
    try:
        if family == "brain":
            value = _source_object(
                tomllib.loads(source_bytes.decode("utf-8")), "Portable Brain profile"
            )
        elif family in {"space", "page"}:
            value = _source_object(dict(parse_markdown(source_bytes).fields), family)
        else:
            value = _source_object(json.loads(source_bytes), family)
    except (UnicodeDecodeError, json.JSONDecodeError, tomllib.TOMLDecodeError, MarkdownFormatError):
        raise PortabilityMappingError("shared record source bytes are invalid") from None

    actor_field = "owner_actor_id" if family == "brain" else "actor_id"
    timestamp = (
        _source_string(value.get(_TIMESTAMP_FIELDS[family]), f"{family} timestamp")
        if family in _TIMESTAMP_FIELDS
        else None
    )
    raw_space_id = value.get("space_id")
    expected_space_id = (
        None if raw_space_id is None else _source_string(raw_space_id, f"{family} space")
    )
    expected = (
        _source_string(value.get(_IDENTITY_FIELDS[family]), f"{family} identity"),
        _source_string(value.get(actor_field), f"{family} actor"),
        timestamp,
        expected_space_id,
        _source_provenance(family, value),
    )
    actual = (semantic_id, actor_id, source_timestamp, space_id, provenance_ids)
    if actual != expected:
        raise PortabilityMappingError("shared record metadata differs from source bytes")

    expected_prefix = _JSON_SOURCE_PATHS.get(family)
    if family == "brain":
        path_matches = source_path == "brain.toml"
    elif family == "space":
        path_matches = source_path.startswith("content/spaces/") and source_path.endswith(
            "/_space.md"
        )
    elif family == "page":
        path_matches = source_path.startswith("content/spaces/") and source_path.endswith(
            f"/notes/{semantic_id}.md"
        )
    elif family in {"event", "measurement"}:
        path_matches = source_path.startswith("sources/batches/") and source_path.endswith(
            ".jsonl"
        )
    else:
        path_matches = (
            expected_prefix is not None
            and source_path.startswith(expected_prefix)
            and source_path.endswith(f"/{semantic_id}.json")
        )
    if not path_matches:
        raise PortabilityMappingError("shared record path does not match its source family")


def _iter_chunks(data: bytes, maximum_bytes: int) -> Iterator[bytes]:
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or maximum_bytes < 1
        or maximum_bytes > RESOURCE_LIMITS.blob_staging_bytes
    ):
        raise ValueError("chunk size exceeds the frozen staging limit")
    for offset in range(0, len(data), maximum_bytes):
        yield data[offset : offset + maximum_bytes]


@dataclass(frozen=True, slots=True)
class SharedRecord:
    family: SharedFamily
    semantic_id: str
    schema_uri: str
    source_path: str
    source_ordinal: int | None
    source_bytes: bytes
    source_sha256: str
    actor_id: str
    source_timestamp: str | None
    space_id: str | None
    provenance_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.family not in FAMILY_ID_PREFIX:
            raise PortabilityMappingError("unsupported shared record family")
        validate_portable_identifier(self.semantic_id, FAMILY_ID_PREFIX[self.family])
        if self.schema_uri != FAMILY_SCHEMA_URI[self.family]:
            raise PortabilityMappingError("shared record schema URI does not match its family")
        validate_portable_path(self.source_path)
        if not isinstance(self.source_bytes, bytes):
            raise PortabilityMappingError("shared record source bytes are invalid")
        _validate_digest(self.source_sha256, "shared record digest")
        if sha256(self.source_bytes).hexdigest() != self.source_sha256:
            raise PortabilityMappingError("shared record digest mismatch")
        if self.family in {"event", "measurement"}:
            if (
                isinstance(self.source_ordinal, bool)
                or not isinstance(self.source_ordinal, int)
                or self.source_ordinal < 0
                or not self.source_path.endswith(".jsonl")
            ):
                raise PortabilityMappingError("JSONL record ordinal is invalid")
        elif self.source_ordinal is not None:
            raise PortabilityMappingError("non-JSONL record cannot have an ordinal")
        validate_portable_identifier(self.actor_id, "actor")
        _validate_portable_timestamp(self.source_timestamp)
        if self.space_id is not None:
            validate_portable_identifier(self.space_id, "space")
        sources = tuple(self.provenance_ids)
        if len(sources) != len(set(sources)) or any(
            not isinstance(source_id, str) or _PORTABLE_ID.fullmatch(source_id) is None
            for source_id in sources
        ):
            raise PortabilityMappingError("shared record provenance is invalid")
        _validate_source_binding(
            family=self.family,
            semantic_id=self.semantic_id,
            source_path=self.source_path,
            source_bytes=self.source_bytes,
            actor_id=self.actor_id,
            source_timestamp=self.source_timestamp,
            space_id=self.space_id,
            provenance_ids=sources,
        )
        object.__setattr__(self, "provenance_ids", sources)


@dataclass(frozen=True, slots=True)
class SharedBlob:
    path: str
    sha256: str
    data: bytes

    def __post_init__(self) -> None:
        validate_portable_path(self.path)
        _validate_digest(self.sha256, "blob digest")
        if (
            _BLOB_PATH.fullmatch(self.path) is None
            or self.path.rsplit("/", 1)[-1] != self.sha256
            or self.path.split("/")[-2] != self.sha256[:2]
            or not isinstance(self.data, bytes)
            or sha256(self.data).hexdigest() != self.sha256
        ):
            raise PortabilityMappingError("content-addressed blob binding is invalid")

    def iter_chunks(
        self, maximum_bytes: int = RESOURCE_LIMITS.blob_staging_bytes
    ) -> Iterator[bytes]:
        return _iter_chunks(self.data, maximum_bytes)


@dataclass(frozen=True, slots=True)
class SharedAttachment:
    path: str
    sha256: str
    data: bytes

    def __post_init__(self) -> None:
        validate_portable_path(self.path)
        _validate_digest(self.sha256, "attachment digest")
        if (
            _OWNER_MARKDOWN.fullmatch(self.path) is None
            or self.path.endswith("/_space.md")
            or self.path.rsplit("/", 1)[-1].startswith("page_")
            or not isinstance(self.data, bytes)
            or sha256(self.data).hexdigest() != self.sha256
        ):
            raise PortabilityMappingError("owner Markdown attachment binding is invalid")

    def iter_chunks(
        self, maximum_bytes: int = RESOURCE_LIMITS.blob_staging_bytes
    ) -> Iterator[bytes]:
        return _iter_chunks(self.data, maximum_bytes)


@dataclass(frozen=True, slots=True)
class SharedImportEvidence:
    manifest_bytes: bytes
    manifest_sha256: str
    export_id: str
    declared_files: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.manifest_bytes, bytes):
            raise PortabilityMappingError("manifest bytes are invalid")
        _validate_digest(self.manifest_sha256, "manifest digest")
        if sha256(self.manifest_bytes).hexdigest() != self.manifest_sha256:
            raise PortabilityMappingError("manifest digest mismatch")
        validate_portable_identifier(self.export_id, "export")
        if not isinstance(self.declared_files, tuple):
            raise PortabilityMappingError("manifest inventory must be immutable")
        declared: tuple[tuple[str, str], ...] = self.declared_files
        if not declared:
            raise PortabilityMappingError("manifest inventory is empty")
        previous: str | None = None
        for entry in declared:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise PortabilityMappingError("manifest inventory entry is malformed")
            path, digest = entry
            validate_portable_path(path)
            _validate_digest(digest, "manifest entry digest")
            if previous is not None and path <= previous:
                raise PortabilityMappingError("manifest inventory must be sorted and unique")
            previous = path
        try:
            manifest = json.loads(self.manifest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise PortabilityMappingError("manifest is invalid JSON") from error
        if portable_canonical_json_bytes(manifest) != self.manifest_bytes:
            raise PortabilityMappingError("manifest bytes are not canonical")
        if not isinstance(manifest, dict):
            raise PortabilityMappingError("manifest is not an object")
        expected_keys = {
            "compatibility",
            "contract_version",
            "created_at",
            "export_id",
            "files",
            "layout_version",
            "schema_catalog_digest",
            "schema_version",
            "tenant_id",
        }
        manifest_files = manifest.get("files")
        projected: tuple[tuple[object, object], ...] = ()
        if isinstance(manifest_files, list) and all(
            isinstance(entry, dict)
            and all(isinstance(key, str) for key in entry)
            for entry in manifest_files
        ):
            projected = tuple(
                (entry.get("path"), entry.get("sha256"))
                for entry in cast(list[dict[str, object]], manifest_files)
            )
        if (
            set(manifest) != expected_keys
            or manifest.get("contract_version") != "1"
            or manifest.get("layout_version") != 1
            or manifest.get("schema_version") != 1
            or manifest.get("schema_catalog_digest") != PORTABLE_V1_SCHEMA_CATALOG_DIGEST
            or manifest.get("export_id") != self.export_id
            or projected != declared
        ):
            raise PortabilityMappingError("manifest evidence does not match its inventory")
        validate_portable_identifier(cast(str, manifest.get("tenant_id")), "tenant")
        _validate_portable_timestamp(cast(str, manifest.get("created_at")))
        object.__setattr__(self, "declared_files", declared)

    @property
    def source_brain_id(self) -> str:
        value = cast(dict[str, object], json.loads(self.manifest_bytes))
        return cast(str, value["tenant_id"])


@dataclass(frozen=True, slots=True)
class SharedBrain:
    source_brain_id: str
    owner_actor_id: str
    evidence: SharedImportEvidence
    records: tuple[SharedRecord, ...]
    blobs: tuple[SharedBlob, ...] = ()
    attachments: tuple[SharedAttachment, ...] = ()

    def __post_init__(self) -> None:
        validate_portable_identifier(self.source_brain_id, "tenant")
        validate_portable_identifier(self.owner_actor_id, "actor")
        if not isinstance(self.evidence, SharedImportEvidence):
            raise PortabilityMappingError("shared import evidence is invalid")
        evidence = replace(self.evidence)
        if evidence.source_brain_id != self.source_brain_id:
            raise PortabilityMappingError("manifest and shared Brain identities differ")
        records = tuple(replace(record) for record in self.records)
        blobs = tuple(replace(blob) for blob in self.blobs)
        attachments = tuple(replace(attachment) for attachment in self.attachments)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "blobs", blobs)
        object.__setattr__(self, "attachments", attachments)

        identities = [record.semantic_id for record in records]
        if len(identities) != len(set(identities)):
            raise PortabilityMappingError("duplicate shared semantic identity")
        known = set(identities)
        for record in records:
            missing = set(record.provenance_ids) - known
            if missing:
                raise PortabilityMappingError("shared record provenance target is missing")
        brain_records = [record for record in records if record.family == "brain"]
        if (
            len(brain_records) != 1
            or brain_records[0].semantic_id != self.source_brain_id
            or brain_records[0].actor_id != self.owner_actor_id
        ):
            raise PortabilityMappingError("shared Brain profile record is missing or ambiguous")
        self._validate_acyclic_provenance()
        self._reconstruct_and_validate()

    def _validate_acyclic_provenance(self) -> None:
        parents = {record.semantic_id: record.provenance_ids for record in self.records}
        active: set[str] = set()
        complete: set[str] = set()

        def visit(semantic_id: str) -> None:
            if semantic_id in complete:
                return
            if semantic_id in active:
                raise PortabilityMappingError("shared record provenance contains a cycle")
            active.add(semantic_id)
            for source_id in parents[semantic_id]:
                visit(source_id)
            active.remove(semantic_id)
            complete.add(semantic_id)

        for semantic_id in sorted(parents):
            visit(semantic_id)

    def _reconstruct_and_validate(self) -> dict[str, bytes]:
        direct: dict[str, bytes] = {}
        rows: dict[str, list[SharedRecord]] = {}
        for record in self.records:
            if record.source_ordinal is None:
                if record.source_path in direct or record.source_path in rows:
                    raise PortabilityMappingError("shared source path is ambiguous")
                direct[record.source_path] = record.source_bytes
            else:
                if record.source_path in direct:
                    raise PortabilityMappingError("shared JSONL path is ambiguous")
                rows.setdefault(record.source_path, []).append(record)
        for path, members in rows.items():
            ordered = sorted(members, key=lambda member: cast(int, member.source_ordinal))
            ordinals = [member.source_ordinal for member in ordered]
            if ordinals != list(range(len(ordered))):
                raise PortabilityMappingError("shared JSONL ordinals are not contiguous")
            direct[path] = b"".join(member.source_bytes + b"\n" for member in ordered)
        for blob in self.blobs:
            if blob.path in direct:
                raise PortabilityMappingError("shared attachment path is ambiguous")
            direct[blob.path] = blob.data
        for attachment in self.attachments:
            if attachment.path in direct:
                raise PortabilityMappingError("shared attachment path is ambiguous")
            direct[attachment.path] = attachment.data

        declared = dict(self.evidence.declared_files)
        if tuple(sorted(direct)) != tuple(declared):
            raise PortabilityMappingError("manifest inventory and shared payloads differ")
        for path, payload in direct.items():
            if sha256(payload).hexdigest() != declared[path]:
                raise PortabilityMappingError("manifest digest and shared payload differ")
        try:
            validate_portable_file_set(direct, tenant_id=self.source_brain_id)
        except ValueError as error:
            raise PortabilityMappingError("reconstructed Portable file set is invalid") from error
        return dict(sorted(direct.items()))

    def reconstruct_file_set(self) -> Mapping[str, bytes]:
        """Return a freshly validated, exact source file set without its manifest."""
        return MappingProxyType(self._reconstruct_and_validate())


__all__ = [
    "FAMILY_ID_PREFIX",
    "FAMILY_SCHEMA_URI",
    "PortabilityMappingError",
    "SharedAttachment",
    "SharedBlob",
    "SharedBrain",
    "SharedFamily",
    "SharedImportEvidence",
    "SharedRecord",
    "validate_portable_identifier",
    "validate_portable_path",
]
