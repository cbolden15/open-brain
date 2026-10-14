"""Read-only owner-local identity lookup through retained managed-file custody."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from open_brain_engine.storage.filesystem import StorageError, read_confined
from open_brain_engine.storage.markdown import MarkdownFormatError

from .contracts import ManagedWorkspaceFailure
from .facet_contracts import FacetError
from .facet_queries import resolve_subject
from .facet_vault import FacetProjectionConflict, FacetVaultTasks, registered_content
from .markdown_import_fs import MAX_FILE_BYTES, ImportDirectoryUnavailable, ImportScanIncomplete
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


@dataclass(frozen=True, slots=True)
class ManagedRecordIdentity:
    workspace_id: str
    record_id: str
    revision_id: str
    subject_kind: str
    subject_id: str

    def to_wire(self) -> dict[str, object]:
        return {
            "status": "ok",
            "dto_version": 1,
            "workspace_id": self.workspace_id,
            "record_id": self.record_id,
            "revision_id": self.revision_id,
            "subject": {"kind": self.subject_kind, "id": self.subject_id},
        }


def _file_identity(fingerprint: str) -> tuple[object, ...]:
    value = json.loads(fingerprint)
    # Facet edits change size and timestamps; replacing the file changes custody.
    return tuple(value[key] for key in ("device", "inode", "mode", "link_count"))


def resolve_managed_record(
    engine: BrainEngine,
    *,
    workspace_id: str,
    relative_path: str,
    authority: EffectiveAuthority,
) -> ManagedRecordIdentity:
    """Resolve an observed canonical note without accepting edits or refreshing it.

    An explicit managed_workspace.observe after the latest materialization supplies
    the retained file identity. Missing custody is unavailable (not_found). Source
    captures currently have no proven managed-note mapping and remain unavailable.
    Editable frontmatter never selects the record. Only a registered facet subtree
    may differ from accepted managed content.
    """
    vault = FacetVaultTasks(engine)
    try:
        vault._admit(authority)
    except StorageError, OSError:
        raise FacetError("not_found") from None
    if not isinstance(relative_path, str) or not isinstance(workspace_id, str):
        raise FacetError("not_found")
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or any(ord(char) < 32 for char in relative_path)
        or path.is_absolute()
        or path.as_posix() != relative_path
        or any(part in {".", ".."} for part in path.parts)
        or path.suffix.casefold() != ".md"
    ):
        raise FacetError("not_found")
    connection: sqlite3.Connection | None = None
    try:
        workspace = engine.managed_workspace._workspace(workspace_id)
        connection = engine._store.connect()
        engine.managed_workspace._assert_workspace_row(
            engine.managed_workspace._workspace_row(connection, workspace_id), workspace
        )
        note = connection.execute(
            "SELECT * FROM managed_notes WHERE workspace_id=? AND relative_path=?",
            (workspace_id, relative_path),
        ).fetchone()
        if note is None or not note["note_id"].startswith("page_"):
            raise FacetError("not_found")
        engine.managed_workspace._require_active_note(connection, note)
        subject = resolve_subject(connection, engine.profile, note["note_id"], authority)
        eligible_note = vault._note(connection, workspace_id, subject)
        if eligible_note["note_id"] != note["note_id"]:
            raise FacetError("not_found")
        observed = connection.execute(
            "SELECT * FROM managed_note_observations WHERE workspace_id=? AND note_id=? "
            "AND generation=? AND relative_path=? AND accepted_revision_id=?",
            (
                workspace_id,
                note["note_id"],
                workspace.generation,
                relative_path,
                note["accepted_revision_id"],
            ),
        ).fetchone()
        if observed is None:
            raise FacetError("not_found")
        projection = vault._projection(connection, workspace_id, note["note_id"])
        if projection is not None and (
            projection["relative_path"] != relative_path
            or projection["subject_kind"] != subject.kind
            or projection["subject_id"] != subject.id
            or projection["content_revision_id"] != subject.revision_id
            or projection["managed_revision_id"] != note["accepted_revision_id"]
        ):
            raise FacetError("not_found")
        payload, fingerprint = vault._read_file(workspace, relative_path)
        if _file_identity(fingerprint) != _file_identity(observed["fingerprint_json"]):
            raise FacetError("not_found")
        content = registered_content(
            connection, workspace_id, note["note_id"], relative_path, payload
        )
        if content != vault._accepted_content(connection, note):
            raise FacetError("not_found")
        current = connection.execute(
            "SELECT canonical_path FROM search_documents WHERE record_type='canonical' "
            "AND result_id=?",
            (subject.record_id,),
        ).fetchone()
        if current is None or content != read_confined(
            root=engine.profile.root,
            relative=current["canonical_path"],
            expected_root_identity=engine.profile.root_identity,
            maximum_bytes=MAX_FILE_BYTES,
        ):
            raise FacetError("not_found")
        # Pin the root again and reject replacement or edits during this lookup.
        if engine.managed_workspace._workspace(workspace_id) != workspace:
            raise FacetError("not_found")
        if vault._read_file(workspace, relative_path) != (payload, fingerprint):
            raise FacetError("not_found")
        engine._assert_root()
        return ManagedRecordIdentity(
            workspace_id, subject.record_id, subject.revision_id, subject.kind, subject.id
        )
    except FacetError:
        raise FacetError("not_found") from None
    except (
        ManagedWorkspaceFailure,
        FacetProjectionConflict,
        MarkdownFormatError,
        ImportDirectoryUnavailable,
        ImportScanIncomplete,
        StorageError,
        OSError,
        sqlite3.Error,
        ValueError,
        TypeError,
        KeyError,
    ):
        raise FacetError("not_found") from None
    finally:
        if connection is not None:
            connection.close()
