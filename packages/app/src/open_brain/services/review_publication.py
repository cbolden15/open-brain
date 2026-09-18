"""Shared bounded review and publication operations for owner CLI and granted MCP tools."""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Mapping
from dataclasses import asdict
from hashlib import sha256
from typing import cast

from open_brain_engine.engine import (
    DecisionOutcome,
    PatchDraft,
    PatchOperation,
    ProposalDraft,
    ProposalRecord,
    ReviewProposal,
    ReviewTask,
)

DEFAULT_REVIEW_PAGE_LIMIT = 50
MAX_REVIEW_PAGE_LIMIT = 100
MAX_REVIEW_PAGE_OFFSET = 1_000_000
MAX_REVIEW_SOURCES = 32
MAX_REVIEW_TITLE_CHARACTERS = 200
MAX_REVIEW_MARKDOWN_BYTES = 64 * 1024
MAX_REVIEW_KEY_CHARACTERS = 128
MAX_REVIEW_LIST_JSON_BYTES = 250_000
MAX_REVIEW_MUTATION_RESPONSE_BYTES = 4096

_UUID4 = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STATUSES = {"pending", "approved", "rejected", "edited"}
_REQUIRED = {
    "propose": {"capture_ids"},
    "list": set(),
    "show": {"proposal_id"},
    "approve": {"proposal_id", "review_token"},
    "reject": {"proposal_id", "review_token"},
    "edit_and_approve": {"proposal_id", "review_token"},
}
_OPTIONAL = {
    "propose": {"target_page_id", "idempotency_key", "title", "markdown", "patch"},
    "list": {"capture_id", "space_id", "status", "limit", "offset"},
    "show": set(),
    "approve": {"idempotency_key"},
    "reject": {"idempotency_key"},
    "edit_and_approve": {"idempotency_key", "markdown", "replacement_body"},
}


