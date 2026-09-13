from __future__ import annotations

from open_brain_connectors.conformance import connector
from open_brain_connectors.runtime.connector_worker_v1 import (
    ConnectorNetworkMode,
    ConnectorWorkerHost,
    ConnectorWorkerRequest,
)
from open_brain_connectors.runtime.connectors import (
    ConnectorBudgetLimits,
    ConnectorOutcome,
    ConnectorProfile,
)


def test_reference_connector_runs_and_replays_through_the_worker_contract() -> None:
    request = ConnectorWorkerRequest(
        schema_version=1,
        invocation_id="inv_" + "c" * 64,
        connector_name="youtube",
        entry_point_value="open_brain_connectors.conformance:connector",
        manifest=connector.manifest,
        budget_limits=ConnectorBudgetLimits(
            max_discoveries=2,
            max_fetches=2,
            max_extractions=2,
            max_submissions=2,
        ),
        network_mode=ConnectorNetworkMode.HOST_MEDIATED,
    )

    receipt = connector.conformance(request)

    assert receipt.first_run.outcome is ConnectorOutcome.COMPLETED
    assert receipt.first_run.created_count == receipt.first_run.submitted_count == 1
    assert receipt.first_run.checkpoint_committed is True
    assert receipt.replay_run.fetched_count == 1
    assert receipt.replay_run.submitted_count == receipt.replay_run.created_count == 0
    assert receipt.capture_count == 1
    assert receipt.direct_network_attempts == 0
    assert len(receipt.checkpoint_receipt_sha256) == 64


def test_installed_reference_entry_point_loads_only_in_the_bounded_child() -> None:
    profile = ConnectorProfile(
        allow_list=("youtube",),
        egress_enabled=True,
        budget_limits=ConnectorBudgetLimits(
            max_discoveries=2,
            max_fetches=2,
            max_extractions=2,
            max_submissions=2,
        ),
    )

    receipt = ConnectorWorkerHost().run_conformance(
        "youtube",
        profile=profile,
        expected_manifest=connector.manifest,
    )

    assert receipt.capture_count == 1
    assert receipt.first_run.submitted_count == 1
    assert receipt.replay_run.submitted_count == 0
