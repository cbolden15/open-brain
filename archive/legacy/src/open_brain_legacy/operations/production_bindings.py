"""Closed, typed composition for every production scheduler catalog job.

This module intentionally wires only typed capabilities.  It neither loads configuration
nor obtains filesystem, service, or network capabilities; a production composition root
must supply those capabilities explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from open_brain_engine.engine import LockScope

from .catalog import JOB_CATALOG, get_job
from .models import HostRole, JobSpec, WriterScope
from .scheduled_results import (
    ScheduledDispatchResult as ScheduledDispatchResult,
)
from .scheduled_results import (
    ScheduledDispatchStatus as ScheduledDispatchStatus,
)
from .scheduler import EXPECTED_JOB_IDS, validate_job_catalog


class ProductionBindingError(ValueError):
    """A scheduler composition is incomplete or exceeds its catalog authority."""


class ApplicationFamily(StrEnum):
    """Concrete application families permitted in the production scheduler."""

    BACKUP = "backup"
    CAPTURE = "capture"
    CURATION = "curation"
    DOCTOR = "doctor"
    GIT_SYNC = "git-sync"
    INDEX = "index"
    LEDGER = "ledger"
    LIFEOS = "lifeos"
    LINT = "lint"
    MESSAGING = "messaging"
    NOW = "now"
    RETENTION = "retention"
    UI = "ui"


class BindingAuthority(StrEnum):
    """The only scheduler authorities a bound capability may claim."""

    NONE = "none"
    CANONICAL_WRITER = "canonical-writer"
    APPEND_ONLY_INGRESS = "append-only-ingress"


_REPLAY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


@dataclass(frozen=True, slots=True)
class ScheduledInvocation:
    """Opaque replay identity delivered to one already-authorized capability."""

    job: JobSpec
    replay_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.job, JobSpec):
            raise ProductionBindingError("invalid scheduled invocation job")
        if not isinstance(self.replay_key, str) or _REPLAY_KEY.fullmatch(self.replay_key) is None:
            raise ProductionBindingError("invalid scheduled invocation replay key")


class ScheduledCapability(Protocol):
    """One effect-capable application supplied by a production composition root."""

    authority: BindingAuthority
    lock_scope: LockScope
    writer_identity: str | None

    def dispatch(self, invocation: ScheduledInvocation) -> ScheduledDispatchResult: ...


@dataclass(frozen=True, slots=True)
class ProductionJobBinding:
    """The closed authority and application allocation for one catalog job."""

    job_id: str
    family: ApplicationFamily
    writer_scope: WriterScope
    lock_scope: LockScope
    authority: BindingAuthority
    capability: ScheduledCapability

    def __post_init__(self) -> None:
        if (
            self.job_id not in EXPECTED_JOB_IDS
            or not isinstance(self.family, ApplicationFamily)
            or not isinstance(self.writer_scope, WriterScope)
            or not isinstance(self.lock_scope, LockScope)
            or not isinstance(self.authority, BindingAuthority)
            or not callable(getattr(self.capability, "dispatch", None))
        ):
            raise ProductionBindingError("invalid production scheduler binding")


_FAMILIES: Mapping[str, ApplicationFamily] = MappingProxyType(
    {
        "JOB-001": ApplicationFamily.DOCTOR,
        "JOB-002": ApplicationFamily.INDEX,
        "JOB-003": ApplicationFamily.NOW,
        "JOB-004": ApplicationFamily.BACKUP,
        "JOB-005": ApplicationFamily.CAPTURE,
        "JOB-006": ApplicationFamily.CURATION,
        "JOB-007": ApplicationFamily.CAPTURE,
        "JOB-008": ApplicationFamily.LINT,
        "JOB-009": ApplicationFamily.CURATION,
        "JOB-010": ApplicationFamily.LEDGER,
        "JOB-011": ApplicationFamily.BACKUP,
        "JOB-012": ApplicationFamily.CURATION,
        "JOB-013": ApplicationFamily.DOCTOR,
        "JOB-014": ApplicationFamily.BACKUP,
        "JOB-015": ApplicationFamily.GIT_SYNC,
        "JOB-016": ApplicationFamily.INDEX,
        "JOB-017": ApplicationFamily.LIFEOS,
        "JOB-018": ApplicationFamily.LIFEOS,
        "JOB-019": ApplicationFamily.LIFEOS,
        "JOB-020": ApplicationFamily.MESSAGING,
        "JOB-021": ApplicationFamily.MESSAGING,
        "JOB-022": ApplicationFamily.NOW,
        "JOB-023": ApplicationFamily.BACKUP,
        "JOB-024": ApplicationFamily.RETENTION,
        "JOB-025": ApplicationFamily.BACKUP,
        "JOB-026": ApplicationFamily.UI,
        "JOB-027": ApplicationFamily.CAPTURE,
        "JOB-028": ApplicationFamily.CAPTURE,
        "JOB-029": ApplicationFamily.CAPTURE,
        "JOB-030": ApplicationFamily.NOW,
    }
)


def _required_authority(job: JobSpec) -> BindingAuthority:
    if job.host_role is HostRole.WRITER:
        return BindingAuthority.CANONICAL_WRITER
    if job.host_role is HostRole.INGRESS:
        return BindingAuthority.APPEND_ONLY_INGRESS
    return BindingAuthority.NONE


@dataclass(frozen=True, slots=True)
class ProductionBindingInventory:
    """An immutable, complete capability inventory for all scheduled catalog jobs."""

    bindings: tuple[ProductionJobBinding, ...]

    def __post_init__(self) -> None:
        _validate_inventory(self.bindings)

    def binding_for(self, job_id: str) -> ProductionJobBinding:
        if job_id not in EXPECTED_JOB_IDS:
            raise ProductionBindingError("unknown production scheduler job")
        return self.bindings[EXPECTED_JOB_IDS.index(job_id)]


def compose_production_bindings(
    capabilities: Mapping[str, ScheduledCapability],
) -> ProductionBindingInventory:
    """Bind all catalog jobs to explicit effect capabilities without executing them."""
    if not isinstance(capabilities, Mapping) or set(capabilities) != set(EXPECTED_JOB_IDS):
        raise ProductionBindingError(
            "production scheduler capabilities must cover the catalog exactly"
        )
    bindings = tuple(
        ProductionJobBinding(
            job_id=job.id,
            family=_FAMILIES[job.id],
            writer_scope=job.writer_scope,
            lock_scope=job.lock_scope,
            authority=_required_authority(job),
            capability=capabilities[job.id],
        )
        for job in JOB_CATALOG
    )
    return ProductionBindingInventory(bindings)


def dispatch_production_job(
    inventory: ProductionBindingInventory,
    *,
    job_id: str,
    replay_key: str,
) -> ScheduledDispatchResult:
    """Prove complete binding authority, then dispatch exactly one scheduled job.

    The only public outcomes are the existing metadata-only scheduled result classes.
    Capability exceptions and malformed, mismatched, or unavailable responses are redacted
    into closed failure classes.
    """
    if job_id not in EXPECTED_JOB_IDS:
        raise ProductionBindingError("unknown production scheduler job")
    try:
        if not isinstance(inventory, ProductionBindingInventory):
            raise ProductionBindingError("invalid production scheduler inventory")
        _validate_inventory(inventory.bindings)
        binding = inventory.binding_for(job_id)
        result = binding.capability.dispatch(
            ScheduledInvocation(job=get_job(job_id), replay_key=replay_key)
        )
        if not isinstance(result, ScheduledDispatchResult) or result.job_id != job_id:
            return ScheduledDispatchResult.failed(job_id)
        if result.status is ScheduledDispatchStatus.UNAVAILABLE:
            return ScheduledDispatchResult.failed(job_id)
        return result
    except ProductionBindingError:
        return ScheduledDispatchResult.configuration(job_id)
    except Exception:
        return ScheduledDispatchResult.failed(job_id)


def _validate_inventory(bindings: tuple[ProductionJobBinding, ...]) -> None:
    validate_job_catalog(JOB_CATALOG)
    if (
        not isinstance(bindings, tuple)
        or len(bindings) != len(EXPECTED_JOB_IDS)
        or any(not isinstance(binding, ProductionJobBinding) for binding in bindings)
    ):
        raise ProductionBindingError("production scheduler inventory must contain every binding")
    ids = tuple(binding.job_id for binding in bindings)
    if ids != EXPECTED_JOB_IDS or len(set(ids)) != len(EXPECTED_JOB_IDS):
        raise ProductionBindingError(
            "production scheduler bindings must cover each catalog job once"
        )

    writer_identities: set[str] = set()
    for binding, job in zip(bindings, JOB_CATALOG, strict=True):
        capability = binding.capability
        if (
            binding.job_id != job.id
            or binding.family is not _FAMILIES[job.id]
            or binding.writer_scope is not job.writer_scope
            or binding.lock_scope is not job.lock_scope
            or binding.authority is not _required_authority(job)
            or getattr(capability, "authority", None) is not binding.authority
            or getattr(capability, "lock_scope", None) is not binding.lock_scope
        ):
            raise ProductionBindingError("production scheduler binding violates catalog authority")
        writer_identity = getattr(capability, "writer_identity", None)
        if job.host_role is HostRole.WRITER:
            if not isinstance(writer_identity, str) or not writer_identity:
                raise ProductionBindingError(
                    "canonical writer binding requires one writer identity"
                )
            writer_identities.add(writer_identity)
        elif writer_identity is not None:
            raise ProductionBindingError("non-writer binding cannot claim writer identity")

    if len(writer_identities) != 1:
        raise ProductionBindingError("scheduled writers must share exactly one canonical identity")
