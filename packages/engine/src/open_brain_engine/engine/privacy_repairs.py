"""Owner-local privacy repairs: closed requests, append-only ledger, and overlays.

One owner-local engine task appends a repair to the schema-nine ledger inside the
existing writer fence and one transaction, then refreshes exactly the affected
search projections and advances the retrieval generation once. Retained evidence,
invalid markers, and ledger history are never rewritten: repairs resolve forward
through the deterministic effective resolvers in this module.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING
from uuid import uuid4

from open_brain_engine.core.access_contracts import (
    aggregate_privacy_decisions,
    validate_issuer_epoch,
    validate_stored_privacy_decision,
)
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, PrivacyTier, ValidationError
from open_brain_engine.portable.v4 import canonical_revision_id
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.migrations import _format_timestamp

from .consent_contracts import EgressMode
from .issuer_state import verify_issuer_evidence
from .privacy_projection import (
    RepairedPrivacyEvidence,
    RetainedPrivacyEvidence,
    apply_privacy_repair,
    project_retained_privacy_evidence,
)
from .search_projection import (
    canonical_source_rows,
    project_search_privacy,
    write_search_privacy,
)
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine

PRIVACY_REPAIR_REQUEST_DOMAIN = "open-brain.privacy.repair-request.v1"
RECEIPT_VERSION = 1

REPAIR_TARGET_KINDS = frozenset({"source_revision", "canonical_revision"})

REPAIR_ERROR_CODES = frozenset(
    {
        "owner_required",
        "invalid_request",
        "not_found",
        "evidence_mismatch",
        "issuer_mismatch",
        "operation_conflict",
        "supersession_invalid",
        "ledger_corrupt",
        "operation_pending",
    }
)

_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TARGET_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


class PrivacyRepairError(ValueError):
    """Bounded closed-code failure for one owner repair operation."""

    def __init__(self, code: str) -> None:
        if code not in REPAIR_ERROR_CODES:
            raise ValueError("invalid privacy repair error code")
        super().__init__(code)
        self.code = code


def _target_id(value: object) -> str:
    if not isinstance(value, str) or _TARGET_ID.fullmatch(value) is None:
        raise PrivacyRepairError("invalid_request")
    return value


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise PrivacyRepairError("invalid_request")
    return value


def _operation_id(value: object) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise PrivacyRepairError("invalid_request")
    return value


@dataclass(frozen=True, slots=True)
class PrivacyRepairRequest:
    """Closed caller-supplied repair request; the engine owns every stamped field."""

    target_kind: str
    target_id: str
    invalid_evidence_sha256: str
    replacement: PrivacyDecision | Mapping[str, object]
    operation_id: str
    supersedes_repair_id: str | None = None

    def __post_init__(self) -> None:
        if self.target_kind not in REPAIR_TARGET_KINDS:
            raise PrivacyRepairError("invalid_request")
        _target_id(self.target_id)
        _digest(self.invalid_evidence_sha256)
        _operation_id(self.operation_id)
        if self.supersedes_repair_id is not None:
            _target_id(self.supersedes_repair_id)
        try:
            decision = validate_stored_privacy_decision(self.replacement)
        except ValueError:
            raise PrivacyRepairError("invalid_request") from None
        if decision.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN} and (
            decision.authority.cloud or decision.authority.external_egress
        ):
            raise PrivacyRepairError("invalid_request")


def privacy_repair_request_sha256(request: PrivacyRepairRequest) -> str:
    """Digest the canonical repair request over its durable binding fields only.

    The operation id is excluded: replay binds one operation id to one digest, and
    the digest itself covers the target triple, the validated replacement decision,
    and the superseded repair.
    """
    decision = validate_stored_privacy_decision(request.replacement)
    return sha256(
        portable_canonical_json_bytes(
            {
                "version": 1,
                "domain": PRIVACY_REPAIR_REQUEST_DOMAIN,
                "target_kind": request.target_kind,
                "target_id": request.target_id,
                "invalid_evidence_sha256": request.invalid_evidence_sha256,
                "replacement_privacy": decision.to_dict(),
                "supersedes_repair_id": request.supersedes_repair_id,
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class PrivacyRepairReceipt:
    """Closed durable receipt for one committed repair."""

    repair_id: str
    repair_sequence: int
    target_kind: str
    target_id: str
    invalid_evidence_sha256: str
    owner_actor_id: str
    issuer_epoch: int
    replacement: PrivacyDecision
    operation_id: str
    request_sha256: str
    supersedes_repair_id: str | None
    recorded_at: str

    def encode(self) -> str:
        """Encode the exact stored receipt bytes for the ledger row."""
        return portable_canonical_json_bytes(self._payload()).decode("utf-8")

    def _payload(self) -> dict[str, object]:
        return {
            "receipt_version": RECEIPT_VERSION,
            "repair_id": self.repair_id,
            "repair_sequence": self.repair_sequence,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "invalid_evidence_sha256": self.invalid_evidence_sha256,
            "owner_actor_id": self.owner_actor_id,
            "issuer_epoch": self.issuer_epoch,
            "replacement_privacy": self.replacement.to_dict(),
            "operation_id": self.operation_id,
            "request_sha256": self.request_sha256,
            "supersedes_repair_id": self.supersedes_repair_id,
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def decode(cls, payload: object) -> PrivacyRepairReceipt:
        """Decode one stored receipt payload, failing closed on any drift."""
        if not isinstance(payload, Mapping) or payload.get("receipt_version") != RECEIPT_VERSION:
            raise ValidationError("invalid privacy repair receipt")
        try:
            decision = validate_stored_privacy_decision(payload.get("replacement_privacy"))
            receipt = cls(
                repair_id=_receipt_id(payload.get("repair_id")),
                repair_sequence=_receipt_sequence(payload.get("repair_sequence")),
                target_kind=_receipt_kind(payload.get("target_kind")),
                target_id=_target_id(payload.get("target_id")),
                invalid_evidence_sha256=_digest(payload.get("invalid_evidence_sha256")),
                owner_actor_id=_owner_actor(payload.get("owner_actor_id")),
                issuer_epoch=validate_issuer_epoch(payload.get("issuer_epoch")),
                replacement=decision,
                operation_id=_operation_id(payload.get("operation_id")),
                request_sha256=_digest(payload.get("request_sha256")),
                supersedes_repair_id=(
                    None
                    if payload.get("supersedes_repair_id") is None
                    else _target_id(payload.get("supersedes_repair_id"))
                ),
                recorded_at=_recorded_at(payload.get("recorded_at")),
            )
        except PrivacyRepairError:
            raise ValidationError("invalid privacy repair receipt") from None
        if payload.keys() != receipt._payload().keys():
            raise ValidationError("invalid privacy repair receipt")
        return receipt


def _receipt_id(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PrivacyRepairError("invalid_request")
    return value


def _receipt_sequence(value: object) -> int:
    if type(value) is not int or value < 1:
        raise PrivacyRepairError("invalid_request")
    return value


def _receipt_kind(value: object) -> str:
    if value not in REPAIR_TARGET_KINDS:
        raise PrivacyRepairError("invalid_request")
    return str(value)


def _owner_actor(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PrivacyRepairError("invalid_request")
    return value


def _recorded_at(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise PrivacyRepairError("invalid_request")
    return value


@dataclass(frozen=True, slots=True)
class LedgerRepair:
    """One parsed ledger row bound to its exact invalid-evidence triple."""

    repair_id: str
    repair_sequence: int
    target_kind: str
    target_id: str
    invalid_evidence_sha256: str
    owner_actor_id: str
    replacement_privacy_json: str
    replacement: PrivacyDecision
    operation_id: str
    request_sha256: str
    issuer_epoch: int
    receipt_json: str
    recorded_at: str
    supersedes_repair_id: str | None


def _parse_ledger_row(row: sqlite3.Row) -> LedgerRepair:
    try:
        replacement_privacy_json = row["replacement_privacy_json"]
        if not isinstance(replacement_privacy_json, str):
            raise ValueError
        replacement = validate_stored_privacy_decision(json.loads(replacement_privacy_json))
        repair = LedgerRepair(
            repair_id=_receipt_id(row["repair_id"]),
            repair_sequence=_receipt_sequence(row["repair_sequence"]),
            target_kind=_receipt_kind(row["target_kind"]),
            target_id=_target_id(row["target_id"]),
            invalid_evidence_sha256=_digest(row["invalid_evidence_sha256"]),
            owner_actor_id=_owner_actor(row["owner_actor_id"]),
            replacement_privacy_json=replacement_privacy_json,
            replacement=replacement,
            operation_id=_operation_id(row["operation_id"]),
            request_sha256=_digest(row["request_sha256"]),
            issuer_epoch=validate_issuer_epoch(row["issuer_epoch"]),
            receipt_json=_receipt_json(row["receipt_json"]),
            recorded_at=_recorded_at(row["recorded_at"]),
            supersedes_repair_id=(
                None
                if row["supersedes_repair_id"] is None
                else _target_id(row["supersedes_repair_id"])
            ),
        )
    except (KeyError, TypeError, ValueError):
        raise PrivacyRepairError("ledger_corrupt") from None
    return repair


def _receipt_json(value: object) -> str:
    if not isinstance(value, str):
        raise PrivacyRepairError("invalid_request")
    return value


def _load_repair_chain(
    connection: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: str,
    invalid_evidence_sha256: str,
) -> tuple[LedgerRepair, ...]:
    rows = connection.execute(
        "SELECT repair_id, repair_sequence, target_kind, target_id, "
        "invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
        "operation_id, request_sha256, issuer_epoch, receipt_json, recorded_at, "
        "supersedes_repair_id FROM privacy_repair_ledger "
        "WHERE target_kind=? AND target_id=? AND invalid_evidence_sha256=? "
        "ORDER BY repair_sequence",
        (target_kind, target_id, invalid_evidence_sha256),
    ).fetchall()
    return tuple(_parse_ledger_row(row) for row in rows)


def _require_immutable_repair_target(
    connection: sqlite3.Connection, *, target_kind: str, target_id: str
) -> None:
    if target_kind == "source_revision":
        table = "source_revisions"
        column = "capture_id"
    elif target_kind == "canonical_revision":
        table = "canonical_revision_members"
        column = "revision_id"
    else:
        raise PrivacyRepairError("ledger_corrupt")
    if connection.execute(
        f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (target_id,)
    ).fetchone() is None:
        raise PrivacyRepairError("ledger_corrupt")


def _stationary_issuer_epoch(connection: sqlite3.Connection) -> int:
    rows = connection.execute("SELECT issuer_epoch FROM brain_identity").fetchall()
    if len(rows) != 1:
        raise PrivacyRepairError("ledger_corrupt")
    try:
        return validate_issuer_epoch(rows[0][0])
    except ValidationError:
        raise PrivacyRepairError("ledger_corrupt") from None


def _validate_repair_chain(
    connection: sqlite3.Connection, repairs: tuple[LedgerRepair, ...]
) -> LedgerRepair | None:
    """Validate every authority-bearing field and return the sole active repair."""
    if not repairs:
        return None
    issuer_epoch = _stationary_issuer_epoch(connection)
    previous: LedgerRepair | None = None
    seen: set[str] = set()
    for repair in repairs:
        _require_immutable_repair_target(
            connection, target_kind=repair.target_kind, target_id=repair.target_id
        )
        if repair.repair_id in seen:
            raise PrivacyRepairError("ledger_corrupt")
        seen.add(repair.repair_id)
        if previous is None:
            if repair.supersedes_repair_id is not None:
                raise PrivacyRepairError("ledger_corrupt")
        elif (
            repair.repair_sequence <= previous.repair_sequence
            or repair.supersedes_repair_id != previous.repair_id
        ):
            raise PrivacyRepairError("ledger_corrupt")
        try:
            request = PrivacyRepairRequest(
                target_kind=repair.target_kind,
                target_id=repair.target_id,
                invalid_evidence_sha256=repair.invalid_evidence_sha256,
                replacement=repair.replacement,
                operation_id=repair.operation_id,
                supersedes_repair_id=repair.supersedes_repair_id,
            )
            receipt = PrivacyRepairReceipt.decode(json.loads(repair.receipt_json))
        except (TypeError, ValueError):
            raise PrivacyRepairError("ledger_corrupt") from None
        if (
            repair.replacement_privacy_json
            != portable_canonical_json_bytes(repair.replacement.to_dict()).decode("utf-8")
            or receipt.encode() != repair.receipt_json
            or receipt.repair_id != repair.repair_id
            or receipt.repair_sequence != repair.repair_sequence
            or receipt.target_kind != repair.target_kind
            or receipt.target_id != repair.target_id
            or receipt.invalid_evidence_sha256 != repair.invalid_evidence_sha256
            or receipt.owner_actor_id != repair.owner_actor_id
            or receipt.issuer_epoch != repair.issuer_epoch
            or repair.issuer_epoch != issuer_epoch
            or receipt.replacement != repair.replacement
            or receipt.operation_id != repair.operation_id
            or receipt.request_sha256 != repair.request_sha256
            or receipt.supersedes_repair_id != repair.supersedes_repair_id
            or receipt.recorded_at != repair.recorded_at
            or privacy_repair_request_sha256(request) != repair.request_sha256
        ):
            raise PrivacyRepairError("ledger_corrupt")
        previous = repair
    return previous


def _active_repair(
    connection: sqlite3.Connection,
    *,
    target_kind: str,
    target_id: str,
    invalid_evidence_sha256: str,
) -> LedgerRepair | None:
    return _validate_repair_chain(
        connection,
        _load_repair_chain(
            connection,
            target_kind=target_kind,
            target_id=target_id,
            invalid_evidence_sha256=invalid_evidence_sha256,
        )
    )


def _retained_capture_privacy(
    connection: sqlite3.Connection, capture_id: str
) -> tuple[str | int | float | bytes | None]:
    row = connection.execute(
        "SELECT privacy_json FROM captures WHERE capture_id = ?", (capture_id,)
    ).fetchone()
    if row is None:
        return (None,)
    return (row[0],)


def _require_repairable_base(
    connection: sqlite3.Connection,
    base: RetainedPrivacyEvidence,
    *,
    target_kind: str,
    target_id: str,
    invalid_evidence_sha256: str,
) -> None:
    if base.valid:
        raise PrivacyRepairError("evidence_mismatch")
    if base.invalid_evidence_sha256 != invalid_evidence_sha256:
        raise PrivacyRepairError("evidence_mismatch")
    marker = connection.execute(
        "SELECT 1 FROM privacy_invalid_evidence "
        "WHERE target_kind=? AND target_id=? AND invalid_reason=? "
        "AND invalid_evidence_sha256=?",
        (
            target_kind,
            target_id,
            base.invalid_reason.value if base.invalid_reason else None,
            base.invalid_evidence_sha256,
        ),
    ).fetchone()
    if marker is None:
        raise PrivacyRepairError("not_found")


def _source_revision_base(
    connection: sqlite3.Connection, *, capture_id: str
) -> RetainedPrivacyEvidence:
    revision = connection.execute(
        "SELECT 1 FROM source_revisions WHERE capture_id = ?", (capture_id,)
    ).fetchone()
    if revision is None:
        raise PrivacyRepairError("not_found")
    return project_retained_privacy_evidence(
        _retained_capture_privacy(connection, capture_id)
    )


def _canonical_revision_values(
    connection: sqlite3.Connection, *, revision_id: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = connection.execute(
        "SELECT m.capture_id AS capture_id, c.privacy_json AS privacy_json "
        "FROM canonical_revision_members m LEFT JOIN captures c ON c.capture_id = m.capture_id "
        "WHERE m.revision_id = ? ORDER BY m.ordinal",
        (revision_id,),
    ).fetchall()
    if not rows:
        raise PrivacyRepairError("not_found")
    return (
        tuple(str(row["capture_id"]) for row in rows),
        tuple(row["privacy_json"] for row in rows),
    )


def _current_page_revision(connection: sqlite3.Connection, page_id: str) -> str | None:
    head = connection.execute(
        "SELECT publication_id FROM review_page_heads WHERE page_id = ?", (page_id,)
    ).fetchone()
    if head is not None:
        return canonical_revision_id(str(head[0]))
    original = connection.execute(
        "SELECT publication_id FROM captures "
        "WHERE page_id = ? AND publication_id IS NOT NULL AND canonical_path IS NOT NULL "
        "ORDER BY capture_id LIMIT 1",
        (page_id,),
    ).fetchone()
    if original is None:
        return None
    return canonical_revision_id(str(original[0]))


def _resolved_member_decisions(
    connection: sqlite3.Connection, rows: Sequence[sqlite3.Row]
) -> list[PrivacyDecision] | None:
    """Resolve every member to a valid decision, honoring active member repairs.

    Returns ``None`` when any member still fails closed without an active repair:
    a canonical aggregate only heals once every member resolves.
    """
    resolved: list[PrivacyDecision] = []
    for row in rows:
        base = project_retained_privacy_evidence((row["privacy_json"],))
        if base.valid:
            assert isinstance(row["privacy_json"], str)
            resolved.append(validate_stored_privacy_decision(json.loads(row["privacy_json"])))
            continue
        assert base.invalid_evidence_sha256 is not None
        active = _active_repair(
            connection,
            target_kind="source_revision",
            target_id=str(row["capture_id"]),
            invalid_evidence_sha256=base.invalid_evidence_sha256,
        )
        if active is None:
            return None
        resolved.append(active.replacement)
    return resolved


def _direct_canonical_overlay(
    connection: sqlite3.Connection,
    *,
    page_id: str,
    base: RetainedPrivacyEvidence,
) -> LedgerRepair | None:
    """Bind a direct canonical repair only to the page's current revision row."""
    if base.valid or base.invalid_evidence_sha256 is None:
        return None
    revision_id = _current_page_revision(connection, page_id)
    if revision_id is None:
        return None
    return _active_repair(
        connection,
        target_kind="canonical_revision",
        target_id=revision_id,
        invalid_evidence_sha256=base.invalid_evidence_sha256,
    )


