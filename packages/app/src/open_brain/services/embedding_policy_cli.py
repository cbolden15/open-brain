"""Owner-local embedding policy CLI; no MCP, plugin or desktop grant."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.embedding_backend import (
    EMBEDDING_ERROR_CATEGORIES,
    EmbeddingBackend,
    EmbeddingFailure,
)
from open_brain_engine.engine.embedding_policy import (
    ComponentVerifier,
    EmbeddingPolicyError,
    EmbeddingPolicyTasks,
    validate_component_id,
)

from .t03_adapters import owner_authority

ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "invalid_component_id",
        "owner_required",
        "local_operation_busy",
        "local_operation_failed",
        *EMBEDDING_ERROR_CATEGORIES,
    }
)
REBUILD_SCHEMA = "ob.embedding-rebuild.v1"
BackendFactory = Callable[[Path, Callable[[], bool]], EmbeddingBackend]


def add_embedding_parsers[Parser: argparse.ArgumentParser](
    subparsers: argparse._SubParsersAction[Parser],
    add_local_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    parent = subparsers.add_parser(
        "embedding", help="Inspect or change owner-local embedding consent."
    )
    add_local_options(parent)
    children = parent.add_subparsers(dest="embedding_action", required=True)
    add_local_options(children.add_parser("status", help="Report the local embedding policy."))
    enable = children.add_parser("enable", help="Enable local embedding for one component.")
    add_local_options(enable)
    enable.add_argument("--component-id", required=True)
    add_local_options(children.add_parser("disable", help="Disable local embedding."))
    add_local_options(
        children.add_parser("rebuild", help="Rebuild the derived local embedding cache.")
    )


def validate_embedding_arguments(parsed: argparse.Namespace) -> None:
    if parsed.embedding_action == "enable":
        validate_component_id(parsed.component_id)


def write_embedding_failure(code: str, *, json_output: bool) -> int:
    if code not in ERROR_CODES:
        code = "local_operation_failed"
    if json_output:
        print(json.dumps({"error": {"code": code}}, ensure_ascii=True, sort_keys=True))
    else:
        print(f"Embedding policy refused: {code}.", file=sys.stderr)
    if code in {"invalid_arguments", "invalid_component_id"}:
        return 2
    if code == "cancelled":
        return 130
    retryable = code in EMBEDDING_ERROR_CATEGORIES and EmbeddingFailure(code).retryable
    return 75 if code == "local_operation_busy" or retryable else 78


def _installed_backend(brain_root: Path, cancelled: Callable[[], bool]) -> EmbeddingBackend:
    from .embedding_helper import HelperEmbeddingBackend, discover_embedding_resource

    return HelperEmbeddingBackend(discover_embedding_resource(), brain_root, cancelled=cancelled)


@contextmanager
def _interrupt_hook() -> Iterator[Callable[[], bool]]:
    """Ctrl-C requests cancellation; the rebuild stops at its next bounded check."""
    requested = threading.Event()
    if threading.current_thread() is not threading.main_thread():
        yield requested.is_set
        return
    previous = signal.signal(signal.SIGINT, lambda _signal, _frame: requested.set())
    try:
        yield requested.is_set
    finally:
        signal.signal(signal.SIGINT, previous)


def _run_rebuild(
    policy_tasks: EmbeddingPolicyTasks,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
    backend_factory: BackendFactory,
) -> int:
    authority = owner_authority(tasks, session_id="owner-cli")
    with _interrupt_hook() as cancelled:
        try:
            backend = backend_factory(tasks.profile.root, cancelled)
            receipt = policy_tasks.rebuild_cache(
                authority=authority, backend=backend, cancelled=cancelled
            )
        except EmbeddingFailure as error:
            return write_embedding_failure(error.category, json_output=json_output)
    if json_output:
        print(
            json.dumps(
                {
                    "schema": REBUILD_SCHEMA,
                    "generation_id": receipt.generation_id,
                    "records": receipt.records,
                    "chunks": receipt.chunks,
                    "dropped_changed": receipt.dropped_changed,
                    "uncovered": receipt.uncovered,
                    "excluded_oversize": receipt.excluded_oversize,
                    "seconds": receipt.seconds,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
    else:
        print(
            f"Local embedding cache rebuilt: {receipt.records} records, "
            f"{receipt.chunks} chunks, {receipt.dropped_changed} changed during rebuild, "
            f"{receipt.uncovered} uncovered, {receipt.excluded_oversize} excluded as oversize."
        )
    return 0


def run_embedding_command(
    parsed: argparse.Namespace,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
    verify_component: ComponentVerifier | None = None,
    backend_factory: BackendFactory = _installed_backend,
) -> int:
    policy_tasks = tasks.embedding_policy
    if not isinstance(policy_tasks, EmbeddingPolicyTasks):
        raise EmbeddingPolicyError("local_operation_failed")
    if parsed.embedding_action == "rebuild":
        return _run_rebuild(
            policy_tasks, tasks, json_output=json_output, backend_factory=backend_factory
        )
    authority = owner_authority(tasks, session_id="owner-cli")
    if parsed.embedding_action == "enable":
        policy = (
            policy_tasks.enable(parsed.component_id, authority=authority)
            if verify_component is None
            else policy_tasks.enable(
                parsed.component_id, authority=authority, verify_component=verify_component
            )
        )
    elif parsed.embedding_action == "disable":
        policy = policy_tasks.disable(authority=authority)
    else:
        policy = policy_tasks.status(authority=authority)
    payload = policy.status()
    if parsed.embedding_action == "status":
        from .embedding_health import embedding_health

        payload["embedding_health"] = embedding_health(tasks)
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        state = "enabled" if policy.enabled else "disabled"
        print(f"Local embedding {state} (consent version {policy.consent_version}).")
    return 0


__all__ = [
    "EmbeddingPolicyError",
    "add_embedding_parsers",
    "run_embedding_command",
    "validate_embedding_arguments",
    "write_embedding_failure",
]
