"""Daemonless bootstrap for one default Open Brain."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from open_brain_engine.engine import (
    PHASE1_STATE_SCHEMA_VERSION,
    EngineTaskSet,
    LocalEngineContext,
    inspect_phase1_state,
    open_local_engine,
)

from open_brain.local_data import (
    FilesystemTypeProbe,
    LocalRootSelection,
    PreparedLocalRoot,
    prepare_local_root,
)
from open_brain.profile import compile_single_user_local

_STATE_DATABASE = ".open-brain/state/phase1.sqlite3"


@dataclass(frozen=True, slots=True)
class LocalInitReceipt:
    status: str
    profile: str
    brain_count: int
    storage: str
    daemon_running: bool
    application_encryption: bool
    state_schema_version: int

    def to_dict(self) -> dict[str, object]:
        return {
            "application_encryption": self.application_encryption,
            "brain_count": self.brain_count,
            "daemon_running": self.daemon_running,
            "profile": self.profile,
            "state_schema_version": self.state_schema_version,
            "status": self.status,
            "storage": self.storage,
        }


@dataclass(frozen=True, slots=True)
class LocalBrainSession:
    """One directly opened Brain while its selected root identity remains pinned."""

    initialized_before: bool
    prepared: PreparedLocalRoot
    profile: LocalEngineContext
    tasks: EngineTaskSet


@contextmanager
def open_local_brain(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> Iterator[LocalBrainSession]:
    """Bootstrap and hold one default Brain through a direct local operation."""
    with prepare_local_root(
        selection, filesystem_type_probe=filesystem_type_probe
    ) as prepared:
        identity_exists = prepared.private_file_exists("brain.toml")
        initialized_before = identity_exists and prepared.private_file_exists(_STATE_DATABASE)
        prepared.revalidate()
        profile = compile_single_user_local(
            selection.brain_root,
            validate_before_identity_write=prepared.revalidate,
        )

        def validate_direct_write() -> None:
            prepared.revalidate()

        tasks = open_local_engine(profile, validate_before_write=validate_direct_write)
        prepared.revalidate()
        _require_current_local_state(prepared, profile)
        yield LocalBrainSession(
            initialized_before=initialized_before,
            prepared=prepared,
            profile=profile,
            tasks=tasks,
        )
        prepared.revalidate()


def initialize_local_brain(
    selection: LocalRootSelection,
    *,
    filesystem_type_probe: FilesystemTypeProbe | None = None,
) -> LocalInitReceipt:
    """Create or reopen one owner, one Brain, and its direct SQLite state."""
    with open_local_brain(
        selection, filesystem_type_probe=filesystem_type_probe
    ) as session:
        initialized_before = session.initialized_before
    return LocalInitReceipt(
        status="already_initialized" if initialized_before else "initialized",
        profile="local",
        brain_count=1,
        storage="sqlite",
        daemon_running=False,
        application_encryption=False,
        state_schema_version=PHASE1_STATE_SCHEMA_VERSION,
    )

def _require_current_local_state(
    prepared: PreparedLocalRoot,
    profile: LocalEngineContext,
) -> None:
    if not prepared.private_file_exists("brain.toml") or not prepared.private_file_exists(
        _STATE_DATABASE
    ):
        raise RuntimeError("local bootstrap did not create private state")
    schema = inspect_phase1_state(profile)
    if schema.state != "current" or schema.version != PHASE1_STATE_SCHEMA_VERSION:
        raise RuntimeError("local bootstrap did not create the current SQLite schema")


__all__ = [
    "LocalBrainSession",
    "LocalInitReceipt",
    "initialize_local_brain",
    "open_local_brain",
]
