from __future__ import annotations

import base64
import json
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    Intent,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine import (
    CaptureAction,
    CaptureSubmission,
    EngineTaskSet,
    PublicJobCaptureContext,
    PublicProvenance,
    ReferencePayload,
    ScopedRetrieval,
    TextPayload,
    open_local_engine,
)
from open_brain_engine.engine.local import (
    BrainEngine,
    CaptureTasks,
    InboxSpaceTasks,
    RetrievalTasks,
    ReviewTasks,
)
from open_brain_engine.engine.source_intake import SourceRevisionSubmission
from open_brain_engine.portable.versioned import validate_portable_root

from open_brain.profile import compile_single_user_local


def _portable_id(prefix: str) -> str:
    return f"{prefix}_{uuid4()}"


def _public_job_context(tasks: EngineTaskSet) -> PublicJobCaptureContext:
    actor_id = _portable_id("actor")
    return PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": _portable_id("role_claim"),
            "role_id": _portable_id("role"),
            "tenant_id": tasks.profile.tenant_id,
        },
    )


def _local_privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _public_submission(
    tasks: EngineTaskSet,
    *,
    context: PublicJobCaptureContext | None = None,
    delivery_id: str = "delivery.contract.public-job",
    source_origin: ContentOrigin = ContentOrigin.THIRD_PARTY,
    source_reference: str = "https://example.test/public-job",
) -> CaptureSubmission:
    active_context = _public_job_context(tasks) if context is None else context
    return CaptureSubmission.for_public_job(
        context=active_context,
        payload=TextPayload("Synthetic public-job capture"),
        delivery_id=delivery_id,
        source_origin=source_origin,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=source_origin,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
    )


def test_capture_submission_is_immutable_and_replays_the_existing_request_fingerprint(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    submission = CaptureSubmission.for_local_owner(
        profile=tasks.profile,
        payload=TextPayload("Synthetic contract capture"),
        delivery_id="delivery.contract.replay",
    )

    with pytest.raises(FrozenInstanceError):
        submission.delivery_id = "delivery.changed"  # type: ignore[misc]

    accepted = tasks.capture.submit(submission)
    replay = tasks.capture.accept(
        TextPayload("Synthetic contract capture"),
        delivery_id="delivery.contract.replay",
    )

    assert replay.capture_id == accepted.capture_id
    assert replay.duplicate is True


def test_legacy_public_job_changed_delivery_requires_explicit_revision_evidence(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)
    source_reference = "https://example.test/github/issues/1"
    first = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic alphaonly upstream revision"),
        delivery_id="connector.github.synthetic-active-item",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )
    changed = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic betaonly upstream revision"),
        delivery_id=first.delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=first.provenance,
        privacy=first.privacy,
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )

    accepted = tasks.capture.submit(first)
    with pytest.raises(ValueError, match="conflicting delivery"):
        tasks.capture.submit(changed)
    replay = tasks.capture.submit(first)
    assert replay.capture_id == accepted.capture_id
    assert replay.duplicate is True
    results = tasks.retrieval.search("alphaonly")
    assert len(results) == 1
    assert results[0].result_id == accepted.capture_id
    assert tasks.retrieval.search("betaonly") == ()