def resolve_search_privacy(
    connection: sqlite3.Connection,
    *,
    result_id: str,
    capture_id: str,
    record_type: str,
) -> tuple[RetainedPrivacyEvidence | RepairedPrivacyEvidence, tuple[str, int] | None]:
    """Resolve one search row's effective privacy from retained evidence and repairs.

    Source rows overlay an active repair bound to their own capture and digest.
    Canonical rows first attempt member resolution: once every member resolves, the
    aggregate heals with complete evidence and ignores direct canonical repairs.
    Otherwise a direct canonical repair applies only while it targets the page's
    current revision and the row's exact invalid digest.
    """
    if record_type == "source":
        base = project_retained_privacy_evidence(
            _retained_capture_privacy(connection, capture_id)
        )
        if base.valid or base.invalid_evidence_sha256 is None:
            return base, None
        active = _active_repair(
            connection,
            target_kind="source_revision",
            target_id=capture_id,
            invalid_evidence_sha256=base.invalid_evidence_sha256,
        )
        if active is None:
            return base, None
        return (
            apply_privacy_repair(
                base,
                active.replacement,
                applied_repair_id=active.repair_id,
                applied_repair_sequence=active.repair_sequence,
            ),
            (active.repair_id, active.repair_sequence),
        )
    if record_type != "canonical":
        return (
            project_retained_privacy_evidence((), caller_declared_inconsistent=True),
            None,
        )
    try:
        rows = canonical_source_rows(connection, result_id=result_id, capture_id=capture_id)
    except ValueError:
        base = project_search_privacy(
            connection,
            result_id=result_id,
            capture_id=capture_id,
            record_type=record_type,
        )
        active = _direct_canonical_overlay(
            connection, page_id=result_id, base=base
        )
        if active is None:
            return base, None
        return (
            apply_privacy_repair(
                base,
                active.replacement,
                applied_repair_id=active.repair_id,
                applied_repair_sequence=active.repair_sequence,
            ),
            (active.repair_id, active.repair_sequence),
        )
    base = project_retained_privacy_evidence(tuple(row["privacy_json"] for row in rows))
    if base.valid:
        return base, None
    resolved = _resolved_member_decisions(connection, rows)
    if resolved is not None:
        aggregated = aggregate_privacy_decisions(resolved)
        return (
            RetainedPrivacyEvidence(
                tier=aggregated.tier,
                authority=aggregated.authority,
                source_decision_sha256s=aggregated.source_decision_sha256s,
                confirmation_refs=aggregated.confirmation_refs,
                invalid_reason=None,
                invalid_evidence_sha256=None,
            ),
            None,
        )
    active = _direct_canonical_overlay(connection, page_id=result_id, base=base)
    if active is None:
        return base, None
    return (
        apply_privacy_repair(
            base,
            active.replacement,
            applied_repair_id=active.repair_id,
            applied_repair_sequence=active.repair_sequence,
        ),
        (active.repair_id, active.repair_sequence),
    )


