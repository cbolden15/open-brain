"""Bounded JSON subprocess adapter; native hooks own their lifecycle and deadlines."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from open_brain_engine.core.models import PrivacyTier

from .harness_bridge import run_harness


class HarnessDeadlineExceeded(BaseException):
    """Process deadline control flow; ordinary error handlers cannot consume it."""


@contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    def expired(_signal: int, _frame: object) -> None:
        raise HarnessDeadlineExceeded

    previous = signal.getsignal(signal.SIGALRM)
    old_timer = signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, *old_timer)


def run_harness_cli(
    parsed: argparse.Namespace, *, environment: Mapping[str, object] | None = None
) -> int:
    """Read bounded event JSON and emit metadata; never dump private exceptions."""
    action = parsed.action
    if action == "setup":
        from .agent_setup import AgentSetupFailure
        from .harness_setup import apply_harness_setup, preview_harness_setup

        try:
            if parsed.runtime is None or parsed.node is None or parsed.mode is None:
                raise ValueError
            arguments = {
                "client": parsed.client,
                "action": parsed.setup_action,
                "runtime_path": parsed.runtime,
                "config_path": parsed.config,
                "environment": os.environ if environment is None else environment,
                "node_path": parsed.node,
            }
            result = (
                apply_harness_setup(**arguments, preview_id=parsed.preview_id)
                if parsed.mode == "apply" else preview_harness_setup(**arguments)
            )
        except AgentSetupFailure as error:
            result = {"status": "refused", "error_code": error.code}
        except Exception:
            result = {"status": "refused", "error_code": "setup_unavailable"}
        print(json.dumps(result, sort_keys=True, ensure_ascii=False))
        return 0 if result.get("status") in {"preview", "configured", "removed", "unchanged"} else 1
    if action == "mcp":
        from .harness_mcp import HarnessMcpAdapter
        from .mcp_protocol import serve_stdio_mcp

        serve_stdio_mcp(
            HarnessMcpAdapter(parsed.config, parsed.client, Path.cwd()),
            input_stream=sys.stdin.buffer,
            output_stream=sys.stdout.buffer,
        )
        return 0
    try:
        if getattr(parsed, "data_dir", None) is not None:
            raise ValueError
        seconds = 60.0
        if action == "enqueue":
            seconds = 2.0
        elif action in {"recall", "search", "source"}:
            seconds = 3.0
        with _deadline(seconds):
            payload: object = None
            if action not in {"drain", "status"}:
                maximum = 9 * 1024 * 1024 if action in {"enqueue", "capture"} else 256 * 1024
                raw = sys.stdin.read(maximum + 1)
                if len(raw.encode("utf-8")) > maximum:
                    raise ValueError
                payload = json.loads(raw)
            provider_id = getattr(parsed, "provider_id", None)
            read_tier = getattr(parsed, "read_tier", None)
            if (provider_id is not None or read_tier is not None) and (
                parsed.client != "hermes" or action not in {"recall", "search", "source"}
            ):
                raise ValueError
            result = run_harness(
                action, parsed.config, parsed.client, payload, provider_id=provider_id,
                read_tiers=None if read_tier is None else frozenset({PrivacyTier(read_tier)}),
            )
    except HarnessDeadlineExceeded:
        result = {"status": "unavailable", "error_code": "bridge_timeout"}
    except Exception:
        result = {"status": "refused", "error_code": "invalid_request"}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    if action == "status" and result.get("complete") is True and result.get("error_count") == 0:
        return 0
    return (
        0
        if result.get("status")
        in {"ok", "queued", "shown", "acknowledged", "success_empty", "delivered"}
        else 1
    )


def add_harness_arguments(parser: argparse.ArgumentParser) -> None:
    """Shared arguments for the core CLI and optional fast foreground runtime."""
    parser.add_argument(
        "action",
        choices=(
            "enqueue", "capture", "drain", "status", "recall", "search", "source", "mcp", "setup",
        ),
    )
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--client", required=True, choices=("codex", "claude-code", "pi", "hermes"))
    parser.add_argument("--provider-id")
    parser.add_argument("--read-tier", choices=("personal", "work"))
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--mode", choices=("preview", "apply"))
    parser.add_argument("--setup-action", choices=("install", "uninstall"), default="install")
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--node", type=Path)
    parser.add_argument("--preview-id")
