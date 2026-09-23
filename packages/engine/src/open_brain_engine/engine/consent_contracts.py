"""Immutable owner-consent contracts for named external providers."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.core.access_contracts import validate_authorization_generation
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier, ValidationError

__all__ = [
    "ConsentContractError",
    "ConsentInspection",
    "ConsentReceipt",
    "ConsentTransition",
    "EgressMode",
    "ProviderConsentRecord",
    "ProviderConsentState",
    "provider_consent_state_from_bytes",
    "provider_consent_state_to_bytes",
]

_CONSENT_ID = re.compile(r"consent_[0-9a-f]{32}")
_PROVIDER_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")
_EXTERNAL_TIERS = frozenset(
    {PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL}
)
_STATE_VERSION = "provider-consent-state.v1"
_MAX_STATE_BYTES = 16 * 1024 * 1024
_STATE_FIELDS = frozenset(
    {"state_version", "authorization_generation", "records", "operations"}
)
_RECORD_FIELDS = frozenset(
    {
        "consent_id",
        "provider_id",
        "egress_mode",
        "allowed_tiers",
        "active",
        "granted_generation",
        "granted_at",
        "revoked_generation",
        "revoked_at",
    }
)
_OPERATION_FIELDS = frozenset({"operation_id", "request_sha256", "receipt"})
_RECEIPT_FIELDS = frozenset(
    {
        "operation",
        "consent_id",
        "authorization_generation",
        "duplicate",
        "previous_consent_id",
    }
)


class ConsentContractError(ValueError):
    """A consent operation or lookup failed its closed contract."""

    def __init__(self, code: str) -> None:
        if code not in {
            "consent_unavailable",
            "invalid_consent",
            "operation_conflict",
            "owner_required",
        }:
            raise ValueError("invalid consent error code")
        self.code = code
        super().__init__(code)


class EgressMode(StrEnum):
    OWNER_LOCAL = "owner_local"
    EXTERNAL_PROVIDER = "external_provider"


@dataclass(frozen=True, slots=True)
class ProviderConsentRecord:
    consent_id: str
    provider_id: str
    egress_mode: EgressMode
    allowed_tiers: frozenset[PrivacyTier]
    active: bool
    granted_generation: int
    granted_at: str
    revoked_generation: int | None = None
    revoked_at: str | None = None

    def __post_init__(self) -> None:
        try:
            _validate_consent_id(self.consent_id)
            _validate_provider_id(self.provider_id)
            _validate_external_tiers(self.allowed_tiers)
            validate_authorization_generation(self.granted_generation)
            _validate_timestamp(self.granted_at)
            if self.egress_mode is not EgressMode.EXTERNAL_PROVIDER:
                raise ValueError
            if type(self.active) is not bool:
                raise ValueError
            if self.active:
                if self.revoked_generation is not None or self.revoked_at is not None:
                    raise ValueError
            elif (
                self.revoked_generation is None
                or self.revoked_generation <= self.granted_generation
                or self.revoked_at is None
            ):
                raise ValueError
            if self.revoked_generation is not None:
                validate_authorization_generation(self.revoked_generation)
            if self.revoked_at is not None:
                _validate_timestamp(self.revoked_at)
        except (TypeError, ValueError, ValidationError):
            raise ConsentContractError("invalid_consent") from None


@dataclass(frozen=True, slots=True)
class ConsentReceipt:
    operation: str
    consent_id: str
    authorization_generation: int
    duplicate: bool = False
    previous_consent_id: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {"grant", "replace", "revoke"}:
            raise ConsentContractError("invalid_consent")
        _validate_consent_id(self.consent_id)
        validate_authorization_generation(self.authorization_generation)
        if type(self.duplicate) is not bool:
            raise ConsentContractError("invalid_consent")
        if self.previous_consent_id is not None:
            _validate_consent_id(self.previous_consent_id)


@dataclass(frozen=True, slots=True)
class _AppliedOperation:
    operation_id: str
    request_sha256: str
    receipt: ConsentReceipt

    def __post_init__(self) -> None:
        _validate_operation_id(self.operation_id)
        if re.fullmatch(r"[0-9a-f]{64}", self.request_sha256) is None:
            raise ConsentContractError("invalid_consent")


@dataclass(frozen=True, slots=True)
class ConsentTransition:
    state: ProviderConsentState
    receipt: ConsentReceipt


@dataclass(frozen=True, slots=True)
class ConsentInspection:
    authorization_generation: int
    records: tuple[ProviderConsentRecord, ...]


@dataclass(frozen=True, slots=True)
class ProviderConsentState:
    """An immutable consent log projection with operation-id replay evidence."""

    authorization_generation: int = 0
    records: tuple[ProviderConsentRecord, ...] = ()
    operations: tuple[_AppliedOperation, ...] = ()

    def __post_init__(self) -> None:
        try:
            validate_authorization_generation(self.authorization_generation)
            if type(self.records) is not tuple or type(self.operations) is not tuple:
                raise ValueError
            if any(not isinstance(record, ProviderConsentRecord) for record in self.records):
                raise ValueError
            if any(not isinstance(operation, _AppliedOperation) for operation in self.operations):
                raise ValueError
        except (TypeError, ValueError, ValidationError):
            raise ConsentContractError("invalid_consent") from None

    def inspect(self, *, owner: bool) -> ConsentInspection:
        _require_owner(owner)
        return ConsentInspection(self.authorization_generation, self.records)

    def grant(
        self,
        *,
        owner: bool,
        provider_id: str,
        allowed_tiers: frozenset[PrivacyTier],
        operation_id: str,
        decided_at: str,
        egress_mode: EgressMode = EgressMode.EXTERNAL_PROVIDER,
        consent_id_factory: Callable[[], str] = lambda: f"consent_{secrets.token_hex(16)}",
    ) -> ConsentTransition:
        _require_owner(owner)
        _validate_provider_id(provider_id)
        _validate_external_tiers(allowed_tiers)
        _validate_timestamp(decided_at)
        if egress_mode is not EgressMode.EXTERNAL_PROVIDER:
            raise ConsentContractError("invalid_consent")
        request = _request_digest(
            "grant",
            provider_id=provider_id,
            allowed_tiers=allowed_tiers,
            consent_id=None,
            egress_mode=egress_mode,
        )
        replay = self._replay(operation_id, request)
        if replay is not None:
            return replay
        active = self._active_records(provider_id=provider_id)
        if len(active) > 1:
            raise ConsentContractError("consent_unavailable")
        if active:
            record = active[0]
            if record.allowed_tiers != allowed_tiers:
                raise ConsentContractError("consent_unavailable")
            receipt = ConsentReceipt(
                operation="grant",
                consent_id=record.consent_id,
                authorization_generation=self.authorization_generation,
                duplicate=True,
            )
            return self._record_operation(operation_id, request, receipt)
        generation = self.authorization_generation + 1
        consent_id = consent_id_factory()
        _validate_consent_id(consent_id)
        if any(record.consent_id == consent_id for record in self.records):
            raise ConsentContractError("invalid_consent")
        record = ProviderConsentRecord(
            consent_id=consent_id,
            provider_id=provider_id,
            egress_mode=egress_mode,
            allowed_tiers=allowed_tiers,
            active=True,
            granted_generation=generation,
            granted_at=decided_at,
        )
        receipt = ConsentReceipt("grant", consent_id, generation)
        return replace(
            self, authorization_generation=generation, records=(*self.records, record)
        )._record_operation(operation_id, request, receipt)

    def replace(
        self,
        *,
        owner: bool,
        consent_id: str,
        provider_id: str,
        allowed_tiers: frozenset[PrivacyTier],
        operation_id: str,
        decided_at: str,
        egress_mode: EgressMode = EgressMode.EXTERNAL_PROVIDER,
        consent_id_factory: Callable[[], str] = lambda: f"consent_{secrets.token_hex(16)}",
    ) -> ConsentTransition:
        _require_owner(owner)
        _validate_consent_id(consent_id)
        _validate_provider_id(provider_id)
        _validate_external_tiers(allowed_tiers)
        _validate_timestamp(decided_at)
        if egress_mode is not EgressMode.EXTERNAL_PROVIDER:
            raise ConsentContractError("invalid_consent")
        request = _request_digest(
            "replace",
            provider_id=provider_id,
            allowed_tiers=allowed_tiers,
            consent_id=consent_id,
            egress_mode=egress_mode,
        )
        replay = self._replay(operation_id, request)
        if replay is not None:
            return replay
        self._single_active(consent_id)
        provider_active = self._active_records(provider_id=provider_id)
        if provider_active and provider_active[0].consent_id != consent_id:
            raise ConsentContractError("consent_unavailable")
        generation = self.authorization_generation + 1
        replacement_id = consent_id_factory()
        _validate_consent_id(replacement_id)
        if any(record.consent_id == replacement_id for record in self.records):
            raise ConsentContractError("invalid_consent")
        records = tuple(
            replace(
                record,
                active=False,
                revoked_generation=generation,
                revoked_at=decided_at,
            )
            if record.consent_id == consent_id
            else record
            for record in self.records
        )
        replacement = ProviderConsentRecord(
            consent_id=replacement_id,
            provider_id=provider_id,
            egress_mode=egress_mode,
            allowed_tiers=allowed_tiers,
            active=True,
            granted_generation=generation,
            granted_at=decided_at,
        )
        receipt = ConsentReceipt(
            "replace", replacement_id, generation, previous_consent_id=consent_id
        )
        return replace(
            self, authorization_generation=generation, records=(*records, replacement)
        )._record_operation(operation_id, request, receipt)

    def revoke(
        self,
        *,
        owner: bool,
        consent_id: str,
        operation_id: str,
        decided_at: str,
    ) -> ConsentTransition:
        _require_owner(owner)
        _validate_consent_id(consent_id)
        _validate_timestamp(decided_at)
        request = _request_digest(
            "revoke",
            provider_id=None,
            allowed_tiers=None,
            consent_id=consent_id,
            egress_mode=None,
        )
        replay = self._replay(operation_id, request)
        if replay is not None:
            return replay
        matches = tuple(record for record in self.records if record.consent_id == consent_id)
        if len(matches) != 1:
            raise ConsentContractError("consent_unavailable")
        if not matches[0].active:
            receipt = ConsentReceipt(
                "revoke",
                consent_id,
                self.authorization_generation,
                duplicate=True,
            )
            return self._record_operation(operation_id, request, receipt)
        self._single_active(consent_id)
        generation = self.authorization_generation + 1
        records = tuple(
            replace(
                record,
                active=False,
                revoked_generation=generation,
                revoked_at=decided_at,
            )
            if record.consent_id == consent_id
            else record
            for record in self.records
        )
        receipt = ConsentReceipt("revoke", consent_id, generation)
        updated = replace(self, authorization_generation=generation, records=records)
        return updated._record_operation(operation_id, request, receipt)

    def active_consent(
        self,
        *,
        consent_id: str,
        provider_id: str,
        authorization_generation: int,
    ) -> ProviderConsentRecord:
        try:
            _validate_consent_id(consent_id)
            _validate_provider_id(provider_id)
            validate_authorization_generation(authorization_generation)
        except (TypeError, ValueError, ValidationError, ConsentContractError):
            raise ConsentContractError("consent_unavailable") from None
        if authorization_generation != self.authorization_generation:
            raise ConsentContractError("consent_unavailable")
        if not self._projection_is_well_formed():
            raise ConsentContractError("consent_unavailable")
        active = self._active_records(provider_id=provider_id)
        if len(active) != 1 or active[0].consent_id != consent_id:
            raise ConsentContractError("consent_unavailable")
        return active[0]

    def _active_records(self, *, provider_id: str) -> tuple[ProviderConsentRecord, ...]:
        return tuple(
            record
            for record in self.records
            if record.active
            and record.provider_id == provider_id
            and record.egress_mode is EgressMode.EXTERNAL_PROVIDER
        )

    def _single_active(self, consent_id: str) -> ProviderConsentRecord:
        matches = tuple(record for record in self.records if record.consent_id == consent_id)
        if len(matches) != 1 or not matches[0].active:
            raise ConsentContractError("consent_unavailable")
        if len(self._active_records(provider_id=matches[0].provider_id)) != 1:
            raise ConsentContractError("consent_unavailable")
        return matches[0]

    def _replay(self, operation_id: str, request_sha256: str) -> ConsentTransition | None:
        _validate_operation_id(operation_id)
        if not self._projection_is_well_formed():
            raise ConsentContractError("consent_unavailable")
        matches = tuple(
            operation for operation in self.operations if operation.operation_id == operation_id
        )
        if len(matches) > 1:
            raise ConsentContractError("consent_unavailable")
        if not matches:
            return None
        if matches[0].request_sha256 != request_sha256:
            raise ConsentContractError("operation_conflict")
        return ConsentTransition(self, matches[0].receipt)

    def _record_operation(
        self, operation_id: str, request_sha256: str, receipt: ConsentReceipt
    ) -> ConsentTransition:
        operation = _AppliedOperation(operation_id, request_sha256, receipt)
        return ConsentTransition(replace(self, operations=(*self.operations, operation)), receipt)

    def _projection_is_well_formed(self) -> bool:
        consent_ids = tuple(record.consent_id for record in self.records)
        operation_ids = tuple(operation.operation_id for operation in self.operations)
        active_providers = tuple(record.provider_id for record in self.records if record.active)
        return (
            len(consent_ids) == len(set(consent_ids))
            and len(operation_ids) == len(set(operation_ids))
            and len(active_providers) == len(set(active_providers))
            and all(
                record.granted_generation <= self.authorization_generation
                and (
                    record.revoked_generation is None
                    or record.revoked_generation <= self.authorization_generation
                )
                for record in self.records
            )
            and all(
                operation.receipt.authorization_generation <= self.authorization_generation
                for operation in self.operations
            )
        )


def provider_consent_state_to_bytes(state: ProviderConsentState) -> bytes:
    """Encode one exact, canonical provider-consent state projection."""
    if not isinstance(state, ProviderConsentState) or not state._projection_is_well_formed():
        raise ConsentContractError("invalid_consent")
    consent_ids = {record.consent_id for record in state.records}
    if any(
        operation.receipt.consent_id not in consent_ids
        or operation.receipt.previous_consent_id is not None
        and operation.receipt.previous_consent_id not in consent_ids
        for operation in state.operations
    ):
        raise ConsentContractError("invalid_consent")
    return portable_canonical_json_bytes(
        {
            "state_version": _STATE_VERSION,
            "authorization_generation": state.authorization_generation,
            "records": [
                {
                    "consent_id": record.consent_id,
                    "provider_id": record.provider_id,
                    "egress_mode": record.egress_mode.value,
                    "allowed_tiers": sorted(tier.value for tier in record.allowed_tiers),
                    "active": record.active,
                    "granted_generation": record.granted_generation,
                    "granted_at": record.granted_at,
                    "revoked_generation": record.revoked_generation,
                    "revoked_at": record.revoked_at,
                }
                for record in state.records
            ],
            "operations": [
                {
                    "operation_id": operation.operation_id,
                    "request_sha256": operation.request_sha256,
                    "receipt": {
                        "operation": operation.receipt.operation,
                        "consent_id": operation.receipt.consent_id,
                        "authorization_generation": (
                            operation.receipt.authorization_generation
                        ),
                        "duplicate": operation.receipt.duplicate,
                        "previous_consent_id": operation.receipt.previous_consent_id,
                    },
                }
                for operation in state.operations
            ],
        }
    )


def provider_consent_state_from_bytes(payload: bytes) -> ProviderConsentState:
    """Parse only the exact canonical provider-consent state representation."""
    try:
        if type(payload) is not bytes or not payload or len(payload) > _MAX_STATE_BYTES:
            raise ValueError
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        if type(value) is not dict or frozenset(value) != _STATE_FIELDS:
            raise ValueError
        if value["state_version"] != _STATE_VERSION:
            raise ValueError
        raw_records = value["records"]
        raw_operations = value["operations"]
        if type(raw_records) is not list or type(raw_operations) is not list:
            raise ValueError
        records = tuple(_record_from_mapping(record) for record in raw_records)
        operations = tuple(_operation_from_mapping(operation) for operation in raw_operations)
        state = ProviderConsentState(
            authorization_generation=cast(int, value["authorization_generation"]),
            records=records,
            operations=operations,
        )
        if provider_consent_state_to_bytes(state) != payload:
            raise ValueError
        return state
    except ConsentContractError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ConsentContractError("invalid_consent") from None


def _record_from_mapping(value: object) -> ProviderConsentRecord:
    if type(value) is not dict or frozenset(value) != _RECORD_FIELDS:
        raise ValueError
    mapping = cast(dict[str, object], value)
    tiers = mapping["allowed_tiers"]
    if (
        type(tiers) is not list
        or len(tiers) != len(set(map(str, tiers)))
        or any(type(tier) is not str for tier in tiers)
    ):
        raise ValueError
    return ProviderConsentRecord(
        consent_id=cast(str, mapping["consent_id"]),
        provider_id=cast(str, mapping["provider_id"]),
        egress_mode=EgressMode(cast(str, mapping["egress_mode"])),
        allowed_tiers=frozenset(PrivacyTier(cast(str, tier)) for tier in tiers),
        active=cast(bool, mapping["active"]),
        granted_generation=cast(int, mapping["granted_generation"]),
        granted_at=cast(str, mapping["granted_at"]),
        revoked_generation=cast(int | None, mapping["revoked_generation"]),
        revoked_at=cast(str | None, mapping["revoked_at"]),
    )


def _operation_from_mapping(value: object) -> _AppliedOperation:
    if type(value) is not dict or frozenset(value) != _OPERATION_FIELDS:
        raise ValueError
    mapping = cast(dict[str, object], value)
    raw_receipt = mapping["receipt"]
    if type(raw_receipt) is not dict or frozenset(raw_receipt) != _RECEIPT_FIELDS:
        raise ValueError
    receipt = cast(dict[str, object], raw_receipt)
    return _AppliedOperation(
        operation_id=cast(str, mapping["operation_id"]),
        request_sha256=cast(str, mapping["request_sha256"]),
        receipt=ConsentReceipt(
            operation=cast(str, receipt["operation"]),
            consent_id=cast(str, receipt["consent_id"]),
            authorization_generation=cast(int, receipt["authorization_generation"]),
            duplicate=cast(bool, receipt["duplicate"]),
            previous_consent_id=cast(str | None, receipt["previous_consent_id"]),
        ),
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> None:
    raise ValueError


def _require_owner(owner: bool) -> None:
    if owner is not True:
        raise ConsentContractError("owner_required")


def _validate_consent_id(value: str) -> str:
    if not isinstance(value, str) or _CONSENT_ID.fullmatch(value) is None:
        raise ConsentContractError("invalid_consent")
    return value


def _validate_provider_id(value: str) -> str:
    if not isinstance(value, str) or _PROVIDER_ID.fullmatch(value) is None:
        raise ConsentContractError("invalid_consent")
    return value


def _validate_operation_id(value: str) -> str:
    if not isinstance(value, str) or _OPERATION_ID.fullmatch(value) is None:
        raise ConsentContractError("invalid_consent")
    return value


def _validate_external_tiers(value: frozenset[PrivacyTier]) -> frozenset[PrivacyTier]:
    if (
        type(value) is not frozenset
        or not value
        or any(not isinstance(tier, PrivacyTier) for tier in value)
        or not value <= _EXTERNAL_TIERS
    ):
        raise ConsentContractError("invalid_consent")
    return value


def _validate_timestamp(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or any(
            ord(character) < 32 or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise ConsentContractError("invalid_consent")
    return value


def _request_digest(
    operation: str,
    *,
    provider_id: str | None,
    allowed_tiers: frozenset[PrivacyTier] | None,
    consent_id: str | None,
    egress_mode: EgressMode | None,
) -> str:
    value = {
        "operation": operation,
        "provider_id": provider_id,
        "allowed_tiers": (
            None if allowed_tiers is None else sorted(tier.value for tier in allowed_tiers)
        ),
        "consent_id": consent_id,
        "egress_mode": None if egress_mode is None else egress_mode.value,
    }
    return sha256(portable_canonical_json_bytes(value)).hexdigest()
