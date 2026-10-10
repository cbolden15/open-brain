"""Bounded JSON subprocess adapter; native hooks own their lifecycle and deadlines."""

from __future__ import annotations

import argparse
import json
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager

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


def run_harness_cli(parsed: argparse.Namespace) -> int:
    """Read bounded event JSON and emit metadata; never dump private exceptions."""
    action = parsed.action
    try:
        if getattr(parsed, "data_dir", None) is not None:
            raise ValueError
        seconds = 2.0 if action == "enqueue" else 3.0 if action in {"recall", "search"} else 60.0
        with _deadline(seconds):
            payload: object = None
            if action not in {"drain", "status"}:
                raw = sys.stdin.read(256 * 1024 + 1)
                if len(raw.encode("utf-8")) > 256 * 1024:
                    raise ValueError
                payload = json.loads(raw)
            result = run_harness(action, parsed.config, parsed.client, payload)
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
