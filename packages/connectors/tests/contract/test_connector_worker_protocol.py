from __future__ import annotations

import json
import subprocess
import sys

import pytest

import open_brain_connectors.runtime.connector_worker_v1 as worker_module
from open_brain_connectors.runtime.connector_worker_v1 import (
    ConnectorNetworkMode,
    ConnectorWorkerError,
    ConnectorWorkerFailureCode,
    ConnectorWorkerHost,
    ConnectorWorkerLimits,
    ConnectorWorkerProtocolError,
    ConnectorWorkerReceipt,
    ConnectorWorkerRequest,
    _bounded_exchange,
    connector_manifest_sha256,
)
from open_brain_connectors.runtime.connectors import (
    ConnectorBudgetLimits,
    ConnectorEntryPointMetadata,
    ConnectorManifest,
    ConnectorOutcome,
    ConnectorPayload,
    ConnectorProfile,
    ConnectorRunReceipt,
)


def _manifest() -> ConnectorManifest:
    return ConnectorManifest(
        schema_version=1,
        name="youtube",
        version="1",
        payloads=(ConnectorPayload.REFERENCE_OR_FILE,),
        schedules=("JOB-029",),
        secrets=(),
        action_authorities=(),
        external_egress=True,
    )


def _request() -> ConnectorWorkerRequest:
    return ConnectorWorkerRequest(
        schema_version=1,
        invocation_id="inv_" + "a" * 64,
        connector_name="youtube",
        entry_point_value="open_brain_connectors.conformance:connector",
        manifest=_manifest(),
        budget_limits=ConnectorBudgetLimits(
            max_discoveries=2,
            max_fetches=2,
            max_extractions=2,
            max_submissions=2,
        ),
        network_mode=ConnectorNetworkMode.HOST_MEDIATED,
    )


def _completed_receipt() -> ConnectorRunReceipt:
    return ConnectorRunReceipt(
        connector_name="youtube",
        outcome=ConnectorOutcome.COMPLETED,
        failure_code=None,
        discovered_count=1,
        fetched_count=1,
        extracted_count=1,
        submitted_count=1,
        stubbed_count=0,
        created_count=1,
        duplicate_count=0,
        checkpoint_committed=True,
        metadata_count=1,
    )


def test_worker_request_is_strict_versioned_and_manifest_bound() -> None:
    request = _request()

    assert ConnectorWorkerRequest.from_dict(request.to_dict()) == request
    assert request.manifest_sha256 == connector_manifest_sha256(request.manifest)
    assert len(request.manifest_sha256) == 64
    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerRequest.from_dict({**request.to_dict(), "unknown": True})


def test_worker_boundary_revalidates_forged_frozen_values() -> None:
    manifest = _manifest()
    object.__setattr__(manifest, "payloads", ())
    budget = ConnectorBudgetLimits()
    object.__setattr__(budget, "max_fetches", 0)

    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerRequest(
            schema_version=1,
            invocation_id="inv_" + "f" * 64,
            connector_name="youtube",
            entry_point_value="open_brain_connectors.conformance:connector",
            manifest=manifest,
            budget_limits=ConnectorBudgetLimits(),
            network_mode=ConnectorNetworkMode.HOST_MEDIATED,
        )
    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerRequest(
            schema_version=1,
            invocation_id="inv_" + "f" * 64,
            connector_name="youtube",
            entry_point_value="open_brain_connectors.conformance:connector",
            manifest=_manifest(),
            budget_limits=budget,
            network_mode=ConnectorNetworkMode.HOST_MEDIATED,
        )


def test_worker_receipt_is_metadata_only_and_round_trips_strictly() -> None:
    request = _request()
    receipt = ConnectorWorkerReceipt(
        schema_version=1,
        invocation_id=request.invocation_id,
        connector_name=request.connector_name,
        manifest_sha256=request.manifest_sha256,
        first_run=_completed_receipt(),
        replay_run=ConnectorRunReceipt.empty("youtube", metadata_count=1),
        checkpoint_receipt_sha256="b" * 64,
        capture_count=1,
        direct_network_attempts=0,
    )

    encoded = json.dumps(receipt.to_dict(), sort_keys=True)

    assert ConnectorWorkerReceipt.from_dict(receipt.to_dict()) == receipt
    assert not any(
        forbidden in encoded
        for forbidden in ("payload", "secret_value", "brain_root", "database_path")
    )
    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerReceipt.from_dict({**receipt.to_dict(), "payload": "not allowed"})


