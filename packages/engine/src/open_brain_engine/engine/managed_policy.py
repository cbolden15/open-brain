"""Durable owner policy for managed-workspace inference."""

from __future__ import annotations

import sqlite3
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from .contracts import (
    ManagedAccessMode,
    ManagedExclusion,
    ManagedPolicyReceipt,
    ManagedProvider,
    ManagedWorkspaceFailure,
)
from .managed_workspace import _request_sha256, _timestamp
from .normalization import _delivery_id, _new_id, _portable_id

if TYPE_CHECKING:
    from .local import BrainEngine
    from .managed_workspace import ManagedWorkspaceTasks


def _provider_access(
    provider: ManagedProvider | str, access_mode: ManagedAccessMode | str
) -> tuple[ManagedProvider, ManagedAccessMode]:
    try:
        selected_provider = ManagedProvider(provider)
        selected_access = ManagedAccessMode(access_mode)
    except (TypeError, ValueError):
        raise ManagedWorkspaceFailure("invalid_policy") from None
    expected = (
        ManagedAccessMode.SUBSCRIPTION
        if selected_provider is ManagedProvider.CLAUDE_SUBSCRIPTION
        else ManagedAccessMode.API_KEY
    )
    if selected_access is not expected:
        raise ManagedWorkspaceFailure("invalid_policy")
    return selected_provider, selected_access


def _folder_subject(value: str) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ManagedWorkspaceFailure("invalid_policy")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ManagedWorkspaceFailure("invalid_policy")
    return path.as_posix().rstrip("/")


def _advance_policy(
    connection: sqlite3.Connection,
    workspace_id: str,
    now: str,
    *,
    revoke_all_consents: bool = False,
) -> int:
    row = connection.execute(
        "SELECT policy_generation FROM managed_workspaces WHERE workspace_id = ?",
        (workspace_id,),
    ).fetchone()
    if row is None:
        raise ManagedWorkspaceFailure("unknown_workspace")
    generation = int(row[0]) + 1
    if revoke_all_consents:
        connection.execute(
            """UPDATE managed_consents SET active = 0, revoked_at = ?
            WHERE workspace_id = ? AND active = 1""",
            (now, workspace_id),
        )
    reserved = tuple(
        connection.execute(
            """SELECT provider, count(*) AS attempts, sum(input_bytes) AS bytes
            FROM managed_inference_requests
            WHERE workspace_id = ? AND status = 'reserved' GROUP BY provider""",
            (workspace_id,),
        )
    )
    dispatching = tuple(
        connection.execute(
            """SELECT provider, count(*) AS attempts, sum(input_bytes) AS bytes
            FROM managed_inference_requests
            WHERE workspace_id = ? AND status = 'dispatching' GROUP BY provider""",
            (workspace_id,),
        )
    )
    for provider, attempts, byte_count in reserved:
        connection.execute(
            """UPDATE managed_inference_budgets
            SET reserved_requests = reserved_requests - ?,
                reserved_bytes = reserved_bytes - ?, updated_at = ?
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (attempts, byte_count, now, workspace_id, provider),
        )
    for provider, attempts, byte_count in dispatching:
        connection.execute(
            """UPDATE managed_inference_budgets
            SET reserved_requests = reserved_requests - ?,
                reserved_bytes = reserved_bytes - ?,
                used_requests = used_requests + ?, used_bytes = used_bytes + ?, updated_at = ?
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (attempts, byte_count, attempts, byte_count, now, workspace_id, provider),
        )
    connection.execute(
        """UPDATE managed_inference_requests SET status = 'cancelled', completed_at = ?
        WHERE workspace_id = ? AND status = 'reserved'""",
        (now, workspace_id),
    )
    connection.execute(
        """UPDATE managed_inference_requests SET status = 'superseded', completed_at = ?
        WHERE workspace_id = ? AND status = 'dispatching'""",
        (now, workspace_id),
    )
    connection.execute(
        """UPDATE managed_suggestions SET status = 'invalidated'
        WHERE workspace_id = ? AND status = 'pending'""",
        (workspace_id,),
    )
    connection.execute(
        "UPDATE managed_workspaces SET policy_generation = ? WHERE workspace_id = ?",
        (generation, workspace_id),
    )
    return generation


