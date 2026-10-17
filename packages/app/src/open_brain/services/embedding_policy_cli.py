"""Owner-local embedding policy CLI; no MCP, plugin or desktop grant."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from open_brain_engine.engine import EngineTaskSet
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
    }
)


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
    return 75 if code == "local_operation_busy" else 78


def run_embedding_command(
    parsed: argparse.Namespace,
    tasks: EngineTaskSet,
    *,
    json_output: bool,
    verify_component: ComponentVerifier | None = None,
) -> int:
    policy_tasks = tasks.embedding_policy
    if not isinstance(policy_tasks, EmbeddingPolicyTasks):
        raise EmbeddingPolicyError("local_operation_failed")
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
