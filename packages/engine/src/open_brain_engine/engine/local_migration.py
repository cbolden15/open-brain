"""The explicit chained local migration coordinator owned by the product bootstrap."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from . import local_schema
from .contracts import LocalEngineContext
from .local import StateSchemaUnavailableError
from .maintenance import inspect_phase1_state
from .privacy_migration import privacy_migration_pending
from .runtime_admission import exclusive_runtime_admission
from .source_migration import migrate_sources, migration_pending


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _needs_source_phase(state_version: int | None) -> bool:
    return state_version is None or state_version < 7


def _needs_privacy_phase(state_version: int | None) -> bool:
    return state_version == 7


def _needs_issuer_phase(state_version: int | None) -> bool:
    return state_version == 8


def coordinate_local_migration(
    profile: LocalEngineContext,
    *,
    clock: Callable[[], datetime] | None = None,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> None:
    """Resume the source, privacy, and issuer cutovers in order, re-inspecting between.

    The whole chain runs under one exclusive runtime admission and ends only at the
    current state schema. The ordinary opener never performs any of these phases.
    """
    resolved_clock = _utc_now if clock is None else clock
    schema = inspect_phase1_state(profile)
    source_pending = migration_pending(profile)
    privacy_pending = privacy_migration_pending(profile)
    target = local_schema.PHASE1_STATE_SCHEMA_VERSION

    if (
        schema.state in {"invalid", "newer"}
        and not source_pending
        and not privacy_pending
    ):
        raise StateSchemaUnavailableError(f"local state schema is {schema.state}")

    source_phase = source_pending or (
        schema.state in {"supported_old", "legacy", "pre_ledger"}
        and target >= 7
        and _needs_source_phase(schema.version)
    )
    privacy_phase = privacy_pending or (
        schema.state == "supported_old"
        and _needs_privacy_phase(schema.version)
        and target >= 8
    )
    issuer_phase = (
        schema.state == "supported_old"
        and _needs_issuer_phase(schema.version)
        and target >= 9
    )
    if not source_phase and not privacy_phase and not issuer_phase:
        return

    with exclusive_runtime_admission(profile) as admission:
        if source_phase:
            migrate_sources(
                profile, admission=admission, clock=resolved_clock, checkpoint=checkpoint
            )
            schema = inspect_phase1_state(profile)
            privacy_phase = privacy_pending or (
                schema.state == "supported_old"
                and _needs_privacy_phase(schema.version)
                and target >= 8
            )
        if privacy_phase:
            from .privacy_migration import migrate_privacy

            migrate_privacy(
                profile, admission=admission, clock=resolved_clock, checkpoint=checkpoint
            )
            schema = inspect_phase1_state(profile)
            issuer_phase = (
                schema.state == "supported_old"
                and _needs_issuer_phase(schema.version)
                and target >= 9
            )
        if issuer_phase:
            from .issuer_state import migrate_issuer

            migrate_issuer(
                profile, admission=admission, clock=resolved_clock, checkpoint=checkpoint
            )
            schema = inspect_phase1_state(profile)
    if schema.state != "current" or schema.version != target:
        raise StateSchemaUnavailableError(
            f"local state schema is {schema.state} after coordinated migration"
        )