def resolve_canonical_revision_privacy(
    connection: sqlite3.Connection, *, revision_id: str
) -> tuple[RetainedPrivacyEvidence, LedgerRepair | None]:
    """Resolve one canonical revision's effective privacy for repair eligibility.

    A revision is repairable only while its effective projection still fails closed:
    once active member repairs heal the aggregate, the direct canonical repair is
    no longer eligible.
    """
    _capture_ids, values = _canonical_revision_values(connection, revision_id=revision_id)
    base = project_retained_privacy_evidence(values)
    if base.valid or base.invalid_evidence_sha256 is None:
        return base, None
    rows = connection.execute(
        "SELECT m.capture_id AS capture_id, c.privacy_json AS privacy_json "
        "FROM canonical_revision_members m LEFT JOIN captures c ON c.capture_id = m.capture_id "
        "WHERE m.revision_id = ? ORDER BY m.ordinal",
        (revision_id,),
    ).fetchall()
    resolved = _resolved_member_decisions(connection, rows)
    if resolved is not None:
        aggregated = aggregate_privacy_decisions(resolved)
        return (
            RetainedPrivacyEvidence(
                tier=aggregated.tier,
                authority=aggregated.authority,
                source_decision_sha256s=aggregated.source_decision_sha256s,
                confirmation_refs=aggregated.confirmation_refs,
                invalid_reason=None,
                invalid_evidence_sha256=None,
            ),
            None,
        )
    active = _active_repair(
        connection,
        target_kind="canonical_revision",
        target_id=revision_id,
        invalid_evidence_sha256=base.invalid_evidence_sha256,
    )
    return base, active


