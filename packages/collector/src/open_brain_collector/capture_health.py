"""Bounded private run receipts. Only allowlisted numerical metadata is retained."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

_MAX_RECEIPTS = 64
_MAX_SOURCES = 500
_METRICS = {
    "backlog_count",
    "oldest_pending_epoch",
    "pending_retries",
    "checkpoint_epoch",
    "input_tokens",
    "output_tokens",
    "cost_microusd",
}
_OUTCOMES = {"completed", "empty", "failed", "partial", "skipped", "deferred"}
_COVERAGE_CODES = {
    "partial_thread_coverage",
    "incomplete_search_coverage",
    "untracked_older_threads_unsupported",
    "unsupported_ai_exclusions",
    "source_candidate_identity_unsupported",
    "discovery_disabled",
    "discovery_disabled_or_exclusions_unverified",
    "discovery_retry_pending",
    "source_policy_excluded",
    "source_permission_revoked",
    "auth_required",
    "not_allowed",
    "rate_limited",
    "provider_unavailable",
    "provider_failed",
}
_COVERAGE_STATES = {"complete", "partial", "unsupported", "excluded", "unknown"}


def _coverage_status(codes: list[str], outcome: str) -> str:
    if outcome in {"failed", "partial", "deferred"} or any(
        code
        in {
            "partial_thread_coverage",
            "incomplete_search_coverage",
            "auth_required",
            "not_allowed",
            "rate_limited",
            "provider_unavailable",
            "provider_failed",
            "discovery_retry_pending",
        }
        for code in codes
    ):
        return "partial"
    if any("unsupported" in code for code in codes):
        return "unsupported"
    if any(
        code
        in {
            "source_policy_excluded",
            "source_permission_revoked",
            "discovery_disabled",
            "discovery_disabled_or_exclusions_unverified",
        }
        for code in codes
    ):
        return "excluded"
    return "unknown" if outcome == "skipped" else "complete"


def _overall_coverage(coverage: list[dict[str, object]]) -> str:
    states = {item["coverage_status"] for item in coverage}
    if not states or "unknown" in states:
        return "unknown" if "partial" not in states else "partial"
    return cast(str, next(iter(states))) if len(states) == 1 else "partial"


def _metrics(value: Mapping[str, object] | None) -> dict[str, int]:
    result: dict[str, int] = {}
    for key, item in (value or {}).items():
        if key not in _METRICS or type(item) is not int or not 0 <= item <= 10**15:
            raise LiveSourceError("invalid_health_metrics")
        result[key] = item
    return result


class CaptureHealthStore:
    """Receipt retention is independent of collection checkpoints and source text."""

    def __init__(self, root: Path, *, clock: Callable[[], int] = lambda: int(time.time())) -> None:
        self._store = PrivateJsonStore(root)
        self._clock = clock

    def _read(self) -> list[dict[str, object]]:
        value = self._store.read("runs.json")
        if value is None:
            return []
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "receipts"}
            or value["schema_version"] not in {1, 2}
            or not isinstance(value["receipts"], list)
            or len(value["receipts"]) > _MAX_RECEIPTS
        ):
            raise LiveSourceError("invalid_health_state")
        receipts = value["receipts"]
        if value["schema_version"] == 1:
            # Old receipts cannot establish coverage they never retained.
            if any(
                not isinstance(receipt, dict)
                or not isinstance(receipt.get("coverage"), list)
                or any(not isinstance(item, dict) for item in receipt["coverage"])
                for receipt in receipts
            ):
                raise LiveSourceError("invalid_health_state")
            receipts = [
                {
                    **receipt,
                    "coverage_status": "unknown",
                    "coverage": [
                        {**item, "coverage_status": "unknown", "coverage_codes": []}
                        for item in receipt["coverage"]
                    ],
                }
                if isinstance(receipt, dict)
                else receipt
                for receipt in receipts
            ]
        fields = {
            "completed_epoch",
            "outcome",
            "source_count",
            "failed_count",
            "partial_count",
            "captured_count",
            "duplicate_count",
            "coverage",
            "metrics",
            "coverage_status",
        }
        for receipt in receipts:
            if not isinstance(receipt, dict) or set(receipt) != fields:
                raise LiveSourceError("invalid_health_state")
            if receipt["outcome"] not in _OUTCOMES:
                raise LiveSourceError("invalid_health_state")
            if receipt["coverage_status"] not in _COVERAGE_STATES:
                raise LiveSourceError("invalid_health_state")
            for key in fields - {"outcome", "coverage", "metrics", "coverage_status"}:
                if type(receipt[key]) is not int or not 0 <= receipt[key] <= 10**15:
                    raise LiveSourceError("invalid_health_state")
            if not isinstance(receipt["coverage"], list) or len(receipt["coverage"]) > _MAX_SOURCES:
                raise LiveSourceError("invalid_health_state")
            for item in receipt["coverage"]:
                if (
                    not isinstance(item, dict)
                    or set(item)
                    != {"source_digest", "outcome", "coverage_status", "coverage_codes"}
                    or item["outcome"] not in _OUTCOMES
                    or type(item["source_digest"]) is not str
                    or len(item["source_digest"]) != 64
                    or item["coverage_status"] not in _COVERAGE_STATES
                    or not isinstance(item["coverage_codes"], list)
                    or len(item["coverage_codes"]) > len(_COVERAGE_CODES)
                    or any(
                        type(code) is not str or code not in _COVERAGE_CODES
                        for code in item["coverage_codes"]
                    )
                ):
                    raise LiveSourceError("invalid_health_state")
            if not isinstance(receipt["metrics"], dict):
                raise LiveSourceError("invalid_health_state")
            _metrics(receipt["metrics"])
        return cast(list[dict[str, object]], receipts)

    def record(
        self,
        results: list[dict[str, object]],
        *,
        failure_code: str | None = None,
        metrics: Mapping[str, object] | None = None,
        persist: bool = True,
        execution_outcome: str | None = None,
    ) -> dict[str, object]:
        if type(results) is not list or len(results) > _MAX_SOURCES:
            raise LiveSourceError("health_source_limit")
        now = self._clock()
        if type(now) is not int or now < 0:
            raise LiveSourceError("invalid_health_clock")
        coverage: list[dict[str, object]] = []
        captured = duplicates = failed = partial = 0
        reported_usage: dict[str, int] = {}
        for result in results:
            outcome = result.get("outcome", "partial")
            if outcome not in _OUTCOMES:
                outcome = "partial"
            if result.get("failure_code") is not None and outcome != "failed":
                outcome = "partial"
            if result.get("has_more") is True or result.get("next_cursor") is not None:
                outcome = "partial" if outcome != "failed" else "failed"
            for field in ("captured_count", "duplicate_count"):
                number = result.get(
                    field, result.get("captures", 0) if field == "captured_count" else 0
                )
                if type(number) is not int or not 0 <= number <= 10**12:
                    raise LiveSourceError("invalid_health_metrics")
                if field == "captured_count":
                    captured += number
                else:
                    duplicates += number
            usage = result.get("usage", {})
            if not isinstance(usage, dict):
                usage = {}
            for field in ("input_tokens", "output_tokens", "cost_microusd"):
                number = result.get(field, usage.get(field))
                if number is not None:
                    checked = _metrics({field: number})
                    reported_usage[field] = reported_usage.get(field, 0) + checked[field]
            source = result.get("source_id")
            digest = hashlib.sha256(
                str(source if type(source) is str else "unknown").encode()
            ).hexdigest()
            notices = result.get("notices", ())
            codes = (
                {code for code in notices if type(code) is str and code in _COVERAGE_CODES}
                if isinstance(notices, (list, tuple))
                else set()
            )
            failure = result.get("failure_code")
            if type(failure) is str and failure in _COVERAGE_CODES:
                codes.add(failure)
            public_codes = sorted(codes)
            coverage.append(
                {
                    "source_digest": digest,
                    "outcome": outcome,
                    "coverage_status": _coverage_status(public_codes, outcome),
                    "coverage_codes": public_codes,
                }
            )
            failed += int(outcome == "failed")
            partial += int(outcome in {"partial", "deferred"})
        overall = (
            "failed"
            if failure_code is not None
            else ("partial" if failed or partial else "completed" if results else "empty")
        )
        if failure_code is None and coverage and not failed and not partial:
            outcomes = {item["outcome"] for item in coverage}
            if outcomes == {"empty"}:
                overall = "empty"
            elif outcomes == {"skipped"}:
                overall = "skipped"
        if execution_outcome is not None:
            if execution_outcome not in _OUTCOMES:
                raise LiveSourceError("invalid_health_outcome")
            overall = execution_outcome
        receipt: dict[str, object] = {
            "completed_epoch": now,
            "outcome": overall,
            "source_count": len(results),
            "failed_count": failed,
            "partial_count": partial,
            "captured_count": captured,
            "duplicate_count": duplicates,
            "coverage": coverage,
            "coverage_status": _overall_coverage(coverage),
            "metrics": {**reported_usage, **_metrics(metrics)},
        }
        if not persist:
            return receipt
        with self._store.lock("runs.json"):
            receipts = self._read()
            receipts.append(receipt)
            self._store.write(
                "runs.json", {"schema_version": 2, "receipts": receipts[-_MAX_RECEIPTS:]}
            )
        return receipt

    def health(
        self,
        *,
        interval_seconds: int = 86_400,
        grace_seconds: int = 7_200,
        metrics: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if (
            type(interval_seconds) is not int
            or not 1 <= interval_seconds <= 31_536_000
            or type(grace_seconds) is not int
            or not 0 <= grace_seconds <= 86_400
        ):
            raise LiveSourceError("invalid_health_deadline")
        now = self._clock()
        if type(now) is not int or now < 0:
            raise LiveSourceError("invalid_health_clock")
        receipts = self._read()
        last = receipts[-1] if receipts else None
        measured = dict(cast(dict[str, int], last["metrics"])) if last else {}
        measured.update(_metrics(metrics))
        completed = cast(int, last["completed_epoch"]) if last else None
        deadline = completed + interval_seconds + grace_seconds if completed is not None else None
        pending = measured.get("oldest_pending_epoch")
        checkpoint = measured.get("checkpoint_epoch")
        return {
            "schema_version": 1,
            "status": "never_run"
            if last is None
            else ("overdue" if deadline is not None and now > deadline else last["outcome"]),
            "last_run": last,
            "receipt_count": len(receipts),
            "deadline_epoch": deadline,
            "heartbeat_age_seconds": max(0, now - completed) if completed is not None else None,
            "backlog_age_seconds": max(0, now - pending) if pending is not None else None,
            "checkpoint_lag_seconds": max(0, now - checkpoint) if checkpoint is not None else None,
            "metrics": measured,
            "coverage_status": last["coverage_status"] if last else "unknown",
        }