class ReviewPublicationError(ValueError):
    """One bounded public failure without draft or source content."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def validate_review_arguments(operation: str, arguments: Mapping[str, object]) -> None:
    required = _REQUIRED.get(operation)
    optional = _OPTIONAL.get(operation)
    if required is None or optional is None:
        raise ReviewPublicationError("invalid_arguments")
    keys = set(arguments)
    if required - keys or keys - (required | optional):
        raise ReviewPublicationError("invalid_arguments")

    if operation == "propose":
        full_page = {"title", "markdown"} <= keys
        patch = "patch" in keys
        if full_page == patch or (patch and "target_page_id" in keys):
            raise ReviewPublicationError("invalid_arguments")
    if operation == "edit_and_approve" and (("markdown" in keys) == ("replacement_body" in keys)):
        raise ReviewPublicationError("invalid_arguments")

    for field, value in arguments.items():
        if field == "capture_ids":
            if (
                not isinstance(value, list)
                or not 1 <= len(value) <= MAX_REVIEW_SOURCES
                or any(not _identifier(item, "capture") for item in value)
                or len(set(cast(list[str], value))) != len(value)
            ):
                raise ReviewPublicationError("invalid_arguments")
        elif field in {"capture_id", "proposal_id", "space_id", "target_page_id"}:
            prefix = "page" if field == "target_page_id" else field.removesuffix("_id")
            if not _identifier(value, prefix):
                raise ReviewPublicationError("invalid_arguments")
        elif field == "review_token":
            if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
                raise ReviewPublicationError("invalid_arguments")
        elif field == "status":
            if not isinstance(value, str) or value not in _STATUSES:
                raise ReviewPublicationError("invalid_arguments")
        elif field in {"limit", "offset"}:
            minimum, maximum = (
                (1, MAX_REVIEW_PAGE_LIMIT) if field == "limit" else (0, MAX_REVIEW_PAGE_OFFSET)
            )
            if type(value) is not int or not minimum <= value <= maximum:
                raise ReviewPublicationError("invalid_arguments")
        elif field == "title":
            if not _valid_text(value, maximum_characters=MAX_REVIEW_TITLE_CHARACTERS):
                raise ReviewPublicationError("invalid_arguments")
        elif field == "markdown" or field == "replacement_body":
            if not _valid_markdown(value):
                raise ReviewPublicationError("invalid_arguments")
        elif field == "patch":
            _patch_draft(value)
        elif field == "idempotency_key" and not _valid_text(
            value, maximum_characters=MAX_REVIEW_KEY_CHARACTERS
        ):
            raise ReviewPublicationError("invalid_arguments")


class ReviewPublicationService:
    def __init__(self, task: ReviewTask) -> None:
        self._task = task

    def propose(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_review_arguments("propose", arguments)
        retry_key, delivery_id = _delivery("propose", arguments.get("idempotency_key"))
        draft = (
            _patch_draft(arguments["patch"])
            if "patch" in arguments
            else ProposalDraft(
                title=unicodedata.normalize("NFC", cast(str, arguments["title"])),
                markdown=cast(str, arguments["markdown"]),
            )
        )
        try:
            records = self._task.propose(
                tuple(cast(list[str], arguments["capture_ids"])),
                (draft,),
                delivery_id=delivery_id,
                target_page_id=(
                    draft.target_page_id
                    if isinstance(draft, PatchDraft)
                    else cast(str | None, arguments.get("target_page_id"))
                ),
            )
        except ValueError as error:
            raise _public_error(error) from None
        if len(records) != 1:
            raise ReviewPublicationError("operation_failed")
        record = records[0]
        return {
            "effective_idempotency_key": retry_key,
            "proposal_id": record.proposal_id,
            "page_id": record.page_id,
            "space_id": record.space_id,
            "target_page_id": record.target_page_id,
            "operation": record.operation,
            "proposal_status": record.status,
            "status": "proposed",
        }

    def list(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_review_arguments("list", arguments)
        limit = cast(int, arguments.get("limit", DEFAULT_REVIEW_PAGE_LIMIT))
        offset = cast(int, arguments.get("offset", 0))
        try:
            records = self._task.list(
                capture_id=cast(str | None, arguments.get("capture_id")),
                status=cast(str | None, arguments.get("status")),
                space_id=cast(str | None, arguments.get("space_id")),
                limit=limit,
                offset=offset,
            )
            has_more = False
            if len(records) == limit:
                has_more = bool(
                    self._task.list(
                        capture_id=cast(str | None, arguments.get("capture_id")),
                        status=cast(str | None, arguments.get("status")),
                        space_id=cast(str | None, arguments.get("space_id")),
                        limit=1,
                        offset=offset + len(records),
                    )
                )
        except ValueError as error:
            raise _public_error(error) from None
        rows = [_proposal_summary(record) for record in records]
        return _page_result(rows, limit, offset, has_more=has_more)

    def show(self, arguments: Mapping[str, object]) -> dict[str, object]:
        validate_review_arguments("show", arguments)
        try:
            proposal = self._task.show(cast(str, arguments["proposal_id"]))
        except ValueError as error:
            raise _public_error(error) from None
        result = asdict(proposal)
        proposal_status = result.pop("status")
        result["review_token"] = result.pop("review_digest")
        if proposal.patch is None:
            result.pop("patch")
            result.pop("patch_diff")
        return {**result, "proposal_status": proposal_status, "status": "shown"}

    def approve(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._decide("approve", arguments, DecisionOutcome.APPROVED)

    def reject(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._decide("reject", arguments, DecisionOutcome.REJECTED)

    def edit_and_approve(self, arguments: Mapping[str, object]) -> dict[str, object]:
        return self._decide("edit_and_approve", arguments, DecisionOutcome.EDITED)

    def _decide(
        self,
        operation: str,
        arguments: Mapping[str, object],
        outcome: DecisionOutcome,
    ) -> dict[str, object]:
        validate_review_arguments(operation, arguments)
        retry_key, delivery_id = _delivery(operation, arguments.get("idempotency_key"))
        if outcome is DecisionOutcome.EDITED:
            inspection = self._task.show(cast(str, arguments["proposal_id"]))
            if (
                isinstance(inspection, ReviewProposal)
                and inspection.patch is not None
                and "replacement_body" not in arguments
            ):
                raise ReviewPublicationError("invalid_arguments")
        try:
            receipt = self._task.decide(
                cast(str, arguments["proposal_id"]),
                outcome,
                delivery_id=delivery_id,
                edited_markdown=cast(
                    str | None,
                    arguments.get("replacement_body", arguments.get("markdown")),
                ),
                expected_review_digest=cast(str, arguments["review_token"]),
            )
        except ValueError as error:
            raise _public_error(error) from None
        return {
            "status": outcome.value,
            "effective_idempotency_key": retry_key,
            **asdict(receipt),
            "outcome": receipt.outcome.value,
        }


def _identifier(value: object, prefix: str) -> bool:
    return isinstance(value, str) and re.fullmatch(prefix + "_" + _UUID4, value) is not None


def _valid_text(value: object, *, maximum_characters: int) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum_characters
        or not value.strip()
        or "\x00" in value
        or any(
            unicodedata.category(character) == "Cc" and character not in {"\t", "\n", "\r"}
            for character in value
        )
    ):
        return False
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return True


def _valid_markdown(value: object) -> bool:
    if not _valid_text(value, maximum_characters=MAX_REVIEW_MARKDOWN_BYTES):
        return False
    return len(cast(str, value).encode("utf-8")) <= MAX_REVIEW_MARKDOWN_BYTES


def _delivery(operation: str, supplied: object) -> tuple[str, str]:
    key = cast(str, supplied) if supplied is not None else "review-" + str(uuid.uuid4())
    digest = sha256(key.encode("utf-8")).hexdigest()
    return key, f"review.{operation}.{digest}"


def _proposal_summary(record: ProposalRecord) -> dict[str, object]:
    return {
        "proposal_id": record.proposal_id,
        "status": record.status,
        "title": record.title,
        "space_id": record.space_id,
        "operation": record.operation,
        "capture_ids": list(record.capture_ids),
        "selected_capture_ids": list(record.selected_capture_ids),
        "page_id": record.page_id,
        "target_page_id": record.target_page_id,
        "terminal_decision_id": record.terminal_decision_id,
    }


def _page_result(
    rows: list[dict[str, object]], limit: int, offset: int, *, has_more: bool
) -> dict[str, object]:
    selected: list[dict[str, object]] = []
    size = 1024
    for row in rows[:limit]:
        row_size = len(json.dumps(row, ensure_ascii=True, separators=(",", ":"))) + 1
        if size + row_size > MAX_REVIEW_LIST_JSON_BYTES:
            break
        selected.append(row)
        size += row_size
    more = has_more or len(rows) > len(selected)
    following = offset + len(selected)
    capped = more and following > MAX_REVIEW_PAGE_OFFSET
    result: dict[str, object] = {
        "status": "listed",
        "proposals": selected,
        "offset": offset,
        "next_offset": following if more and not capped else None,
    }
    if capped:
        result["offset_limit_reached"] = True
    return result


def _public_error(error: ValueError) -> ReviewPublicationError:
    code = {
        "conflicting delivery": "idempotency_conflict",
        "unknown proposal": "unknown_proposal",
        "unknown capture": "unknown_capture",
        "unknown page": "unknown_page",
        "unknown target page": "unknown_page",
        "duplicate proposal source": "duplicate_source",
        "duplicate review source": "duplicate_source",
        "mixed proposal spaces": "mixed_source_spaces",
        "review sources must share a space": "mixed_source_spaces",
        "review sources and target must share a space": "mixed_source_spaces",
        "page proposal requires routed capture": "source_unrouted",
        "proposal already has a terminal decision": "terminal_decision",
        "review digest mismatch": "review_conflict",
        "review digest is required": "review_conflict",
        "review digest conflict": "review_conflict",
        "stale review state": "review_conflict",
        "review source state conflict": "review_conflict",
        "canonical page revision conflict": "review_conflict",
        "review proposal exceeds inspection limit": "response_too_large",
    }.get(str(error), "operation_failed")
    return ReviewPublicationError(code)


def _patch_draft(value: object) -> PatchDraft:
    if not isinstance(value, Mapping) or set(value) != {
        "target_page_id",
        "expected_page_sha256",
        "operations",
    }:
        raise ReviewPublicationError("invalid_arguments")
    raw_operations = value["operations"]
    if not isinstance(raw_operations, list):
        raise ReviewPublicationError("invalid_arguments")
    try:
        operations = tuple(
            PatchOperation(
                start_byte=entry["start_byte"],
                end_byte=entry["end_byte"],
                replacement=entry["replacement"],
            )
            for entry in raw_operations
            if isinstance(entry, Mapping)
        )
        if len(operations) != len(raw_operations):
            raise ValueError
        return PatchDraft(
            target_page_id=cast(str, value["target_page_id"]),
            expected_page_sha256=cast(str, value["expected_page_sha256"]),
            operations=operations,
        )
    except KeyError, TypeError, ValueError:
        raise ReviewPublicationError("invalid_arguments") from None
