"""Revision-bound inference reservations and accepted-link provenance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    ValidationError,
)
from open_brain_engine.storage.filesystem import read_confined
from open_brain_engine.storage.markdown import (
    MarkdownFormatError,
    ParsedMarkdown,
    parse_markdown,
    render_markdown,
)

from .contracts import (
    ManagedAccessMode,
    ManagedInferenceReceipt,
    ManagedInferenceRequest,
    ManagedInferenceSource,
    ManagedProvider,
    ManagedSuggestion,
    ManagedWorkspaceFailure,
)
from .managed_policy import _advance_policy, _provider_access
from .managed_workspace import _digest, _request_sha256, _timestamp
from .markdown_import_fs import MAX_FILE_BYTES
from .normalization import _delivery_id, _new_id, _portable_id

if TYPE_CHECKING:
    from .local import BrainEngine
    from .managed_workspace import ManagedWorkspaceTasks, _Workspace

MAX_INFERENCE_INPUT_BYTES = 16 * 1024
MAX_INFERENCE_OUTPUT_BYTES = 16 * 1024
MAX_INFERENCE_SECONDS = 60
PROVIDER_REQUEST_LIMIT = 16
PROVIDER_BYTE_LIMIT = PROVIDER_REQUEST_LIMIT * MAX_INFERENCE_INPUT_BYTES


class ManagedInferenceTasks:
    """Reserve exact accepted sources before an adapter can receive note content."""

    def __init__(self, engine: BrainEngine, workspace: ManagedWorkspaceTasks) -> None:
        self._engine = engine
        self._workspace_tasks = workspace

    def suggestions(self, workspace_id: str) -> tuple[ManagedSuggestion, ...]:
        _portable_id(workspace_id, "workspace")
        connection = self._engine._store.connect()
        try:
            if connection.execute(
                "SELECT 1 FROM managed_workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone() is None:
                raise ManagedWorkspaceFailure("unknown_workspace")
            rows = tuple(
                connection.execute(
                    """SELECT * FROM managed_suggestions
                    WHERE workspace_id = ? AND status = 'pending'
                    ORDER BY issued_at, suggestion_id""",
                    (workspace_id,),
                )
            )
        finally:
            connection.close()
        return tuple(
            ManagedSuggestion(
                suggestion_id=cast(str, row["suggestion_id"]),
                workspace_id=workspace_id,
                source_note_id=cast(str, row["source_note_id"]),
                target_note_id=cast(str, row["target_note_id"]),
                source_quote=cast(str, row["source_quote"]),
                target_quote=cast(str, row["target_quote"]),
                provider=ManagedProvider(cast(str, row["provider"])),
                model=cast(str, row["model"]),
            )
            for row in rows
        )

    def prepare(
        self,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
        adapter_identity: str,
        note_ids: tuple[str, ...],
        *,
        request_id: str,
        max_output_bytes: int,
        timeout_seconds: int,
    ) -> ManagedInferenceRequest:
        _portable_id(workspace_id, "workspace")
        _portable_id(request_id, "request")
        selected_provider, selected_access = _provider_access(provider, access_mode)
        self._validate_adapter(selected_provider, adapter_identity)
        note_ids = self._validate_note_ids(note_ids)
        self._validate_limits(max_output_bytes, timeout_seconds)
        caller_digest = _request_sha256(
            {
                "access_mode": selected_access.value,
                "adapter_identity": adapter_identity,
                "max_output_bytes": max_output_bytes,
                "note_ids": list(note_ids),
                "provider": selected_provider.value,
                "request_id": request_id,
                "timeout_seconds": timeout_seconds,
                "workspace_id": workspace_id,
            }
        )
        existing = self._request_row(request_id)
        if existing is not None:
            if existing["request_sha256"] != caller_digest:
                raise ManagedWorkspaceFailure("request_replay_mismatch")
            return self._request_value(existing)
        workspace = self._workspace_tasks._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                current = self._workspace_tasks._workspace_row(connection, workspace_id)
                self._workspace_tasks._assert_workspace_row(current, workspace)
                generation = int(current["policy_generation"])
                consent_id = self._active_consent(
                    connection, workspace_id, selected_provider, selected_access
                )
                sources, prompt, effective = self._selection(
                    connection,
                    workspace,
                    note_ids,
                    consent_id=consent_id,
                )
                prompt_bytes = prompt.encode("utf-8")
                self._reserve_budget(
                    connection,
                    workspace_id,
                    selected_provider,
                    len(prompt_bytes),
                )
                now = _timestamp(self._engine._clock())
                connection.execute(
                    """INSERT INTO managed_inference_requests
                    (request_id, request_sha256, workspace_id, provider, access_mode, operation,
                     adapter_identity, policy_generation, selections_json, prompt_bytes,
                     prompt_sha256, effective_privacy_json, input_bytes, max_output_bytes,
                     timeout_seconds, status, created_at)
                    VALUES (?, ?, ?, ?, ?, 'semantic_graph', ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            'reserved', ?)""",
                    (
                        request_id,
                        caller_digest,
                        workspace_id,
                        selected_provider.value,
                        selected_access.value,
                        adapter_identity,
                        generation,
                        self._sources_json(sources),
                        prompt_bytes,
                        _digest(prompt_bytes),
                        portable_canonical_json_bytes(effective.to_dict()).decode("utf-8"),
                        len(prompt_bytes),
                        max_output_bytes,
                        timeout_seconds,
                        now,
                    ),
                )
        row = self._request_row(request_id)
        if row is None:
            raise ManagedWorkspaceFailure("invalid_request")
        return self._request_value(row)

    def release(self, request_id: str) -> ManagedInferenceRequest:
        _portable_id(request_id, "request")
        existing = self._request_row(request_id)
        if existing is None:
            raise ManagedWorkspaceFailure("unknown_request")
        if existing["status"] != "reserved":
            raise ManagedWorkspaceFailure("stale_request")
        workspace = self._workspace_tasks._workspace(cast(str, existing["workspace_id"]))
        failure: ManagedWorkspaceFailure | None = None
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                row = self._request_row_from(connection, request_id)
                if row["status"] != "reserved":
                    raise ManagedWorkspaceFailure("stale_request")
                try:
                    provider, access = _provider_access(row["provider"], row["access_mode"])
                    current = self._workspace_tasks._workspace_row(
                        connection, cast(str, row["workspace_id"])
                    )
                    self._workspace_tasks._assert_workspace_row(current, workspace)
                    if int(row["policy_generation"]) != int(current["policy_generation"]):
                        raise ManagedWorkspaceFailure("stale_request")
                    consent_id = self._active_consent(
                        connection, workspace.workspace_id, provider, access
                    )
                    sources = self._sources(cast(str, row["selections_json"]))
                    current_sources, prompt, effective = self._selection(
                        connection,
                        workspace,
                        tuple(source.note_id for source in sources),
                        consent_id=consent_id,
                    )
                    if (
                        current_sources != sources
                        or _digest(prompt.encode("utf-8")) != row["prompt_sha256"]
                        or portable_canonical_json_bytes(effective.to_dict()).decode("utf-8")
                        != row["effective_privacy_json"]
                    ):
                        raise ManagedWorkspaceFailure("stale_request")
                except ManagedWorkspaceFailure as error:
                    self._cancel_locked(connection, row)
                    failure = error
                else:
                    connection.execute(
                        "UPDATE managed_inference_requests SET status = 'dispatching' "
                        "WHERE request_id = ?",
                        (request_id,),
                    )
        if failure is not None:
            raise failure
        final_row = self._request_row(request_id)
        if final_row is None:
            raise ManagedWorkspaceFailure("unknown_request")
        return self._request_value(final_row)

    def record_suggestion(
        self,
        request_id: str,
        *,
        source_note_id: str,
        target_note_id: str,
        source_quote: str,
        target_quote: str,
        model: str,
    ) -> ManagedSuggestion:
        _portable_id(request_id, "request")
        _portable_id(source_note_id, "page")
        _portable_id(target_note_id, "page")
        if source_note_id == target_note_id:
            raise ManagedWorkspaceFailure("invalid_suggestion")
        for text in (source_quote, target_quote, model):
            if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > 4096:
                raise ManagedWorkspaceFailure("invalid_suggestion")
        existing = self._suggestion_for_request(request_id)
        if existing is not None:
            existing_suggestion = self._suggestion_value(existing)
            if (
                existing_suggestion.source_note_id,
                existing_suggestion.target_note_id,
                existing_suggestion.source_quote,
                existing_suggestion.target_quote,
                existing_suggestion.model,
            ) != (source_note_id, target_note_id, source_quote, target_quote, model):
                raise ManagedWorkspaceFailure("request_replay_mismatch")
            return existing_suggestion
        request = self._request_row(request_id)
        if request is None:
            raise ManagedWorkspaceFailure("unknown_request")
        workspace = self._workspace_tasks._workspace(cast(str, request["workspace_id"]))
        failure: ManagedWorkspaceFailure | None = None
        suggestion_id: str | None = None
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                row = self._request_row_from(connection, request_id)
                if row["status"] != "dispatching":
                    raise ManagedWorkspaceFailure("stale_request")
                try:
                    current = self._workspace_tasks._workspace_row(
                        connection, workspace.workspace_id
                    )
                    self._workspace_tasks._assert_workspace_row(current, workspace)
                    if int(row["policy_generation"]) != int(current["policy_generation"]):
                        raise ManagedWorkspaceFailure("stale_request")
                    provider, access = _provider_access(row["provider"], row["access_mode"])
                    consent_id = self._active_consent(
                        connection, workspace.workspace_id, provider, access
                    )
                    selected = self._sources(cast(str, row["selections_json"]))
                    selected_by_id = {item.note_id: item for item in selected}
                    if source_note_id not in selected_by_id or target_note_id not in selected_by_id:
                        raise ManagedWorkspaceFailure("invalid_suggestion")
                    sources, prompt, _ = self._selection(
                        connection,
                        workspace,
                        tuple(item.note_id for item in selected),
                        consent_id=consent_id,
                    )
                    if (
                        sources != selected
                        or _digest(prompt.encode("utf-8")) != row["prompt_sha256"]
                    ):
                        raise ManagedWorkspaceFailure("stale_request")
                    bodies = self._source_bodies(connection, selected)
                    if (
                        source_quote not in bodies[source_note_id]
                        or target_quote not in bodies[target_note_id]
                    ):
                        raise ManagedWorkspaceFailure("invalid_suggestion")
                    selected_ids = tuple(item.note_id for item in selected)
                    output = portable_canonical_json_bytes(
                        {
                            "model": model,
                            "source": "source-"
                            + str(selected_ids.index(source_note_id) + 1),
                            "source_quote": source_quote,
                            "target": "source-"
                            + str(selected_ids.index(target_note_id) + 1),
                            "target_quote": target_quote,
                        }
                    )
                    if len(output) > int(row["max_output_bytes"]):
                        raise ManagedWorkspaceFailure("invalid_suggestion")
                except ManagedWorkspaceFailure as error:
                    self._settle_locked(connection, row, "failed", output_bytes=None)
                    failure = error
                else:
                    suggestion_id = _new_id("suggestion")
                    now = _timestamp(self._engine._clock())
                    connection.execute(
                        """INSERT INTO managed_suggestions
                        (suggestion_id, request_id, workspace_id, source_note_id,
                         source_revision_id, target_note_id, target_revision_id,
                         source_quote, target_quote, provider, model, output_sha256,
                         status, issued_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                        (
                            suggestion_id,
                            request_id,
                            workspace.workspace_id,
                            source_note_id,
                            selected_by_id[source_note_id].revision_id,
                            target_note_id,
                            selected_by_id[target_note_id].revision_id,
                            source_quote,
                            target_quote,
                            row["provider"],
                            model,
                            _digest(output),
                            now,
                        ),
                    )
                    self._settle_locked(connection, row, "succeeded", output_bytes=len(output))
        if failure is not None:
            raise failure
        if suggestion_id is None:
            raise ManagedWorkspaceFailure("invalid_suggestion")
        result = self._suggestion_row(suggestion_id)
        return self._suggestion_value(result)

    def fail(self, request_id: str) -> ManagedInferenceReceipt:
        _portable_id(request_id, "request")
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                row = self._request_row_from(connection, request_id)
                status = cast(str, row["status"])
                if status == "reserved":
                    self._cancel_locked(connection, row)
                    return ManagedInferenceReceipt("cancelled", request_id)
                if status == "dispatching":
                    self._settle_locked(connection, row, "failed", output_bytes=None)
                    return ManagedInferenceReceipt("failed", request_id)
                if status in {"cancelled", "failed"}:
                    return ManagedInferenceReceipt(status, request_id, duplicate=True)
                raise ManagedWorkspaceFailure("stale_request")

    def accept_suggestion(
        self, workspace_id: str, suggestion_id: str, *, operation_id: str
    ) -> ManagedInferenceReceipt:
        _portable_id(workspace_id, "workspace")
        _portable_id(suggestion_id, "suggestion")
        _delivery_id(operation_id)
        request_sha256 = _request_sha256(
            {
                "kind": "accept_link",
                "operation_id": operation_id,
                "suggestion_id": suggestion_id,
                "workspace_id": workspace_id,
            }
        )
        existing = self._workspace_tasks._operation(operation_id)
        if existing is not None:
            self._workspace_tasks._require_matching_operation(
                existing, request_sha256, "accept_link"
            )
            suggestion = self._suggestion_row(suggestion_id)
            return ManagedInferenceReceipt(
                "suggestion_accepted", cast(str, suggestion["request_id"]), suggestion_id, True
            )
        workspace = self._workspace_tasks._workspace(workspace_id)
        with self._engine._writer_lease.acquire_shared_writer():  # noqa: SIM117
            with self._engine._store.transaction() as connection:
                suggestion = self._suggestion_row_from(connection, suggestion_id)
                if suggestion["workspace_id"] != workspace_id:
                    raise ManagedWorkspaceFailure("unknown_suggestion")
                if suggestion["status"] != "pending":
                    raise ManagedWorkspaceFailure("invalid_suggestion")
                request = self._request_row_from(connection, cast(str, suggestion["request_id"]))
                current = self._workspace_tasks._workspace_row(connection, workspace_id)
                self._workspace_tasks._assert_workspace_row(current, workspace)
                if (
                    request["status"] != "succeeded"
                    or int(request["policy_generation"]) != int(current["policy_generation"])
                ):
                    raise ManagedWorkspaceFailure("invalid_suggestion")
                provider, access = _provider_access(request["provider"], request["access_mode"])
                self._active_consent(connection, workspace_id, provider, access)
                source = self._workspace_tasks._note_row(
                    connection, workspace_id, cast(str, suggestion["source_note_id"])
                )
                target = self._workspace_tasks._note_row(
                    connection, workspace_id, cast(str, suggestion["target_note_id"])
                )
                self._workspace_tasks._require_active_note(connection, source)
                self._workspace_tasks._require_active_note(connection, target)
                if (
                    source["accepted_revision_id"] != suggestion["source_revision_id"]
                    or target["accepted_revision_id"] != suggestion["target_revision_id"]
                ):
                    raise ManagedWorkspaceFailure("invalid_suggestion")
                source_revision = self._revision(
                    connection, cast(str, suggestion["source_revision_id"])
                )
                target_revision = self._revision(
                    connection, cast(str, suggestion["target_revision_id"])
                )
                source_parsed = self._parsed_revision(source_revision)
                target_parsed = self._parsed_revision(target_revision)
                if (
                    suggestion["source_quote"] not in source_parsed.body
                    or suggestion["target_quote"] not in target_parsed.body
                ):
                    raise ManagedWorkspaceFailure("invalid_suggestion")
                now = _timestamp(self._engine._clock())
                fields = dict(source_parsed.fields)
                fields["modified_at"] = now
                linked_body = source_parsed.body.rstrip("\n") + (
                    f"\n\n[[{suggestion['target_note_id']}]]\n"
                )
                payload = render_markdown(fields=fields, body=linked_body).encode("utf-8")
                revision_id = _new_id("revision")
                connection.execute(
                    """INSERT INTO managed_note_revisions
                    (revision_id, note_id, parent_revision_id, kind, body_bytes, body_sha256,
                     accepted_by_actor_id, provenance_json, privacy_json, recorded_at, operation_id)
                    VALUES (?, ?, ?, 'link', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        revision_id,
                        source["note_id"],
                        source_revision["revision_id"],
                        payload,
                        _digest(payload),
                        self._engine.profile.owner_actor_id,
                        source_revision["provenance_json"],
                        source_revision["privacy_json"],
                        now,
                        operation_id,
                    ),
                )
                connection.execute(
                    "UPDATE managed_notes SET accepted_revision_id = ?, updated_at = ? "
                    "WHERE note_id = ?",
                    (revision_id, now, source["note_id"]),
                )
                connection.execute(
                    """INSERT INTO managed_links
                    (link_id, source_note_id, source_revision_id, target_note_id,
                     target_revision_id, source_quote, target_quote, provenance_json,
                     accepted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _new_id("link"),
                        source["note_id"],
                        source_revision["revision_id"],
                        target["note_id"],
                        target_revision["revision_id"],
                        suggestion["source_quote"],
                        suggestion["target_quote"],
                        portable_canonical_json_bytes(
                            {
                                "model": suggestion["model"],
                                "provider": suggestion["provider"],
                                "request_id": suggestion["request_id"],
                                "suggestion_id": suggestion_id,
                            }
                        ).decode("utf-8"),
                        now,
                    ),
                )
                _advance_policy(connection, workspace_id, now)
                connection.execute(
                    "UPDATE managed_suggestions SET status = 'accepted', accepted_at = ? "
                    "WHERE suggestion_id = ?",
                    (now, suggestion_id),
                )
                self._workspace_tasks._insert_operation(
                    connection,
                    operation_id=operation_id,
                    request_sha256=request_sha256,
                    workspace_id=workspace_id,
                    note_id=cast(str, source["note_id"]),
                    kind="accept_link",
                    target_relative_path=None,
                    expected_revision_id=cast(str, source_revision["revision_id"]),
                    expected_target_sha256=cast(str | None, source["write_base_sha256"]),
                    body=payload,
                    now=now,
                    status="completed",
                )
        return ManagedInferenceReceipt(
            "suggestion_accepted",
            cast(str, suggestion["request_id"]),
            suggestion_id,
        )

    def _selection(
        self,
        connection: sqlite3.Connection,
        workspace: _Workspace,
        note_ids: tuple[str, ...],
        *,
        consent_id: str,
    ) -> tuple[tuple[ManagedInferenceSource, ...], str, PrivacyDecision]:
        sources: list[ManagedInferenceSource] = []
        prompt_sources: list[dict[str, str]] = []
        privacy_values: list[PrivacyDecision] = []
        for index, note_id in enumerate(note_ids, start=1):
            note = self._workspace_tasks._note_row(connection, workspace.workspace_id, note_id)
            self._workspace_tasks._require_active_note(connection, note)
            if self._is_excluded(connection, workspace.workspace_id, note):
                raise ManagedWorkspaceFailure("ineligible_source")
            if (
                note["accepted_revision_id"] != note["materialized_revision_id"]
                or note["materialized_sha256"] != note["write_base_sha256"]
            ):
                raise ManagedWorkspaceFailure("ineligible_source")
            revision = self._revision(connection, cast(str, note["accepted_revision_id"]))
            payload = cast(bytes, revision["body_bytes"])
            if (
                _digest(payload) != revision["body_sha256"]
                or note["materialized_sha256"] != _digest(payload)
            ):
                raise ManagedWorkspaceFailure("ineligible_source")
            current = read_confined(
                root=workspace.root,
                relative=cast(str, note["relative_path"]),
                expected_root_identity=workspace.root_identity,
                maximum_bytes=MAX_FILE_BYTES,
            )
            if current != payload:
                raise ManagedWorkspaceFailure("ineligible_source")
            parsed = self._parsed_revision(revision)
            privacy = self._effective_privacy(
                cast(str, revision["privacy_json"]), consent_id=consent_id
            )
            privacy_values.append(privacy)
            privacy_sha256 = _digest(cast(str, revision["privacy_json"]).encode("utf-8"))
            sources.append(
                ManagedInferenceSource(
                    note_id,
                    cast(str, revision["revision_id"]),
                    privacy_sha256,
                )
            )
            prompt_sources.append({"body": parsed.body, "source": f"source-{index}"})
        effective = self._combined_privacy(privacy_values, consent_id)
        prompt = portable_canonical_json_bytes(
            {"operation": "semantic_graph", "selected_sources": prompt_sources}
        ).decode("utf-8")
        if len(prompt.encode("utf-8")) > MAX_INFERENCE_INPUT_BYTES or has_redaction_finding(prompt):
            raise ManagedWorkspaceFailure("ineligible_source")
        return tuple(sources), prompt, effective

    def _source_bodies(
        self, connection: sqlite3.Connection, sources: tuple[ManagedInferenceSource, ...]
    ) -> dict[str, str]:
        return {
            source.note_id: self._parsed_revision(
                self._revision(connection, source.revision_id)
            ).body
            for source in sources
        }

    def _effective_privacy(self, raw: str, *, consent_id: str) -> PrivacyDecision:
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError
            stored = PrivacyDecision.from_dict(value)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            raise ManagedWorkspaceFailure("ineligible_source") from None
        if (
            stored.tier is PrivacyTier.PERSONAL
            and stored.reason is PrivacyReason.PERSONAL_LOCAL_ONLY
        ):
            return PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.PERSONAL_CONFIRMED,
                policy_version="managed-workspace-v1",
                authority=Authority(cloud=True, external_egress=False),
                confirmation_ref=consent_id,
            )
        if (
            stored.tier is PrivacyTier.PUBLIC
            and stored.reason is PrivacyReason.POLICY_PUBLIC
            or stored.tier is PrivacyTier.WORK
            and stored.reason is PrivacyReason.POLICY_WORK
        ):
            return PrivacyDecision.create(
                tier=stored.tier,
                reason=stored.reason,
                policy_version="managed-workspace-v1",
                authority=Authority(cloud=True, external_egress=False),
            )
        raise ManagedWorkspaceFailure("ineligible_source")

    @staticmethod
    def _combined_privacy(
        values: list[PrivacyDecision], consent_id: str
    ) -> PrivacyDecision:
        if not values:
            raise ManagedWorkspaceFailure("ineligible_source")
        if any(value.tier is PrivacyTier.PERSONAL for value in values):
            return PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.PERSONAL_CONFIRMED,
                policy_version="managed-workspace-v1",
                authority=Authority(cloud=True, external_egress=False),
                confirmation_ref=consent_id,
            )
        selected = (
            PrivacyTier.WORK
            if any(value.tier is PrivacyTier.WORK for value in values)
            else PrivacyTier.PUBLIC
        )
        return PrivacyDecision.create(
            tier=selected,
            reason=(
                PrivacyReason.POLICY_WORK
                if selected is PrivacyTier.WORK
                else PrivacyReason.POLICY_PUBLIC
            ),
            policy_version="managed-workspace-v1",
            authority=Authority(cloud=True, external_egress=False),
        )

    def _active_consent(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        provider: ManagedProvider,
        access_mode: ManagedAccessMode,
    ) -> str:
        row = connection.execute(
            """SELECT consent_id, owner_actor_id FROM managed_consents
            WHERE workspace_id = ? AND provider = ? AND access_mode = ?
              AND operation = 'semantic_graph' AND note_scope = '*' AND active = 1""",
            (workspace_id, provider.value, access_mode.value),
        ).fetchone()
        if row is None or row["owner_actor_id"] != self._engine.profile.owner_actor_id:
            raise ManagedWorkspaceFailure("active_consent_required")
        return cast(str, row["consent_id"])

    @staticmethod
    def _is_excluded(
        connection: sqlite3.Connection, workspace_id: str, note: sqlite3.Row
    ) -> bool:
        rows = connection.execute(
            """SELECT kind, subject FROM managed_exclusions
            WHERE workspace_id = ? AND active = 1""",
            (workspace_id,),
        )
        relative = PurePosixPath(cast(str, note["relative_path"]))
        for kind, subject in rows:
            if kind == "note" and subject == note["note_id"]:
                return True
            if kind == "folder":
                folder = PurePosixPath(cast(str, subject))
                if relative == folder or folder in relative.parents:
                    return True
            if kind == "portable_set":
                try:
                    values = json.loads(cast(str, subject))
                except (TypeError, json.JSONDecodeError):
                    raise ManagedWorkspaceFailure("invalid_policy") from None
                if isinstance(values, list) and note["note_id"] in values:
                    return True
        return False

    def _reserve_budget(
        self,
        connection: sqlite3.Connection,
        workspace_id: str,
        provider: ManagedProvider,
        input_bytes: int,
    ) -> None:
        now = _timestamp(self._engine._clock())
        connection.execute(
            """INSERT OR IGNORE INTO managed_inference_budgets
            (workspace_id, provider, window_key, request_limit, byte_limit, updated_at)
            VALUES (?, ?, 'release-v1', ?, ?, ?)""",
            (workspace_id, provider.value, PROVIDER_REQUEST_LIMIT, PROVIDER_BYTE_LIMIT, now),
        )
        row = connection.execute(
            """SELECT * FROM managed_inference_budgets
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (workspace_id, provider.value),
        ).fetchone()
        if row is None or (
            int(row["used_requests"])
            + int(row["reserved_requests"])
            + int(row["uncertain_requests"])
            + 1
            > int(row["request_limit"])
            or int(row["used_bytes"])
            + int(row["reserved_bytes"])
            + int(row["uncertain_bytes"])
            + input_bytes
            > int(row["byte_limit"])
        ):
            raise ManagedWorkspaceFailure("budget_exhausted")
        connection.execute(
            """UPDATE managed_inference_budgets
            SET reserved_requests = reserved_requests + 1,
                reserved_bytes = reserved_bytes + ?, updated_at = ?
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (input_bytes, now, workspace_id, provider.value),
        )

    def _cancel_locked(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if row["status"] != "reserved":
            raise ManagedWorkspaceFailure("stale_request")
        now = _timestamp(self._engine._clock())
        connection.execute(
            """UPDATE managed_inference_budgets
            SET reserved_requests = reserved_requests - 1,
                reserved_bytes = reserved_bytes - ?, updated_at = ?
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (row["input_bytes"], now, row["workspace_id"], row["provider"]),
        )
        connection.execute(
            """UPDATE managed_inference_requests
            SET status = 'cancelled', completed_at = ? WHERE request_id = ?""",
            (now, row["request_id"]),
        )

    def _settle_locked(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        status: str,
        *,
        output_bytes: int | None,
    ) -> None:
        if row["status"] != "dispatching" or status not in {
            "succeeded",
            "failed",
            "superseded",
        }:
            raise ManagedWorkspaceFailure("stale_request")
        now = _timestamp(self._engine._clock())
        connection.execute(
            """UPDATE managed_inference_budgets
            SET reserved_requests = reserved_requests - 1,
                reserved_bytes = reserved_bytes - ?, used_requests = used_requests + 1,
                used_bytes = used_bytes + ?, updated_at = ?
            WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
            (
                row["input_bytes"],
                row["input_bytes"],
                now,
                row["workspace_id"],
                row["provider"],
            ),
        )
        connection.execute(
            """UPDATE managed_inference_requests
            SET status = ?, output_bytes = ?, completed_at = ? WHERE request_id = ?""",
            (status, output_bytes, now, row["request_id"]),
        )

    def _recover_startup_locked(self) -> int:
        connection = self._engine._store.connect()
        try:
            workspace_ids = tuple(
                cast(str, row[0])
                for row in connection.execute(
                    """SELECT DISTINCT workspace_id FROM managed_consents WHERE active = 1
                    UNION SELECT DISTINCT workspace_id FROM managed_inference_requests
                    WHERE status IN ('reserved', 'dispatching')"""
                )
            )
        finally:
            connection.close()
        recovered = 0
        for workspace_id in workspace_ids:
            with self._engine._store.transaction() as transaction:
                now = _timestamp(self._engine._clock())
                reserved = tuple(
                    transaction.execute(
                        """SELECT * FROM managed_inference_requests
                        WHERE workspace_id = ? AND status = 'reserved'""",
                        (workspace_id,),
                    )
                )
                dispatching = tuple(
                    transaction.execute(
                        """SELECT * FROM managed_inference_requests
                        WHERE workspace_id = ? AND status = 'dispatching'""",
                        (workspace_id,),
                    )
                )
                for row in reserved:
                    self._cancel_locked(transaction, row)
                for row in dispatching:
                    transaction.execute(
                        """UPDATE managed_inference_budgets
                        SET reserved_requests = reserved_requests - 1,
                            reserved_bytes = reserved_bytes - ?,
                            uncertain_requests = uncertain_requests + 1,
                            uncertain_bytes = uncertain_bytes + ?, updated_at = ?
                        WHERE workspace_id = ? AND provider = ? AND window_key = 'release-v1'""",
                        (
                            row["input_bytes"],
                            row["input_bytes"],
                            now,
                            workspace_id,
                            row["provider"],
                        ),
                    )
                    transaction.execute(
                        """UPDATE managed_inference_requests
                        SET status = 'uncertain', completed_at = ? WHERE request_id = ?""",
                        (now, row["request_id"]),
                    )
                active = transaction.execute(
                    "SELECT 1 FROM managed_consents WHERE workspace_id = ? AND active = 1",
                    (workspace_id,),
                ).fetchone()
                if active is not None:
                    transaction.execute(
                        """UPDATE managed_consents SET active = 0, revoked_at = ?
                        WHERE workspace_id = ? AND active = 1""",
                        (now, workspace_id),
                    )
                    transaction.execute(
                        """UPDATE managed_workspaces
                        SET policy_generation = policy_generation + 1 WHERE workspace_id = ?""",
                        (workspace_id,),
                    )
                    transaction.execute(
                        """UPDATE managed_suggestions SET status = 'invalidated'
                        WHERE workspace_id = ? AND status = 'pending'""",
                        (workspace_id,),
                    )
                recovered += len(reserved) + len(dispatching) + int(active is not None)
        return recovered

    @staticmethod
    def _validate_adapter(provider: ManagedProvider, adapter_identity: str) -> None:
        if (
            not isinstance(adapter_identity, str)
            or not adapter_identity.startswith(f"{provider.value}:")
            or not adapter_identity.removeprefix(f"{provider.value}:")
            or len(adapter_identity) > 256
        ):
            raise ManagedWorkspaceFailure("invalid_request")

    @staticmethod
    def _validate_note_ids(note_ids: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not isinstance(note_ids, tuple)
            or not 0 < len(note_ids) <= 64
            or len(set(note_ids)) != len(note_ids)
        ):
            raise ManagedWorkspaceFailure("invalid_request")
        for note_id in note_ids:
            _portable_id(note_id, "page")
        return note_ids

    @staticmethod
    def _validate_limits(max_output_bytes: int, timeout_seconds: int) -> None:
        if (
            type(max_output_bytes) is not int
            or not 0 < max_output_bytes <= MAX_INFERENCE_OUTPUT_BYTES
            or type(timeout_seconds) is not int
            or not 0 < timeout_seconds <= MAX_INFERENCE_SECONDS
        ):
            raise ManagedWorkspaceFailure("invalid_request")

    @staticmethod
    def _sources_json(sources: tuple[ManagedInferenceSource, ...]) -> str:
        return portable_canonical_json_bytes(
            [
                {
                    "note_id": source.note_id,
                    "privacy_sha256": source.privacy_sha256,
                    "revision_id": source.revision_id,
                }
                for source in sources
            ]
        ).decode("utf-8")

    @staticmethod
    def _sources(raw: str) -> tuple[ManagedInferenceSource, ...]:
        try:
            value = json.loads(raw)
            if not isinstance(value, list) or not value or not all(
                isinstance(item, dict) for item in value
            ):
                raise ValueError
            return tuple(
                ManagedInferenceSource(
                    note_id=cast(str, item["note_id"]),
                    revision_id=cast(str, item["revision_id"]),
                    privacy_sha256=cast(str, item["privacy_sha256"]),
                )
                for item in cast(list[dict[str, object]], value)
            )
        except (KeyError, TypeError, ValueError):
            raise ManagedWorkspaceFailure("invalid_request") from None

    def _request_value(self, row: sqlite3.Row) -> ManagedInferenceRequest:
        try:
            privacy_value = json.loads(cast(str, row["effective_privacy_json"]))
            if not isinstance(privacy_value, dict):
                raise ValueError
            privacy = PrivacyDecision.from_dict(privacy_value)
            prompt = cast(bytes, row["prompt_bytes"]).decode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, ValidationError):
            raise ManagedWorkspaceFailure("invalid_request") from None
        if _digest(prompt.encode("utf-8")) != row["prompt_sha256"]:
            raise ManagedWorkspaceFailure("invalid_request")
        return ManagedInferenceRequest(
            request_id=cast(str, row["request_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            provider=ManagedProvider(cast(str, row["provider"])),
            access_mode=ManagedAccessMode(cast(str, row["access_mode"])),
            adapter_identity=cast(str, row["adapter_identity"]),
            policy_generation=int(row["policy_generation"]),
            sources=self._sources(cast(str, row["selections_json"])),
            prompt=prompt,
            effective_privacy=privacy,
            max_output_bytes=int(row["max_output_bytes"]),
            timeout_seconds=int(row["timeout_seconds"]),
        )

    @staticmethod
    def _suggestion_value(row: sqlite3.Row) -> ManagedSuggestion:
        return ManagedSuggestion(
            suggestion_id=cast(str, row["suggestion_id"]),
            workspace_id=cast(str, row["workspace_id"]),
            source_note_id=cast(str, row["source_note_id"]),
            target_note_id=cast(str, row["target_note_id"]),
            source_quote=cast(str, row["source_quote"]),
            target_quote=cast(str, row["target_quote"]),
            provider=ManagedProvider(cast(str, row["provider"])),
            model=cast(str, row["model"]),
        )

    @staticmethod
    def _parsed_revision(row: sqlite3.Row) -> ParsedMarkdown:
        try:
            return parse_markdown(cast(bytes, row["body_bytes"]))
        except MarkdownFormatError:
            raise ManagedWorkspaceFailure("ineligible_source") from None

    @staticmethod
    def _revision(connection: sqlite3.Connection, revision_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_note_revisions WHERE revision_id = ?", (revision_id,)
        ).fetchone()
        if row is None:
            raise ManagedWorkspaceFailure("ineligible_source")
        return cast(sqlite3.Row, row)

    def _request_row(self, request_id: str) -> sqlite3.Row | None:
        connection = self._engine._store.connect()
        try:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM managed_inference_requests WHERE request_id = ?", (request_id,)
                ).fetchone(),
            )
        finally:
            connection.close()

    @staticmethod
    def _request_row_from(connection: sqlite3.Connection, request_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_inference_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_request")
        return cast(sqlite3.Row, row)

    def _suggestion_for_request(self, request_id: str) -> sqlite3.Row | None:
        connection = self._engine._store.connect()
        try:
            return cast(
                sqlite3.Row | None,
                connection.execute(
                    "SELECT * FROM managed_suggestions WHERE request_id = ?", (request_id,)
                ).fetchone(),
            )
        finally:
            connection.close()

    def _suggestion_row(self, suggestion_id: str) -> sqlite3.Row:
        connection = self._engine._store.connect()
        try:
            return self._suggestion_row_from(connection, suggestion_id)
        finally:
            connection.close()

    @staticmethod
    def _suggestion_row_from(connection: sqlite3.Connection, suggestion_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM managed_suggestions WHERE suggestion_id = ?", (suggestion_id,)
        ).fetchone()
        if row is None:
            raise ManagedWorkspaceFailure("unknown_suggestion")
        return cast(sqlite3.Row, row)


__all__ = [
    "MAX_INFERENCE_INPUT_BYTES",
    "MAX_INFERENCE_OUTPUT_BYTES",
    "MAX_INFERENCE_SECONDS",
    "ManagedInferenceTasks",
]
