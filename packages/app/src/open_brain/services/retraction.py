"""Owner CLI grammar and body-free retraction output; no external tool grant."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable

from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.capture_lifecycle_contracts import (
    LifecycleError,
    validate_capture_id,
    validate_session_id,
)
from open_brain_engine.engine.owner_retraction_contracts import (
    ERROR_CODES,
    RetractionError,
    operation_identifier,
    preview_identifier,
)

from .t03_adapters import owner_authority


def retraction_grammar_requested(arguments: tuple[str, ...]) -> bool:
    """Recognize the command after root-local options without echoing arguments."""
    index = 0
    while index < len(arguments):
        word = arguments[index]
        if word == "--json" or word.startswith("--data-dir="):
            index += 1
        elif word == "--data-dir":
            index += 2
        else:
            return word == "retract"
    return False


def add_retraction_parsers[Parser: argparse.ArgumentParser](
    subparsers: argparse._SubParsersAction[Parser],
    add_local_options: Callable[[argparse.ArgumentParser], None],
) -> None:
    parent = subparsers.add_parser(
        "retract", help="Preview and commit owner retraction; retain history."
    )
    add_local_options(parent)
    children = parent.add_subparsers(dest="retraction_action", required=True)
    preview = children.add_parser("preview", help="Inspect exact impact before retraction.")
    add_local_options(preview)
    target = preview.add_mutually_exclusive_group(required=True)
    target.add_argument("--capture", dest="capture_id")
    target.add_argument("--session", dest="capture_session_id")
    target.add_argument("--preview-id")
    preview.add_argument("--item-page", type=int)
    commit = children.add_parser("commit", help="Retract exactly the approved preview.")
    add_local_options(commit)
    commit.add_argument("--preview-id", required=True)
    commit.add_argument("--operation-id", required=True)
    status = children.add_parser("status", help="Read a retained retraction receipt.")
    add_local_options(status)
    status.add_argument("--operation-id", required=True)


def validate_retraction_arguments(parsed: argparse.Namespace) -> None:
    try:
        if parsed.retraction_action == "preview":
            if parsed.preview_id is not None:
                preview_identifier(parsed.preview_id)
                if type(parsed.item_page) is not int or parsed.item_page < 0:
                    raise RetractionError("invalid_arguments")
            else:
                if parsed.item_page is not None:
                    raise RetractionError("invalid_arguments")
                if parsed.capture_id is not None:
                    validate_capture_id(parsed.capture_id)
                else:
                    validate_session_id(parsed.capture_session_id)
        else:
            operation_identifier(parsed.operation_id)
            if parsed.retraction_action == "commit":
                preview_identifier(parsed.preview_id)
    except LifecycleError:
        raise RetractionError("invalid_arguments") from None


def write_retraction_failure(code: str, *, json_output: bool) -> int:
    if code not in ERROR_CODES:
        code = "local_operation_failed"
    payload = {
        "dto_version": 1,
        "status": "failed",
        "error": {
            "code": code,
            "retryable": code == "local_operation_busy",
            "item_token": None,
        },
    }
    if json_output:
        print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
    else:
        print(f"Retraction refused: {code}.", file=sys.stderr)
    if code == "invalid_arguments":
        return 2
    return 75 if code == "local_operation_busy" else 78


def run_retraction_command(
    parsed: argparse.Namespace, tasks: EngineTaskSet, *, json_output: bool
) -> int:
    lifecycle = tasks.lifecycle
    if lifecycle is None:
        raise RetractionError("operation_pending")
    authority = owner_authority(tasks, session_id="owner-cli")
    if parsed.retraction_action == "preview":
        result = (
            lifecycle.preview_retraction_page(
                parsed.preview_id, item_page=parsed.item_page, authority=authority
            )
            if parsed.preview_id is not None
            else lifecycle.preview_retraction(
                capture_id=parsed.capture_id,
                session_id=parsed.capture_session_id,
                authority=authority,
            )
        )
    elif parsed.retraction_action == "commit":
        result = lifecycle.commit_retraction(
            parsed.preview_id, parsed.operation_id, authority=authority
        )
    else:
        result = lifecycle.retraction_status(parsed.operation_id, authority=authority)
    print(json.dumps(result, ensure_ascii=True, sort_keys=True, indent=None if json_output else 2))
    return 0