def test_worker_receipt_revalidates_forged_connector_counts() -> None:
    request = _request()
    first_run = _completed_receipt()
    object.__setattr__(first_run, "submitted_count", 2)

    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerReceipt(
            schema_version=1,
            invocation_id=request.invocation_id,
            connector_name=request.connector_name,
            manifest_sha256=request.manifest_sha256,
            first_run=first_run,
            replay_run=ConnectorRunReceipt.empty("youtube", metadata_count=1),
            checkpoint_receipt_sha256="b" * 64,
            capture_count=1,
            direct_network_attempts=0,
        )


def test_worker_receipt_rejects_replay_created_captures() -> None:
    request = _request()

    with pytest.raises(ConnectorWorkerProtocolError):
        ConnectorWorkerReceipt(
            schema_version=1,
            invocation_id=request.invocation_id,
            connector_name=request.connector_name,
            manifest_sha256=request.manifest_sha256,
            first_run=_completed_receipt(),
            replay_run=_completed_receipt(),
            checkpoint_receipt_sha256="b" * 64,
            capture_count=2,
            direct_network_attempts=0,
        )


def test_worker_host_rejects_receipt_counts_above_issued_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def discover(
        _host: ConnectorWorkerHost,
        _profile: ConnectorProfile,
    ) -> tuple[ConnectorEntryPointMetadata, ...]:
        return (
            ConnectorEntryPointMetadata(
                name="youtube",
                value="open_brain_connectors.conformance:connector",
            ),
        )

    def over_budget(
        request: ConnectorWorkerRequest,
        _limits: ConnectorWorkerLimits,
    ) -> ConnectorWorkerReceipt:
        first_run = ConnectorRunReceipt(
            connector_name="youtube",
            outcome=ConnectorOutcome.COMPLETED,
            failure_code=None,
            discovered_count=2,
            fetched_count=1,
            extracted_count=1,
            submitted_count=1,
            stubbed_count=0,
            created_count=1,
            duplicate_count=0,
            checkpoint_committed=True,
            metadata_count=1,
        )
        return ConnectorWorkerReceipt(
            schema_version=1,
            invocation_id=request.invocation_id,
            connector_name=request.connector_name,
            manifest_sha256=request.manifest_sha256,
            first_run=first_run,
            replay_run=ConnectorRunReceipt.empty("youtube", metadata_count=1),
            checkpoint_receipt_sha256="b" * 64,
            capture_count=1,
            direct_network_attempts=0,
        )

    monkeypatch.setattr(ConnectorWorkerHost, "discover", discover)
    monkeypatch.setattr(worker_module, "_run_worker_process", over_budget)
    profile = ConnectorProfile(
        allow_list=("youtube",),
        egress_enabled=True,
        budget_limits=ConnectorBudgetLimits(
            max_discoveries=1,
            max_fetches=1,
            max_extractions=1,
            max_submissions=1,
        ),
    )

    with pytest.raises(ConnectorWorkerError) as raised:
        ConnectorWorkerHost().run_conformance(
            "youtube",
            profile=profile,
            expected_manifest=_manifest(),
        )

    assert raised.value.code is ConnectorWorkerFailureCode.INVALID_RECEIPT


@pytest.mark.parametrize(
    ("child_source", "limits", "failure_code"),
    [
        (
            "import sys,time;sys.stdin.buffer.read();time.sleep(60)",
            ConnectorWorkerLimits(wall_seconds=0.1),
            ConnectorWorkerFailureCode.TIMEOUT,
        ),
        (
            "import sys;sys.stdin.buffer.read();sys.stdout.buffer.write(b'x'*64)",
            ConnectorWorkerLimits(max_stdout_bytes=16),
            ConnectorWorkerFailureCode.OUTPUT_LIMIT,
        ),
    ],
)
def test_bounded_exchange_terminates_timeout_and_output_limit_children(
    child_source: str,
    limits: ConnectorWorkerLimits,
    failure_code: ConnectorWorkerFailureCode,
) -> None:
    process = subprocess.Popen[bytes](
        (sys.executable, "-I", "-c", child_source),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
        text=False,
    )

    with pytest.raises(ConnectorWorkerError) as raised:
        _bounded_exchange(process, b"{}\n", limits)

    assert raised.value.code is failure_code
    assert process.poll() is not None


def test_worker_child_command_uses_the_frozen_executable_without_python_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "/opt/open-brain/current/open-brain")

    assert worker_module._worker_command() == (
        "/opt/open-brain/current/open-brain",
        "__connector-worker",
    )
