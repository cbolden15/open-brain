"""Real-engine regressions for queued edits and evidence-specific developments."""

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from open_brain_engine.engine import (
    CaptureReceipt,
    DecisionOutcome,
    ProposalDraft,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine import facet_contracts as fc
from open_brain_engine.engine.developments import DevelopmentTasks

from open_brain.profile import compile_single_user_local
from open_brain_collector.capture_contracts import SourceObservation, development_identity, digest
from open_brain_collector.capture_policy import observe_slack
from open_brain_collector.capture_processing import local_extract
from open_brain_collector.capture_workflow import owner_authority
from open_brain_collector.lifecycle import EngineCaptureSink, EngineRevisionSink
from open_brain_collector.live_manager import LiveSourceManager
from open_brain_collector.runner import collector_capture_sink, collector_revision_sink
from open_brain_connectors.runtime.live_common import LiveBatch
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from packages.collector.tests.integration.test_capture_workflow import (
    ACCOUNT,
    SELECTION,
    Runtime,
    intake,
)


def setup(tmp_path: Path, *, candidates: bool = False) -> tuple[LiveSourceManager, Runtime, Any]:
    profile = compile_single_user_local(tmp_path / "brain", starter_spaces=("Synthetic",))
    tasks = open_local_engine(profile)
    seed = tasks.capture.accept(
        TextPayload("Seed"), delivery_id="revision.seed", space_id=tasks.inbox.spaces()[0].space_id
    )
    proposal = tasks.review.propose(
        (seed.capture_id,), (ProposalDraft("Synthetic", "Initial."),), delivery_id="revision.page"
    )[0]
    tasks.review.decide(
        proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="revision.approve",
        expected_review_digest=proposal.review_digest,
    )
    runtime = Runtime(tmp_path / "live")
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.dispatch(
        "sources.slack_policy_setup", {"connection_id": ACCOUNT, "proposal_opt_in": True}
    )
    manager.dispatch(
        "sources.slack_mapping_add",
        {
            "connection_id": ACCOUNT,
            "channel_id": "CSYNTHETIC",
            "page_id": proposal.page_id,
        },
    )
    config: dict[str, object] = {"enabled": True, "ai_exclusions_verified": True}
    if candidates:
        assert tasks.facets is not None
        dimension = tasks.facets.dimension_create(
            fc.DimensionCreateRequest(
                operation_id="revision.dimension", expected_version=0, name="Topics"
            ),
            authority=owner_authority(),
        ).to_wire()
        config["candidate_dimensions"] = {"subject": dimension["subject_id"]}
    manager.dispatch("sources.capture_setup", config)
    manager.capture.configure("synthetic", SELECTION, {})
    return manager, runtime, tasks


def ingest(
    manager: LiveSourceManager,
    runtime: Runtime,
    text: str,
    revision: str,
    message: str = "1791900000.000001",
) -> None:
    item = intake(text, revision)
    item = replace(item, key=replace(item.key, external_id="message:" + message))
    runtime.batch = LiveBatch((item,), {"revision": revision, "message": message})
    preview = manager.capture.preview("synthetic")
    manager.capture.apply("synthetic", cast(str, preview["preview_id"]))


def test_superseded_pending_evidence_does_not_write_or_block_independent_work(
    tmp_path: Path,
) -> None:
    manager, runtime, tasks = setup(tmp_path)
    ingest(manager, runtime, "Decision: Obsoleteclaim", "edited:1")
    ingest(manager, runtime, "Decision: Currentclaim", "edited:2")
    ingest(manager, runtime, "Decision: Independentclaim", "edited:1", "1791900000.000002")
    result = manager.workflow.process()
    assert result["captures"] == 2
    assert result["superseded"] == 1
    assert result["outcome"] == "partial"
    assert not tasks.retrieval.search("Obsoleteclaim")
    entries = manager.workflow.queue._load()["observations"]
    assert any(entry["state"] == "superseded" for entry in cast(dict[str, Any], entries).values())
    assert manager.workflow.status()["pending_count"] == 0
    assert manager.workflow.status()["superseded_count"] == 1
    assert manager.workflow.process()["captures"] == 0


def test_same_claim_new_evidence_has_one_current_development_and_approvable_proposal(
    tmp_path: Path,
) -> None:
    manager, runtime, tasks = setup(tmp_path)
    ingest(manager, runtime, "Decision: Keep local", "edited:1")
    assert manager.workflow.process()["captures"] == 1
    first = list(cast(dict[str, Any], manager.workflow.queue._load()["operations"]).values())[0]
    ingest(manager, runtime, "Decision: Keep local\nAdditional context", "edited:2")
    assert manager.workflow.process()["captures"] == 1
    operations = list(cast(dict[str, Any], manager.workflow.queue._load()["operations"]).values())
    assert len(operations) == 2
    assert len({operation["semantic_id"] for operation in operations}) == 1
    latest = next(
        operation["capture_id"]
        for operation in operations
        if operation["capture_id"] != first["capture_id"]
    )
    assert latest != first["capture_id"]
    current = {hit.capture_id for hit in tasks.retrieval.search("Keep local")}
    assert latest in current and first["capture_id"] not in current
    items = manager.dispatch("sources.capture_review", {})["items"]
    latest_proposal = next(
        proposal
        for item in cast(list[dict[str, Any]], items)
        if latest in (proposal := tasks.review.show(item["proposal_id"])).capture_ids
    )
    tasks.review.decide(
        latest_proposal.proposal_id,
        DecisionOutcome.APPROVED,
        delivery_id="revision.current.approve",
        expected_review_digest=latest_proposal.review_digest,
    )
    assert manager.workflow.process()["captures"] == 0


def test_long_candidate_name_retains_development_and_does_not_block_next_item(
    tmp_path: Path,
) -> None:
    manager, runtime, _ = setup(tmp_path, candidates=True)
    ingest(manager, runtime, "Decision: " + "x" * 513, "edited:1")
    ingest(manager, runtime, "Decision: Nextclaim", "edited:1", "1791900000.000002")
    result = manager.workflow.process()
    assert result["captures"] == 2
    assert result["processed"] == 2
    assert "candidate_name_too_long" in cast(list[str], result["notices"])
    assert manager.workflow.status()["pending_count"] == 0
    candidates = manager.dispatch("sources.capture_candidates", {})["candidates"]
    assert len(cast(list[object], candidates)) == 1
    operations = cast(dict[str, Any], manager.workflow.queue._load()["operations"])
    assert any(
        "candidate_name_too_long" in operation["notices"] for operation in operations.values()
    )


@pytest.mark.parametrize("stage", ("capture", "bind", "postprocess"))
def test_uncertain_acceptance_replays_exact_development_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    manager, runtime, tasks = setup(tmp_path)
    ingest(manager, runtime, "Decision: Retryclaim", "edited:1")
    target: Any = {
        "capture": EngineRevisionSink,
        "bind": DevelopmentTasks,
        "postprocess": manager.workflow,
    }[stage]
    name = {"capture": "submit", "bind": "bind", "postprocess": "_postprocess"}[stage]
    original = getattr(target, name)
    attempts = 0

    def uncertain(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        result = original(*args, **kwargs)
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic lost acknowledgement")
        return result

    monkeypatch.setattr(target, name, uncertain)
    first = manager.workflow.process()
    assert first["failed"] == 1
    assert first["failure_codes"] == ["processing_item_failed"]
    assert "synthetic" not in str(first)
    assert manager.workflow.process()["processed"] == 1
    assert manager.workflow.process()["captures"] == 0
    operations = cast(dict[str, Any], manager.workflow.queue._load()["operations"])
    assert len(operations) == 1
    operation = next(iter(operations.values()))
    receipt = tasks.developments.get(operation["capture_id"], authority=owner_authority())
    assert receipt["operation_id"] == next(iter(operations))
    assert len(cast(list[Any], manager.dispatch("sources.capture_review", {})["items"])) == 1


def test_development_revision_sink_replays_history_without_promoting_it(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    sink = collector_revision_sink(profile.root, tmp_path / "revisions")
    first = observe_slack(intake("Originaldevelopment", "evidence-1"))
    first = replace(first, key=replace(first.key, connector_name="slack_development"))
    second = observe_slack(intake("Currentdevelopment", "evidence-2"))
    second = replace(second, key=replace(second.key, connector_name="slack_development"))
    old = sink.submit(first)
    current = sink.submit(second)
    replay = sink.submit(first)
    assert old.source_receipt is not None and current.source_receipt is not None
    assert replay.source_receipt is not None
    assert replay.source_receipt.capture_id == old.source_receipt.capture_id
    assert old.source_receipt.source_id == current.source_receipt.source_id
    assert not tasks.retrieval.search("Originaldevelopment")
    assert len(tasks.retrieval.search("Currentdevelopment")) == 1


@pytest.mark.parametrize("tamper", (False, True))
def test_legacy_development_adoption_preserves_exact_retry_and_source_history(
    tmp_path: Path,
    tamper: bool,
) -> None:
    manager, runtime, tasks = setup(tmp_path)
    ingest(manager, runtime, "Decision: Legacyclaim", "edited:1")
    state = manager.workflow.queue._load()
    observations = cast(dict[str, Any], state["observations"])
    key, entry = next(iter(observations.items()))
    observation = SourceObservation.from_dict(entry["observation"])
    development = local_extract(observation)[0]
    operation_id = development_identity(development, observation)
    old_intake = SourceRecordIntake(
        SourceRecordKey(
            "slack_development",
            ACCOUNT,
            SELECTION.resource_id,
            operation_id,
            digest(development.to_dict()),
        ),
        observation.source_link,
        json.dumps(development.to_dict(), sort_keys=True, ensure_ascii=False),
        observation.privacy,
        title="decision: Legacyclaim",
    )
    sink = EngineCaptureSink(collector_capture_sink(manager.brain_root))
    receipt = sink.submit(old_intake)
    assert isinstance(receipt, CaptureReceipt)
    old_capture = receipt.capture_id
    cast(dict[str, Any], state["operations"])[operation_id] = {
        "observation_id": key,
        "development": development.to_dict(),
        "capture_id": old_capture,
        "postprocessed": False,
    }
    entry.update(state="prepared", operation_ids=[operation_id])
    manager.workflow.queue._save(state)
    assert manager.workflow.process()["processed"] == 1
    replay = sink.submit(old_intake)
    assert isinstance(replay, CaptureReceipt) and replay.capture_id == old_capture
    if tamper:
        state = manager.workflow.queue._load()
        cast(dict[str, Any], state["operations"])[operation_id]["development"]["claim"] = "Changed"
        manager.workflow.queue._save(state)
    ingest(manager, runtime, "Decision: Legacyclaim\nNew context", "edited:2")
    result = manager.workflow.process()
    if tamper:
        assert result["captures"] == 0
        assert result["failure_codes"] == ["processing_legacy_adoption_conflict"]
        return
    assert result["captures"] == 1
    operations = cast(dict[str, Any], manager.workflow.queue._load()["operations"])
    new_capture = next(
        value["capture_id"] for key, value in operations.items() if key != operation_id
    )
    with sqlite3.connect(manager.brain_root / ".open-brain/state/phase1.sqlite3") as connection:
        sources = connection.execute(
            "SELECT DISTINCT source_id FROM source_revisions WHERE capture_id IN (?,?)",
            (old_capture, new_capture),
        ).fetchall()
    assert len(sources) == 1
    current = {hit.capture_id for hit in tasks.retrieval.search("Legacyclaim")}
    assert new_capture in current and old_capture not in current
    assert manager.workflow.process()["captures"] == 0


def test_changed_support_after_uncertain_acceptance_retains_recovery_disposition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, runtime, _ = setup(tmp_path)
    ingest(manager, runtime, "Decision: Pendingclaim", "edited:1")
    original = EngineRevisionSink.submit
    failed = False

    def uncertain(sink: EngineRevisionSink, item: SourceRecordIntake) -> Any:
        nonlocal failed
        receipt = original(sink, item)
        if not failed:
            failed = True
            raise RuntimeError("synthetic uncertain capture")
        return receipt

    monkeypatch.setattr(EngineRevisionSink, "submit", uncertain)
    assert manager.workflow.process()["failed"] == 1
    ingest(manager, runtime, "Decision: Pendingclaim\nNew context", "edited:2")
    ingest(manager, runtime, "Decision: Independentclaim", "edited:1", "1791900000.000002")
    result = manager.workflow.process()
    assert result["captures"] == 2 and result["processed"] == 2
    assert result["failure_codes"] == ["processing_evidence_changed_pending_recovery"]
    assert result["superseded"] == 0
    assert manager.workflow.status()["pending_count"] == 1
