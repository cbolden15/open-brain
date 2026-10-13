"""Pure, owner-safe pieces of managed facet projection.

Filesystem custody and semantic assignment remain with the engine integration.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from functools import wraps
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.filesystem import StorageError, atomic_replace, read_confined
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown

from . import facet_contracts as fc
from .contracts import ManagedWorkspaceFailure
from .managed_selection import managed_note_is_excluded
from .markdown_import_fs import (
    MAX_FILE_BYTES,
    ImportDirectoryUnavailable,
    ImportScanIncomplete,
    ScanLimits,
    enumerate_markdown,
    pin_import_root,
    read_markdown_candidate,
)
from .normalization import _timestamp
from .paging import authority_binding
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine
    from .managed_workspace import _Workspace


class FacetProjectionConflict(ValueError):
    """A generated projection would claim or overwrite owner content."""


@dataclass(frozen=True, slots=True)
class ProjectedValue:
    dimension_id: str
    dimension_name: str
    value_id: str
    value_name: str
    effectively_archived: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationConflict:
    code: str
    value_id: str | None = None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    add_value_ids: tuple[str, ...]
    remove_value_ids: tuple[str, ...]
    conflicts: tuple[ReconciliationConflict, ...]


@dataclass(frozen=True, slots=True)
class IndexLink:
    label: str
    relative_path: str


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def rendered_facet_map(values: Sequence[ProjectedValue]) -> dict[str, list[str]]:
    """Return the deterministic, quoted YAML-flow-compatible facet subtree."""
    result: dict[str, list[str]] = {}
    dimensions: dict[str, tuple[str, list[ProjectedValue]]] = {}
    seen: set[str] = set()
    for value in values:
        if value.value_id in seen:
            raise ValueError("duplicate facet value")
        seen.add(value.value_id)
        existing = dimensions.setdefault(value.dimension_id, (value.dimension_name, []))
        if existing[0] != value.dimension_name:
            raise ValueError("inconsistent dimension name")
        existing[1].append(value)
    for _dimension_id, (name, members) in sorted(
        dimensions.items(), key=lambda item: (_key(item[1][0]), item[0])
    ):
        if name in result:
            raise ValueError("duplicate rendered dimension")
        ordered = sorted(members, key=lambda member: (_key(member.value_name), member.value_id))
        labels = [member.value_name for member in ordered]
        if len(labels) != len(set(labels)):
            raise ValueError("duplicate rendered value")
        result[name] = labels
    return result


def replace_registered_facets(
    payload: bytes, values: Sequence[ProjectedValue], *, registered: bool
) -> bytes:
    """Change only the facets line in deterministic managed Markdown.

    An unregistered owner property is never adopted as generated metadata.
    The caller must separately guard file identity, digest, and projection custody.
    """
    parsed = parse_markdown(payload)
    if "facets" in parsed.fields and not registered:
        raise FacetProjectionConflict("unregistered owner facets")
    text = payload.decode("utf-8")
    frontmatter, separator, body = text.partition("\n---\n\n")
    if not separator:
        raise MarkdownFormatError("invalid Markdown frontmatter")
    lines = frontmatter.split("\n")
    encoded = json.dumps(rendered_facet_map(values), ensure_ascii=False, separators=(",", ":"))
    line = f"facets: {encoded}"
    existing = [index for index, item in enumerate(lines) if item.startswith("facets: ")]
    if existing:
        lines[existing[0]] = line
    else:
        lines.append(line)
    result = ("\n".join(lines) + separator + body).encode("utf-8")
    parse_markdown(result)
    return result


def content_without_registered_facets(payload: bytes, *, registered: bool) -> bytes:
    """Recover content bytes for revision/evidence comparison only for a known projection."""
    parsed = parse_markdown(payload)
    if not registered or "facets" not in parsed.fields:
        return payload
    text = payload.decode("utf-8")
    frontmatter, separator, body = text.partition("\n---\n\n")
    lines = [line for line in frontmatter.split("\n") if not line.startswith("facets: ")]
    result = ("\n".join(lines) + separator + body).encode("utf-8")
    parse_markdown(result)
    return result


def reconcile_facets(
    proposed: Mapping[str, object] | None,
    *,
    previous: Sequence[ProjectedValue],
    vocabulary: Sequence[ProjectedValue],
    current_memberships: frozenset[str],
) -> Reconciliation:
    """Resolve labels against retained identity and current vocabulary.

    Missing or empty facets propose removal of previously projected memberships.
    Neither this function nor an observer commits classification.
    """
    aliases: dict[tuple[str, str], set[str]] = {}
    current_by_id = {value.value_id: value for value in vocabulary}
    if len(current_by_id) != len(vocabulary):
        raise ValueError("duplicate vocabulary ID")
    for value in (*previous, *vocabulary):
        aliases.setdefault((_key(value.dimension_name), _key(value.value_name)), set()).add(
            value.value_id
        )
    desired: set[str] = set()
    conflicts: list[ReconciliationConflict] = []
    if proposed is not None:
        if not isinstance(proposed, Mapping):
            return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
        for dimension_name, raw_values in proposed.items():
            if not isinstance(dimension_name, str) or not isinstance(raw_values, list):
                return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
            for value_name in raw_values:
                if not isinstance(value_name, str):
                    return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
                candidates = aliases.get((_key(dimension_name), _key(value_name)), set())
                if not candidates:
                    conflicts.append(ReconciliationConflict("unknown_name"))
                elif len(candidates) != 1:
                    conflicts.append(ReconciliationConflict("ambiguous_name"))
                else:
                    value_id = next(iter(candidates))
                    if value_id in desired:
                        conflicts.append(ReconciliationConflict("invalid_frontmatter"))
                    else:
                        desired.add(value_id)
    additions = desired - current_memberships
    for value_id in sorted(additions):
        current_value = current_by_id.get(value_id)
        if current_value is None:
            conflicts.append(ReconciliationConflict("unknown_name"))
        elif current_value.effectively_archived:
            conflicts.append(ReconciliationConflict("archived_addition", value_id))
    prior_ids = {value.value_id for value in previous}
    return Reconciliation(
        tuple(sorted(additions & current_by_id.keys())),
        tuple(sorted((prior_ids & current_memberships) - desired)),
        tuple(conflicts),
    )


def _markdown_text(value: str) -> str:
    return "".join("\\" + char if char in "\\`*_{}[]<>()#+-.!|" else char for char in value)


def facet_index_relative_path(dimension_id: str, value_id: str) -> str:
    """Use immutable IDs, never display names, for an index path."""
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if not re.fullmatch("facetdim_" + uuid, dimension_id) or not re.fullmatch(
        "facetval_" + uuid, value_id
    ):
        raise ValueError("invalid facet index identity")
    return f"Facets/{dimension_id}/{value_id}.md"


def render_facet_index(
    *, title: str, effectively_archived: bool, links: Sequence[IndexLink]
) -> bytes:
    """Render deterministic navigation, with no source-note or evidence semantics."""
    if any(ord(char) < 32 for char in title):
        raise ValueError("unsafe index title")
    by_path: dict[str, IndexLink] = {}
    for link in links:
        path = PurePosixPath(link.relative_path)
        if (
            path.is_absolute()
            or not path.parts
            or str(path) != link.relative_path
            or any(part in {".", ".."} for part in path.parts)
            or any(ord(char) < 32 for char in link.relative_path + link.label)
        ):
            raise ValueError("unsafe index link")
        by_path.setdefault(link.relative_path, link)
    heading = _markdown_text(title)
    if effectively_archived:
        heading += " (archived)"
    lines = [f"# {heading}", ""]
    for link in sorted(by_path.values(), key=lambda item: (_key(item.label), item.relative_path)):
        target = "../../" + quote(link.relative_path, safe="/-._~")
        lines.append(f"- [{_markdown_text(link.label)}]({target})")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def index_write_status(
    *, previous_digest: str | None, existing: bytes | None, proposed: bytes
) -> str:
    """Return write, current, or conflict without discarding an owner edit."""
    if existing is not None and (
        previous_digest is None or sha256(existing).hexdigest() != previous_digest
    ):
        return "conflict"
    return "current" if existing == proposed else "write"


def projection_schema_available(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='facet_projection_state'"
        ).fetchone()
        is not None
    )


def registered_content(
    connection: sqlite3.Connection,
    workspace_id: str,
    note_id: str,
    relative_path: str,
    payload: bytes,
) -> bytes:
    """Content workflow seam: only registered record metadata can be removed."""
    if not projection_schema_available(connection):
        return payload
    row = connection.execute(
        "SELECT * FROM facet_projection_state WHERE workspace_id=? AND relative_path=?",
        (workspace_id, relative_path),
    ).fetchone()
    if row is None:
        return payload
    if row["file_kind"] != "record" or row["file_id"] != note_id:
        raise FacetProjectionConflict("generated index is not managed content")
    return content_without_registered_facets(payload, registered=True)


def _json(value: object) -> str:
    return portable_canonical_json_bytes(value).decode("utf-8")


def _public_errors[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return method(*args, **kwargs)
        except ManagedWorkspaceFailure as error:
            code = "not_found" if error.code == "unknown_workspace" else "projection_conflict"
            raise fc.FacetError(code) from None
        except sqlite3.Error, StorageError, LockBusyError, OSError:
            raise fc.FacetError("operation_pending") from None
        except ImportDirectoryUnavailable, ImportScanIncomplete:
            raise fc.FacetError("preview_stale") from None

    return guarded


class FacetVaultTasks:
    """Owner-local preview custody and journaled facet projection.

    Registration is explicit internal work on an existing managed record. Public
    preview/apply methods never create a managed note or a vocabulary entity.
    """

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def _admit(self, authority: EffectiveAuthority) -> None:
        if (
            not isinstance(authority, EffectiveAuthority)
            or not authority.owner
            or authority.principal_id != self._engine.profile.owner_actor_id
            or authority.egress_mode.value != "owner_local"
            or authority.space_ids is not None
        ):
            raise fc.FacetError("unsupported_capability")
        self._engine._assert_root()

    def _authority_digest(self, authority: EffectiveAuthority, workspace: _Workspace) -> str:
        binding = authority_binding(authority)
        binding.pop("session_id")  # A later owner CLI session can apply within custody lifetime.
        binding["brain_root"] = str(self._engine.profile.root)
        binding["brain_root_identity"] = list(self._engine.profile.root_identity)
        binding["workspace_root"] = str(workspace.root)
        binding["workspace_root_identity"] = list(workspace.root_identity)
        return sha256(portable_canonical_json_bytes(binding)).hexdigest()

    @staticmethod
    def _vocabulary(connection: sqlite3.Connection) -> tuple[ProjectedValue, ...]:
        return tuple(
            ProjectedValue(
                row["dimension_id"],
                row["dimension_name"],
                row["value_id"],
                row["value_name"],
                bool(row["dimension_archived"] or row["value_archived"]),
            )
            for row in connection.execute(
                "SELECT d.dimension_id,d.name AS dimension_name,d.archived AS dimension_archived,"
                "v.value_id,v.name AS value_name,v.archived AS value_archived "
                "FROM facet_values v JOIN facet_dimensions d USING(dimension_id) "
                "ORDER BY v.value_id"
            )
        )

    @staticmethod
    def _catalogue_versions(connection: sqlite3.Connection) -> str:
        # Conservatively bind the owner catalogue, including names newly reusing old labels.
        return _json(
            [
                list(row)
                for row in connection.execute(
                    "SELECT dimension_id,version FROM facet_dimensions UNION ALL "
                    "SELECT value_id,version FROM facet_values ORDER BY 1"
                )
            ]
        )

    @staticmethod
    def _projection(
        connection: sqlite3.Connection, workspace_id: str, file_id: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM facet_projection_state "
                "WHERE workspace_id=? AND file_kind='record' AND file_id=?",
                (workspace_id, file_id),
            ).fetchone(),
        )

    def _note(self, connection: sqlite3.Connection, workspace_id: str, subject: Any) -> sqlite3.Row:
        note = connection.execute(
            "SELECT n.* FROM managed_notes n WHERE n.workspace_id=? AND n.active=1 "
            "AND (n.note_id=? OR n.note_id IN (SELECT file_id FROM facet_projection_state "
            "WHERE workspace_id=? AND file_kind='record' AND subject_kind=? AND subject_id=?))",
            (workspace_id, subject.id, workspace_id, subject.kind, subject.id),
        ).fetchall()
        if len(note) != 1 or managed_note_is_excluded(
            connection,
            workspace_id,
            note_id=note[0]["note_id"],
            relative_path=note[0]["relative_path"],
        ):
            raise fc.FacetError("not_found")
        return cast(sqlite3.Row, note[0])

    @staticmethod
    def _read_file(workspace: _Workspace, relative_path: str) -> tuple[bytes, str]:
        from .managed_workspace import _fingerprint

        with pin_import_root(str(workspace.root)) as pinned:
            if pinned.snapshot.identity != workspace.root_identity:
                raise fc.FacetError("preview_stale")
            inventory = enumerate_markdown(
                pinned.descriptor,
                limits=ScanLimits(),
                allow_large_vault=False,
                interrupted=lambda: False,
            )
            if any(outcome.failed for outcome in inventory.outcomes):
                raise fc.FacetError("operation_pending")
            candidates = [
                item for item in inventory.candidates if item.relative_path == relative_path
            ]
            if len(candidates) != 1:
                raise fc.FacetError("preview_stale")
            candidate = candidates[0]
            payload = read_markdown_candidate(
                pinned.descriptor,
                candidate,
                maximum_bytes=MAX_FILE_BYTES,
            )
            return payload, _fingerprint(candidate)

    @staticmethod
    def _accepted_content(connection: sqlite3.Connection, note: sqlite3.Row) -> bytes:
        row = connection.execute(
            "SELECT body_bytes FROM managed_note_revisions WHERE note_id=? AND revision_id=?",
            (note["note_id"], note["accepted_revision_id"]),
        ).fetchone()
        if row is None:
            raise fc.FacetError("projection_conflict")
        return cast(bytes, row[0])

    @_public_errors
    def vault_preview(
        self, request: fc.VaultPreviewRequest, *, authority: EffectiveAuthority
    ) -> fc.VaultPreviewResponse:
        from .facet_queries import membership_rows, resolve_subject, subject_version

        args = fc.request_from_wire("facet.vault.preview", request).to_wire()
        self._admit(authority)
        workspace = self._engine.managed_workspace._workspace(args["workspace_id"])
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            subject = resolve_subject(
                connection, self._engine.profile, args["record_id"], authority
            )
            note = self._note(connection, workspace.workspace_id, subject)
            projection = self._projection(connection, workspace.workspace_id, note["note_id"])
            if projection is None:
                raise fc.FacetError("projection_conflict")
            payload, fingerprint = self._read_file(workspace, note["relative_path"])
            conflicts: list[dict[str, object]] = []
            previous = tuple(
                ProjectedValue(**item) for item in json.loads(projection["id_name_mapping_json"])
            )
            memberships = frozenset(
                item["value_id"] for item in membership_rows(connection, subject.kind, subject.id)
            )
            try:
                parsed = parse_markdown(payload)
                proposed = parsed.fields.get("facets")
                if "facets" in parsed.fields and not isinstance(proposed, dict):
                    raise MarkdownFormatError("invalid facets")
                patch = reconcile_facets(
                    cast(Mapping[str, object] | None, proposed),
                    previous=previous,
                    vocabulary=self._vocabulary(connection),
                    current_memberships=memberships,
                )
                conflicts.extend(asdict(item) for item in patch.conflicts)
                if content_without_registered_facets(
                    payload, registered=True
                ) != self._accepted_content(connection, note):
                    conflicts.append({"code": "content_changed", "value_id": None})
            except MarkdownFormatError:
                patch = Reconciliation((), (), ())
                conflicts.append({"code": "invalid_frontmatter", "value_id": None})
            if (
                projection["relative_path"] != note["relative_path"]
                or projection["content_revision_id"] != subject.revision_id
                or projection["managed_revision_id"] != note["accepted_revision_id"]
                or {item.value_id for item in previous} != memberships
            ):
                conflicts.append({"code": "projection_changed", "value_id": None})
            if len((memberships | set(patch.add_value_ids)) - set(patch.remove_value_ids)) > 128:
                raise fc.FacetError("limit_exceeded")
            now = self._engine._clock()
            expires = _timestamp(now + timedelta(minutes=15))
            preview_id = "facetpreview_" + str(uuid4())
            version = subject_version(connection, subject)
            fields = {
                "preview_id": preview_id,
                "workspace_id": workspace.workspace_id,
                "file_id": note["note_id"],
                "relative_path": note["relative_path"],
                "root_device": str(workspace.root_identity[0]),
                "root_inode": str(workspace.root_identity[1]),
                "subject_kind": subject.kind,
                "subject_id": subject.id,
                "content_revision_id": subject.revision_id,
                "managed_revision_id": note["accepted_revision_id"],
                "file_sha256": sha256(payload).hexdigest(),
                "file_fingerprint_json": fingerprint,
                "previous_facets_json": projection["emitted_facets_json"],
                "previous_mapping_json": projection["id_name_mapping_json"],
                "expected_version": version,
                "add_value_ids_json": _json(list(patch.add_value_ids)),
                "remove_value_ids_json": _json(list(patch.remove_value_ids)),
                "catalogue_versions_json": self._catalogue_versions(connection),
                "authority_sha256": self._authority_digest(authority, workspace),
                "conflicts_json": _json(conflicts),
                "created_at": _timestamp(now),
                "expires_at": expires,
            }
            connection.execute(
                f"INSERT INTO facet_vault_previews ({','.join(fields)}) "
                f"VALUES ({','.join('?' for _ in fields)})",
                tuple(fields.values()),
            )
        return fc.VaultPreviewResponse(
            _json(
                {
                    "status": "ok",
                    "dto_version": 1,
                    "preview_id": preview_id,
                    "subject": subject.to_wire(),
                    "expected_version": version,
                    "add_value_ids": list(patch.add_value_ids),
                    "remove_value_ids": list(patch.remove_value_ids),
                    "conflicts": conflicts,
                    "expires_at": expires,
                }
            )
        )

    def _prepare_record(
        self,
        connection: sqlite3.Connection,
        workspace: _Workspace,
        note: sqlite3.Row,
        subject: Any,
        payload: bytes,
        operation_id: str,
    ) -> None:
        from .facet_queries import membership_rows

        projection = self._projection(connection, workspace.workspace_id, note["note_id"])
        assigned = {
            item["value_id"] for item in membership_rows(connection, subject.kind, subject.id)
        }
        values = tuple(item for item in self._vocabulary(connection) if item.value_id in assigned)
        output = replace_registered_facets(payload, values, registered=projection is not None)
        state = {
            "workspace_id": workspace.workspace_id,
            "file_kind": "record",
            "file_id": note["note_id"],
            "relative_path": note["relative_path"],
            "subject_kind": subject.kind,
            "subject_id": subject.id,
            "content_revision_id": subject.revision_id,
            "managed_revision_id": note["accepted_revision_id"],
            "emitted_facets_json": _json(rendered_facet_map(values)),
            "id_name_mapping_json": _json([asdict(item) for item in values]),
            "materialized_sha256": sha256(output).hexdigest(),
            "updated_at": _timestamp(self._engine._clock()),
        }
        latest, fingerprint = self._read_file(workspace, note["relative_path"])
        if latest != payload:
            raise fc.FacetError("preview_stale")
        self._prepare_write(
            connection, workspace, operation_id, payload, output, state, fingerprint
        )

    @staticmethod
    def _prepare_write(
        connection: sqlite3.Connection,
        workspace: _Workspace,
        operation_id: str,
        previous: bytes | None,
        output: bytes,
        state: dict[str, Any],
        fingerprint: str | None = None,
    ) -> None:
        policy_generation = connection.execute(
            "SELECT policy_generation FROM managed_workspaces WHERE workspace_id=?",
            (workspace.workspace_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO facet_projection_writes "
            "(operation_id,workspace_id,file_kind,file_id,relative_path,root_device,root_inode,"
            "expected_sha256,expected_fingerprint_json,policy_generation,payload,"
            "payload_sha256,projection_json,status,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'prepared',?)",
            (
                operation_id,
                workspace.workspace_id,
                state["file_kind"],
                state["file_id"],
                state["relative_path"],
                str(workspace.root_identity[0]),
                str(workspace.root_identity[1]),
                None if previous is None else sha256(previous).hexdigest(),
                fingerprint,
                policy_generation,
                output,
                sha256(output).hexdigest(),
                _json(state),
                state["updated_at"],
            ),
        )

    def register_record(
        self, workspace_id: str, record_id: str, *, authority: EffectiveAuthority
    ) -> None:
        from .facet_queries import resolve_subject

        self._admit(authority)
        workspace = self._engine.managed_workspace._workspace(workspace_id)
        operation_id = "facetprojection_" + str(uuid4())
        with self._engine._writer_lease.acquire_shared_writer():
            with self._engine._store.transaction() as connection:
                subject = resolve_subject(connection, self._engine.profile, record_id, authority)
                note = self._note(connection, workspace_id, subject)
                payload, _ = self._read_file(workspace, note["relative_path"])
                projection = self._projection(connection, workspace_id, note["note_id"])
                if (
                    projection is not None
                    and sha256(payload).hexdigest() != projection["materialized_sha256"]
                ):
                    raise fc.FacetError("projection_conflict")
                if registered_content(
                    connection, workspace_id, note["note_id"], note["relative_path"], payload
                ) != self._accepted_content(connection, note):
                    raise fc.FacetError("projection_conflict")
                try:
                    self._prepare_record(
                        connection, workspace, note, subject, payload, operation_id
                    )
                except FacetProjectionConflict:
                    raise fc.FacetError("projection_conflict") from None
            self._materialize(operation_id)

    @_public_errors
    def vault_apply(
        self, request: fc.VaultApplyRequest, *, authority: EffectiveAuthority
    ) -> fc.VaultApplyResponse:
        from .facet_queries import authorize_subject
        from .facet_store import commit_mutation, replay_receipt
        from .facets import FacetTasks

        args = fc.request_from_wire("facet.vault.apply", request).to_wire()
        self._admit(authority)
        request_digest = request.request_sha256()
        projection_operation = "facetapply_" + sha256(args["operation_id"].encode()).hexdigest()
        with self._engine._writer_lease.acquire_shared_writer():
            with self._engine._store.transaction() as connection:
                preview = connection.execute(
                    "SELECT * FROM facet_vault_previews WHERE preview_id=?", (args["preview_id"],)
                ).fetchone()
                replay = connection.execute(
                    "SELECT * FROM facet_vault_apply_operations WHERE operation_id=?",
                    (args["operation_id"],),
                ).fetchone()
                if replay is not None and (
                    replay["principal_id"] != authority.principal_id
                    or replay["request_sha256"] != request_digest
                ):
                    raise fc.FacetError("idempotency_conflict")
                if preview is None:
                    raise fc.FacetError("preview_stale")
                subject = authorize_subject(
                    connection,
                    self._engine.profile,
                    {"kind": preview["subject_kind"], "id": preview["subject_id"]},
                    authority,
                )
                if replay is not None:
                    receipt = json.loads(replay["receipt_json"])
                else:
                    workspace = self._engine.managed_workspace._workspace(preview["workspace_id"])
                    note = self._note(connection, workspace.workspace_id, subject)
                    projection = self._projection(
                        connection, workspace.workspace_id, note["note_id"]
                    )
                    payload, fingerprint = self._read_file(workspace, note["relative_path"])
                    if (
                        datetime.fromisoformat(preview["expires_at"]) <= self._engine._clock()
                        or json.loads(preview["conflicts_json"])
                        or projection is None
                        or note["note_id"] != preview["file_id"]
                        or note["relative_path"] != preview["relative_path"]
                        or note["accepted_revision_id"] != preview["managed_revision_id"]
                        or subject.revision_id != preview["content_revision_id"]
                        or fingerprint != preview["file_fingerprint_json"]
                        or sha256(payload).hexdigest() != preview["file_sha256"]
                        or self._authority_digest(authority, workspace)
                        != preview["authority_sha256"]
                        or self._catalogue_versions(connection)
                        != preview["catalogue_versions_json"]
                        or projection["emitted_facets_json"] != preview["previous_facets_json"]
                        or projection["id_name_mapping_json"] != preview["previous_mapping_json"]
                    ):
                        raise fc.FacetError("preview_stale")
                    semantic_request = fc.AssignmentsPatchRequest(
                        operation_id=args["operation_id"],
                        record_id=subject.record_id,
                        expected_revision_id=preview["content_revision_id"],
                        expected_version=preview["expected_version"],
                        add_value_ids=json.loads(preview["add_value_ids_json"]),
                        remove_value_ids=json.loads(preview["remove_value_ids_json"]),
                    )
                    # Use E's existing assignment logic in this same transaction. Replace
                    # this adapter with E's public caller-transaction seam when verified.
                    tasks = FacetTasks(self._engine)
                    method = "facet.assignment.patch"
                    semantic_args = tasks._admit(semantic_request, method, authority)
                    before, admitted_subject = tasks._scope_write(
                        connection, method, semantic_args, authority
                    )
                    receipt = replay_receipt(
                        connection,
                        method=method,
                        arguments=semantic_args,
                        principal_id=authority.principal_id,
                    )
                    if receipt is None:
                        tasks._expect(semantic_args["expected_version"], before)
                        recorded = _timestamp(self._engine._clock())
                        changes, subject_id, version = tasks._changes(
                            connection,
                            method,
                            semantic_args,
                            before,
                            admitted_subject,
                            authority,
                            recorded,
                        )
                        brain_id = connection.execute(
                            "SELECT brain_id FROM brain_identity WHERE singleton=1"
                        ).fetchone()[0]
                        receipt = commit_mutation(
                            connection,
                            brain_id=brain_id,
                            method=method,
                            arguments=semantic_args,
                            authority=authority,
                            recorded_at=recorded,
                            changes=changes,
                            subject_id=subject_id,
                            version=version,
                        )
                    connection.execute(
                        "INSERT INTO facet_vault_apply_operations VALUES(?,?,?,?,?,?,?)",
                        (
                            args["operation_id"],
                            args["preview_id"],
                            authority.principal_id,
                            request_digest,
                            args["operation_id"],
                            _json(receipt),
                            _timestamp(self._engine._clock()),
                        ),
                    )
                    self._prepare_record(
                        connection, workspace, note, subject, payload, projection_operation
                    )
            status = "current"
            try:
                self._materialize(projection_operation)
            except ValueError, StorageError, sqlite3.Error, OSError, ManagedWorkspaceFailure:
                status = "refresh_required"
        return fc.VaultApplyResponse(
            _json(
                {"status": "ok", "dto_version": 1, "receipt": receipt, "projection_status": status}
            )
        )

    def refresh_indexes(
        self,
        workspace_id: str,
        *,
        authority: EffectiveAuthority,
        after_value_id: str | None = None,
        limit: int = 32,
    ) -> str | None:
        """Materialize a bounded owner-local batch; return the next stable ID boundary."""
        from .facet_queries import authorize_subject, resolve_query, select_records

        self._admit(authority)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise fc.FacetError("invalid_arguments")
        workspace = self._engine.managed_workspace._workspace(workspace_id)
        operations: list[str] = []
        with self._engine._writer_lease.acquire_shared_writer():
            with self._engine._store.transaction() as connection:
                vocabulary = sorted(self._vocabulary(connection), key=lambda item: item.value_id)
                remaining = [
                    item
                    for item in vocabulary
                    if after_value_id is None or item.value_id > after_value_id
                ]
                batch = remaining[:limit]
                for value in batch:
                    subjects = {
                        (item.subject.kind, item.subject.id): item.subject
                        for item in select_records(
                            connection,
                            self._engine.profile,
                            {
                                "all_value_ids": [value.value_id],
                                "space_ids": [],
                                "payload_families": [],
                                "record_types": ["source", "canonical"],
                            },
                            authority,
                        )
                    }
                    for attached in connection.execute(
                        "SELECT page_id,query_id FROM facet_page_queries ORDER BY page_id"
                    ):
                        try:
                            query = resolve_query(connection, attached["query_id"], authority)
                            if value.value_id not in query["selector"]["all_value_ids"]:
                                continue
                            subject = authorize_subject(
                                connection,
                                self._engine.profile,
                                {"kind": "canonical", "id": attached["page_id"]},
                                authority,
                            )
                            subjects[(subject.kind, subject.id)] = subject
                        except fc.FacetError as error:
                            if error.code != "not_found":
                                raise
                    links: list[IndexLink] = []
                    for subject in subjects.values():
                        try:
                            note = self._note(connection, workspace_id, subject)
                        except fc.FacetError as error:
                            if error.code == "not_found":
                                continue
                            raise
                        if (
                            read_confined(
                                root=workspace.root,
                                relative=note["relative_path"],
                                expected_root_identity=workspace.root_identity,
                                maximum_bytes=MAX_FILE_BYTES,
                            )
                            is not None
                        ):
                            links.append(IndexLink(subject.summary["title"], note["relative_path"]))
                    relative = facet_index_relative_path(value.dimension_id, value.value_id)
                    existing = read_confined(
                        root=workspace.root,
                        relative=relative,
                        expected_root_identity=workspace.root_identity,
                        maximum_bytes=MAX_FILE_BYTES,
                    )
                    previous = connection.execute(
                        "SELECT materialized_sha256 FROM facet_projection_state "
                        "WHERE workspace_id=? AND file_kind='index' AND file_id=?",
                        (workspace_id, value.value_id),
                    ).fetchone()
                    output = render_facet_index(
                        title=f"{value.dimension_name}: {value.value_name}",
                        effectively_archived=value.effectively_archived,
                        links=links,
                    )
                    status = index_write_status(
                        previous_digest=None if previous is None else previous[0],
                        existing=existing,
                        proposed=output,
                    )
                    if status == "conflict":
                        raise fc.FacetError("projection_conflict")
                    if status == "current":
                        continue
                    operation_id = "facetindex_" + str(uuid4())
                    state = {
                        "workspace_id": workspace_id,
                        "file_kind": "index",
                        "file_id": value.value_id,
                        "relative_path": relative,
                        "subject_kind": None,
                        "subject_id": None,
                        "content_revision_id": None,
                        "managed_revision_id": None,
                        "emitted_facets_json": None,
                        "id_name_mapping_json": None,
                        "materialized_sha256": sha256(output).hexdigest(),
                        "updated_at": _timestamp(self._engine._clock()),
                    }
                    self._prepare_write(
                        connection, workspace, operation_id, existing, output, state
                    )
                    operations.append(operation_id)
            for operation_id in operations:
                self._materialize(operation_id)
        return batch[-1].value_id if len(remaining) > limit else None

    def _materialize(self, operation_id: str) -> None:
        with self._engine._store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM facet_projection_writes WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if row is None or row["status"] == "conflict":
                raise fc.FacetError("projection_conflict")
            workspace = self._engine.managed_workspace._workspace(row["workspace_id"])
            if (str(workspace.root_identity[0]), str(workspace.root_identity[1])) != (
                row["root_device"],
                row["root_inode"],
            ):
                raise fc.FacetError("projection_conflict")
            current = read_confined(
                root=workspace.root,
                relative=row["relative_path"],
                expected_root_identity=workspace.root_identity,
                maximum_bytes=MAX_FILE_BYTES,
            )
            digest = None if current is None else sha256(current).hexdigest()
            if row["status"] == "completed":
                latest = connection.execute(
                    "SELECT materialized_sha256 FROM facet_projection_state "
                    "WHERE workspace_id=? AND file_kind=? AND file_id=?",
                    (row["workspace_id"], row["file_kind"], row["file_id"]),
                ).fetchone()
                if latest is None or digest != latest[0]:
                    raise fc.FacetError("projection_conflict")
                return
            state = json.loads(row["projection_json"])
            if (
                sha256(row["payload"]).hexdigest() != row["payload_sha256"]
                or state["materialized_sha256"] != row["payload_sha256"]
            ):
                raise fc.FacetError("projection_conflict")
            policy = connection.execute(
                "SELECT policy_generation FROM managed_workspaces WHERE workspace_id=?",
                (workspace.workspace_id,),
            ).fetchone()[0]
            eligible = policy == row["policy_generation"]
            if row["file_kind"] == "record":
                note = connection.execute(
                    "SELECT * FROM managed_notes WHERE workspace_id=? AND note_id=?",
                    (workspace.workspace_id, row["file_id"]),
                ).fetchone()
                eligible = (
                    eligible
                    and note is not None
                    and bool(note["active"])
                    and (
                        note["relative_path"] == row["relative_path"]
                        and note["accepted_revision_id"] == state["managed_revision_id"]
                        and not managed_note_is_excluded(
                            connection,
                            workspace.workspace_id,
                            note_id=row["file_id"],
                            relative_path=row["relative_path"],
                        )
                    )
                )
                if eligible and digest != row["payload_sha256"]:
                    _, fingerprint = self._read_file(workspace, row["relative_path"])
                    eligible = fingerprint == row["expected_fingerprint_json"]
            if not eligible or digest not in {row["expected_sha256"], row["payload_sha256"]}:
                connection.execute(
                    "UPDATE facet_projection_writes SET status='conflict' WHERE operation_id=?",
                    (operation_id,),
                )
                conflict = True
            else:
                conflict = False
                if digest != row["payload_sha256"]:
                    atomic_replace(
                        root=workspace.root,
                        relative=row["relative_path"],
                        data=row["payload"],
                        require_existing=current is not None,
                        expected_existing_sha256=digest,
                        expected_root_identity=workspace.root_identity,
                    )
                columns = tuple(state)
                connection.execute(
                    f"INSERT INTO facet_projection_state ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)}) "
                    "ON CONFLICT(workspace_id,file_kind,file_id) DO UPDATE SET "
                    + ",".join(
                        f"{key}=excluded.{key}"
                        for key in columns
                        if key not in {"workspace_id", "file_kind", "file_id"}
                    ),
                    tuple(state.values()),
                )
                connection.execute(
                    "UPDATE facet_projection_writes SET status='completed',completed_at=? "
                    "WHERE operation_id=?",
                    (_timestamp(self._engine._clock()), operation_id),
                )
        if conflict:
            raise fc.FacetError("projection_conflict")

    def recover(self) -> int:
        with self._engine._writer_lease.acquire_shared_writer():
            return self.recover_locked()

    def recover_locked(self) -> int:
        connection = self._engine._store.connect()
        try:
            if not projection_schema_available(connection):
                return 0
            operations = [
                row[0]
                for row in connection.execute(
                    "SELECT operation_id FROM facet_projection_writes WHERE status='prepared' "
                    "ORDER BY created_at,operation_id"
                )
            ]
        finally:
            connection.close()
        for operation_id in operations:
            try:
                self._materialize(operation_id)
            except fc.FacetError as error:
                if error.code != "projection_conflict":
                    raise
        return len(operations)
