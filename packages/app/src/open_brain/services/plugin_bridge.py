"""Bounded lifecycle-owned stdio bridge for the desktop Obsidian plugin."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, cast

from open_brain_engine import __version__
from open_brain_engine.engine import (
    EngineTaskSet,
    ManagedAccessMode,
    ManagedGraphSource,
    ManagedProvider,
    ManagedSuggestion,
    ManagedWorkspaceFailure,
    ManagedWorkspaceReceipt,
    ManagedWorkspaceStatus,
    StateSchemaUnavailableError,
    canonical_json_bytes,
)
from open_brain_engine.storage.locks import LockBusyError

from open_brain.local_data import FilesystemTypeProbe, LocalDataError, LocalRootSelection
from open_brain.profile import ProfileError
from open_brain.services.graphify_projection import GraphifyFailure
from open_brain.services.local_bootstrap import (
    LocalBrainSession,
    initialize_local_brain,
    open_local_brain,
)
from open_brain.services.local_operations import (
    capture_result,
    capture_text,
    graph_canvas,
    graph_suggestions,
    refresh_graph,
    refresh_structural_graph,
    search_brain,
    search_result,
    workspace_status,
)
from open_brain.services.managed_providers import (
    DEFAULT_MODELS,
    DirectApiAdapter,
    ManagedProviderFailure,
)
from open_brain.services.provider_credentials import (
    CredentialStoreFailure,
    OsCredentialStore,
    discover_credential_store,
    validate_credential,
)

OPEN_BRAIN_CLIENT_PROTOCOL = "open-brain-client"
OPEN_BRAIN_CLIENT_PROTOCOL_VERSION = 1
MAX_PLUGIN_REQUEST_BYTES = 64 * 1024
MAX_PLUGIN_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_PLUGIN_SESSION_REQUESTS = 2_000
_REQUEST_ID = re.compile(
    r"^plugin_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPERATIONS = (
    "brain.initialize",
    "capture.create",
    "graph.accept",
    "graph.canvas",
    "graph.refresh_semantic",
    "graph.refresh_structural",
    "graph.review",
    "graph.suggestions",
    "policy.exclusions",
    "policy.set_exclusion",
    "provider.configure",
    "provider.remove",
    "provider.status",
    "search.query",
    "system.handshake",
    "workspace.reconcile",
    "workspace.conflict_review",
    "workspace.conflicts",
    "workspace.refresh",
    "workspace.resolve",
    "workspace.setup",
    "workspace.status",
)

_DIRECT_PROVIDERS = (
    ManagedProvider.OPENAI_API,
    ManagedProvider.ANTHROPIC_API,
    ManagedProvider.GEMINI_API,
)
_MAX_PROVIDER_ATTEMPTS = 40
_MAX_PROVIDER_INPUT_BYTES = 1024 * 1024


class PluginBridgeFailure(RuntimeError):
    """One bounded plugin-facing failure category."""

    def __init__(self, code: str) -> None:
        if code not in {
            "credential_store_unavailable",
            "credential_unavailable",
            "database_busy",
            "incompatible_schema",
            "incompatible_protocol",
            "invalid_arguments",
            "invalid_request",
            "operation_failed",
            "response_too_large",
            "session_exhausted",
            "setup_required",
            "subscription_unavailable",
            "unknown_operation",
        }:
            raise ValueError("invalid plugin bridge failure")
        self.code = code
        super().__init__(code)


class PluginRuntimeState:
    """In-memory provider selection, credentials, and process-wide inference budget."""

    __slots__ = (
        "credential_store",
        "remaining_attempts",
        "remaining_input_bytes",
        "selected_custody",
        "selected_provider",
        "session_credentials",
    )

    def __init__(self, credential_store: OsCredentialStore | None) -> None:
        self.credential_store = credential_store
        self.remaining_attempts = _MAX_PROVIDER_ATTEMPTS
        self.remaining_input_bytes = _MAX_PROVIDER_INPUT_BYTES
        self.selected_provider: ManagedProvider | None = None
        self.selected_custody: str | None = None
        self.session_credentials: dict[ManagedProvider, str] = {}

    def resolve_credential(self, provider: ManagedProvider, custody: str) -> str | None:
        if custody == "session":
            return self.session_credentials.get(provider)
        if custody == "os" and self.credential_store is not None:
            return self.credential_store.resolve(provider)
        return None


def serve_plugin_stdio(
    selection: LocalRootSelection,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
    base_executable: Path | None = None,
    environment: Mapping[str, object] | None = None,
    credential_store: OsCredentialStore | None = None,
) -> int:
    """Serve bounded newline-framed requests for the lifetime of one plugin child."""
    selected_environment = os.environ if environment is None else environment
    runtime = PluginRuntimeState(
        credential_store
        if credential_store is not None
        else discover_credential_store(selection.platform_name, selected_environment)
    )
    with ExitStack() as stack:
        session: LocalBrainSession | None = None
        for _index in range(MAX_PLUGIN_SESSION_REQUESTS):
            payload = input_stream.readline(MAX_PLUGIN_REQUEST_BYTES + 2)
            if not payload:
                break
            if len(payload) > MAX_PLUGIN_REQUEST_BYTES + 1 or (
                len(payload) == MAX_PLUGIN_REQUEST_BYTES + 1 and not payload.endswith(b"\n")
            ):
                _write_error(output_stream, None, "invalid_request")
                break
            request_id: str | None = None
            try:
                request = _read_request(payload.rstrip(b"\n"))
                request_id = cast(str, request["request_id"])
                operation = cast(str, request["operation"])
                arguments = cast(dict[str, object], request["arguments"])
                if operation == "system.handshake":
                    _require_keys(arguments, frozenset())
                    result: dict[str, object] = {
                        "desktop_only": True,
                        "operations": list(_OPERATIONS),
                        "product_version": __version__,
                        "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
                        "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
                        "status": "ok",
                    }
                elif operation == "brain.initialize":
                    _require_keys(arguments, frozenset())
                    if session is not None:
                        raise PluginBridgeFailure("invalid_arguments")
                    result = initialize_local_brain(
                        selection, filesystem_type_probe=filesystem_type_probe
                    ).to_dict()
                else:
                    if session is None:
                        session = stack.enter_context(
                            open_local_brain(selection, filesystem_type_probe=filesystem_type_probe)
                        )
                    result = dispatch_plugin_request(
                        session,
                        operation,
                        arguments,
                        request_id=request_id,
                        base_executable=base_executable,
                        runtime=runtime,
                    )
                _write_response(
                    output_stream,
                    {
                        "ok": True,
                        "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
                        "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
                        "request_id": request_id,
                        "result": result,
                    },
                )
            except PluginBridgeFailure as error:
                _write_error(output_stream, request_id, error.code)
            except LockBusyError:
                _write_error(output_stream, request_id, "database_busy")
            except StateSchemaUnavailableError:
                _write_error(output_stream, request_id, "incompatible_schema")
            except ManagedWorkspaceFailure as error:
                _write_error(output_stream, request_id, error.code)
            except GraphifyFailure as error:
                _write_error(output_stream, request_id, error.code)
            except ManagedProviderFailure as error:
                _write_error(output_stream, request_id, error.code)
            except CredentialStoreFailure:
                _write_error(output_stream, request_id, "credential_store_unavailable")
            except LocalDataError, ProfileError:
                _write_error(output_stream, request_id, "private_data_unavailable")
            except KeyError, TypeError, ValueError:
                _write_error(output_stream, request_id, "invalid_arguments")
            except Exception:
                _write_error(output_stream, request_id, "operation_failed")
        else:
            _write_error(output_stream, None, "session_exhausted")
    return 0


def dispatch_plugin_request(
    session: LocalBrainSession,
    operation: str,
    arguments: dict[str, object],
    *,
    request_id: str,
    base_executable: Path | None,
    runtime: PluginRuntimeState | None = None,
) -> dict[str, object]:
    tasks = session.tasks
    if operation == "capture.create":
        _require_keys(arguments, frozenset({"text"}))
        text = _bounded_text(arguments["text"], maximum_bytes=16 * 1024)
        return capture_result(capture_text(tasks.capture, text, delivery_id=request_id))
    if operation == "search.query":
        _require_keys(arguments, frozenset({"limit", "query"}))
        query = _bounded_text(arguments["query"], maximum_bytes=4096)
        limit = arguments["limit"]
        if type(limit) is not int or not 1 <= limit <= 100:
            raise PluginBridgeFailure("invalid_arguments")
        result = search_result(
            search_brain(tasks.retrieval, tasks.reconciliation, query, limit=limit)
        )
        return _with_workspace_paths(tasks, result)
    if operation == "workspace.setup":
        _require_keys(arguments, frozenset())
        workspace = _workspace_path(session)
        _ensure_workspace_directory(workspace)
        return _workspace_receipt(
            tasks.managed_workspace.setup(workspace.as_posix(), operation_id=request_id),
            vault_path=workspace,
        )
    if operation == "workspace.status":
        _require_keys(arguments, frozenset())
        result = workspace_status(tasks)
        if result["status"] == "ok":
            result["vault_path"] = _workspace_path(session).as_posix()
        return result
    if operation == "workspace.reconcile":
        _require_keys(arguments, frozenset())
        return _reconcile_workspace(tasks, request_id=request_id)
    if operation == "workspace.conflicts":
        _require_keys(arguments, frozenset())
        status = _configured_status(tasks)
        return {
            "conflicts": [
                {
                    "conflict_id": conflict.conflict_id,
                    "note_id": conflict.note_id,
                    "relative_path": conflict.relative_path,
                }
                for conflict in tasks.managed_workspace.open_conflicts(status.workspace_id)
            ],
            "open_conflicts": status.open_conflicts,
            "status": "ok",
            "workspace_id": status.workspace_id,
        }
    if operation == "workspace.conflict_review":
        _require_keys(arguments, frozenset({"note_id"}))
        note_id = _bounded_text(arguments["note_id"], maximum_bytes=200)
        status = _configured_status(tasks)
        conflict = tasks.managed_workspace.review_conflict(status.workspace_id, note_id)
        return {
            "accepted_body": conflict.accepted_body,
            "accepted_revision_id": conflict.accepted_revision_id,
            "conflict_id": conflict.conflict_id,
            "note_id": conflict.note_id,
            "relative_path": conflict.relative_path,
            "status": "review",
            "workspace_body": conflict.workspace_body,
        }
    if operation == "workspace.resolve":
        _require_keys(arguments, frozenset({"choice", "conflict_id", "note_id"}))
        note_id = _bounded_text(arguments["note_id"], maximum_bytes=200)
        conflict_id = _bounded_text(arguments["conflict_id"], maximum_bytes=200)
        choice = arguments["choice"]
        if choice not in {"accepted", "workspace"}:
            raise PluginBridgeFailure("invalid_arguments")
        status = _configured_status(tasks)
        resolved = tasks.managed_workspace.resolve_conflict(
            status.workspace_id,
            note_id,
            choice,
            conflict_id=conflict_id,
            operation_id=request_id + ".resolve",
        )
        materialized: ManagedWorkspaceReceipt | None = None
        if choice == "accepted":
            materialized = tasks.managed_workspace.materialize(
                status.workspace_id,
                note_id,
                operation_id=request_id + ".materialize",
            )
        return {
            "choice": choice,
            "duplicate": resolved.duplicate,
            "materialized_duplicate": (
                None if materialized is None else materialized.duplicate
            ),
            "note_id": note_id,
            "status": "resolved",
        }
    if operation == "workspace.refresh":
        _require_keys(arguments, frozenset())
        status = _configured_status(tasks)
        receipt = tasks.managed_workspace.refresh(status.workspace_id, operation_id=request_id)
        return _workspace_receipt(receipt, vault_path=_workspace_path(session))
    if operation == "provider.status":
        _require_keys(arguments, frozenset())
        return _provider_status(_runtime(runtime))
    if operation == "provider.configure":
        _require_keys(
            arguments,
            frozenset({"credential", "custody", "provider", "scope_ack"}),
        )
        return _configure_provider(tasks, _runtime(runtime), arguments, request_id=request_id)
    if operation == "provider.remove":
        _require_keys(arguments, frozenset({"custody", "provider"}))
        return _remove_provider(tasks, _runtime(runtime), arguments, request_id=request_id)
    if operation == "policy.exclusions":
        _require_keys(arguments, frozenset())
        status = _configured_status(tasks)
        exclusions = tasks.managed_policy.active_exclusions(status.workspace_id)
        return {
            "exclusions": [
                {
                    "kind": exclusion.kind,
                    "relative_path": exclusion.relative_path,
                }
                for exclusion in exclusions
            ],
            "status": "ok",
            "workspace_id": status.workspace_id,
        }
    if operation == "policy.set_exclusion":
        _require_keys(arguments, frozenset({"excluded", "kind", "relative_path"}))
        kind = arguments["kind"]
        excluded = arguments["excluded"]
        relative_path = _bounded_text(arguments["relative_path"], maximum_bytes=4096)
        if kind not in {"folder", "note"} or type(excluded) is not bool:
            raise PluginBridgeFailure("invalid_arguments")
        status = _configured_status(tasks)
        subject = (
            tasks.managed_workspace.note_id_for_path(status.workspace_id, relative_path)
            if kind == "note"
            else relative_path
        )
        policy_receipt = tasks.managed_policy.set_exclusion(
            status.workspace_id,
            kind,
            subject,
            excluded=excluded,
            operation_id=request_id,
        )
        return {
            "duplicate": policy_receipt.duplicate,
            "excluded": excluded,
            "kind": kind,
            "policy_generation": policy_receipt.policy_generation,
            "relative_path": relative_path,
            "status": "updated",
        }
    if operation == "graph.refresh_semantic":
        _require_keys(arguments, frozenset())
        return _refresh_semantic(tasks, _runtime(runtime), request_id=request_id)
    if operation == "graph.refresh_structural":
        _require_keys(arguments, frozenset())
        return refresh_structural_graph(tasks, base_executable=base_executable)
    if operation == "graph.canvas":
        _require_keys(arguments, frozenset())
        return graph_canvas(tasks)
    if operation == "graph.suggestions":
        _require_keys(arguments, frozenset())
        return graph_suggestions(tasks)
    if operation == "graph.review":
        _require_keys(arguments, frozenset({"suggestion_id"}))
        suggestion_id = _bounded_text(arguments["suggestion_id"], maximum_bytes=200)
        return _suggestion_review(tasks, suggestion_id)
    if operation == "graph.accept":
        _require_keys(arguments, frozenset({"suggestion_id"}))
        suggestion_id = _bounded_text(arguments["suggestion_id"], maximum_bytes=200)
        review = _suggestion_review(tasks, suggestion_id)
        status = _configured_status(tasks)
        accepted = tasks.managed_inference.accept_suggestion(
            status.workspace_id,
            suggestion_id,
            operation_id=request_id + ".accept",
        )
        source = cast(dict[str, object], review["source"])
        note_id = cast(str, source["note_id"])
        materialized = tasks.managed_workspace.materialize(
            status.workspace_id,
            note_id,
            operation_id=request_id + ".materialize",
        )
        return {
            "accepted_duplicate": accepted.duplicate,
            "materialized_duplicate": materialized.duplicate,
            "relative_path": source["relative_path"],
            "status": "accepted",
            "suggestion_id": suggestion_id,
        }
    if operation not in _OPERATIONS:
        raise PluginBridgeFailure("unknown_operation")
    raise PluginBridgeFailure("invalid_arguments")


def _provider_status(runtime: PluginRuntimeState) -> dict[str, object]:
    stored: dict[str, bool | None] = {}
    for provider in _DIRECT_PROVIDERS:
        if runtime.credential_store is None:
            stored[provider.value] = None
            continue
        try:
            stored[provider.value] = runtime.credential_store.contains(provider)
        except CredentialStoreFailure:
            stored[provider.value] = None
    return {
        "os_store": (
            "unavailable" if runtime.credential_store is None else runtime.credential_store.kind
        ),
        "providers": [
            {
                "access_mode": "api_key",
                "credential_saved": stored[provider.value],
                "provider": provider.value,
                "status": "available",
            }
            for provider in _DIRECT_PROVIDERS
        ]
        + [
            {
                "access_mode": "subscription",
                "credential_saved": None,
                "provider": ManagedProvider.CLAUDE_SUBSCRIPTION.value,
                "reason": "subscription_isolation_unproven",
                "status": "blocked",
            }
        ],
        "selected": (
            None
            if runtime.selected_provider is None
            else {
                "access_mode": "api_key",
                "custody": runtime.selected_custody,
                "provider": runtime.selected_provider.value,
            }
        ),
        "status": "ok",
    }


def _configure_provider(
    tasks: EngineTaskSet,
    runtime: PluginRuntimeState,
    arguments: Mapping[str, object],
    *,
    request_id: str,
) -> dict[str, object]:
    provider = _direct_provider(arguments["provider"])
    custody = arguments["custody"]
    credential = arguments["credential"]
    if custody not in {"os", "session"} or arguments["scope_ack"] is not True:
        raise PluginBridgeFailure("invalid_arguments")
    if credential is not None and not isinstance(credential, str):
        raise PluginBridgeFailure("invalid_arguments")
    if custody == "session":
        if credential is None:
            raise PluginBridgeFailure("credential_unavailable")
        runtime.session_credentials[provider] = validate_credential(credential)
    else:
        store = runtime.credential_store
        if store is None:
            raise PluginBridgeFailure("credential_store_unavailable")
        if credential is None:
            if not store.contains(provider):
                raise PluginBridgeFailure("credential_unavailable")
        else:
            store.store(provider, credential)
    previous = runtime.selected_provider
    previous_custody = runtime.selected_custody
    status = _configured_status(tasks)
    if previous is not None and (previous != provider or previous_custody != custody):
        tasks.managed_policy.revoke_consent(
            status.workspace_id,
            previous,
            ManagedAccessMode.API_KEY,
            operation_id=request_id + ".revoke",
        )
    receipt = tasks.managed_policy.grant_consent(
        status.workspace_id,
        provider,
        ManagedAccessMode.API_KEY,
        operation_id=request_id + ".grant",
    )
    runtime.selected_provider = provider
    runtime.selected_custody = custody
    return {
        "access_mode": "api_key",
        "custody": custody,
        "policy_generation": receipt.policy_generation,
        "provider": provider.value,
        "scope": "eligible_managed_vault_notes",
        "status": "configured",
    }


def _remove_provider(
    tasks: EngineTaskSet,
    runtime: PluginRuntimeState,
    arguments: Mapping[str, object],
    *,
    request_id: str,
) -> dict[str, object]:
    provider = _direct_provider(arguments["provider"])
    custody = arguments["custody"]
    if custody not in {"os", "session"}:
        raise PluginBridgeFailure("invalid_arguments")
    status = _configured_status(tasks)
    receipt = tasks.managed_policy.revoke_consent(
        status.workspace_id,
        provider,
        ManagedAccessMode.API_KEY,
        operation_id=request_id + ".revoke",
    )
    if custody == "session":
        runtime.session_credentials.pop(provider, None)
    else:
        if runtime.credential_store is None:
            raise PluginBridgeFailure("credential_store_unavailable")
        runtime.credential_store.delete(provider)
    if runtime.selected_provider == provider and runtime.selected_custody == custody:
        runtime.selected_provider = None
        runtime.selected_custody = None
    return {
        "policy_generation": receipt.policy_generation,
        "provider": provider.value,
        "status": "removed",
    }


def _refresh_semantic(
    tasks: EngineTaskSet,
    runtime: PluginRuntimeState,
    *,
    request_id: str,
) -> dict[str, object]:
    provider = runtime.selected_provider
    custody = runtime.selected_custody
    if provider is None or custody is None:
        raise PluginBridgeFailure("setup_required")
    adapter = DirectApiAdapter(provider, model=DEFAULT_MODELS[provider])
    result, attempts, input_bytes = refresh_graph(
        tasks,
        provider=provider,
        access_mode=ManagedAccessMode.API_KEY,
        adapter_identity=adapter.identity,
        request_id="request_" + request_id.removeprefix("plugin_"),
        invoke=adapter.bind(lambda: runtime.resolve_credential(provider, custody)),
        remaining_attempts=runtime.remaining_attempts,
        remaining_input_bytes=runtime.remaining_input_bytes,
    )
    runtime.remaining_attempts -= attempts
    runtime.remaining_input_bytes -= input_bytes
    result["remaining_attempts"] = runtime.remaining_attempts
    result["remaining_input_bytes"] = runtime.remaining_input_bytes
    return result


def _runtime(value: PluginRuntimeState | None) -> PluginRuntimeState:
    if value is None:
        raise PluginBridgeFailure("setup_required")
    return value


def _direct_provider(value: object) -> ManagedProvider:
    if not isinstance(value, str):
        raise PluginBridgeFailure("invalid_arguments")
    try:
        provider = ManagedProvider(value)
    except (TypeError, ValueError):
        raise PluginBridgeFailure("invalid_arguments") from None
    if provider is ManagedProvider.CLAUDE_SUBSCRIPTION:
        raise PluginBridgeFailure("subscription_unavailable")
    if provider not in _DIRECT_PROVIDERS:
        raise PluginBridgeFailure("invalid_arguments")
    return provider


def _read_request(payload: bytes) -> dict[str, object]:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, child in pairs:
            if key in value:
                raise PluginBridgeFailure("invalid_request")
            value[key] = child
        return value

    try:
        decoded = json.loads(
            payload,
            object_pairs_hook=unique,
            parse_constant=lambda _value: _reject_request(),
        )
    except PluginBridgeFailure:
        raise
    except UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError:
        raise PluginBridgeFailure("invalid_request") from None
    if not isinstance(decoded, dict) or set(decoded) != {
        "arguments",
        "operation",
        "protocol",
        "protocol_version",
        "request_id",
    }:
        raise PluginBridgeFailure("invalid_request")
    if (
        decoded["protocol"] != OPEN_BRAIN_CLIENT_PROTOCOL
        or type(decoded["protocol_version"]) is not int
        or decoded["protocol_version"] != OPEN_BRAIN_CLIENT_PROTOCOL_VERSION
    ):
        raise PluginBridgeFailure("incompatible_protocol")
    request_id = decoded["request_id"]
    operation = decoded["operation"]
    arguments = decoded["arguments"]
    if (
        not isinstance(request_id, str)
        or _REQUEST_ID.fullmatch(request_id) is None
        or not isinstance(operation, str)
        or operation not in _OPERATIONS
        or not isinstance(arguments, dict)
        or not all(isinstance(key, str) for key in arguments)
    ):
        if isinstance(operation, str) and operation not in _OPERATIONS:
            raise PluginBridgeFailure("unknown_operation")
        raise PluginBridgeFailure("invalid_request")
    return cast(dict[str, object], decoded)


def _write_error(output_stream: BinaryIO, request_id: str | None, code: str) -> None:
    _write_response(
        output_stream,
        {
            "error": {"code": code},
            "ok": False,
            "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
            "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
            "request_id": request_id,
        },
    )


def _write_response(output_stream: BinaryIO, response: Mapping[str, object]) -> None:
    payload = canonical_json_bytes(dict(response)) + b"\n"
    if len(payload) > MAX_PLUGIN_RESPONSE_BYTES:
        fallback = (
            canonical_json_bytes(
                {
                    "error": {"code": "response_too_large"},
                    "ok": False,
                    "protocol": OPEN_BRAIN_CLIENT_PROTOCOL,
                    "protocol_version": OPEN_BRAIN_CLIENT_PROTOCOL_VERSION,
                    "request_id": response.get("request_id"),
                }
            )
            + b"\n"
        )
        output_stream.write(fallback)
        output_stream.flush()
        return
    output_stream.write(payload)
    output_stream.flush()


def _configured_status(tasks: EngineTaskSet) -> ManagedWorkspaceStatus:
    status = tasks.managed_workspace.status()
    if status is None:
        raise ManagedWorkspaceFailure("unknown_workspace")
    return status


def _workspace_path(session: LocalBrainSession) -> Path:
    return session.profile.root.parent / "Open Brain Vault"


def _ensure_workspace_directory(workspace: Path) -> None:
    try:
        metadata = workspace.lstat()
    except FileNotFoundError:
        workspace.mkdir(mode=0o700)
        return
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ManagedWorkspaceFailure("unsafe_workspace")


def _reconcile_workspace(tasks: EngineTaskSet, *, request_id: str) -> dict[str, object]:
    status = _configured_status(tasks)
    accepted: list[str] = []
    for index in range(64):
        observation = tasks.managed_workspace.observe(status.workspace_id)
        changed = [note for note in observation.notes if note.changed and note.present]
        if not changed:
            return {
                "accepted_note_ids": accepted,
                "generation": observation.generation,
                "missing_note_ids": [
                    note.note_id for note in observation.notes if not note.present
                ],
                "status": "reconciled",
                "workspace_id": status.workspace_id,
            }
        note = changed[0]
        tasks.managed_workspace.accept_observed(
            status.workspace_id,
            note.note_id,
            generation=observation.generation,
            operation_id=f"{request_id}.accept.{index}",
        )
        accepted.append(note.note_id)
    raise PluginBridgeFailure("operation_failed")


def _with_workspace_paths(tasks: EngineTaskSet, result: dict[str, object]) -> dict[str, object]:
    status = tasks.managed_workspace.status()
    if status is None:
        return result
    snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
    paths = {source.note_id: source.relative_path for source in snapshot.sources}
    values = cast(list[dict[str, object]], result["results"])
    for value in values:
        result_id = value.get("result_id")
        if isinstance(result_id, str) and result_id in paths:
            value["relative_path"] = paths[result_id]
    return result


def _suggestion_review(tasks: EngineTaskSet, suggestion_id: str) -> dict[str, object]:
    status = _configured_status(tasks)
    suggestion = tasks.managed_inference.suggestion(status.workspace_id, suggestion_id)
    snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
    sources = {source.note_id: source for source in snapshot.sources}
    source = sources.get(suggestion.source_note_id)
    target = sources.get(suggestion.target_note_id)
    if source is None or target is None:
        raise ManagedWorkspaceFailure("invalid_suggestion")
    return {
        "append_text": f"[[{target.note_id}]]",
        "model": suggestion.model,
        "provider": suggestion.provider.value,
        "revision_status": _revision_status(suggestion, sources),
        "source": {
            "note_id": source.note_id,
            "quote": suggestion.source_quote,
            "relative_path": source.relative_path,
            "revision_id": source.revision_id,
        },
        "status": "review",
        "suggestion_id": suggestion.suggestion_id,
        "target": {
            "note_id": target.note_id,
            "quote": suggestion.target_quote,
            "relative_path": target.relative_path,
            "revision_id": target.revision_id,
        },
    }


def _revision_status(
    suggestion: ManagedSuggestion, sources: Mapping[str, ManagedGraphSource]
) -> str:
    source = sources.get(suggestion.source_note_id)
    target = sources.get(suggestion.target_note_id)
    return (
        "current"
        if getattr(source, "revision_id", None) == suggestion.source_revision_id
        and getattr(target, "revision_id", None) == suggestion.target_revision_id
        else "stale"
    )


def _workspace_receipt(receipt: ManagedWorkspaceReceipt, *, vault_path: Path) -> dict[str, object]:
    return {
        "duplicate": receipt.duplicate,
        "generation": receipt.generation,
        "note_id": receipt.note_id,
        "status": receipt.status,
        "vault_path": vault_path.as_posix(),
        "workspace_id": receipt.workspace_id,
    }


def _require_keys(arguments: Mapping[str, object], expected: frozenset[str]) -> None:
    if set(arguments) != expected:
        raise PluginBridgeFailure("invalid_arguments")


def _bounded_text(value: object, *, maximum_bytes: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > maximum_bytes
    ):
        raise PluginBridgeFailure("invalid_arguments")
    return value


def _reject_request() -> None:
    raise PluginBridgeFailure("invalid_request")


__all__ = [
    "MAX_PLUGIN_REQUEST_BYTES",
    "MAX_PLUGIN_RESPONSE_BYTES",
    "MAX_PLUGIN_SESSION_REQUESTS",
    "OPEN_BRAIN_CLIENT_PROTOCOL",
    "OPEN_BRAIN_CLIENT_PROTOCOL_VERSION",
    "PluginBridgeFailure",
    "PluginRuntimeState",
    "dispatch_plugin_request",
    "serve_plugin_stdio",
]
