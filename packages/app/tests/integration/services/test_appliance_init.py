from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from open_brain_engine.engine import TextPayload, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain.services.appliance_init import (
    APPLIANCE_INIT_RECEIPT,
    APPLIANCE_OWNER_CREDENTIAL,
    ApplianceInitError,
    initialize_appliance,
)


def test_initialize_appliance_is_idempotent_and_preserves_generated_credential_and_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"

    first = initialize_appliance(root, starter_spaces=("Personal", "Work"))
    credential_path = root / APPLIANCE_OWNER_CREDENTIAL
    receipt_path = root / APPLIANCE_INIT_RECEIPT
    credential = credential_path.read_text(encoding="utf-8").strip()
    initial_identity = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert first.status == "initialized"
    assert first.credential_state == "created"
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600
    assert credential

    tasks = open_local_engine(compile_single_user_local(root))
    capture = tasks.capture.accept(
        TextPayload("Synthetic idempotent init content"),
        delivery_id="appliance-init.capture",
    )
    assert {space.name for space in tasks.inbox.spaces()} == {"Personal", "Work"}

    second = initialize_appliance(root, starter_spaces=("Personal", "Work"))
    replayed_identity = json.loads(receipt_path.read_text(encoding="utf-8"))

    assert second.status == "already_initialized"
    assert second.credential_state == "preserved"
    assert second.tenant_id == first.tenant_id
    assert second.owner_actor_id == first.owner_actor_id
    assert second.index_generation == first.index_generation
    assert credential_path.read_text(encoding="utf-8").strip() == credential
    assert replayed_identity == initial_identity

    reopened = open_local_engine(compile_single_user_local(root))
    assert reopened.retrieval.fetch(capture.capture_id) is not None
    assert {space.name for space in reopened.inbox.spaces()} == {"Personal", "Work"}


def test_initialize_appliance_reports_bounded_preflight_failure_and_no_partial_writer(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"

    with pytest.raises(ApplianceInitError) as excinfo:
        initialize_appliance(
            root,
            supervisor_probe=lambda _host_family: None,
        )

    failure = excinfo.value.receipt

    assert failure.status == "failed"
    assert failure.failed_check == "supervisor"
    assert failure.cleanup == (
        "No partial writer was created.",
        "Fix the failed preflight check and rerun init.",
    )
    assert not root.exists()


def test_initialize_appliance_accepts_a_nested_root_below_a_writable_ancestor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "nested" / "owner" / "brain"

    receipt = initialize_appliance(
        root,
        supervisor_probe=lambda _host_family: "synthetic-supervisor",
    )

    assert receipt.status == "initialized"
    assert root.is_dir()


def test_initialize_appliance_rejects_unsafe_credential_without_replacing_it(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root)
    credential_path = root / APPLIANCE_OWNER_CREDENTIAL
    credential_path.write_text("preserve-this-credential\n", encoding="utf-8")
    credential_path.chmod(0o644)
    before = credential_path.read_bytes()

    with pytest.raises(ApplianceInitError) as excinfo:
        initialize_appliance(root)

    assert excinfo.value.receipt.failed_check == "credential"
    assert credential_path.read_bytes() == before
    assert stat.S_IMODE(credential_path.stat().st_mode) == 0o644


def test_initialize_appliance_rejects_newer_state_before_writer_or_credential_changes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    initialize_appliance(root)
    credential_path = root / APPLIANCE_OWNER_CREDENTIAL
    credential_before = credential_path.read_bytes()
    database = root / ".open-brain" / "state" / "phase1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA user_version = 3")
    lock_root = root / ".open-brain" / ".open-brain-locks"
    locks_before = {
        path.name: path.read_bytes() for path in lock_root.iterdir()
    } if lock_root.exists() else {}

    with pytest.raises(ApplianceInitError) as excinfo:
        initialize_appliance(root)

    assert excinfo.value.receipt.failed_check == "state_schema"
    assert credential_path.read_bytes() == credential_before
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (3,)
    locks_after = {
        path.name: path.read_bytes() for path in lock_root.iterdir()
    } if lock_root.exists() else {}
    assert locks_after == locks_before