def _append_repair_row(
    connection: sqlite3.Connection, *, receipt: PrivacyRepairReceipt
) -> None:
    connection.execute(
        "INSERT INTO privacy_repair_ledger ("
        "repair_id, repair_sequence, target_kind, target_id, invalid_evidence_sha256, "
        "owner_actor_id, replacement_privacy_json, operation_id, request_sha256, "
        "issuer_epoch, receipt_json, recorded_at, supersedes_repair_id"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            receipt.repair_id,
            receipt.repair_sequence,
            receipt.target_kind,
            receipt.target_id,
            receipt.invalid_evidence_sha256,
            receipt.owner_actor_id,
            portable_canonical_json_bytes(receipt.replacement.to_dict()).decode("utf-8"),
            receipt.operation_id,
            receipt.request_sha256,
            receipt.issuer_epoch,
            receipt.encode(),
            receipt.recorded_at,
            receipt.supersedes_repair_id,
        ),
    )


def _refresh_affected_search_rows(
    connection: sqlite3.Connection, *, target_kind: str, target_id: str
) -> None:
    """Recompute every search row whose effective privacy this repair touches."""
    for row in list(
        connection.execute(
            "SELECT result_id, capture_id, record_type FROM search_documents ORDER BY result_id"
        )
    ):
        result_id = str(row["result_id"])
        record_type = str(row["record_type"])
        affected = False
        if record_type == "source":
            affected = target_kind == "source_revision" and str(row["capture_id"]) == target_id
        elif record_type == "canonical":
            if target_kind == "canonical_revision":
                affected = _current_page_revision(connection, result_id) == target_id
            else:
                try:
                    members = canonical_source_rows(
                        connection, result_id=result_id, capture_id=str(row["capture_id"])
                    )
                except ValueError:
                    members = ()
                affected = any(
                    str(member["capture_id"]) == target_id for member in members
                )
        if not affected:
            continue
        evidence, applied = resolve_search_privacy(
            connection,
            result_id=result_id,
            capture_id=str(row["capture_id"]),
            record_type=record_type,
        )
        write_search_privacy(
            connection, result_id=result_id, evidence=evidence, applied_repair=applied
        )


