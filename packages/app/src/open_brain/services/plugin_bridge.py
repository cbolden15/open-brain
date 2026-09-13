"""Bounded lifecycle-owned stdio bridge for the desktop Obsidian plugin."""

from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, cast

from open_brain_engine import __version__
from open_brain_engine.engine import (
    EngineTaskSet,
    ManagedGraphSource,
    ManagedSuggestion,
    ManagedWorkspaceFailure,
    ManagedWorkspaceReceipt,
    ManagedWorkspaceStatus,
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
    refresh_structural_graph,
    search_brain,
    search_result,
    workspace_status,
)

PLUGIN_PROTOCOL = "open-brain-plugin-v1"
MAX_PLUGIN_REQUEST_BYTES = 64 * 1024
MAX_PLUGIN_RESPONSE_BYTES = 1024 * 1024
MAX_PLUGIN_SESSION_REQUESTS = 2_000
_REQUEST_ID = re.compile(
    r"^plugin_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_OPERATIONS = (
    "brain.initialize",
    "capture.create",
    "graph.accept",
    "graph.canvas",
    "graph.refresh_structural",
    "graph.review",
    "graph.suggestions",
    "search.query",
    "system.handshake",
    "workspace.reconcile",
    "workspace.refresh",
    "workspace.setup",
    "workspace.status",
)


class PluginBridgeFailure(RuntimeError):
    """One bounded plugin-facing failure category."""

    def __init__(self, code: str) -> None:
        if code not in {
            "database_busy",
            "incompatible_protocol",
            "invalid_arguments",
            "invalid_request",
            "operation_failed",
            "response_too_large",
            "unknown_operation",
        }:
            raise ValueError("invalid plugin bridge failure")
        self.code = code
        super().__init__(code)


def serve_plugin_stdio(
    selection: LocalRootSelection,
    *,
    input_stream: BinaryIO,
    output_stream: BinaryIO,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
    base_executable: Path | None = None,
) -> int:
    """Serve bounded newline-framed requests for the lifetime of one plugin child."""
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
                        "protocol": PLUGIN_PROTOCOL,
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
                    )
                _write_response(
                    output_stream,
                    {
                        "ok": True,
                        "protocol": PLUGIN_PROTOCOL,
                        "request_id": request_id,
                        "result": result,
                    },
                )
            except PluginBridgeFailure as error:
                _write_error(output_stream, request_id, error.code)
            except LockBusyError:
                _write_error(output_stream, request_id, "database_busy")
            except ManagedWorkspaceFailure as error:
                _write_error(output_stream, request_id, error.code)
            except GraphifyFailure as error:
                _write_error(output_stream, request_id, error.code)
            except LocalDataError, ProfileError:
                _write_error(output_stream, request_id, "private_data_unavailable")
            except KeyError, TypeError, ValueError:
                _write_error(output_stream, request_id, "invalid_arguments")
            except Exception:
                _write_error(output_stream, request_id, "operation_failed")
        else:
            _write_error(output_stream, None, "operation_failed")
    return 0


def dispatch_plugin_request(
    session: LocalBrainSession,
    operation: str,
    arguments: dict[str, object],
    *,
    request_id: str,
    base_executable: Path | None,
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
    if operation == "workspace.refresh":
        _require_keys(arguments, frozenset())
        status = _configured_status(tasks)
        receipt = tasks.managed_workspace.refresh(status.workspace_id, operation_id=request_id)
        return _workspace_receipt(receipt, vault_path=_workspace_path(session))
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
        "request_id",
    }:
        raise PluginBridgeFailure("invalid_request")
    if decoded["protocol"] != PLUGIN_PROTOCOL:
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
            "protocol": PLUGIN_PROTOCOL,
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
                    "protocol": PLUGIN_PROTOCOL,
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
    "PLUGIN_PROTOCOL",
    "PluginBridgeFailure",
    "dispatch_plugin_request",
    "serve_plugin_stdio",
]
