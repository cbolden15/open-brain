"""`open-brain source guide`: owner-local guided Slack commands before root selection.

This build implements read-only `status`. The interactive `slack` flow and the
one-phase `advance` writes are registered and refuse as not implemented. No
command here selects a default Brain, bootstraps the engine or creates state.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from open_brain_engine.engine import PHASE1_STATE_SCHEMA_VERSION

from open_brain.services.local_runtime_session import RUNTIME_SESSION_VERSION
from open_brain.services.source_guide_adapter import (
    GuideAdapterError,
    collector_handshake,
    read_guide_metadata,
    resolve_collector_executable,
)
from open_brain.services.source_guide_display import render_health, safe_display

__all__ = ["GuidePaths", "guide_status", "render_status", "run_source_guide"]

_COMMON: Final = {
    "--data-dir": "brain",
    "--collector-state": "state",
    "--workflow-state": "journal",
    "--collector-executable": "executable",
}
_LEAVES: Final = frozenset({"slack", "status", "advance"})
_MESSAGES: Final = {
    "invalid_request": "The guide command is invalid. Pass each required absolute path once.",
    "unsafe_path": (
        "The collector executable or its directories are not owner-controlled."
    ),
    "collector_unavailable": (
        "The optional collector is not available. Install open-brain-collector separately "
        "and pass its absolute path with --collector-executable."
    ),
    "collector_incompatible": (
        "The selected collector does not support guide protocol 1 for this build "
        f"(schema {PHASE1_STATE_SCHEMA_VERSION}, runtime {RUNTIME_SESSION_VERSION}, "
        "Portable 16)."
    ),
    "collector_changed": "The collector executable changed. Select it again.",
    "state_uninitialized": (
        "No guided Slack workflow has selected a source yet. Nothing was created or changed."
    ),
    "journal_incompatible": "This build cannot read the existing workflow state file.",
    "operation_failed": "The collector refused the read-only metadata request.",
    "operation_unknown": "The collector did not return a valid result in time.",
}


class _UsageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GuidePaths:
    brain: Path
    state: Path
    journal: Path
    executable: Path


def _envelope(
    action: str,
    outcome: str,
    code: str | None,
    data: Mapping[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "workflow_id": None,
        "revision": None,
        "action": action,
        "outcome": outcome,
        "code": code,
        "intent": None,
        "data": None if data is None else dict(data),
        "next_actions": [],
        "retry_after_epoch": None,
    }


def _within(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _parse(arguments: Sequence[str]) -> tuple[str, GuidePaths, bool, Path | None]:
    if not arguments or arguments[0] not in _LEAVES:
        raise _UsageError
    leaf, values, json_output, decision = arguments[0], dict[str, Path](), False, None
    index = 1
    while index < len(arguments):
        token = arguments[index]
        if token == "--json" and not json_output and leaf in {"status", "advance"}:
            json_output, index = True, index + 1
            continue
        if token in _COMMON or (token == "--decision-file" and leaf == "advance"):
            if index + 1 >= len(arguments):
                raise _UsageError
            value = Path(arguments[index + 1])
            if not value.is_absolute() or ".." in value.parts:
                raise _UsageError
            key = _COMMON.get(token, "decision")
            if key in values or (key == "decision" and decision is not None):
                raise _UsageError
            if key == "decision":
                decision = value
            else:
                values[key] = Path(os.path.normpath(value))
            index += 2
            continue
        raise _UsageError
    if set(values) != set(_COMMON.values()) or (
        leaf == "advance" and (decision is None or not json_output)
    ):
        raise _UsageError
    paths = GuidePaths(**values)
    live = paths.state.parent / "live"
    stores = {"brain": paths.brain, "state": paths.state, "journal": paths.journal, "live": live}
    for left, left_path in stores.items():
        for right, right_path in stores.items():
            if left != right and {left, right} != {"state", "live"} and (
                _within(left_path, right_path)
            ):
                raise _UsageError
    if paths.executable in stores.values():
        raise _UsageError
    return leaf, paths, json_output, decision


def guide_status(paths: GuidePaths, *, selected_source_id: str | None) -> dict[str, object]:
    """Read-only status: executable trust, inert handshake, then collector metadata."""
    try:
        executable = resolve_collector_executable(paths.executable)
        collector_handshake(executable)
        if selected_source_id is None:
            return _envelope("status", "refused", "state_uninitialized")
        report = read_guide_metadata(
            executable, state=paths.state, brain=paths.brain, source_id=selected_source_id
        )
    except GuideAdapterError as error:
        return _envelope("status", "refused", error.code)
    return _envelope("status", "completed", None, {"kind": "health", "report": report})


def _selected_source(journal: Path) -> str | None:
    """The workflow journal names the selected source; this build has no journal reader."""
    try:
        os.lstat(journal)
    except FileNotFoundError:
        return None
    raise GuideAdapterError("journal_incompatible")


def render_status(result: Mapping[str, object]) -> str:
    data = result["data"]
    if result["outcome"] == "completed" and isinstance(data, Mapping):
        report = data["report"]
        if isinstance(report, Mapping):
            return render_health(report)
    code = str(result["code"])
    return safe_display(_MESSAGES.get(code, "The guide could not complete: " + code)) + "\n"


def _emit(result: Mapping[str, object], *, json_output: bool) -> int:
    outcome = result["outcome"]
    if json_output:
        print(json.dumps(dict(result), sort_keys=True, separators=(",", ":"), allow_nan=False))
    elif outcome == "completed":
        sys.stdout.write(render_status(result))
    else:
        sys.stderr.write(render_status(result))
    if outcome in {"completed", "cancelled"}:
        return 0
    if outcome in {"needs_confirmation", "partial"}:
        return 3
    return 2 if result["code"] == "invalid_request" else 78


def run_source_guide(arguments: Sequence[str]) -> int:
    """Dispatch `open-brain source guide LEAF ...` without root selection or bootstrap."""
    leaf = arguments[0] if arguments else "status"
    action = leaf if leaf in _LEAVES - {"slack"} else "status"
    try:
        leaf, paths, json_output, _decision = _parse(arguments)
    except _UsageError:
        return _emit(
            _envelope(action, "refused", "invalid_request"), json_output="--json" in arguments
        )
    if leaf != "status":
        if not json_output:
            sys.stderr.write(f"source guide {leaf} is not implemented in this build.\n")
            return 78
        return _emit(_envelope(leaf, "refused", "operation_failed"), json_output=True)
    try:
        selected = _selected_source(paths.journal)
    except GuideAdapterError as error:
        return _emit(_envelope("status", "refused", error.code), json_output=json_output)
    return _emit(guide_status(paths, selected_source_id=selected), json_output=json_output)