def _advance_retrieval_generation(
    connection: sqlite3.Connection, *, baseline: int
) -> None:
    """Advance the retrieval generation exactly once regardless of row churn."""
    connection.execute(
        "UPDATE engine_generations SET retrieval_generation=? WHERE singleton=1",
        (baseline + 1,),
    )


class PrivacyRepairTasks:
    """Owner-local repair task exposed through the engine task set."""

    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def repair_privacy(
        self, request: PrivacyRepairRequest, *, authority: EffectiveAuthority
    ) -> PrivacyRepairReceipt:
        engine = self._engine
        profile = engine.profile
        if (
            not isinstance(authority, EffectiveAuthority)
            or not authority.owner
            or authority.principal_id != profile.owner_actor_id
            or authority.egress_mode is not EgressMode.OWNER_LOCAL
        ):
            raise PrivacyRepairError("owner_required")
        if not isinstance(request, PrivacyRepairRequest):
            raise PrivacyRepairError("invalid_request")
        request_digest = privacy_repair_request_sha256(request)
        try:
            with engine._writer_lease.acquire_shared_writer():
                return self._repair_with_writer(request, request_digest=request_digest)
        except LockBusyError:
            raise PrivacyRepairError("operation_pending") from None

    def _repair_with_writer(
        self, request: PrivacyRepairRequest, *, request_digest: str
    ) -> PrivacyRepairReceipt:
        engine = self._engine
        profile = engine.profile
        with engine._store.transaction() as connection:
            baseline_row = connection.execute(
                "SELECT retrieval_generation FROM engine_generations WHERE singleton=1"
            ).fetchone()
            if baseline_row is None:
                raise PrivacyRepairError("ledger_corrupt")
            baseline = int(baseline_row[0])
            existing = connection.execute(
                "SELECT repair_id, repair_sequence, target_kind, target_id, "
                "invalid_evidence_sha256, owner_actor_id, replacement_privacy_json, "
                "operation_id, request_sha256, issuer_epoch, receipt_json, recorded_at, "
                "supersedes_repair_id FROM privacy_repair_ledger "
                "WHERE operation_id=?",
                (request.operation_id,),
            ).fetchall()
            if len(existing) > 1:
                raise PrivacyRepairError("ledger_corrupt")
            if existing:
                stored = _parse_ledger_row(existing[0])
                _validate_repair_chain(
                    connection,
                    _load_repair_chain(
                        connection,
                        target_kind=stored.target_kind,
                        target_id=stored.target_id,
                        invalid_evidence_sha256=stored.invalid_evidence_sha256,
                    ),
                )
                if stored.request_sha256 != request_digest:
                    raise PrivacyRepairError("operation_conflict")
                try:
                    return PrivacyRepairReceipt.decode(json.loads(stored.receipt_json))
                except (TypeError, ValueError):
                    raise PrivacyRepairError("ledger_corrupt") from None
            try:
                verify_issuer_evidence(connection, tenant_id=profile.tenant_id)
            except ValidationError:
                raise PrivacyRepairError("issuer_mismatch") from None
            epoch_row = connection.execute(
                "SELECT issuer_epoch FROM brain_identity"
            ).fetchone()
            if epoch_row is None:
                raise PrivacyRepairError("issuer_mismatch")
            issuer_epoch = validate_issuer_epoch(epoch_row[0])
            if request.target_kind == "source_revision":
                base = _source_revision_base(connection, capture_id=request.target_id)
            else:
                _capture_ids, values = _canonical_revision_values(
                    connection, revision_id=request.target_id
                )
                base = project_retained_privacy_evidence(values)
            _require_repairable_base(
                connection,
                base,
                target_kind=request.target_kind,
                target_id=request.target_id,
                invalid_evidence_sha256=request.invalid_evidence_sha256,
            )
            active = _validate_repair_chain(
                connection,
                _load_repair_chain(
                    connection,
                    target_kind=request.target_kind,
                    target_id=request.target_id,
                    invalid_evidence_sha256=request.invalid_evidence_sha256,
                )
            )
            if request.target_kind == "canonical_revision":
                # A direct canonical repair applies only while the effective
                # canonical projection still fails closed: member repairs that
                # heal the aggregate retire it before supersession applies.
                effective, _active = resolve_canonical_revision_privacy(
                    connection, revision_id=request.target_id
                )
                if effective.valid:
                    raise PrivacyRepairError("evidence_mismatch")
            if request.supersedes_repair_id is None:
                if active is not None:
                    raise PrivacyRepairError("supersession_invalid")
            elif active is None or request.supersedes_repair_id != active.repair_id:
                raise PrivacyRepairError("supersession_invalid")
            max_row = connection.execute(
                "SELECT max(repair_sequence) FROM privacy_repair_ledger"
            ).fetchone()
            sequence = (int(max_row[0]) if max_row[0] is not None else 0) + 1
            receipt = PrivacyRepairReceipt(
                repair_id="repair_" + str(uuid4()),
                repair_sequence=sequence,
                target_kind=request.target_kind,
                target_id=request.target_id,
                invalid_evidence_sha256=request.invalid_evidence_sha256,
                owner_actor_id=profile.owner_actor_id,
                issuer_epoch=issuer_epoch,
                replacement=validate_stored_privacy_decision(request.replacement),
                operation_id=request.operation_id,
                request_sha256=request_digest,
                supersedes_repair_id=request.supersedes_repair_id,
                recorded_at=_format_timestamp(engine._clock()),
            )
            _append_repair_row(connection, receipt=receipt)
            _refresh_affected_search_rows(
                connection,
                target_kind=request.target_kind,
                target_id=request.target_id,
            )
            _advance_retrieval_generation(connection, baseline=baseline)
            return receipt