def test_public_job_third_party_revision_preserves_superseded_source_in_export(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)
    source_reference = "https://example.test/github/issues/1"
    first = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic alphaonly upstream revision"),
        delivery_id="connector.github.synthetic-export-item",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )
    changed = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic betaonly upstream revision"),
        delivery_id=first.delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=first.provenance,
        privacy=first.privacy,
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )

    assert tasks.sources is not None
    namespace = dict(
        connector_name="synthetic", connection_id="one", resource_id="one", external_id="one"
    )
    accepted = tasks.sources.submit_revision(
        SourceRevisionSubmission(
            capture=first,
            namespace=namespace,
            revision_key="one",
            canonical_sha256=first.request_sha256(),
            expected_head=None,
            ordering={"kind": "unordered"},
            expected_control_epoch=0,
        )
    )
    replaced = tasks.sources.submit_revision(
        SourceRevisionSubmission(
            capture=changed,
            namespace=namespace,
            revision_key="two",
            canonical_sha256=changed.request_sha256(),
            expected_head=accepted.capture_id,
            ordering={"kind": "predecessor", "revision_key": "one"},
            expected_control_epoch=0,
        )
    )
    export = tmp_path / "export"
    receipt = tasks.portability.export(
        export,
        export_id="export_00000000-0000-4000-8000-000000000701",
    )
    manifest = validate_portable_root(export)
    manifest_files = cast(list[dict[str, object]], manifest["files"])
    capture_paths = [
        entry["path"]
        for entry in manifest_files
        if str(entry["path"]).startswith("sources/captures/")
    ]
    exported = {
        capture["capture_id"]: capture
        for capture in (
            json.loads((export / str(path)).read_text(encoding="utf-8")) for path in capture_paths
        )
    }

    assert receipt.captures == 2
    assert set(exported) == {accepted.capture_id, replaced.capture_id}
    assert _exported_reference_text(exported[accepted.capture_id]) == (
        "Synthetic alphaonly upstream revision"
    )
    assert _exported_reference_text(exported[replaced.capture_id]) == (
        "Synthetic betaonly upstream revision"
    )
    assert exported[accepted.capture_id]["source"] == {
        "origin": "third_party",
        "reference": source_reference,
    }
    assert exported[replaced.capture_id]["source"] == exported[accepted.capture_id]["source"]
    assert exported[accepted.capture_id]["provenance"]["source_ref"] == source_reference
    assert exported[replaced.capture_id]["provenance"]["source_ref"] == source_reference


def test_public_job_third_party_revision_cannot_retarget_source_identity(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)
    first_reference = "https://example.test/github/issues/1"
    changed_reference = "https://example.test/github/issues/2"
    first = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(first_reference, "Synthetic alphaonly upstream revision"),
        delivery_id="connector.github.synthetic-retarget-item",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=first_reference,
        provenance=Provenance.create(
            source_ref=first_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )
    retargeted = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(changed_reference, "Synthetic betaonly upstream revision"),
        delivery_id=first.delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=changed_reference,
        provenance=Provenance.create(
            source_ref=changed_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=first.privacy,
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )

    accepted = tasks.capture.submit(first)
    with pytest.raises(ValueError, match="conflicting delivery"):
        tasks.capture.submit(retargeted)

    assert tasks.retrieval.search("betaonly") == ()
    assert tasks.retrieval.search("alphaonly")[0].result_id == accepted.capture_id


def test_public_job_third_party_revision_cannot_replace_routed_capture(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)
    source_reference = "https://example.test/github/issues/1"
    first = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic alphaonly upstream revision"),
        delivery_id="connector.github.synthetic-routed-item",
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.THIRD_PARTY,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )
    changed = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(source_reference, "Synthetic betaonly upstream revision"),
        delivery_id=first.delivery_id,
        source_origin=ContentOrigin.THIRD_PARTY,
        source_reference=source_reference,
        provenance=first.provenance,
        privacy=first.privacy,
        intent=Intent.REFERENCE,
        title="Synthetic issue",
    )

    accepted = tasks.capture.submit(first)
    space = tasks.inbox.create_space("Synthetic routed source", delivery_id="delivery.space.routed")
    tasks.inbox.route(
        accepted.capture_id,
        space.space_id,
        delivery_id="delivery.route.routed-source",
    )

    with pytest.raises(ValueError, match="conflicting delivery"):
        tasks.capture.submit(changed)

    results = tasks.retrieval.search("alphaonly")
    assert len(results) == 1
    assert results[0].result_id == accepted.capture_id
    assert results[0].space_id == space.space_id
    assert tasks.retrieval.search("betaonly") == ()


def _exported_reference_text(capture: dict[str, object]) -> str:
    original_payload = capture["original_payload"]
    assert isinstance(original_payload, dict)
    encoded = original_payload["bytes_base64"]
    assert isinstance(encoded, str)
    payload = json.loads(base64.b64decode(encoded).decode("utf-8"))
    assert isinstance(payload, dict)
    text = payload["supplied_text"]
    assert isinstance(text, str)
    return text