class ManagedPolicyTasks:
    """Owner-only policy mutations with replay-safe audit records."""

    def __init__(self, engine: BrainEngine, workspace: ManagedWorkspaceTasks) -> None:
        self._engine = engine
        self._workspace_tasks = workspace

    def active_exclusions(
        self, workspace_id: str, *, limit: int = 64
    ) -> tuple[ManagedExclusion, ...]:
        _portable_id(workspace_id, "workspace")
        if type(limit) is not int or not 1 <= limit <= 64:
            raise ManagedWorkspaceFailure("invalid_policy")
        connection = self._engine._store.connect()
        try:
            self._workspace_tasks._workspace_row(connection, workspace_id)
            rows = tuple(
                connection.execute(
                    """SELECT e.kind, e.subject, n.relative_path
                    FROM managed_exclusions AS e
                    LEFT JOIN managed_notes AS n
                      ON e.kind = 'note' AND n.workspace_id = e.workspace_id
                      AND n.note_id = e.subject
                    WHERE e.workspace_id = ? AND e.active = 1
                    ORDER BY e.kind, e.subject LIMIT ?""",
                    (workspace_id, limit),
                )
            )
            return tuple(
                ManagedExclusion(
                    kind=cast(str, row["kind"]),
                    subject=cast(str, row["subject"]),
                    relative_path=(
                        cast(str, row["subject"])
                        if row["kind"] == "folder"
                        else cast(str, row["relative_path"])
                    ),
                )
                for row in rows
            )
        except ManagedWorkspaceFailure:
            raise
        except (sqlite3.DatabaseError, TypeError, ValueError):
            raise ManagedWorkspaceFailure("invalid_policy") from None
        finally:
            connection.close()

    def grant_consent(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        *,
        operation_id: str,
    ) -> ManagedPolicyReceipt:
        _portable_id(workspace_id, "workspace")
        _delivery_id(operation_id)
        selected_provider, selected_access = _provider_access(provider, access_mode)
        request_sha256 = _request_sha256(
            {
                "access_mode": selected_access.value,
                "kind": "grant_consent",
                "operation_id": operation_id,
                "provider": selected_provider.value,
                "workspace_id": workspace_id,
            }
        )
        existing = self._workspace_tasks._operation(operation_id)
        if existing is not None:
            self._workspace_tasks._require_matching_operation(
                existing, request_sha256, "grant_consent"
            )
            generation = self._policy_generation(workspace_id)
            return ManagedPolicyReceipt(
                "consent_granted", workspace_id, generation, duplicate=True
            )
        self._workspace_tasks._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                active = connection.execute(
                    """SELECT consent_id FROM managed_consents
                    WHERE workspace_id = ? AND provider = ? AND access_mode = ?
                      AND operation = 'semantic_graph' AND note_scope = '*' AND active = 1""",
                    (workspace_id, selected_provider.value, selected_access.value),
                ).fetchone()
                now = _timestamp(self._engine._clock())
                duplicate = active is not None
                if duplicate:
                    generation = int(
                        self._workspace_tasks._workspace_row(connection, workspace_id)[
                            "policy_generation"
                        ]
                    )
                else:
                    generation = _advance_policy(connection, workspace_id, now)
                    connection.execute(
                        """INSERT INTO managed_consents
                        (consent_id, workspace_id, provider, access_mode, operation, note_scope,
                         owner_actor_id, granted_generation, active, granted_at)
                        VALUES (?, ?, ?, ?, 'semantic_graph', '*', ?, ?, 1, ?)""",
                        (
                            _new_id("consent"),
                            workspace_id,
                            selected_provider.value,
                            selected_access.value,
                            self._engine.profile.owner_actor_id,
                            generation,
                            now,
                        ),
                    )
                self._workspace_tasks._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=None,
                    kind="grant_consent",
                    target_relative_path=None,
                    expected_revision_id=None,
                    expected_target_sha256=None,
                    body=None,
                    now=now,
                    status="completed",
                )
        return ManagedPolicyReceipt(
            "consent_granted", workspace_id, generation, duplicate=duplicate
        )

    def revoke_consent(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        *,
        operation_id: str,
    ) -> ManagedPolicyReceipt:
        _portable_id(workspace_id, "workspace")
        _delivery_id(operation_id)
        selected_provider, selected_access = _provider_access(provider, access_mode)
        request_sha256 = _request_sha256(
            {
                "access_mode": selected_access.value,
                "kind": "revoke_consent",
                "operation_id": operation_id,
                "provider": selected_provider.value,
                "workspace_id": workspace_id,
            }
        )
        existing = self._workspace_tasks._operation(operation_id)
        if existing is not None:
            self._workspace_tasks._require_matching_operation(
                existing, request_sha256, "revoke_consent"
            )
            return ManagedPolicyReceipt(
                "consent_revoked",
                workspace_id,
                self._policy_generation(workspace_id),
                duplicate=True,
            )
        self._workspace_tasks._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                now = _timestamp(self._engine._clock())
                rows = tuple(
                    connection.execute(
                        """SELECT consent_id FROM managed_consents
                        WHERE workspace_id = ? AND provider = ? AND access_mode = ?
                          AND operation = 'semantic_graph' AND active = 1""",
                        (workspace_id, selected_provider.value, selected_access.value),
                    )
                )
                duplicate = not rows
                if duplicate:
                    generation = int(
                        self._workspace_tasks._workspace_row(connection, workspace_id)[
                            "policy_generation"
                        ]
                    )
                else:
                    generation = _advance_policy(connection, workspace_id, now)
                    connection.execute(
                        """UPDATE managed_consents SET active = 0, revoked_at = ?
                        WHERE workspace_id = ? AND provider = ? AND access_mode = ?
                          AND operation = 'semantic_graph' AND active = 1""",
                        (now, workspace_id, selected_provider.value, selected_access.value),
                    )
                self._workspace_tasks._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=None,
                    kind="revoke_consent",
                    target_relative_path=None,
                    expected_revision_id=None,
                    expected_target_sha256=None,
                    body=None,
                    now=now,
                    status="completed",
                )
        return ManagedPolicyReceipt(
            "consent_revoked", workspace_id, generation, duplicate=duplicate
        )

    def set_exclusion(
        self,
        workspace_id: str,
        kind: str,
        subject: str,
        *,
        excluded: bool,
        operation_id: str,
    ) -> ManagedPolicyReceipt:
        _portable_id(workspace_id, "workspace")
        _delivery_id(operation_id)
        if kind == "note":
            normalized_subject = _portable_id(subject, "page")
        elif kind == "folder":
            normalized_subject = _folder_subject(subject)
        else:
            raise ManagedWorkspaceFailure("invalid_policy")
        if type(excluded) is not bool:
            raise ManagedWorkspaceFailure("invalid_policy")
        request_sha256 = _request_sha256(
            {
                "excluded": excluded,
                "kind": kind,
                "operation_id": operation_id,
                "subject": normalized_subject,
                "workspace_id": workspace_id,
            }
        )
        existing = self._workspace_tasks._operation(operation_id)
        if existing is not None:
            self._workspace_tasks._require_matching_operation(
                existing, request_sha256, "set_exclusion"
            )
            return ManagedPolicyReceipt(
                "exclusion_updated",
                workspace_id,
                self._policy_generation(workspace_id),
                duplicate=True,
            )
        self._workspace_tasks._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                if kind == "note":
                    self._workspace_tasks._note_row(connection, workspace_id, normalized_subject)
                current = connection.execute(
                    """SELECT active FROM managed_exclusions
                    WHERE workspace_id = ? AND kind = ? AND subject = ?""",
                    (workspace_id, kind, normalized_subject),
                ).fetchone()
                duplicate = current is not None and bool(current["active"]) is excluded
                now = _timestamp(self._engine._clock())
                if duplicate:
                    generation = int(
                        self._workspace_tasks._workspace_row(connection, workspace_id)[
                            "policy_generation"
                        ]
                    )
                else:
                    generation = _advance_policy(connection, workspace_id, now)
                    connection.execute(
                        """INSERT INTO managed_exclusions
                        (exclusion_id, workspace_id, kind, subject, active,
                         policy_generation, recorded_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(workspace_id, kind, subject) DO UPDATE SET
                          active = excluded.active,
                          policy_generation = excluded.policy_generation,
                          recorded_at = excluded.recorded_at""",
                        (
                            _new_id("exclusion"),
                            workspace_id,
                            kind,
                            normalized_subject,
                            int(excluded),
                            generation,
                            now,
                        ),
                    )
                self._workspace_tasks._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=normalized_subject if kind == "note" else None,
                    kind="set_exclusion",
                    target_relative_path=None,
                    expected_revision_id=None,
                    expected_target_sha256=None,
                    body=None,
                    now=now,
                    status="completed",
                )
        return ManagedPolicyReceipt(
            "exclusion_updated", workspace_id, generation, duplicate=duplicate
        )

    def _policy_generation(self, workspace_id: str) -> int:
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT policy_generation FROM managed_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_workspace")
        return int(cast(sqlite3.Row, row)["policy_generation"])


__all__ = ["ManagedPolicyTasks"]
