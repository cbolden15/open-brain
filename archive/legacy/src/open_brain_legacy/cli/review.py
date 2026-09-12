"""Read-only, redacted CLI adapters for typed review aggregates."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from open_brain_engine.core.ids import ReviewId, validate_identifier
from open_brain_engine.core.ports import Clock
from open_brain_engine.review.models import (
    Actor,
    ActorKind,
    ReviewAggregate,
    ReviewDecisionCommand,
    ReviewDecisionResult,
    ReviewState,
)

from open_brain_legacy._compat.open_brain.cli._common import ExitCode, redacted_error
from open_brain_legacy.review.maintenance import (
    ArchiveResult,
    CurationTarget,
    CurationTaxonomy,
    ReviewTargetEdit,
)

_ACTION_STATES = {
    "apply": ReviewState.APPLIED,
    "reject": ReviewState.REJECTED,
}


class ReviewReader(Protocol):
    """Read a typed aggregate without exposing storage details to the CLI."""

    def get(self, review_id: ReviewId) -> ReviewAggregate | None: ...


class ReviewDecisionService(Protocol):
    """Apply a typed decision through the lock-safe application service."""

    def decide(
        self, review_id: ReviewId, command: ReviewDecisionCommand
    ) -> ReviewDecisionResult: ...


class ReviewMaintenanceStore(Protocol):
    def edit_curation_target(
        self,
        review_id: ReviewId,
        command: ReviewTargetEdit,
        *,
        taxonomy: CurationTaxonomy,
        dry_run: bool,
    ) -> CurationTarget: ...

    def archive_reviews(
        self,
        *,
        before: str,
        occurred_at: datetime,
        dry_run: bool,
    ) -> ArchiveResult: ...


@dataclass(frozen=True, slots=True)
class ReviewCliResult:
    """A deterministic public result that omits review text and reasons."""

    exit_code: ExitCode
    envelope: dict[str, object]

    def to_json(self) -> str:
        """Serialize the redacted response for automation callers."""
        return json.dumps(self.envelope, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ReviewCommandAdapter:
    maintenance: ReviewMaintenanceStore
    taxonomy: CurationTaxonomy
    clock: Clock

    def dispatch(self, argv: tuple[str, ...]) -> ReviewCliResult:
        try:
            positional, options, dry_run = _request(argv)
        except ValueError:
            return _invalid()
        if not positional:
            return _invalid()
        action = positional[0]
        if action == "edit":
            return self._edit(positional, options, dry_run=dry_run)
        if action == "archive":
            return self._archive(positional, options, dry_run=dry_run)
        return _invalid()

    def _edit(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        *,
        dry_run: bool,
    ) -> ReviewCliResult:
        if (
            len(positional) != 2
            or not {"tier", "category", "slug"}.issubset(options)
            or set(options) - {"tier", "category", "slug", "title", "class"}
        ):
            return _invalid()
        try:
            command = ReviewTargetEdit.create(
                tier=options["tier"],
                category=options["category"],
                slug=options["slug"],
                title=options.get("title"),
                classification_class=options.get("class"),
                occurred_at=self.clock.now(),
                actor=Actor(ActorKind.OWNER, "cli-owner"),
            )
            edited = self.maintenance.edit_curation_target(
                _review_id(positional[1]),
                command,
                taxonomy=self.taxonomy,
                dry_run=dry_run,
            )
            if not isinstance(edited, CurationTarget):
                raise ValueError("invalid curation edit result")
        except Exception:
            return _failed("review_edit_failed")
        return ReviewCliResult(
            ExitCode.SUCCESS,
            {
                "action": "edit",
                "command": "review",
                "dry_run": dry_run,
                "state": "open",
                "status": "planned" if dry_run else "edited",
                "tier": edited.tier.value,
            },
        )

    def _archive(
        self,
        positional: tuple[str, ...],
        options: dict[str, str],
        *,
        dry_run: bool,
    ) -> ReviewCliResult:
        if len(positional) != 1 or set(options) != {"before"}:
            return _invalid()
        try:
            result = self.maintenance.archive_reviews(
                before=options["before"],
                occurred_at=self.clock.now(),
                dry_run=dry_run,
            )
            if not isinstance(result, ArchiveResult):
                raise ValueError("invalid review archive result")
        except Exception:
            return _failed("review_archive_failed")
        return ReviewCliResult(
            ExitCode.SUCCESS,
            {
                "action": "archive",
                "archived": result.archived,
                "command": "review",
                "dry_run": dry_run,
                "status": "planned" if dry_run else "archived",
            },
        )


def list_reviews(
    *, reviews: Iterable[ReviewAggregate], state: ReviewState | str | None = None
) -> ReviewCliResult:
    """Return redacted typed reviews in stable identifier order."""
    try:
        selected_state = _state(state)
        summaries = tuple(_summary(review) for review in reviews)
    except Exception:
        return _failed("review_operation_failed")
    if selected_state is not None:
        summaries = tuple(
            summary for summary in summaries if summary["state"] == selected_state.value
        )
    return ReviewCliResult(
        ExitCode.SUCCESS,
        {
            "command": "review",
            "reviews": sorted(summaries, key=lambda summary: str(summary["review_id"])),
            "state": None if selected_state is None else selected_state.value,
            "status": "listed",
        },
    )


def show_review(*, reader: ReviewReader, review_id: ReviewId | str) -> ReviewCliResult:
    """Return one redacted review aggregate without changing its state."""
    try:
        aggregate = reader.get(_review_id(review_id))
        if aggregate is None:
            return _failed("review_not_found")
        summary = _summary(aggregate)
    except Exception:
        return _failed("review_operation_failed")
    return ReviewCliResult(
        ExitCode.SUCCESS,
        {"command": "review", "review": summary, "status": "shown"},
    )


def preview_review(*, reader: ReviewReader, review_id: ReviewId | str) -> ReviewCliResult:
    """Return the same redacted data as show in an explicitly non-mutating mode."""
    shown = show_review(reader=reader, review_id=review_id)
    if shown.exit_code is not ExitCode.SUCCESS:
        return shown
    return ReviewCliResult(
        ExitCode.SUCCESS,
        {
            "command": "review",
            "dry_run": True,
            "review": shown.envelope["review"],
            "status": "previewed",
        },
    )


def decide_review(
    *,
    service: ReviewDecisionService,
    review_id: ReviewId | str,
    action: str,
    decision_id: str,
    reason: str,
    occurred_at: datetime,
    dry_run: bool = False,
) -> ReviewCliResult:
    """Validate an owner decision and delegate its transition to the review service."""
    try:
        if not isinstance(dry_run, bool):
            raise ValueError("invalid review decision")
        validated_id = _review_id(review_id)
        target_state = _ACTION_STATES[action]
        command = ReviewDecisionCommand.create(
            decision_id=decision_id,
            target_state=target_state,
            reason=reason,
            occurred_at=occurred_at,
            actor=Actor(ActorKind.OWNER, "cli-owner"),
        )
    except Exception:
        return _failed("review_decision_failed")
    envelope: dict[str, object] = {
        "action": action,
        "command": "review",
        "dry_run": dry_run,
        "review_id": str(validated_id),
        "state": command.target_state.value,
        "status": "planned" if dry_run else "decided",
    }
    if dry_run:
        return ReviewCliResult(ExitCode.SUCCESS, envelope)
    try:
        result = service.decide(validated_id, command)
        if (
            not isinstance(result, ReviewDecisionResult)
            or result.aggregate.proposal.review_id != validated_id
            or result.aggregate.proposal.state is not command.target_state
        ):
            raise ValueError("invalid review decision result")
    except Exception:
        return _failed("review_decision_failed")
    envelope["idempotent"] = result.idempotent
    return ReviewCliResult(ExitCode.SUCCESS, envelope)


def _state(value: ReviewState | str | None) -> ReviewState | None:
    if value is None:
        return None
    return ReviewState(value)


def _review_id(value: ReviewId | str) -> ReviewId:
    return ReviewId(validate_identifier(str(value), prefix="review_"))


def _summary(aggregate: ReviewAggregate) -> dict[str, str]:
    if not isinstance(aggregate, ReviewAggregate):
        raise ValueError("invalid review aggregate")
    proposal = aggregate.proposal
    return {
        "capture_id": str(proposal.capture_id),
        "privacy_tier": proposal.privacy_tier.value,
        "proposed_intent": proposal.proposed_intent.value,
        "review_id": str(proposal.review_id),
        "state": proposal.state.value,
    }


def _failed(code: str) -> ReviewCliResult:
    return ReviewCliResult(
        ExitCode.FAILURE,
        {"command": "review", "error": redacted_error(code), "status": "failed"},
    )


def _invalid() -> ReviewCliResult:
    return ReviewCliResult(
        ExitCode.USAGE,
        {
            "command": "review",
            "error": redacted_error("invalid_review_request"),
            "status": "invalid",
        },
    )


def _request(argv: tuple[str, ...]) -> tuple[tuple[str, ...], dict[str, str], bool]:
    if not isinstance(argv, tuple) or any(
        not isinstance(argument, str) or not argument or "\x00" in argument
        for argument in argv
    ):
        raise ValueError("invalid review request")
    positional: list[str] = []
    options: dict[str, str] = {}
    dry_run = False
    json_seen = False
    for argument in argv:
        if argument == "--dry-run":
            if dry_run:
                raise ValueError("invalid review request")
            dry_run = True
            continue
        if argument == "--json":
            if json_seen:
                raise ValueError("invalid review request")
            json_seen = True
            continue
        if argument.startswith("--"):
            key, marker, value = argument[2:].partition("=")
            if not marker or not key or not value or key in options:
                raise ValueError("invalid review request")
            options[key] = value
            continue
        if argument.startswith("-"):
            raise ValueError("invalid review request")
        positional.append(argument)
    return tuple(positional), options, dry_run