def test_capture_submission_validates_delivery_occurrence_and_bound_profile(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    submission = CaptureSubmission.for_local_owner(
        profile=tasks.profile,
        payload=TextPayload("Synthetic submission validation"),
        delivery_id="delivery.contract.validation",
    )

    with pytest.raises(ValueError, match="delivery"):
        CaptureSubmission.for_local_owner(
            profile=tasks.profile,
            payload=TextPayload("Synthetic invalid delivery"),
            delivery_id="not a delivery identity",
        )
    with pytest.raises(ValueError, match="occurrence"):
        replace(submission, occurrence_at="2026-08-31T12:00:00Z")
    other_actor_id = _portable_id("actor")
    with pytest.raises(ValueError, match="local profile"):
        tasks.capture.submit(
            replace(
                submission,
                actor_id=other_actor_id,
                role_claim={**submission.role_claim, "actor_id": other_actor_id},
            )
        )


def test_open_local_engine_returns_one_named_task_set(tmp_path: Path) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    legacy = BrainEngine.open(profile)

    assert isinstance(tasks, EngineTaskSet)
    assert tasks.inbox is tasks.spaces
    assert tasks.profile.provider_mode.value == "none"
    assert isinstance(legacy.capture, CaptureTasks)
    assert isinstance(legacy.inbox, InboxSpaceTasks)
    assert isinstance(legacy.review, ReviewTasks)
    assert isinstance(legacy.retrieval, RetrievalTasks)


def test_public_provenance_exposes_only_opaque_ids_and_a_bounded_origin(tmp_path: Path) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    accepted = tasks.capture.accept(
        TextPayload("Synthetic safe provenance"),
        delivery_id="delivery.contract.provenance",
    )

    result = tasks.retrieval.search("safe provenance")[0]

    assert isinstance(result.provenance, PublicProvenance)
    assert result.provenance.capture_id == accepted.capture_id
    assert result.provenance.source_record_id == accepted.capture_id
    assert result.provenance.source_origin == "owner_authored"
    assert result.provenance.as_dict() == {
        "capture_id": accepted.capture_id,
        "source_origin": "owner_authored",
        "source_record_id": accepted.capture_id,
    }
    assert not hasattr(result.provenance, "source_reference")
    assert not hasattr(result.provenance, "source_ref_sha256")


def test_scoped_retrieval_denies_unassigned_and_known_disallowed_results_like_unknown(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Allowed", "Denied"))
    )
    allowed, denied = tasks.spaces.spaces()
    allowed_capture = tasks.capture.accept(
        TextPayload("Synthetic scope token allowed"),
        delivery_id="delivery.contract.allowed",
        space_id=allowed.space_id,
    )
    denied_capture = tasks.capture.accept(
        TextPayload("Synthetic scope token denied"),
        delivery_id="delivery.contract.denied",
        space_id=denied.space_id,
    )
    unassigned_capture = tasks.capture.accept(
        TextPayload("Synthetic scope token unassigned"),
        delivery_id="delivery.contract.unassigned",
    )
    scoped = ScopedRetrieval(tasks.retrieval, allowed_space_ids=frozenset({allowed.space_id}))
    empty = ScopedRetrieval(tasks.retrieval, allowed_space_ids=frozenset())

    results = scoped.search("Synthetic scope token")

    assert {result.capture_id for result in results} == {allowed_capture.capture_id}
    fetched = scoped.fetch(results[0].result_id)
    assert fetched is not None
    assert fetched.result_id == results[0].result_id
    known_disallowed = scoped.fetch(denied_capture.capture_id)
    assert scoped.fetch(unassigned_capture.capture_id) is None
    unknown = scoped.fetch("result_00000000-0000-4000-8000-000000000000")
    assert known_disallowed == unknown is None
    assert empty.search("Synthetic scope token") == ()
    assert empty.fetch(results[0].result_id) is None


def test_scoped_fetch_denies_a_known_canonical_result_before_content_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = open_local_engine(
        compile_single_user_local(tmp_path / "brain", starter_spaces=("Allowed", "Denied"))
    )
    allowed, denied = tasks.spaces.spaces()
    canonical = tasks.capture.accept(
        TextPayload("Synthetic denied canonical content"),
        delivery_id="delivery.contract.denied.canonical",
        action=CaptureAction.CANONICAL_NOTE,
        space_id=denied.space_id,
    )
    assert canonical.canonical_path is not None
    denied_result = next(
        result
        for result in tasks.retrieval.search("denied canonical", record_type="canonical")
        if result.space_id == denied.space_id
    )
    scoped = ScopedRetrieval(tasks.retrieval, allowed_space_ids=frozenset({allowed.space_id}))
    reads: list[str] = []

    def read_if_called(*_args: object, **_kwargs: object) -> bytes:
        reads.append("canonical content read")
        raise AssertionError("scoped fetch must not read a disallowed canonical file")

    monkeypatch.setattr("open_brain_engine.engine.retrieval.read_confined", read_if_called)

    known_disallowed = scoped.fetch(denied_result.result_id)
    unknown = scoped.fetch("result_00000000-0000-4000-8000-000000000000")

    assert known_disallowed == unknown is None
    assert reads == []


def test_owner_submission_keeps_the_legacy_request_value_fingerprint_and_storage_values(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    submission = CaptureSubmission.for_local_owner(
        profile=tasks.profile,
        payload=TextPayload("Synthetic fingerprint stable"),
        delivery_id="delivery.contract.owner-fingerprint",
        intent=Intent.REFERENCE,
        capture_why="Keep this synthetic note",
        title="Synthetic title",
    )
    expected_value = {
        "action": "quick",
        "capture_why": "Keep this synthetic note",
        "intent": "reference",
        "payload": {"family": "text", "text": "Synthetic fingerprint stable"},
        "source_origin": "owner",
        "space_id": None,
        "title": "Synthetic title",
    }
    expected_bytes = (
        b'{"action":"quick","capture_why":"Keep this synthetic note",'
        b'"intent":"reference","payload":{"family":"text",'
        b'"text":"Synthetic fingerprint stable"},"source_origin":"owner",'
        b'"space_id":null,"title":"Synthetic title"}'
    )

    accepted = tasks.capture.submit(submission)
    connection = tasks.capture._engine._store.connect()  # type: ignore[attr-defined]
    try:
        row = connection.execute(
            "SELECT source_origin, source_reference, intent, capture_why, action, title "
            "FROM captures WHERE capture_id = ?",
            (accepted.capture_id,),
        ).fetchone()
    finally:
        connection.close()

    assert submission.schema_version == 1
    assert submission.request_value() == expected_value
    assert portable_canonical_json_bytes(submission.request_value()) == expected_bytes
    assert submission.request_sha256() == (
        "e5826914b9026d7074b3587832822c0bc2ee0bd6bf0416a23772d73302752bc4"
    )
    assert sha256(expected_bytes).hexdigest() == submission.request_sha256()
    assert row is not None
    assert tuple(row) == (
        "owner",
        "urn:open-brain:local:1029fe18bbf4285e4bf8637d3796bd5d3484b0b4dd6818aa03e4bea4196cee21",
        "reference",
        "Keep this synthetic note",
        "quick",
        "Synthetic title",
    )


def test_capture_submission_rejects_invalid_versions_origins_and_metadata_invariants(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    owner = CaptureSubmission.for_local_owner(
        profile=tasks.profile,
        payload=TextPayload("Synthetic typed owner capture"),
        delivery_id="delivery.contract.typed-owner",
        intent=Intent.REFERENCE,
        capture_why="Keep this typed owner capture",
    )

    with pytest.raises(ValueError, match="schema version"):
        replace(owner, schema_version=2)
    with pytest.raises(ValueError, match="source origin"):
        _public_submission(tasks, source_origin=ContentOrigin.MIXED)
    with pytest.raises(ValueError, match="source origin"):
        _public_submission(tasks, source_origin=ContentOrigin.OWNER_AUTHORED)
    with pytest.raises(ValueError):
        _public_submission(
            tasks,
            source_origin="unbounded",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="capture reason"):
        replace(
            owner,
            capture_why_origin=CaptureWhyOrigin.AUTOMATION_ABSENT,
        )
    with pytest.raises(ValueError, match="occurrence"):
        replace(
            CaptureSubmission.for_local_owner(
                profile=tasks.profile,
                payload=TextPayload("Synthetic no-occurrence capture"),
                delivery_id="delivery.contract.occurrence",
            ),
            occurrence_at="2026-08-31T12:00:00Z",
        )

    assert owner.intent is Intent.REFERENCE
    assert owner.capture_why_origin is CaptureWhyOrigin.OWNER_AUTHORED
    assert isinstance(owner.provenance, Provenance)
    assert isinstance(owner.privacy, PrivacyDecision)
    with pytest.raises(TypeError):
        owner.role_claim["actor_id"] = "changed"  # type: ignore[index]
    capabilities = owner.role_claim["capabilities"]
    assert isinstance(capabilities, tuple)
    with pytest.raises(AttributeError):
        capabilities.append("canonical.publish")  # type: ignore[attr-defined]


def test_public_job_submission_is_non_owner_and_cannot_canonicalize_or_publish(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)
    submission = _public_submission(tasks, context=context)

    accepted = tasks.capture.submit(submission)

    assert submission.schema_version == 1
    assert submission.source_origin is ContentOrigin.THIRD_PARTY
    assert accepted.canonical_path is None
    assert accepted.state == "inbox"
    with pytest.raises(ValueError, match="canonical-note"):
        tasks.capture.submit(replace(submission, action=CaptureAction.CANONICAL_NOTE))
    with pytest.raises(ValueError, match="conflicting delivery"):
        tasks.capture.submit(
            _public_submission(
                tasks,
                context=context,
                source_reference="https://example.test/public-job-changed",
            )
        )


def test_public_job_sink_accepts_unknown_ingress_without_owner_publication_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "brain"
    tasks = open_local_engine(compile_single_user_local(root))
    context = _public_job_context(tasks)
    source_reference = "urn:open-brain:public-job:unknown"

    accepted = tasks.capture.public_job_sink(context).submit(
        TextPayload("Synthetic unknown public-job capture"),
        delivery_id="delivery.contract.public-job-unknown",
        source_origin=ContentOrigin.UNKNOWN,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.UNKNOWN,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
    )
    source = json.loads(next((root / "sources" / "captures").rglob("*.json")).read_bytes())

    assert accepted.canonical_path is None
    assert tuple((root / "content" / "spaces").rglob("page_*.md")) == ()
    assert source["actor_id"] == context.actor_id
    assert source["role_claim"] == {
        "actor_id": context.actor_id,
        "capabilities": ["capture.accept"],
        "role_claim_id": context.role_claim["role_claim_id"],
        "role_id": context.role_claim["role_id"],
        "tenant_id": context.tenant_id,
    }
    assert source["source"]["origin"] == "third_party"
    assert source["provenance"]["content_origin"] == "unknown"
    assert source["trust"]["label"] == "unverified"
    result = tasks.retrieval.search("unknown public-job capture")[0]
    assert result.provenance.source_origin == "unknown"
    assert result.trust == "unverified"


def test_public_job_context_rejects_owner_spoofing_tenant_mismatch_and_bad_roles(
    tmp_path: Path,
) -> None:
    tasks = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    context = _public_job_context(tasks)

    with pytest.raises(ValueError, match="owner"):
        PublicJobCaptureContext.create(
            profile=tasks.profile,
            actor_id=tasks.profile.owner_actor_id,
            role_claim=tasks.profile.owner_role_claim,
        )
    with pytest.raises(ValueError, match="tenant"):
        mismatched_tenant = _portable_id("tenant")
        tasks.capture.submit(
            _public_submission_from_context(
                tasks,
                replace(
                    context,
                    tenant_id=mismatched_tenant,
                    role_claim={**context.role_claim, "tenant_id": mismatched_tenant},
                ),
            )
        )
    with pytest.raises(ValueError, match="role"):
        PublicJobCaptureContext.create(
            profile=tasks.profile,
            actor_id=_portable_id("actor"),
            role_claim={"capabilities": ["capture.accept"]},
        )


def _public_submission_from_context(
    tasks: EngineTaskSet, context: PublicJobCaptureContext
) -> CaptureSubmission:
    source_reference = "https://example.test/public-job-context"
    return CaptureSubmission.for_public_job(
        context=context,
        payload=TextPayload("Synthetic public-job context"),
        delivery_id="delivery.contract.public-job-context",
        source_origin=ContentOrigin.UNKNOWN,
        source_reference=source_reference,
        provenance=Provenance.create(
            source_ref=source_reference,
            content_origin=ContentOrigin.UNKNOWN,
            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
        ),
        privacy=_local_privacy(),
    )
