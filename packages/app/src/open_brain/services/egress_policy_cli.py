"""Owner-local egress policy CLI; no MCP, plugin or desktop grant."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.owner_egress_policy import (
    OwnerEgressPolicyError,
    OwnerEgressPolicyTasks,
    validate_operation_id,
    validate_tiers,
)

from .t03_adapters import owner_authority

ERROR_CODES = frozenset(
    {
        "invalid_arguments",
        "invalid_tier",
        "invalid_operation_id",
        "operation_conflict",
        "owner_required",
        "local_operation_busy",
        "local_operation_failed",
    }
)


def add_egress_policy_parsers[Parser: argparse.ArgumentParser](
    subparsers: argparse._SubParsersAction[Parser],
    add_local_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    parent = subparsers.add_parser(
        "egress-policy",
        help="Show or set which privacy tiers external-provider sessions may read.",
    )
    add_local_options(parent)
    children = parent.add_subparsers(dest="egress_policy_action", required=True)
    add_local_options(children.add_parser("show", help="Report the owner egress policy."))
    replace = children.add_parser(
        "set", help="Replace the shareable tier set (public, work, personal)."
    )
    add_local_options(replace)
    replace.add_argument("--tier", action="append", default=[], metavar="TIER")
    replace.add_argument("--none", action="store_true", help="Share no tier.")
    replace.add_argument("--operation-id", required=True)


def validate_egress_policy_arguments(parsed: argparse.Namespace) -> None:
    if parsed.egress_policy_action != "set":
        return
    if bool(parsed.none) == bool(parsed.tier):
        raise OwnerEgressPolicyError("invalid_arguments")
    validate_tiers(parsed.tier)
    validate_operation_id(parsed.operation_id)


def write_egress_policy_failure(code: str, *, json_output: bool) -> int:
    if code not in ERROR_CODES:
        code = "local_operation_failed"
    if json_output:
        print(json.dumps({"error": {"code": code}}, ensure_ascii=True, sort_keys=True))
    else:
        print(f"Owner egress policy refused: {code}.", file=sys.stderr)
    if code in {"invalid_arguments", "invalid_tier", "invalid_operation_id"}:
        return 2
    return 75 if code == "local_operation_busy" else 78


def run_egress_policy_command(
    parsed: argparse.Namespace, tasks: EngineTaskSet, *, json_output: bool
) -> int:
    policy_tasks = tasks.owner_egress_policy
    if not isinstance(policy_tasks, OwnerEgressPolicyTasks):
        raise OwnerEgressPolicyError("local_operation_failed")
    authority = owner_authority(tasks, session_id="owner-cli")
    if parsed.egress_policy_action == "set":
        policy = policy_tasks.set(
            () if parsed.none else parsed.tier,
            operation_id=parsed.operation_id,
            authority=authority,
        )
    else:
        policy = policy_tasks.status(authority=authority)
    payload = policy.status()
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        shared = ", ".join(sorted(tier.value for tier in policy.tiers))
        print(
            f"Owner egress policy version {policy.version}: "
            + (f"shares {shared}." if shared else "no tier is shared.")
        )
    return 0


__all__ = [
    "OwnerEgressPolicyError",
    "add_egress_policy_parsers",
    "run_egress_policy_command",
    "validate_egress_policy_arguments",
    "write_egress_policy_failure",
]
