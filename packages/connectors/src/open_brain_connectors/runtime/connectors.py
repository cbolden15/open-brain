"""Versioned provisional extension values for optional capture connectors.

Only the names in ``__all__`` are published to connector distributions. The
interface remains provisional until reference, event, and measurement proofs all
pass. These values describe bounded capabilities; they are not a hostile-Python
sandbox by themselves.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from importlib.metadata import entry_points
from typing import Protocol, cast, runtime_checkable

from open_brain_engine.engine import (
    CaptureReceipt,
    ContentOrigin,
    Payload,
    PrivacyDecision,
    Provenance,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
)

CONNECTOR_API_STATUS = "provisional"
CONNECTOR_API_VERSION = 1
CONNECTOR_ENTRY_POINT_GROUP = "open_brain.connectors.v1"
INTERNAL_CONNECTOR_ENTRY_POINT_GROUP = "open_brain.internal_connectors.v1"

__all__ = [
    "CONNECTOR_API_STATUS",
    "CONNECTOR_API_VERSION",
    "CONNECTOR_ENTRY_POINT_GROUP",
    "ConnectorBudget",
    "ConnectorBudgetLimits",
    "ConnectorCaptureIdentity",
    "ConnectorCaptureSink",
    "ConnectorCheckpoint",
    "ConnectorContractError",
    "ConnectorFailureCode",
    "ConnectorManifest",
    "ConnectorMetadataLogger",
    "ConnectorOutcome",
    "ConnectorPayload",
    "ConnectorRunContext",
    "ConnectorRunEvidence",
    "ConnectorRunReceipt",
    "ConnectorTransport",
]

_CONNECTOR_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_ENTRY_POINT_VALUE = re.compile(r"[A-Za-z_][A-Za-z0-9_.]*(?::[A-Za-z_][A-Za-z0-9_]*)?")
_JOB_ID = re.compile(r"JOB-[0-9]{3}")
_CAPABILITY_NAME = re.compile(r"[a-z][a-z0-9_.-]{0,63}")
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")
_MAX_RECEIPT_COUNT = 1_000


class ConnectorContractError(ValueError):
    """A connector does not satisfy the closed internal extension contract."""


class ConnectorDiscoveryError(ConnectorContractError):
    """Entry-point metadata is malformed or not permitted by the profile."""


class ConnectorConfigurationError(ConnectorContractError):
    """A requested connector cannot be configured from the closed profile."""


class ConnectorCapabilityError(ConnectorContractError):
    """A connector requests an unsupported capability before it receives context."""


class ConnectorPayload(StrEnum):
    TEXT = "text"
    REFERENCE_OR_FILE = "reference_or_file"
    EVENT = "event"
    MEASUREMENT = "measurement"


class ConnectorOutcome(StrEnum):
    COMPLETED = "completed"
    EMPTY = "empty"
    DEFERRED = "deferred"
    FAILED = "failed"


class ConnectorFailureCode(StrEnum):
    EGRESS_DISABLED = "egress_disabled"
    NOT_ALLOWED = "not_allowed"
    NOT_DISCOVERED = "not_discovered"
    INVALID_REGISTRATION = "invalid_registration"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    INVALID_RECEIPT = "invalid_receipt"
    RUNTIME_FAILED = "runtime_failed"


@dataclass(frozen=True, slots=True)
class ConnectorManifest:
    """One versioned, closed declaration made before a connector receives authority."""

    schema_version: int
    name: str
    version: str
    payloads: tuple[ConnectorPayload, ...]
    schedules: tuple[str, ...]
    secrets: tuple[str, ...]
    action_authorities: tuple[str, ...]
    external_egress: bool

    def __post_init__(self) -> None:
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or not isinstance(self.name, str)
            or _CONNECTOR_NAME.fullmatch(self.name) is None
            or not isinstance(self.version, str)
            or _VERSION.fullmatch(self.version) is None
            or len(self.version) > 64
            or type(self.external_egress) is not bool
        ):
            raise ConnectorContractError("invalid connector manifest")
        try:
            payloads = tuple(ConnectorPayload(value) for value in self.payloads)
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid connector manifest") from error
        schedules = _ordered_unique(self.schedules, _JOB_ID)
        secrets = _ordered_unique(self.secrets, _CAPABILITY_NAME)
        action_authorities = _ordered_unique(self.action_authorities, _CAPABILITY_NAME)
        if (
            not payloads
            or len(payloads) > 4
            or len(schedules) > 32
            or len(secrets) > 32
            or len(action_authorities) > 16
            or payloads != tuple(sorted(set(payloads), key=str))
        ):
            raise ConnectorContractError("invalid connector manifest")
        object.__setattr__(self, "payloads", payloads)
        object.__setattr__(self, "schedules", schedules)
        object.__setattr__(self, "secrets", secrets)
        object.__setattr__(self, "action_authorities", action_authorities)

    @classmethod
    def from_dict(cls, value: object) -> ConnectorManifest:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "name",
            "version",
            "payloads",
            "schedules",
            "secrets",
            "action_authorities",
            "external_egress",
        }:
            raise ConnectorContractError("invalid connector manifest")
        payloads = value["payloads"]
        schedules = value["schedules"]
        secrets = value["secrets"]
        action_authorities = value["action_authorities"]
        if not all(
            isinstance(item, list)
            for item in (payloads, schedules, secrets, action_authorities)
        ):
            raise ConnectorContractError("invalid connector manifest")
        return cls(
            schema_version=cast(int, value["schema_version"]),
            name=cast(str, value["name"]),
            version=cast(str, value["version"]),
            payloads=tuple(cast(list[ConnectorPayload], payloads)),
            schedules=tuple(cast(list[str], schedules)),
            secrets=tuple(cast(list[str], secrets)),
            action_authorities=tuple(cast(list[str], action_authorities)),
            external_egress=cast(bool, value["external_egress"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "version": self.version,
            "payloads": [payload.value for payload in self.payloads],
            "schedules": list(self.schedules),
            "secrets": list(self.secrets),
            "action_authorities": list(self.action_authorities),
            "external_egress": self.external_egress,
        }


@dataclass(frozen=True, slots=True)
class ConnectorBudgetLimits:
    max_discoveries: int = 1_000
    max_fetches: int = 32
    max_extractions: int = 32
    max_submissions: int = 32

    def __post_init__(self) -> None:
        values = (
            self.max_discoveries,
            self.max_fetches,
            self.max_extractions,
            self.max_submissions,
        )
        if any(type(value) is not int or not 1 <= value <= _MAX_RECEIPT_COUNT for value in values):
            raise ConnectorContractError("invalid connector budget")


@dataclass(slots=True)
class _ConnectorBudgetMeter:
    limits: ConnectorBudgetLimits
    discovered_count: int = 0
    fetched_count: int = 0
    extracted_count: int = 0
    submitted_count: int = 0

    def consume_fetch(self) -> bool:
        if self.fetched_count >= self.limits.max_fetches:
            return False
        self.fetched_count += 1
        return True

    def consume_discovery(self) -> bool:
        if self.discovered_count >= self.limits.max_discoveries:
            return False
        self.discovered_count += 1
        return True

    def consume_extraction(self) -> bool:
        if self.extracted_count >= self.limits.max_extractions:
            return False
        self.extracted_count += 1
        return True

    def consume_submission(self) -> bool:
        if self.submitted_count >= self.limits.max_submissions:
            return False
        self.submitted_count += 1
        return True


class ConnectorBudget:
    """A read-only live view backed by a separate host-owned meter."""

    __slots__ = (
        "__meter",
        "_discovered_count",
        "_extracted_count",
        "_fetched_count",
        "_limits",
        "_submitted_count",
    )

    def __init__(self, limits: ConnectorBudgetLimits) -> None:
        if type(limits) is not ConnectorBudgetLimits:
            raise ConnectorContractError("invalid connector budget")
        meter_limits = _snapshot_budget_limits(limits)
        self.__meter = _ConnectorBudgetMeter(meter_limits)
        self._limits = _snapshot_budget_limits(meter_limits)
        self._discovered_count = 0
        self._fetched_count = 0
        self._extracted_count = 0
        self._submitted_count = 0

    @property
    def limits(self) -> ConnectorBudgetLimits:
        return self._limits

    @property
    def discovered_count(self) -> int:
        return self._discovered_count

    @property
    def fetched_count(self) -> int:
        return self._fetched_count

    @property
    def extracted_count(self) -> int:
        return self._extracted_count

    @property
    def submitted_count(self) -> int:
        return self._submitted_count

    def within_limits(self) -> bool:
        host_counts = self._host_counts()
        visible_counts = (
            self.discovered_count,
            self.fetched_count,
            self.extracted_count,
            self.submitted_count,
        )
        return (
            self.limits == self.__meter.limits
            and visible_counts == host_counts
            and all(
                type(value) is int and 0 <= value <= limit
                for value, limit in (
                    (host_counts[0], self.__meter.limits.max_discoveries),
                    (host_counts[1], self.__meter.limits.max_fetches),
                    (host_counts[2], self.__meter.limits.max_extractions),
                    (host_counts[3], self.__meter.limits.max_submissions),
                )
            )
        )

    @property
    def remaining_discoveries(self) -> int:
        return self.__meter.limits.max_discoveries - self.__meter.discovered_count

    @property
    def remaining_extractions(self) -> int:
        return self.__meter.limits.max_extractions - self.__meter.extracted_count

    @property
    def remaining_submissions(self) -> int:
        return self.__meter.limits.max_submissions - self.__meter.submitted_count

    def _consume_fetch(self) -> bool:
        consumed = self.__meter.consume_fetch()
        self._fetched_count = self.__meter.fetched_count
        return consumed

    def _consume_discovery(self) -> bool:
        consumed = self.__meter.consume_discovery()
        self._discovered_count = self.__meter.discovered_count
        return consumed

    def _consume_extraction(self) -> bool:
        consumed = self.__meter.consume_extraction()
        self._extracted_count = self.__meter.extracted_count
        return consumed

    def _consume_submission(self) -> bool:
        consumed = self.__meter.consume_submission()
        self._submitted_count = self.__meter.submitted_count
        return consumed

    def _host_counts(self) -> tuple[int, int, int, int]:
        return (
            self.__meter.discovered_count,
            self.__meter.fetched_count,
            self.__meter.extracted_count,
            self.__meter.submitted_count,
        )


@dataclass(frozen=True, slots=True)
class ConnectorProfile:
    """App-owned connector settings; the default profile discovers no connectors."""

    allow_list: tuple[str, ...] = ()
    egress_enabled: bool = False
    budget_limits: ConnectorBudgetLimits = ConnectorBudgetLimits()

    def __post_init__(self) -> None:
        allow_list = _ordered_unique(self.allow_list, _CONNECTOR_NAME)
        if (
            len(allow_list) > 32
            or type(self.egress_enabled) is not bool
            or type(self.budget_limits) is not ConnectorBudgetLimits
        ):
            raise ConnectorContractError("invalid connector profile")
        object.__setattr__(self, "allow_list", allow_list)

    def allows(self, connector_name: str) -> bool:
        return connector_name in self.allow_list


@dataclass(frozen=True, slots=True)
class ConnectorCaptureIdentity:
    """An opaque non-owner public-capture identity selected by the app."""

    connector_name: str
    job_id: str
    actor: PublicJobCaptureContext

    def __post_init__(self) -> None:
        if (
            not isinstance(self.connector_name, str)
            or _CONNECTOR_NAME.fullmatch(self.connector_name) is None
            or not isinstance(self.job_id, str)
            or _JOB_ID.fullmatch(self.job_id) is None
            or not isinstance(self.actor, PublicJobCaptureContext)
        ):
            raise ConnectorContractError("invalid connector capture identity")


class ConnectorMetadataLogger:
    """Bounded event-name-only metadata collection; payloads never enter this sink."""

    def __init__(self, *, maximum_events: int = 16) -> None:
        if type(maximum_events) is not int or not 1 <= maximum_events <= 64:
            raise ConnectorContractError("invalid connector metadata logger")
        self._maximum_events = maximum_events
        self._events: list[str] = []

    @property
    def count(self) -> int:
        return len(self._events)

    def record(self, event: str) -> None:
        if _CAPABILITY_NAME.fullmatch(event) is None:
            raise ConnectorContractError("invalid connector metadata event")
        if len(self._events) < self._maximum_events:
            self._events.append(event)


class ConnectorRunEvidence:
    """Host-owned binding between accepted captures and checkpoint commits."""

    __slots__ = ("__capture_receipts",)

    def __init__(self) -> None:
        self.__capture_receipts: dict[str, tuple[int, str, bool, str]] = {}

    def record_capture(
        self,
        delivery_id: str,
        source_reference: str,
        receipt: CaptureReceipt,
    ) -> None:
        if (
            type(delivery_id) is not str
            or not delivery_id
            or type(source_reference) is not str
            or not source_reference
            or type(receipt) is not CaptureReceipt
        ):
            raise ConnectorContractError("invalid connector capture evidence")
        self.__capture_receipts[delivery_id] = (
            id(receipt),
            receipt.capture_id,
            receipt.duplicate,
            source_reference,
        )

    def authorizes_checkpoint(
        self,
        delivery_id: str,
        source_reference: str,
        receipt: CaptureReceipt,
    ) -> bool:
        if (
            type(delivery_id) is not str
            or type(source_reference) is not str
            or type(receipt) is not CaptureReceipt
        ):
            return False
        return self.__capture_receipts.get(delivery_id) == (
            id(receipt),
            receipt.capture_id,
            receipt.duplicate,
            source_reference,
        )


class ConnectorCaptureSink:
    """Host-owned capture capability that enforces and records submission bounds."""

    __slots__ = (
        "__budget",
        "__created_count",
        "__duplicate_count",
        "__evidence",
        "__sink",
    )

    def __init__(
        self,
        sink: PublicJobCaptureSink,
        budget: ConnectorBudget,
        evidence: ConnectorRunEvidence,
    ) -> None:
        if (
            type(sink) is not PublicJobCaptureSink
            or type(budget) is not ConnectorBudget
            or type(evidence) is not ConnectorRunEvidence
        ):
            raise ConnectorContractError("invalid connector capture sink")
        self.__sink = sink
        self.__budget = budget
        self.__evidence = evidence
        self.__created_count = 0
        self.__duplicate_count = 0

    @property
    def context(self) -> PublicJobCaptureContext:
        return self.__sink.context

    @property
    def budget(self) -> ConnectorBudget:
        return self.__budget

    @property
    def created_count(self) -> int:
        return self.__created_count

    @property
    def duplicate_count(self) -> int:
        return self.__duplicate_count

    def submit(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        source_origin: ContentOrigin | str,
        source_reference: str,
        provenance: Provenance,
        privacy: PrivacyDecision,
        intent: str | None = None,
        title: str | None = None,
    ) -> CaptureReceipt:
        if not self.__budget._consume_submission():
            raise ConnectorContractError("connector submission budget exhausted")
        receipt = self.__sink.submit(
            payload,
            delivery_id=delivery_id,
            source_origin=source_origin,
            source_reference=source_reference,
            provenance=provenance,
            privacy=privacy,
            intent=intent,
            title=title,
        )
        self.__evidence.record_capture(delivery_id, source_reference, receipt)
        if receipt.duplicate:
            self.__duplicate_count += 1
        else:
            self.__created_count += 1
        return receipt


@runtime_checkable
class ConnectorTransport(Protocol):
    """Opaque connector-specific transport capability selected by the app."""

    @property
    def connector_name(self) -> str: ...

    @property
    def budget(self) -> ConnectorBudget | None: ...

    def bind_budget(self, budget: ConnectorBudget) -> ConnectorTransport: ...


@runtime_checkable
class ConnectorCheckpoint(Protocol):
    """Opaque connector-specific checkpoint capability selected by the app."""

    @property
    def connector_name(self) -> str: ...

    @property
    def budget(self) -> ConnectorBudget | None: ...

    @property
    def checkpoint_committed(self) -> bool: ...

    def bind_run(
        self,
        budget: ConnectorBudget,
        evidence: ConnectorRunEvidence,
    ) -> ConnectorCheckpoint: ...


@dataclass(frozen=True, slots=True)
class ConnectorRunContext:
    """The complete capability set available to an internal capture connector."""

    capture_identity: ConnectorCaptureIdentity
    capture_sink: PublicJobCaptureSink | ConnectorCaptureSink
    transport: ConnectorTransport
    checkpoint: ConnectorCheckpoint
    clock: Callable[[], datetime]
    budget: ConnectorBudget
    metadata_logger: ConnectorMetadataLogger

    def __post_init__(self) -> None:
        if (
            type(self.capture_identity) is not ConnectorCaptureIdentity
            or not isinstance(self.capture_sink, PublicJobCaptureSink | ConnectorCaptureSink)
            or self.capture_sink.context != self.capture_identity.actor
            or not isinstance(self.transport, ConnectorTransport)
            or self.transport.connector_name != self.capture_identity.connector_name
            or not isinstance(self.checkpoint, ConnectorCheckpoint)
            or self.checkpoint.connector_name != self.capture_identity.connector_name
            or not callable(self.clock)
            or type(self.budget) is not ConnectorBudget
            or type(self.metadata_logger) is not ConnectorMetadataLogger
        ):
            raise ConnectorContractError("invalid connector run context")


@dataclass(frozen=True, slots=True)
class ConnectorRunReceipt:
    """Bounded metadata-only result for a connector invocation."""

    connector_name: str
    outcome: ConnectorOutcome
    failure_code: ConnectorFailureCode | None
    discovered_count: int
    fetched_count: int
    extracted_count: int
    submitted_count: int
    stubbed_count: int
    created_count: int
    duplicate_count: int
    checkpoint_committed: bool
    metadata_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.connector_name, str)
            or _CONNECTOR_NAME.fullmatch(self.connector_name) is None
        ):
            raise ConnectorContractError("invalid connector receipt")
        try:
            outcome = ConnectorOutcome(self.outcome)
            failure_code = (
                None if self.failure_code is None else ConnectorFailureCode(self.failure_code)
            )
        except (TypeError, ValueError) as error:
            raise ConnectorContractError("invalid connector receipt") from error
        counts = (
            self.discovered_count,
            self.fetched_count,
            self.extracted_count,
            self.submitted_count,
            self.stubbed_count,
            self.created_count,
            self.duplicate_count,
            self.metadata_count,
        )
        if (
            any(type(value) is not int or not 0 <= value <= _MAX_RECEIPT_COUNT for value in counts)
            or type(self.checkpoint_committed) is not bool
            or self.submitted_count != self.created_count + self.duplicate_count
            or (outcome is ConnectorOutcome.COMPLETED and failure_code is not None)
            or (
                outcome is ConnectorOutcome.EMPTY
                and (failure_code is not None or any(counts[:-1]) or self.checkpoint_committed)
            )
            or (
                outcome is ConnectorOutcome.DEFERRED
                and failure_code is not ConnectorFailureCode.EGRESS_DISABLED
            )
            or (
                outcome not in {
                    ConnectorOutcome.COMPLETED,
                    ConnectorOutcome.EMPTY,
                    ConnectorOutcome.DEFERRED,
                }
                and failure_code is None
            )
            or (
                outcome is ConnectorOutcome.DEFERRED
                and (any(counts) or self.checkpoint_committed)
            )
            or (outcome is ConnectorOutcome.FAILED and self.checkpoint_committed)
        ):
            raise ConnectorContractError("invalid connector receipt")
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "failure_code", failure_code)

    @classmethod
    def deferred(cls, connector_name: str) -> ConnectorRunReceipt:
        return cls(
            connector_name=connector_name,
            outcome=ConnectorOutcome.DEFERRED,
            failure_code=ConnectorFailureCode.EGRESS_DISABLED,
            discovered_count=0,
            fetched_count=0,
            extracted_count=0,
            submitted_count=0,
            stubbed_count=0,
            created_count=0,
            duplicate_count=0,
            checkpoint_committed=False,
            metadata_count=0,
        )

    @classmethod
    def empty(cls, connector_name: str, *, metadata_count: int = 0) -> ConnectorRunReceipt:
        return cls(
            connector_name=connector_name,
            outcome=ConnectorOutcome.EMPTY,
            failure_code=None,
            discovered_count=0,
            fetched_count=0,
            extracted_count=0,
            submitted_count=0,
            stubbed_count=0,
            created_count=0,
            duplicate_count=0,
            checkpoint_committed=False,
            metadata_count=metadata_count,
        )

    @classmethod
    def failed(
        cls,
        connector_name: str,
        failure_code: ConnectorFailureCode,
    ) -> ConnectorRunReceipt:
        return cls(
            connector_name=connector_name,
            outcome=ConnectorOutcome.FAILED,
            failure_code=failure_code,
            discovered_count=0,
            fetched_count=0,
            extracted_count=0,
            submitted_count=0,
            stubbed_count=0,
            created_count=0,
            duplicate_count=0,
            checkpoint_committed=False,
            metadata_count=0,
        )

    @classmethod
    def from_dict(cls, value: object) -> ConnectorRunReceipt:
        fields = {
            "checkpoint_committed",
            "connector_name",
            "created_count",
            "discovered_count",
            "duplicate_count",
            "extracted_count",
            "failure_code",
            "fetched_count",
            "metadata_count",
            "outcome",
            "stubbed_count",
            "submitted_count",
        }
        if not isinstance(value, dict) or set(value) != fields:
            raise ConnectorContractError("invalid connector receipt")
        try:
            return cls(
                connector_name=cast(str, value["connector_name"]),
                outcome=cast(ConnectorOutcome, value["outcome"]),
                failure_code=cast(ConnectorFailureCode | None, value["failure_code"]),
                discovered_count=cast(int, value["discovered_count"]),
                fetched_count=cast(int, value["fetched_count"]),
                extracted_count=cast(int, value["extracted_count"]),
                submitted_count=cast(int, value["submitted_count"]),
                stubbed_count=cast(int, value["stubbed_count"]),
                created_count=cast(int, value["created_count"]),
                duplicate_count=cast(int, value["duplicate_count"]),
                checkpoint_committed=cast(bool, value["checkpoint_committed"]),
                metadata_count=cast(int, value["metadata_count"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ConnectorContractError("invalid connector receipt") from error

    def to_dict(self) -> dict[str, object]:
        """Return only bounded receipt metadata; no connector payload crosses this boundary."""
        return {
            "checkpoint_committed": self.checkpoint_committed,
            "connector_name": self.connector_name,
            "created_count": self.created_count,
            "discovered_count": self.discovered_count,
            "duplicate_count": self.duplicate_count,
            "extracted_count": self.extracted_count,
            "failure_code": None if self.failure_code is None else self.failure_code.value,
            "fetched_count": self.fetched_count,
            "metadata_count": self.metadata_count,
            "outcome": self.outcome.value,
            "stubbed_count": self.stubbed_count,
            "submitted_count": self.submitted_count,
        }


@dataclass(frozen=True, slots=True)
class ConnectorEntryPointMetadata:
    name: str
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or _CONNECTOR_NAME.fullmatch(self.name) is None
            or not isinstance(self.value, str)
            or _ENTRY_POINT_VALUE.fullmatch(self.value) is None
            or len(self.value) > 512
        ):
            raise ConnectorDiscoveryError("invalid connector entry-point metadata")


class ConnectorEntryPoint(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def value(self) -> str: ...

    def load(self) -> object: ...


class ConnectorEntryPointSource(Protocol):
    def entry_points(self, *, group: str) -> Sequence[ConnectorEntryPoint]: ...


class InstalledConnectorEntryPointSource:
    """Production metadata source. Calling it never imports a connector module."""

    def entry_points(self, *, group: str) -> Sequence[ConnectorEntryPoint]:
        if group != CONNECTOR_ENTRY_POINT_GROUP:
            raise ConnectorDiscoveryError("invalid connector entry-point group")
        return tuple(cast(Sequence[ConnectorEntryPoint], entry_points(group=group)))


@dataclass(frozen=True, slots=True)
class ConnectorCapabilityPolicy:
    payloads: frozenset[ConnectorPayload] = frozenset({ConnectorPayload.REFERENCE_OR_FILE})
    schedules: frozenset[str] = frozenset({"JOB-029"})
    secrets: frozenset[str] = frozenset()
    action_authorities: frozenset[str] = frozenset()
    external_egress: bool = True

    def __post_init__(self) -> None:
        if (
            not isinstance(self.payloads, frozenset)
            or any(not isinstance(value, ConnectorPayload) for value in self.payloads)
            or not isinstance(self.schedules, frozenset)
            or any(
                not isinstance(value, str) or _JOB_ID.fullmatch(value) is None
                for value in self.schedules
            )
            or not isinstance(self.secrets, frozenset)
            or any(
                not isinstance(value, str) or _CAPABILITY_NAME.fullmatch(value) is None
                for value in self.secrets
            )
            or not isinstance(self.action_authorities, frozenset)
            or any(
                not isinstance(value, str) or _CAPABILITY_NAME.fullmatch(value) is None
                for value in self.action_authorities
            )
            or type(self.external_egress) is not bool
        ):
            raise ConnectorContractError("invalid connector capability policy")

    def validate(self, manifest: ConnectorManifest) -> None:
        if (
            not set(manifest.payloads) <= self.payloads
            or not set(manifest.schedules) <= self.schedules
            or not set(manifest.secrets) <= self.secrets
            or not set(manifest.action_authorities) <= self.action_authorities
            or (manifest.external_egress and not self.external_egress)
        ):
            raise ConnectorCapabilityError("unsupported connector capability")


class InternalConnector(Protocol):
    @property
    def manifest(self) -> ConnectorManifest: ...

    def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt: ...


@dataclass(frozen=True, slots=True)
class _ResolvedConnector:
    """One manifest and bound run method captured under registry validation."""

    manifest: ConnectorManifest
    run: Callable[[ConnectorRunContext], ConnectorRunReceipt]


class ConnectorRegistry:
    """Discover installed metadata or resolve an explicitly injected connector."""

    def __init__(
        self,
        source: ConnectorEntryPointSource | None = None,
        *,
        capability_policy: ConnectorCapabilityPolicy | None = None,
    ) -> None:
        if capability_policy is None:
            capability_policy = ConnectorCapabilityPolicy()
        if type(capability_policy) is not ConnectorCapabilityPolicy:
            raise ConnectorContractError("invalid connector registry")
        selected_source = InstalledConnectorEntryPointSource() if source is None else source
        self._source = selected_source
        self._installed_metadata_only = isinstance(
            selected_source, InstalledConnectorEntryPointSource
        )
        self._entry_point_group = (
            CONNECTOR_ENTRY_POINT_GROUP
            if self._installed_metadata_only
            else INTERNAL_CONNECTOR_ENTRY_POINT_GROUP
        )
        self._capability_policy = _snapshot_capability_policy(capability_policy)

    def discover(self, profile: ConnectorProfile) -> tuple[ConnectorEntryPointMetadata, ...]:
        try:
            selected_profile = _snapshot_profile(profile)
            return tuple(
                metadata
                for metadata, _entry in self._validated_entries(selected_profile)
            )
        except ConnectorDiscoveryError:
            raise
        except Exception as error:
            raise ConnectorDiscoveryError("invalid connector profile") from error

    def _validated_entries(
        self, profile: ConnectorProfile
    ) -> tuple[tuple[ConnectorEntryPointMetadata, ConnectorEntryPoint], ...]:
        try:
            entries = tuple(self._source.entry_points(group=self._entry_point_group))
        except Exception as error:
            raise ConnectorDiscoveryError("invalid connector entry-point metadata") from error
        if len(entries) > 32:
            raise ConnectorDiscoveryError("too many connector entry-point registrations")
        validated: list[tuple[ConnectorEntryPointMetadata, ConnectorEntryPoint]] = []
        for entry in entries:
            try:
                metadata = ConnectorEntryPointMetadata(name=entry.name, value=entry.value)
            except Exception as error:
                raise ConnectorDiscoveryError("invalid connector entry-point metadata") from error
            validated.append((metadata, entry))
        names = [metadata.name for metadata, _entry in validated]
        if len(names) != len(set(names)):
            raise ConnectorDiscoveryError("duplicate connector entry-point registration")
        return tuple(
            sorted(
                (
                    (metadata, entry)
                    for metadata, entry in validated
                    if profile.allows(metadata.name)
                ),
                key=lambda item: item[0].name,
            )
        )

    def resolve(self, connector_name: str, profile: ConnectorProfile) -> _ResolvedConnector:
        _require_connector_name(connector_name)
        try:
            selected_profile = _snapshot_profile(profile)
        except Exception as error:
            raise ConnectorConfigurationError("invalid connector profile") from error
        if not selected_profile.allows(connector_name):
            raise ConnectorConfigurationError("connector is not allow-listed")
        if self._installed_metadata_only:
            raise ConnectorConfigurationError("installed connector requires isolated worker")
        entries = self._validated_entries(selected_profile)
        selected = next(
            (entry for metadata, entry in entries if metadata.name == connector_name),
            None,
        )
        if selected is None:
            raise ConnectorConfigurationError("connector is not discovered")
        try:
            connector = selected.load()
        except Exception as error:
            raise ConnectorConfigurationError("connector could not load") from error
        if isinstance(connector, type):
            raise ConnectorConfigurationError("connector entry point must return an instance")
        try:
            manifest = getattr(connector, "manifest", None)
            run = getattr(connector, "run", None)
        except Exception as error:
            raise ConnectorConfigurationError("invalid connector registration") from error
        if type(manifest) is not ConnectorManifest or not callable(run):
            raise ConnectorConfigurationError("invalid connector registration")
        try:
            validated_manifest = _snapshot_manifest(manifest)
        except Exception as error:
            raise ConnectorConfigurationError("invalid connector registration") from error
        if validated_manifest.name != connector_name:
            raise ConnectorConfigurationError("connector name does not match entry point")
        self._capability_policy.validate(validated_manifest)
        return _ResolvedConnector(
            manifest=validated_manifest,
            run=cast(Callable[[ConnectorRunContext], ConnectorRunReceipt], run),
        )


RunContextFactory = Callable[
    [ConnectorManifest, ConnectorBudget, ConnectorMetadataLogger],
    ConnectorRunContext,
]


class ConnectorHost:
    """App-owned discovery and execution boundary for one explicitly requested connector."""

    def __init__(self, registry: ConnectorRegistry | None = None) -> None:
        selected = ConnectorRegistry() if registry is None else registry
        if type(selected) is not ConnectorRegistry:
            raise ConnectorContractError("invalid connector host")
        self._registry = selected

    def discover(self, profile: ConnectorProfile) -> tuple[ConnectorEntryPointMetadata, ...]:
        return self._registry.discover(profile)

    def run(
        self,
        connector_name: str,
        *,
        profile: ConnectorProfile,
        context_factory: RunContextFactory,
    ) -> ConnectorRunReceipt:
        try:
            _require_connector_name(connector_name)
            if type(profile) is not ConnectorProfile or not callable(context_factory):
                raise ConnectorContractError("invalid connector invocation")
            selected_profile = _snapshot_profile(profile)
        except Exception:
            return ConnectorRunReceipt.failed(
                connector_name
                if type(connector_name) is str
                and _CONNECTOR_NAME.fullmatch(connector_name) is not None
                else "invalid",
                ConnectorFailureCode.NOT_ALLOWED,
            )
        if not selected_profile.allows(connector_name):
            return ConnectorRunReceipt.failed(connector_name, ConnectorFailureCode.NOT_ALLOWED)
        if not selected_profile.egress_enabled:
            return ConnectorRunReceipt.deferred(connector_name)
        try:
            connector = self._registry.resolve(connector_name, selected_profile)
        except ConnectorCapabilityError:
            return ConnectorRunReceipt.failed(
                connector_name, ConnectorFailureCode.UNSUPPORTED_CAPABILITY
            )
        except ConnectorDiscoveryError:
            return ConnectorRunReceipt.failed(
                connector_name, ConnectorFailureCode.INVALID_REGISTRATION
            )
        except ConnectorConfigurationError as error:
            failure = (
                ConnectorFailureCode.NOT_DISCOVERED
                if "not discovered" in str(error)
                else ConnectorFailureCode.INVALID_REGISTRATION
            )
            return ConnectorRunReceipt.failed(connector_name, failure)
        except Exception:
            return ConnectorRunReceipt.failed(
                connector_name, ConnectorFailureCode.RUNTIME_FAILED
            )
        budget = ConnectorBudget(selected_profile.budget_limits)
        logger = ConnectorMetadataLogger()
        try:
            candidate_context = context_factory(
                _snapshot_manifest(connector.manifest), budget, logger
            )
        except Exception:
            return ConnectorRunReceipt.failed(
                connector_name, ConnectorFailureCode.RUNTIME_FAILED
            )
        try:
            context_valid = _run_context_is_valid(
                candidate_context,
                connector_name=connector_name,
                schedules=connector.manifest.schedules,
                budget=budget,
                logger=logger,
                bounded=False,
            )
            context = _bind_run_context(candidate_context, budget=budget)
            context_valid = context_valid and _run_context_is_valid(
                context,
                connector_name=connector_name,
                schedules=connector.manifest.schedules,
                budget=budget,
                logger=logger,
                bounded=True,
            )
        except Exception:
            context_valid = False
        if not context_valid:
            return ConnectorRunReceipt.failed(connector_name, ConnectorFailureCode.INVALID_RECEIPT)
        try:
            receipt = connector.run(context)
        except Exception:
            return ConnectorRunReceipt.failed(
                connector_name, ConnectorFailureCode.RUNTIME_FAILED
            )
        try:
            if type(receipt) is not ConnectorRunReceipt:
                raise ConnectorContractError("invalid connector receipt")
            validated_receipt = _snapshot_receipt(receipt)
            sink = context.capture_sink
            if type(sink) is not ConnectorCaptureSink:
                raise ConnectorContractError("invalid connector capture sink")
            (
                discovered_count,
                fetched_count,
                extracted_count,
                submitted_count,
            ) = budget._host_counts()
            receipt_valid = (
                validated_receipt.connector_name == connector_name
                and budget.within_limits()
                and validated_receipt.discovered_count == discovered_count
                and validated_receipt.fetched_count == fetched_count
                and validated_receipt.extracted_count == extracted_count
                and validated_receipt.submitted_count == submitted_count
                and validated_receipt.created_count == sink.created_count
                and validated_receipt.duplicate_count == sink.duplicate_count
                and validated_receipt.stubbed_count <= extracted_count
                and validated_receipt.checkpoint_committed
                is context.checkpoint.checkpoint_committed
                and validated_receipt.metadata_count == logger.count
            )
            host_receipt = ConnectorRunReceipt(
                connector_name=connector_name,
                outcome=validated_receipt.outcome,
                failure_code=validated_receipt.failure_code,
                discovered_count=discovered_count,
                fetched_count=fetched_count,
                extracted_count=extracted_count,
                submitted_count=submitted_count,
                stubbed_count=validated_receipt.stubbed_count,
                created_count=sink.created_count,
                duplicate_count=sink.duplicate_count,
                checkpoint_committed=context.checkpoint.checkpoint_committed,
                metadata_count=logger.count,
            )
        except Exception:
            receipt_valid = False
        if not receipt_valid:
            return ConnectorRunReceipt.failed(connector_name, ConnectorFailureCode.INVALID_RECEIPT)
        return host_receipt


def _snapshot_capability_policy(
    value: ConnectorCapabilityPolicy,
) -> ConnectorCapabilityPolicy:
    if type(value) is not ConnectorCapabilityPolicy:
        raise ConnectorContractError("invalid connector capability policy")
    return ConnectorCapabilityPolicy(
        payloads=frozenset(value.payloads),
        schedules=frozenset(value.schedules),
        secrets=frozenset(value.secrets),
        action_authorities=frozenset(value.action_authorities),
        external_egress=value.external_egress,
    )


def _snapshot_budget_limits(value: ConnectorBudgetLimits) -> ConnectorBudgetLimits:
    if type(value) is not ConnectorBudgetLimits:
        raise ConnectorContractError("invalid connector budget")
    return ConnectorBudgetLimits(
        max_discoveries=value.max_discoveries,
        max_fetches=value.max_fetches,
        max_extractions=value.max_extractions,
        max_submissions=value.max_submissions,
    )


def _snapshot_profile(value: ConnectorProfile) -> ConnectorProfile:
    if type(value) is not ConnectorProfile:
        raise ConnectorContractError("invalid connector profile")
    return ConnectorProfile(
        allow_list=tuple(value.allow_list),
        egress_enabled=value.egress_enabled,
        budget_limits=_snapshot_budget_limits(value.budget_limits),
    )


def _snapshot_manifest(value: ConnectorManifest) -> ConnectorManifest:
    if type(value) is not ConnectorManifest:
        raise ConnectorContractError("invalid connector manifest")
    return ConnectorManifest(
        schema_version=value.schema_version,
        name=value.name,
        version=value.version,
        payloads=tuple(value.payloads),
        schedules=tuple(value.schedules),
        secrets=tuple(value.secrets),
        action_authorities=tuple(value.action_authorities),
        external_egress=value.external_egress,
    )


def _snapshot_receipt(value: ConnectorRunReceipt) -> ConnectorRunReceipt:
    if type(value) is not ConnectorRunReceipt:
        raise ConnectorContractError("invalid connector receipt")
    return ConnectorRunReceipt(
        connector_name=value.connector_name,
        outcome=value.outcome,
        failure_code=value.failure_code,
        discovered_count=value.discovered_count,
        fetched_count=value.fetched_count,
        extracted_count=value.extracted_count,
        submitted_count=value.submitted_count,
        stubbed_count=value.stubbed_count,
        created_count=value.created_count,
        duplicate_count=value.duplicate_count,
        checkpoint_committed=value.checkpoint_committed,
        metadata_count=value.metadata_count,
    )


def _run_context_is_valid(
    value: object,
    *,
    connector_name: str,
    schedules: tuple[str, ...],
    budget: ConnectorBudget,
    logger: ConnectorMetadataLogger,
    bounded: bool,
) -> bool:
    if type(value) is not ConnectorRunContext:
        return False
    context = value
    identity = context.capture_identity
    sink = context.capture_sink
    actor = identity.actor
    sink_is_valid = (
        type(sink) is ConnectorCaptureSink
        and sink.budget is budget
        if bounded
        else type(sink) is PublicJobCaptureSink
    )
    capability_budgets_are_valid = (
        context.transport.budget is budget and context.checkpoint.budget is budget
        if bounded
        else context.transport.budget is None and context.checkpoint.budget is None
    )
    return (
        type(identity) is ConnectorCaptureIdentity
        and identity.connector_name == connector_name
        and identity.job_id in schedules
        and type(actor) is PublicJobCaptureContext
        and actor.role_claim["capabilities"] == ("capture.accept",)
        and sink_is_valid
        and sink.context == actor
        and isinstance(context.transport, ConnectorTransport)
        and context.transport.connector_name == connector_name
        and isinstance(context.checkpoint, ConnectorCheckpoint)
        and context.checkpoint.connector_name == connector_name
        and capability_budgets_are_valid
        and callable(context.clock)
        and context.budget is budget
        and context.metadata_logger is logger
    )


def _bind_run_context(
    value: ConnectorRunContext,
    *,
    budget: ConnectorBudget,
) -> ConnectorRunContext:
    if (
        type(value) is not ConnectorRunContext
        or type(value.capture_sink) is not PublicJobCaptureSink
    ):
        raise ConnectorContractError("invalid connector run context")
    evidence = ConnectorRunEvidence()
    transport = value.transport.bind_budget(budget)
    checkpoint = value.checkpoint.bind_run(budget, evidence)
    return ConnectorRunContext(
        capture_identity=value.capture_identity,
        capture_sink=ConnectorCaptureSink(value.capture_sink, budget, evidence),
        transport=transport,
        checkpoint=checkpoint,
        clock=value.clock,
        budget=budget,
        metadata_logger=value.metadata_logger,
    )


def _ordered_unique(values: object, pattern: re.Pattern[str]) -> tuple[str, ...]:
    if not isinstance(values, tuple) or any(
        type(value) is not str or pattern.fullmatch(value) is None for value in values
    ):
        raise ConnectorContractError("invalid connector manifest")
    if values != tuple(sorted(set(values))):
        raise ConnectorContractError("invalid connector manifest")
    return values


def _require_connector_name(value: object) -> str:
    if type(value) is not str or _CONNECTOR_NAME.fullmatch(value) is None:
        raise ConnectorContractError("invalid connector name")
    return value
