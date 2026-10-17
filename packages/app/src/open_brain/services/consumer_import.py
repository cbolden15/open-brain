"""Owner-local normalized intake for shell agents; no delegated MCP authority."""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections.abc import Callable
from hashlib import sha256
from typing import cast
from uuid import UUID

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.consumer_plans import ConsumerImportPlan
from open_brain_engine.engine.consumer_profiles import ConsumerIntakeError

from .t03_adapters import owner_authority

CLI_CONTRACT = "ob.consumer-intake-cli.v1"
MAX_INPUT_BYTES = 1_048_576


def consumer_grammar_requested(arguments: tuple[str, ...]) -> bool:
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--json" or word.startswith("--data-dir="):
            index += 1
        elif word == "--data-dir":
            index += 2
        else:
            return word == "consumer-import"
    return False


def add_consumer_parsers[Parser: argparse.ArgumentParser](
    subparsers: argparse._SubParsersAction[Parser],
    add_local_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    parent = subparsers.add_parser(
        "consumer-import", help="Owner-local normalized conversation intake, not archive parsing."
    )
    add_local_options(parent)
    children = parent.add_subparsers(dest="consumer_action", required=True)
    for action in ("preview", "body", "commit", "status", "recover", "halt"):
        child = children.add_parser(action)
        add_local_options(child)
        if action in {"body", "commit"}:
            child.add_argument("--preview-id", required=True)
        if action == "body":
            child.add_argument("--body-page", type=int, required=True)
        if action in {"status", "recover", "halt"}:
            child.add_argument("--operation-id", required=True)
        if action in {"commit", "recover", "halt"}:
            child.add_argument(
                "--confirm",
                action="store_true",
                required=True,
                help="Confirm an owner mutation; transcript text cannot grant approval.",
            )


def validate_consumer_arguments(parsed: argparse.Namespace) -> None:
    preview = getattr(parsed, "preview_id", None)
    if preview is not None:
        if re.fullmatch(r"chatprev_[A-Za-z0-9_-]{43}", preview) is None:
            raise ConsumerIntakeError()
        if (
            base64.urlsafe_b64encode(base64.urlsafe_b64decode(preview[9:] + "="))
            .decode()
            .rstrip("=")
            != preview[9:]
        ):
            raise ConsumerIntakeError()
    operation = getattr(parsed, "operation_id", None)
    if operation is not None:
        try:
            identity = UUID(operation)
        except ValueError:
            raise ConsumerIntakeError() from None
        if identity.version != 4 or str(identity) != operation:
            raise ConsumerIntakeError()
    if parsed.consumer_action == "body" and parsed.body_page < 0:
        raise ConsumerIntakeError()


def read_consumer_plan() -> ConsumerImportPlan:
    """Bound input before decode; reuse exact engine evidence and closure validation."""

    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ConsumerIntakeError()
            result[key] = value
        return result

    def invalid_constant(_value: str) -> object:
        raise ConsumerIntakeError()

    try:
        if sys.stdin.isatty():
            raise ConsumerIntakeError()
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if not 0 < len(raw) <= MAX_INPUT_BYTES:
            raise ConsumerIntakeError("conversation_limit")
        value = json.loads(
            raw.decode("utf-8", "strict"), object_pairs_hook=unique, parse_constant=invalid_constant
        )
        if type(value) is not dict or set(value) != {"contract", "plan", "objects"}:
            raise ConsumerIntakeError()
        if value["contract"] != CLI_CONTRACT or type(value["objects"]) is not dict:
            raise ConsumerIntakeError()
        objects: dict[str, bytes] = {}
        for key, payload in value["objects"].items():
            encoded = portable_canonical_json_bytes(payload)
            if type(key) is not str or sha256(encoded).hexdigest() != key:
                raise ConsumerIntakeError()
            objects[key] = encoded
        plan = ConsumerImportPlan.from_value(value["plan"], objects)
        if plan.objects() != objects:
            raise ConsumerIntakeError()
        return plan
    except ConsumerIntakeError:
        raise
    except OSError, ValueError, TypeError, UnicodeError, RecursionError:
        raise ConsumerIntakeError() from None


def write_consumer_failure(code: str, *, json_output: bool) -> int:
    try:
        ConsumerIntakeError(code)
    except ValueError:
        code = "local_operation_failed"
    if json_output:
        print(
            json.dumps(
                {
                    "dto_version": 1,
                    "status": "failed",
                    "error": {
                        "code": code,
                        "retryable": code == "local_operation_busy",
                        "item_token": None,
                    },
                },
                sort_keys=True,
            )
        )
    else:
        print(f"Conversation intake refused: {code}.", file=sys.stderr)
    if code == "invalid_arguments":
        return 2
    return 75 if code == "local_operation_busy" else 78


def write_consumer_interrupted(operation_id: str | None, *, json_output: bool) -> int:
    if json_output:
        print(
            json.dumps(
                {
                    "dto_version": 1,
                    "status": "interrupted",
                    "operation_id": operation_id,
                    "error": {
                        "code": "import_interrupted",
                        "retryable": False,
                        "item_token": None,
                    },
                },
                sort_keys=True,
            )
        )
    else:
        print("Conversation intake interrupted; check operation status.", file=sys.stderr)
    return 130


def run_consumer_command(
    parsed: argparse.Namespace, tasks: EngineTaskSet, *, json_output: bool
) -> int:
    intake = tasks.consumer_imports
    if intake is None:
        raise ConsumerIntakeError("operation_pending")
    authority = owner_authority(tasks, session_id="owner-cli")
    action = parsed.consumer_action
    if action == "preview":
        result = intake.preview_consumer_import(parsed.consumer_plan, authority=authority)
    elif action == "body":
        result = intake.consumer_import_preview_body(
            parsed.preview_id, parsed.body_page, authority=authority
        )
    elif action == "commit":
        plan = cast(ConsumerImportPlan, parsed.consumer_plan)
        result = intake.commit_consumer_import(
            parsed.preview_id, plan.operation_id, plan, authority=authority
        )
    elif action == "status":
        result = intake.consumer_import_status(parsed.operation_id, authority=authority)
    elif action == "recover":
        result = intake.recover_consumer_import(parsed.operation_id, authority=authority)
    else:
        result = intake.halt_consumer_import(parsed.operation_id, authority=authority)
    # Body output is explicit, escaped inert JSON even in human mode.
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=None if json_output else 2))
    counts = cast(dict[str, int], result.get("counts", {}))
    mixed = result["status"] == "committed" and any(
        counts.get(key, 0) for key in ("conflict", "retracted", "halted")
    )
    return 1 if mixed or result["status"] in {"conflict", "halted"} else 0
