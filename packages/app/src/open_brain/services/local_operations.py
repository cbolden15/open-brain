"""Shared default-product operations and public representations for CLI and MCP."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import cast

from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    CaptureCustodyReceipt,
    CaptureOutcome,
    CaptureSubmission,
    CaptureTask,
    EngineTaskSet,
    ManagedAccessMode,
    ManagedProvider,
    PublicJobCaptureContext,
    PublicJobCaptureSink,
    ReconciliationTask,
    RetrievalResult,
    RetrievalTask,
    TextPayload,
)
from open_brain_engine.engine.local_schema import PHASE1_STATE_DATABASE
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.storage.locks import LockBusyError
from open_brain_engine.storage.sqlite import connect_database_read_only, is_database_busy

from open_brain.services.graph_projection_store import (
    GraphProjectionStore,
    canvas_result,
    projection_result,
)
from open_brain.services.graphify_projection import (
    GraphifyAdapter,
    GraphifyFailure,
    discover_graphify_executable,
)
from open_brain.services.launcher_policy import (
    LauncherPolicyError,
    validate_startup_policy,
)
from open_brain.services.managed_providers import (
    ManagedGraphProviderResult,
    ManagedProviderFailure,
)

_MCP_IDENTITY = "cf350566-c33d-49ab-bef7-e0d760171ae1"
GraphProvider = Callable[[str, int, int], ManagedGraphProviderResult]


def mcp_capture_sink(tasks: EngineTaskSet) -> PublicJobCaptureSink:
    actor = "actor_" + _MCP_IDENTITY
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor,
        role_claim={
            "actor_id": actor,
            "capabilities": ["capture.accept"],
            "role_claim_id": "role_claim_" + _MCP_IDENTITY,
            "role_id": "role_" + _MCP_IDENTITY,
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    return tasks.capture.public_job_sink(context)


def capture_text(
    capture: CaptureTask | PublicJobCaptureSink,
    text: str,
    *,
    delivery_id: str,
    privacy_tier: PrivacyTier | None = None,
) -> CaptureOutcome:
    payload = TextPayload(text)
    if isinstance(capture, PublicJobCaptureSink):
        if privacy_tier is not None:
            # The explicit tier is owner-only authority; the non-owner sink
            # must never carry it.
            raise ValueError("capture privacy tier requires owner authority")
        # Bind exact input bytes as well as the engine-normalized payload on replay.
        source = "urn:open-brain:mcp:" + sha256(text.encode("utf-8")).hexdigest()
        return capture.submit(
            payload,
            delivery_id=delivery_id,
            source_origin=ContentOrigin.UNKNOWN,
            source_reference=source,
            provenance=Provenance.create(
                source_ref=source,
                content_origin=ContentOrigin.UNKNOWN,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=PrivacyDecision.create(
                tier=PrivacyTier.PERSONAL,
                reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
                policy_version="privacy-v1",
                authority=Authority(cloud=False, external_egress=False),
            ),
        )
    return capture.accept(payload, delivery_id=delivery_id, privacy_tier=privacy_tier)


def search_brain(
    retrieval: RetrievalTask, reconciliation: ReconciliationTask, query: str, *, limit: int
) -> tuple[RetrievalResult, ...]:
    reconciliation.reconcile()
    return retrieval.search(query, limit=limit)


def destination_bound_authority(
    tasks: EngineTaskSet, raw_policy: str | bytes | Mapping[str, object]
) -> EffectiveAuthority:
    """Validate the trusted startup policy against this Brain's durable identity.

    The durable ``brain_identity`` row is the only source of the current Brain
    ID and issuer epoch, so a stale-epoch or wrong-Brain policy is refused
    here, before any submission exists. The destination-bound capture path
    never grants egress authority, so no provider consent is consulted;
    external-provider policies fail closed.
    """
    profile = tasks.profile
    connection = connect_database_read_only(
        root=profile.root,
        database_name=PHASE1_STATE_DATABASE,
        expected_root_identity=profile.root_identity,
    )
    try:
        row = connection.execute("SELECT brain_id, issuer_epoch FROM brain_identity").fetchone()
    finally:
        connection.close()
    if row is None:
        raise LauncherPolicyError("destination_mismatch")
    return validate_startup_policy(
        raw_policy,
        current_brain_id=cast(str, row[0]),
        current_issuer_epoch=cast(int, row[1]),
        current_authorization_generation=0,
        consent_state=None,
    )


def submit_destination_bound_capture(
    tasks: EngineTaskSet,
    authority: EffectiveAuthority,
    text: str,
    *,
    requested_tier: PrivacyTier | None,
    delivery_id: str,
) -> CaptureOutcome:
    """Submit one destination-bound capture; the engine owns every admission check."""
    return tasks.capture.submit(
        CaptureSubmission.for_destination_bound(
            profile=tasks.profile,
            authority=authority,
            payload=TextPayload(text),
            delivery_id=delivery_id,
            requested_tier=requested_tier,
        )
    )


def destination_bound_capture_result(receipt: CaptureOutcome) -> dict[str, object]:
    """The public destination-bound receipt projection with its full binding."""
    if isinstance(receipt, CaptureCustodyReceipt):
        return receipt.to_dict()
    return {
        "capture_id": receipt.capture_id,
        "delivery_id": receipt.delivery_id,
        "destination_brain_id": receipt.destination_brain_id,
        "duplicate": receipt.duplicate,
        "final_admitted_tier": receipt.final_admitted_tier.value,
        "issuer_epoch": receipt.issuer_epoch,
        "payload_family": receipt.payload_family,
        "request_sha256": receipt.request_sha256,
        "requested_tier": receipt.requested_tier.value,
        "state": receipt.state,
        "status": "captured",
    }


@dataclass(frozen=True, slots=True)
class DestinationBoundCaptureCapability:
    """Authority-bearing destination submission capability for one MCP session."""

    tasks: EngineTaskSet
    authority: EffectiveAuthority

    def __post_init__(self) -> None:
        if (
            not isinstance(self.authority, EffectiveAuthority)
            or self.authority.owner
            or self.authority.brain_id is None
            or self.authority.issuer_epoch is None
        ):
            raise ValueError("invalid destination-bound capture authority")

    def __call__(
        self, text: str, requested_tier: PrivacyTier | None, delivery_id: str
    ) -> dict[str, object]:
        return destination_bound_capture_result(
            submit_destination_bound_capture(
                self.tasks,
                self.authority,
                text,
                requested_tier=requested_tier,
                delivery_id=delivery_id,
            )
        )


def destination_bound_capture_submit(
    tasks: EngineTaskSet, authority: EffectiveAuthority
) -> DestinationBoundCaptureCapability:
    """One destination-bound submission capability for a launched MCP session."""
    return DestinationBoundCaptureCapability(tasks, authority)


def capture_result(receipt: CaptureOutcome) -> dict[str, object]:
    if isinstance(receipt, CaptureCustodyReceipt):
        return receipt.to_dict()
    return {
        "capture_id": receipt.capture_id,
        "duplicate": receipt.duplicate,
        "payload_family": receipt.payload_family,
        "state": receipt.state,
        "status": "captured",
    }


def search_result(results: tuple[RetrievalResult, ...]) -> dict[str, object]:
    return {
        "results": [
            {
                "capture_id": result.capture_id,
                "excerpt": result.excerpt,
                "payload_family": result.payload_family,
                "record_type": result.record_type,
                "result_id": result.result_id,
                "source_origin": result.provenance.source_origin,
                "title": result.title,
                "trust": result.trust,
                "explanation": result.explanation,
            }
            for result in results
        ],
        "status": "ok",
    }


def workspace_status(tasks: EngineTaskSet) -> dict[str, object]:
    status = tasks.managed_workspace.status()
    if status is None:
        return {"status": "unconfigured"}
    return {
        "active_notes": status.active_notes,
        "connected": status.connected,
        "inactive_notes": status.inactive_notes,
        "observation_generation": status.observation_generation,
        "open_conflicts": status.open_conflicts,
        "pending_suggestions": status.pending_suggestions,
        "policy_generation": status.policy_generation,
        "status": "ok",
        "workspace_id": status.workspace_id,
    }


def graph_suggestions(tasks: EngineTaskSet) -> dict[str, object]:
    status = tasks.managed_workspace.status()
    if status is None:
        return {"status": "unconfigured", "suggestions": []}
    suggestions = tasks.managed_inference.suggestions(status.workspace_id)
    snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
    revisions = {source.note_id: source.revision_id for source in snapshot.sources}
    return {
        "status": "ok",
        "suggestions": [
            {
                "model": suggestion.model,
                "provider": suggestion.provider.value,
                "source_note_id": suggestion.source_note_id,
                "source_quote": suggestion.source_quote,
                "source_revision_id": suggestion.source_revision_id,
                "suggestion_id": suggestion.suggestion_id,
                "target_note_id": suggestion.target_note_id,
                "target_quote": suggestion.target_quote,
                "target_revision_id": suggestion.target_revision_id,
                "revision_status": (
                    "current"
                    if revisions.get(suggestion.source_note_id) == suggestion.source_revision_id
                    and revisions.get(suggestion.target_note_id) == suggestion.target_revision_id
                    else "stale"
                ),
            }
            for suggestion in suggestions
        ],
        "workspace_id": status.workspace_id,
    }


def graph_projection(tasks: EngineTaskSet) -> dict[str, object]:
    status = tasks.managed_workspace.status()
    if status is None:
        return {
            "inferred_suggestions": [],
            "status": "unconfigured",
            "structural_links": [],
        }
    try:
        snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
        structural = GraphProjectionStore(tasks.profile.root, tasks.profile.root_identity).load(
            snapshot
        )
    except GraphifyFailure as error:
        return {
            "inferred_suggestions": [],
            "reason": error.code,
            "status": "unavailable",
            "structural_links": [],
            "workspace_id": status.workspace_id,
        }
    return projection_result(
        snapshot,
        structural,
        tasks.managed_inference.suggestions(status.workspace_id),
    )


def graph_canvas(tasks: EngineTaskSet) -> dict[str, object]:
    """Return a generated Canvas without writing into the managed workspace."""
    status = tasks.managed_workspace.status()
    if status is None:
        return {"status": "unconfigured"}
    snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
    try:
        structural = GraphProjectionStore(tasks.profile.root, tasks.profile.root_identity).load(
            snapshot
        )
    except GraphifyFailure as error:
        return {
            "reason": error.code,
            "status": "unavailable",
            "workspace_id": status.workspace_id,
        }
    return {
        "canvas": canvas_result(
            snapshot,
            structural,
            tasks.managed_inference.suggestions(status.workspace_id),
        ),
        "generation_id": structural.generation_id,
        "snapshot_sha256": structural.snapshot_sha256,
        "status": structural.status,
        "workspace_id": status.workspace_id,
    }


def refresh_structural_graph(
    tasks: EngineTaskSet,
    *,
    base_executable: Path | None = None,
) -> dict[str, object]:
    """Rebuild the structural projection with the helper from this installed prefix."""
    status = tasks.managed_workspace.status()
    if status is None:
        return {
            "inferred_suggestions": [],
            "status": "unconfigured",
            "structural_links": [],
        }
    snapshot = tasks.managed_workspace.graph_snapshot(status.workspace_id)
    store = GraphProjectionStore(tasks.profile.root, tasks.profile.root_identity)
    try:
        helper = discover_graphify_executable(base_executable)
        structural = store.refresh(snapshot, GraphifyAdapter(helper))
    except GraphifyFailure as error:
        structural = store.record_failure(snapshot, error.code)
    return projection_result(
        snapshot,
        structural,
        tasks.managed_inference.suggestions(status.workspace_id),
    )


def refresh_graph(
    tasks: EngineTaskSet,
    *,
    provider: ManagedProvider,
    access_mode: ManagedAccessMode,
    adapter_identity: str,
    request_id: str,
    invoke: GraphProvider,
    remaining_attempts: int,
    remaining_input_bytes: int,
) -> tuple[dict[str, object], int, int]:
    """Run one exact-source graph attempt for an already configured provider."""
    if remaining_attempts < 1 or remaining_input_bytes < 1:
        raise ValueError("graph refresh process budget is exhausted")
    status = tasks.managed_workspace.status()
    if status is None or not status.connected:
        raise ValueError("managed workspace is unavailable")
    observation = tasks.managed_workspace.observe(status.workspace_id)
    note_ids = tuple(
        note.note_id for note in observation.notes if note.present and not note.changed
    )
    request = tasks.managed_inference.prepare(
        status.workspace_id,
        provider,
        access_mode,
        adapter_identity,
        note_ids,
        request_id=request_id,
        max_output_bytes=16 * 1024,
        timeout_seconds=60,
    )
    input_bytes = len(request.prompt.encode("utf-8"))
    if input_bytes > remaining_input_bytes:
        tasks.managed_inference.fail(request_id)
        raise ValueError("graph refresh process budget is exhausted")
    released = tasks.managed_inference.release(request_id)
    try:
        provider_result = invoke(
            released.prompt,
            released.max_output_bytes,
            released.timeout_seconds,
        )
        draft = provider_result.suggestion()
        source_index = draft["source"]
        target_index = draft["target"]
        if (
            type(source_index) is not int
            or type(target_index) is not int
            or not 1 <= source_index <= len(released.sources)
            or not 1 <= target_index <= len(released.sources)
            or source_index == target_index
            or not isinstance(draft["source_quote"], str)
            or not isinstance(draft["target_quote"], str)
        ):
            raise ValueError("invalid graph provider result")
        suggestion = tasks.managed_inference.record_suggestion(
            request_id,
            source_note_id=released.sources[source_index - 1].note_id,
            target_note_id=released.sources[target_index - 1].note_id,
            source_quote=draft["source_quote"],
            target_quote=draft["target_quote"],
            model=provider_result.actual_model,
        )
    except ManagedProviderFailure as error:
        tasks.managed_inference.fail(request_id)
        return {"reason": error.code, "status": "failed"}, 1, input_bytes
    except Exception:
        tasks.managed_inference.fail(request_id)
        return {"reason": "provider_failure", "status": "failed"}, 1, input_bytes
    return (
        {
            "status": "refreshed",
            "actual_model": provider_result.actual_model,
            "suggestion": {
                "model": suggestion.model,
                "provider": suggestion.provider.value,
                "source_note_id": suggestion.source_note_id,
                "source_quote": suggestion.source_quote,
                "suggestion_id": suggestion.suggestion_id,
                "target_note_id": suggestion.target_note_id,
                "target_quote": suggestion.target_quote,
            },
            "usage": dict(provider_result.usage),
            "workspace_id": suggestion.workspace_id,
        },
        1,
        input_bytes,
    )


def database_is_busy(error: BaseException) -> bool:
    """Recognize typed contention through redacting storage exception wrappers."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, LockBusyError):
            return True
        if is_database_busy(current):
            return True
        current = current.__cause__ or current.__context__
    return False
