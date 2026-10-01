from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import (
    CaptureSubmission,
    ContentOrigin,
    PrivacyDecision,
    PublicJobCaptureContext,
    open_local_engine,
)

from open_brain.profile import compile_single_user_local
from open_brain_collector.lifecycle import (
    CollectorController,
    CollectorStateStore,
    MemoryCaptureSink,
)
from open_brain_collector.saved_markdown import SavedMarkdownCollectorRuntime
from open_brain_connectors.runtime.saved_markdown import SavedMarkdownRootAdapter
from open_brain_connectors.runtime.source_intake import SourceRecordIntake


def test_saved_markdown_public_job_admission_is_local_only_and_third_party(tmp_path: Path) -> None:
    adapter = _adapter(_root(tmp_path))
    candidate = adapter.dry_run().candidates[0]
    assert candidate.intake is not None
    intake = candidate.intake
    brain = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(brain))
    context = _context(tasks.profile)
    submission = CaptureSubmission.for_public_job(
        context=context,
        payload=intake.payload(),
        delivery_id=intake.key.delivery_id(),
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=intake.source_reference,
        provenance=intake.provenance(),
        privacy=intake.privacy,
        intent="reference",
        title=intake.title,
    )

    receipt = tasks.capture.submit(submission)

    assert receipt.requested_tier.value == "public"
    assert receipt.final_admitted_tier.value == "public"
    assert submission.provenance.content_origin is ContentOrigin.THIRD_PARTY
    assert submission.privacy.authority.cloud is False
    assert submission.privacy.authority.external_egress is False
    assert len(intake.text.encode("utf-8")) <= 65_536


def test_saved_markdown_replays_exact_staged_intake_after_lost_response(
    tmp_path: Path,
) -> None:
    adapter = _adapter(_root(tmp_path))
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100
    )
    controller.enable(
        source_id="saved-markdown.synthetic",
        selection=adapter.selection,
        interval_seconds=60,
    )
    sink = _LostResponseSink()

    with pytest.raises(RuntimeError, match="synthetic lost response"):
        controller.sync_due(
            source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=sink
        )
    receipt_ids = cast(
        list[str], controller.custody_status("saved-markdown.synthetic")["receipt_ids"]
    )
    receipt_id = receipt_ids[0]
    retained = controller._custody.intake(receipt_id)

    restarted = CollectorController(CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100)
    replay = restarted.sync_due(
        source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=sink
    )

    assert replay.outcome.value == "completed"
    assert sink.intakes == [retained, retained]
    assert sink.intakes[0].key.delivery_id() == sink.intakes[1].key.delivery_id()


def test_saved_markdown_refusals_do_not_enter_collector_custody(tmp_path: Path) -> None:
    root = _root(tmp_path)
    (root / "secret.md").write_text("# secret\napi_key = synthetic\n", encoding="utf-8")
    adapter = _adapter(root)
    runtime = SavedMarkdownCollectorRuntime(adapter)
    controller = CollectorController(
        CollectorStateStore(tmp_path / "state.json"), clock=lambda: 100
    )
    controller.enable(
        source_id="saved-markdown.synthetic", selection=adapter.selection, interval_seconds=60
    )

    result = controller.sync_due(
        source_id="saved-markdown.synthetic", runtime=runtime, capture_sink=MemoryCaptureSink()
    )

    assert result.captured_count == 1
    assert runtime.last_scan is not None
    assert [item.refusal_code for item in runtime.last_scan.candidates if not item.accepted] == [
        "saved_markdown_secret_bearing"
    ]
    assert controller.custody_status("saved-markdown.synthetic")["retained_items"] == 0


class _LostResponseSink:
    def __init__(self) -> None:
        self.intakes: list[SourceRecordIntake] = []

    def submit(self, intake: SourceRecordIntake) -> None:
        self.intakes.append(intake)
        if len(self.intakes) == 1:
            raise RuntimeError("synthetic lost response")


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "selected"
    root.mkdir()
    (root / "saved.md").write_text(
        "---\nowner: synthetic\n---\n# Third-party saved item\n\nbody\n\n# Why Saved\nprivate\n",
        encoding="utf-8",
    )
    return root


def _adapter(root: Path) -> SavedMarkdownRootAdapter:
    return SavedMarkdownRootAdapter(
        root,
        destination_identity="destination:synthetic",
        accepted_source_identity="source:synthetic",
        source_reference="https://saved.example.test/library",
        privacy=PrivacyDecision.from_dict(
            {
                "authority": {"cloud": False, "external_egress": False},
                "confirmation_ref": None,
                "policy_version": "policy-v1",
                "reason": "policy_public",
                "tier": "public",
            }
        ),
    )


def _context(profile: object) -> PublicJobCaptureContext:
    from open_brain_engine.engine import LocalEngineContext

    assert isinstance(profile, LocalEngineContext)
    actor = "actor_00000000-0000-4000-8000-000000000701"
    return PublicJobCaptureContext.create(
        profile=profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_00000000-0000-4000-8000-000000000702",
            "role_id": "role_00000000-0000-4000-8000-000000000703",
            "tenant_id": profile.tenant_id,
        },
    )