def audit_privacy_repair_ledger(connection: sqlite3.Connection) -> None:
    """Deterministically audit the repair ledger and every repair-aware projection.

    Below schema nine the repair ledger must still be empty (the schema-eight
    migration began with no repairs). From schema nine the full audit holds: every
    chain is a strict supersession sequence, every row agrees with its stored
    receipt and recomputed request digest, every stamped issuer epoch matches the
    durable identity, and every search row equals its repair-aware resolution.
    """
    if connection.execute("PRAGMA user_version").fetchone()[0] < 9:
        if connection.execute("SELECT count(*) FROM privacy_repair_ledger").fetchone()[0]:
            raise T03Error("operation_pending")
        return
    try:
        _stationary_issuer_epoch(connection)
    except PrivacyRepairError:
        raise T03Error("operation_pending") from None
    triples = connection.execute(
        "SELECT DISTINCT target_kind, target_id, invalid_evidence_sha256 "
        "FROM privacy_repair_ledger ORDER BY target_kind, target_id, invalid_evidence_sha256"
    ).fetchall()
    for triple in triples:
        try:
            chain = _load_repair_chain(
                connection,
                target_kind=str(triple["target_kind"]),
                target_id=str(triple["target_id"]),
                invalid_evidence_sha256=str(triple["invalid_evidence_sha256"]),
            )
            active = _validate_repair_chain(connection, chain)
        except PrivacyRepairError:
            raise T03Error("operation_pending") from None
        if active is None:
            raise T03Error("operation_pending")
    for row in connection.execute(
        "SELECT result_id, capture_id, record_type, effective_tier, effective_cloud, "
        "effective_external_egress, invalid_evidence_reason, invalid_evidence_sha256, "
        "applied_repair_id, applied_repair_sequence FROM search_documents ORDER BY result_id"
    ):
        try:
            evidence, applied = resolve_search_privacy(
                connection,
                result_id=str(row["result_id"]),
                capture_id=str(row["capture_id"]),
                record_type=str(row["record_type"]),
            )
        except PrivacyRepairError:
            raise T03Error("operation_pending") from None
        stored = (
            str(row["effective_tier"]),
            int(row["effective_cloud"]),
            int(row["effective_external_egress"]),
            row["invalid_evidence_reason"],
            row["invalid_evidence_sha256"],
            row["applied_repair_id"],
            row["applied_repair_sequence"],
        )
        if stored != (
            evidence.tier.value,
            int(evidence.authority.cloud),
            int(evidence.authority.external_egress),
            None if evidence.invalid_reason is None else evidence.invalid_reason.value,
            evidence.invalid_evidence_sha256,
            None if applied is None else applied[0],
            None if applied is None else applied[1],
        ):
            raise T03Error("operation_pending")


__all__ = [
    "LedgerRepair",
    "PRIVACY_REPAIR_REQUEST_DOMAIN",
    "PrivacyRepairError",
    "PrivacyRepairReceipt",
    "PrivacyRepairRequest",
    "PrivacyRepairTasks",
    "REPAIR_TARGET_KINDS",
    "audit_privacy_repair_ledger",
    "privacy_repair_request_sha256",
    "resolve_canonical_revision_privacy",
    "resolve_search_privacy",
]
