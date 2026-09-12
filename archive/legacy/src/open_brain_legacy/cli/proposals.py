"""Deterministic proposal CLI adapters over typed review services."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from open_brain_engine.review.models import ReviewAggregate

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.cli.review import ReviewDecisionService, decide_review, list_reviews


@dataclass(frozen=True, slots=True)
class ProposalCliResult:
    """A deterministic response that excludes proposal text and reasons."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the redacted response for automation callers."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


def list_proposals(*, proposals: Iterable[object]) -> ProposalCliResult:
    """List current typed proposals or explicitly defer old mapping-shaped records."""
    current: list[ReviewAggregate] = []
    try:
        for proposal in proposals:
            if isinstance(proposal, Mapping):
                return _migration_required()
            if not isinstance(proposal, ReviewAggregate):
                raise ValueError("invalid proposal")
            current.append(proposal)
    except Exception:
        return _failed("proposal_operation_failed")

    listed = list_reviews(reviews=current)
    if listed.exit_code is not ExitCode.SUCCESS:
        return _failed("proposal_operation_failed")
    return ProposalCliResult(
        ExitCode.SUCCESS,
        {
            "command": "proposals",
            "proposals": listed.envelope["reviews"],
            "status": "listed",
        },
    )


def resolve_proposal(
    *,
    service: ReviewDecisionService,
    proposal: object,
    action: str,
    decision_id: str,
    reason: str,
    occurred_at: datetime,
    dry_run: bool = False,
) -> ProposalCliResult:
    """Resolve one typed review proposal through the existing decision service."""
    if isinstance(proposal, Mapping):
        return _migration_required()
    if not isinstance(proposal, ReviewAggregate):
        return _failed("proposal_operation_failed")

    decided = decide_review(
        service=service,
        review_id=proposal.proposal.review_id,
        action=action,
        decision_id=decision_id,
        reason=reason,
        occurred_at=occurred_at,
        dry_run=dry_run,
    )
    envelope = dict(decided.envelope)
    envelope["command"] = "proposals"
    if decided.exit_code is ExitCode.SUCCESS and not dry_run:
        envelope["status"] = "resolved"
    return ProposalCliResult(decided.exit_code, envelope)


def _migration_required() -> ProposalCliResult:
    return ProposalCliResult(
        ExitCode.DEFERRED,
        {
            "command": "proposals",
            "error": {
                "code": "proposal_format_migration_required",
                "message": "old proposal format detected; migrate before retrying",
                "redacted": True,
            },
            "status": "migration_required",
        },
    )


def _failed(code: str) -> ProposalCliResult:
    return ProposalCliResult(
        ExitCode.FAILURE,
        {"command": "proposals", "error": redacted_error(code), "status": "failed"},
    )
