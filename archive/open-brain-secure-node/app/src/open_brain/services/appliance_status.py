"""Bounded appliance status and doctor evidence over the read-only maintenance surface."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from open_brain_engine.engine import MaintenanceSnapshot, read_maintenance_snapshot

from open_brain.integrations.ui import UiBindConfig
from open_brain.profile import open_existing_single_user_local

from .appliance_auth import allowed_origin_for_host
from .appliance_history import last_successful_run, read_appliance_run_history
from .appliance_scheduler import read_scheduler_snapshot


@dataclass(frozen=True, slots=True)
class ApplianceStatusReceipt:
    tenant_id: str
    owner_actor_id: str
    provider_mode: str
    configuration: dict[str, object]
    maintenance: MaintenanceSnapshot
    scheduler: dict[str, object]
    ownership: dict[str, object]
    last_successful_run: dict[str, object] | None
    doctor: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "configuration": dict(self.configuration),
            "doctor": dict(self.doctor),
            "last_successful_run": (
                None if self.last_successful_run is None else dict(self.last_successful_run)
            ),
            "maintenance": self.maintenance.to_dict(),
            "owner_actor_id": self.owner_actor_id,
            "ownership": dict(self.ownership),
            "provider_mode": self.provider_mode,
            "scheduler": dict(self.scheduler),
            "status": "ok",
            "tenant_id": self.tenant_id,
        }


def read_appliance_status(
    root: Path,
    *,
    bind: UiBindConfig | None = None,
    allowed_origin: str | None = None,
    external_encryption_terminated: bool = False,
    daemon_authority_held: bool = False,
    now: Callable[[], datetime] | None = None,
) -> ApplianceStatusReceipt:
    profile = open_existing_single_user_local(root)
    maintenance = read_maintenance_snapshot(profile)
    current = _utc(now)
    history = read_appliance_run_history(root)
    last_run = last_successful_run(history)
    scheduler = _scheduler_snapshot(
        root,
        profile.root_identity,
        now=current,
    )
    ownership = _ownership_snapshot(
        maintenance,
        daemon_authority_held=daemon_authority_held,
    )
    configuration = _configuration_snapshot(
        bind=bind or UiBindConfig(),
        allowed_origin=allowed_origin,
        external_encryption_terminated=external_encryption_terminated,
    )
    doctor = _doctor_snapshot(
        configuration=configuration,
        maintenance=maintenance,
        scheduler=scheduler,
        ownership=ownership,
        last_successful_run=last_run.to_dict() if last_run is not None else None,
    )
    return ApplianceStatusReceipt(
        tenant_id=profile.tenant_id,
        owner_actor_id=profile.owner_actor_id,
        provider_mode=profile.provider_mode.value,
        configuration=configuration,
        maintenance=maintenance,
        scheduler=scheduler,
        ownership=ownership,
        last_successful_run=None if last_run is None else last_run.to_dict(),
        doctor=doctor,
    )


def _configuration_snapshot(
    *,
    bind: UiBindConfig,
    allowed_origin: str | None,
    external_encryption_terminated: bool,
) -> dict[str, object]:
    if allowed_origin is not None and (
        not isinstance(allowed_origin, str) or not allowed_origin
    ):
        raise ValueError("invalid appliance status configuration")
    access = "private-network" if bind.allow_private_network else "loopback"
    return {
        "http": {
            "access": access,
            "allow_private_network": bind.allow_private_network,
            "allowed_origin": (
                allowed_origin
                if allowed_origin is not None
                else allowed_origin_for_host(bind.host, bind.port)
            ),
            "external_encryption_terminated": external_encryption_terminated,
            "host": bind.host,
            "port": bind.port,
            "remote_access": "ssh_tunnel",
        }
    }


def _scheduler_snapshot(
    root: Path,
    root_identity: tuple[int, int],
    *,
    now: datetime,
) -> dict[str, object]:
    return read_scheduler_snapshot(root, root_identity, now=now)


def _ownership_snapshot(
    maintenance: MaintenanceSnapshot,
    *,
    daemon_authority_held: bool,
) -> dict[str, object]:
    held = set(maintenance.writer.held_leases)
    return {
        "daemon_authority": "held" if daemon_authority_held else "absent",
        "held_lock_count": maintenance.writer.held_count,
        "held_leases": list(maintenance.writer.held_leases),
        "malformed_lock_count": maintenance.writer.malformed_count,
        "shared_writer": "held" if "shared-writer" in held else "absent",
    }


def _doctor_snapshot(
    *,
    configuration: Mapping[str, object],
    maintenance: MaintenanceSnapshot,
    scheduler: Mapping[str, object],
    ownership: Mapping[str, object],
    last_successful_run: Mapping[str, object] | None,
) -> dict[str, object]:
    findings: list[dict[str, str]] = []
    remediation: list[str] = []
    _check(
        findings,
        remediation,
        check="schema",
        state=maintenance.schema.state,
        healthy=maintenance.schema.state == "current",
        fix="Repair or migrate the Phase 1 state schema before serving browser traffic.",
    )
    _check(
        findings,
        remediation,
        check="index",
        state=maintenance.index.state,
        healthy=maintenance.index.state == "current",
        fix="Rebuild the search index before relying on browser search or page lookup.",
    )
    _check(
        findings,
        remediation,
        check="scheduler",
        state=str(scheduler.get("state")),
        healthy=str(scheduler.get("state")) in {"idle", "running", "due"},
        fix="Restart the appliance daemon to republish scheduler state.",
    )
    _check(
        findings,
        remediation,
        check="daemon_authority",
        state=str(ownership.get("daemon_authority")),
        healthy=str(ownership.get("daemon_authority")) == "held",
        fix="Start exactly one appliance daemon before using browser mutations.",
    )
    _check(
        findings,
        remediation,
        check="locks",
        state="malformed" if maintenance.writer.malformed_count else "ok",
        healthy=maintenance.writer.malformed_count == 0,
        fix="Clear malformed lease evidence through the verified appliance recovery path.",
    )
    _check(
        findings,
        remediation,
        check="backup",
        state=maintenance.backup.state,
        healthy=maintenance.backup.state == "present",
        fix="Create a verified appliance backup before upgrade, restore, or replacement.",
    )
    _check(
        findings,
        remediation,
        check="export",
        state=maintenance.export.state,
        healthy=maintenance.export.state == "present",
        fix="Create a portable export before migration or host replacement.",
    )
    http = cast(dict[str, object], configuration["http"])
    _check(
        findings,
        remediation,
        check="http_access",
        state=str(http["access"]),
        healthy=str(http["access"]) == "loopback" or bool(http["external_encryption_terminated"]),
        fix="Keep the UI on loopback or configure explicit external encryption termination.",
    )
    _check(
        findings,
        remediation,
        check="last_successful_run",
        state="present" if last_successful_run is not None else "absent",
        healthy=last_successful_run is not None,
        fix="Let the daemon complete at least one successful maintenance run.",
    )
    return {
        "checks": findings,
        "remediation": remediation,
        "state": "healthy" if not remediation else "needs_attention",
    }


def _check(
    findings: list[dict[str, str]],
    remediation: list[str],
    *,
    check: str,
    state: str,
    healthy: bool,
    fix: str,
) -> None:
    findings.append({"check": check, "state": state})
    if not healthy and fix not in remediation:
        remediation.append(fix)


def _utc(now: Callable[[], datetime] | None) -> datetime:
    current = datetime.now(UTC) if now is None else now()
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise ValueError("invalid appliance clock")
    return current.astimezone(UTC)
