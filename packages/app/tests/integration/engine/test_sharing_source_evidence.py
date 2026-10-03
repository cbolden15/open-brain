"""Independent retained source evidence and frozen sharing-job admission checks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine.contracts import JournalEnvelope, ReferencePayload
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingPreview,
    SharingPreviewRequest,
    SharingRevokeRequest,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import SourceRouteRequest

from packages.app.tests.unit.engine.test_foundation_contracts import _public_submission
from packages.app.tests.unit.engine.test_sharing_tasks import _managed_source


def _decision(request: SharingPreviewRequest, preview: SharingPreview) -> SharingDecisionRequest:
    return SharingDecisionRequest(
        operation_id="sharing.evidence.decision",
        preview_id=preview.preview_id,
        preview_sha256=preview.preview_sha256,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        expected_decision_version=0,
        decision="approve",
    )


def _counts(engine: BrainEngine) -> tuple[int, ...]:
    with engine._store.connect() as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in (
                "sharing_previews",
                "sharing_decisions",
                "sharing_links",
                "captures",
                "sharing_revocations",
                "capture_ingestion_items",
            )
        )


@pytest.mark.parametrize("adopt_alias", [False, True])
def test_supported_alias_adoption_sharing_and_damaged_admission_plan_refusal(
    tmp_path: Path, adopt_alias: bool
) -> None:
    tasks, request, owner, text = _managed_source(tmp_path, adopt_alias=adopt_alias)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    with engine._store.connect() as connection:
        intake = connection.execute("SELECT * FROM source_intakes").fetchone()
        assert intake is not None
        plan = json.loads(intake["plan_json"])
        assert (plan == {}) is adopt_alias
        assert intake["delivery_id"].startswith("adoption.") is adopt_alias
    preview = tasks.sharing.preview(request, authority=owner)
    assert preview.text == text
    assert tasks.sharing.preview(request, authority=owner) == preview
    before = _counts(engine)
    with _damage(engine, "source_intakes", {"plan_json": '{"unexpected":true}'}):
        with pytest.raises(SharingError, match="binding_mismatch|revision_changed"):
            tasks.sharing.preview(
                replace(request, operation_id="sharing.adoption.damaged"), authority=owner
            )
        with pytest.raises(SharingError, match="binding_mismatch|revision_changed"):
            tasks.sharing.decide(_decision(request, preview), authority=owner)
        assert _counts(engine) == before
    approved = tasks.sharing.decide(_decision(request, preview), authority=owner)
    assert approved.state == "captured" and approved.copy_capture_id is not None
    assert tasks.sharing.decide(_decision(request, preview), authority=owner) == approved
    with engine._store.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sharing_links").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 2


@contextmanager
def _damage(engine: BrainEngine, table: str, changes: dict[str, Any]) -> Iterator[None]:
    """Inject disposable corruption with exact trigger DDL restored before calls."""
    with engine._store.transaction() as connection:
        original = dict(connection.execute(f"SELECT * FROM {table}").fetchone())  # noqa: S608
        sql = f"UPDATE {table} SET " + ",".join(f"{key}=?" for key in changes)  # noqa: S608
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=? "
            "AND sql LIKE '%BEFORE UPDATE%'",
            (table,),
        ).fetchall()
        if triggers:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, tuple(changes.values()))
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')  # noqa: S608
        connection.execute(sql, tuple(changes.values()))
        for _, ddl in triggers:
            connection.execute(ddl)
    try:
        yield
    finally:
        with engine._store.transaction() as connection:
            for name, _ in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')  # noqa: S608
            connection.execute(sql, tuple(original[key] for key in changes))
            for _, ddl in triggers:
                connection.execute(ddl)


SOURCE_CASES = (
    "intake-payload",
    "intake-privacy",
    "intake-provenance",
    "intake-actor",
    "intake-request-hash",
    "intake-revision-key",
    "intake-namespace",
    "intake-receipt-source",
    "intake-receipt-head",
    "intake-receipt-outcome",
    "intake-receipt-epoch",
    "intake-receipt-extra",
    "intake-submission-extra",
    "intake-plan-namespace",
    "intake-plan-ordering",
    "intake-plan-head",
    "intake-plan-predecessor",
    "intake-plan-promote",
    "intake-plan-epoch",
    "intake-plan-delivery",
    "intake-plan-extra",
    "managed-receipt-source",
    "managed-receipt-epoch",
    "managed-receipt-custody",
    "managed-receipt-extra",
    "managed-receipt-malformed",
    "envelope-payload",
    "envelope-privacy",
    "envelope-provenance",
    "envelope-extra",
    "envelope-dto-type",
    "envelope-delivery",
    "envelope-source-delivery",
    "envelope-namespace",
    "envelope-destination",
    "envelope-issuer",
    "envelope-head",
    "envelope-lifecycle",
    "envelope-hash",
    "observation-normalization",
    "observation-policy",
    "observation-payload",
    "observation-extra",
    "capture-payload",
    "capture-privacy",
    "capture-provenance",
    "capture-source-reference",
    "capture-source-origin",
    "capture-path",
    "revision-request-hash",
    "revision-order",
    "revision-source-hash",
    "row-destination",
    "row-issuer",
)


def _source_damage(engine: BrainEngine, case: str) -> tuple[str, dict[str, Any]]:
    with engine._store.connect() as connection:
        managed = dict(connection.execute("SELECT * FROM managed_source_deliveries").fetchone())
        intake = dict(connection.execute("SELECT * FROM source_intakes").fetchone())
        capture = dict(connection.execute("SELECT * FROM captures").fetchone())
    envelope = json.loads(managed["envelope_bytes"])
    submission = json.loads(intake["submission_json"])
    receipt = json.loads(intake["receipt_json"])
    retained = json.loads(managed["receipt_json"])
    if case.startswith("intake-receipt-") or case.startswith("managed-receipt-"):
        value = receipt if case.startswith("intake-") else retained["source_receipt"]
        field = case.rsplit("-", 1)[1]
        if field == "extra":
            value["unexpected"] = True
        elif field == "malformed":
            return "managed_source_deliveries", {"receipt_json": "{"}
        else:
            key, replacement = {
                "source": ("source_id", "source_" + str(uuid4())),
                "head": ("capture_id", "capture_" + str(uuid4())),
                "outcome": ("outcome", "history_only"),
                "epoch": ("control_epoch", 99),
                "custody": ("custody_id", "custody_" + str(uuid4())),
            }[field]
            value[key] = replacement
        return (
            "source_intakes" if case.startswith("intake-") else "managed_source_deliveries",
            {"receipt_json": json.dumps(receipt if case.startswith("intake-") else retained)},
        )
    if case.startswith("intake-plan-"):
        plan = json.loads(intake["plan_json"])
        key, replacement = {
            "namespace": ("namespace_json", "{}"),
            "ordering": ("ordering", {"kind": "predecessor", "revision_key": "other"}),
            "head": ("expected_head", "capture_" + str(uuid4())),
            "predecessor": ("predecessor_capture_id", "capture_" + str(uuid4())),
            "promote": ("promote", False),
            "epoch": ("control_epoch", 99),
            "delivery": ("legacy_delivery_id", "other-delivery"),
            "extra": ("unexpected", True),
        }[case.removeprefix("intake-plan-")]
        plan[key] = replacement
        return "source_intakes", {"plan_json": json.dumps(plan)}
    if case.startswith("intake-"):
        field = case.removeprefix("intake-")
        if field == "request-hash":
            return "source_intakes", {"request_sha256": "c" * 64}
        if field == "revision-key":
            return "source_intakes", {"revision_key": "other-revision"}
        if field == "namespace":
            submission["namespace"]["external_id"] = "other-item"
        elif field == "submission-extra":
            submission["unexpected"] = True
        else:
            _change_capture(submission["capture"], field)
            submission["canonical_sha256"] = sha256(
                portable_canonical_json_bytes(submission["capture"])
            ).hexdigest()
        return "source_intakes", {"submission_json": portable_canonical_json_bytes(submission)}
    if case.startswith("envelope-") or case.startswith("observation-"):
        field = case.removeprefix("envelope-")
        if field in {"payload", "privacy", "provenance"}:
            _change_capture(envelope["submission"]["capture"], field)
            envelope["submission"]["canonical_sha256"] = sha256(
                portable_canonical_json_bytes(envelope["submission"]["capture"])
            ).hexdigest()
        elif field == "extra":
            envelope["unexpected"] = True
        elif field == "dto-type":
            envelope["dto_version"] = "2"
        elif field == "delivery":
            envelope["delivery_id"] = "other-delivery"
        elif field == "source-delivery":
            envelope["submission"]["delivery_id"] = "other-source-delivery"
        elif field == "namespace":
            envelope["binding"]["namespace"]["external_id"] = "other-item"
        elif field == "destination":
            envelope["binding"]["destination_brain_id"] = "brn_" + "b" * 26
        elif field == "issuer":
            envelope["binding"]["issuer_epoch"] = 99
        elif field == "head":
            envelope["submission"]["expected_head"] = "capture_" + str(uuid4())
        elif field == "lifecycle":
            envelope["expected_lifecycle_version"] = 99
        elif field == "hash":
            return "managed_source_deliveries", {"envelope_sha256": "c" * 64}
        elif case.startswith("observation-"):
            key, replacement = {
                "normalization": ("normalization_version", "unsupported.v1"),
                "policy": ("privacy_policy_sha256", "c" * 64),
                "payload": ("admitted_payload_sha256", "c" * 64),
                "extra": ("unexpected", True),
            }[case.removeprefix("observation-")]
            envelope["observation"][key] = replacement
        raw = portable_canonical_json_bytes(envelope)
        return "managed_source_deliveries", {
            "envelope_bytes": raw,
            "envelope_sha256": sha256(raw).hexdigest(),
        }
    if case.startswith("capture-"):
        field = case.removeprefix("capture-")
        if field in {"payload", "privacy", "provenance"}:
            column = field + "_json"
            value = json.loads(capture[column])
            _change_capture({field: value}, field)
            raw = portable_canonical_json_bytes(value)
            return "captures", {column: raw if field == "payload" else raw.decode()}
        key, replacement = {
            "source-reference": ("source_reference", "https://example.test/other"),
            "source-origin": ("source_origin", "owner"),
            "path": ("submission_path", "owner"),
        }[field]
        return "captures", {key: replacement}
    if case.startswith("revision-"):
        key, replacement = {
            "request-hash": ("request_sha256", "c" * 64),
            "order": ("ordering_json", '{"kind":"predecessor","revision_key":"other"}'),
            "source-hash": ("source_sha256", "c" * 64),
        }[case.removeprefix("revision-")]
        return "source_revisions", {key: replacement}
    key, replacement = {
        "row-destination": ("destination_brain_id", "brn_" + "b" * 26),
        "row-issuer": ("issuer_epoch", 99),
    }[case]
    return "managed_source_deliveries", {key: replacement}


def _change_capture(value: dict[str, Any], field: str) -> None:
    if field == "payload":
        value["payload"]["supplied_text"] = "Synthetic forged text\n"
    elif field == "privacy":
        value["privacy"]["policy_version"] = "other-policy"
    elif field == "provenance":
        value["provenance"]["source_ref"] = "https://example.test/other"
    elif field == "actor":
        value["actor_id"] = "actor_" + str(uuid4())


@pytest.mark.parametrize("boundary", ["preview", "decision"])
@pytest.mark.parametrize("case", SOURCE_CASES)
def test_retained_source_corruption_refuses_before_mutation(
    tmp_path: Path, boundary: str, case: str
) -> None:
    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    assert preview.text == text
    decision = _decision(request, preview)
    fresh = replace(request, operation_id="sharing.evidence.fresh")
    table, changes = _source_damage(engine, case)
    before = _counts(engine)
    with engine._store.connect() as connection:
        independent = bytes(
            connection.execute("SELECT source_bytes FROM source_revisions").fetchone()[0]
        )
    with _damage(engine, table, changes):
        with pytest.raises(SharingError) as refused:
            if boundary == "preview":
                tasks.sharing.preview(fresh, authority=owner)
            else:
                tasks.sharing.decide(decision, authority=owner)
        assert refused.value.code in {"binding_mismatch", "revision_changed"}
        assert _counts(engine) == before
        with engine._store.connect() as connection:
            assert (
                bytes(connection.execute("SELECT source_bytes FROM source_revisions").fetchone()[0])
                == independent
            )
    if boundary == "preview":
        assert tasks.sharing.preview(fresh, authority=owner).text == text
    approved = tasks.sharing.decide(decision, authority=owner)
    assert approved.copy_capture_id is not None
    assert tasks.sharing.decide(decision, authority=owner) == approved


JOB_CASES = (
    "tenant",
    "owner-actor",
    "owner-role",
    "owner-claim",
    "actor-format",
    "role-format",
    "claim-format",
    "actor-uuid-version",
    "brain",
    "epoch",
    "fresh-actor",
    "fresh-role",
    "fresh-claim",
)


@pytest.mark.parametrize("case", JOB_CASES)
@pytest.mark.parametrize("boundary", ["preview", "approve", "reject"])
def test_job_identity_is_profile_bound_and_frozen_before_reservation(
    tmp_path: Path, case: str, boundary: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    with engine._store.connect() as connection:
        job = dict(connection.execute("SELECT * FROM sharing_job_identity").fetchone())
    for key, prefix in (
        ("actor_id", "actor_"),
        ("role_id", "role_"),
        ("role_claim_id", "role_claim_"),
    ):
        assert UUID(job[key].removeprefix(prefix)).version == 4
    key, value = {
        "tenant": ("tenant_id", "tenant_" + str(uuid4())),
        "owner-actor": ("actor_id", tasks.profile.owner_actor_id),
        "owner-role": ("role_id", tasks.profile.owner_role_claim["role_id"]),
        "owner-claim": ("role_claim_id", tasks.profile.owner_role_claim["role_claim_id"]),
        "actor-format": ("actor_id", "actor_invalid"),
        "role-format": ("role_id", "role_invalid"),
        "claim-format": ("role_claim_id", "role_claim_invalid"),
        "actor-uuid-version": ("actor_id", "actor_00000000-0000-1000-8000-000000000001"),
        "brain": ("brain_id", "brn_" + "b" * 26),
        "epoch": ("issuer_epoch", 99),
        "fresh-actor": ("actor_id", "actor_" + str(uuid4())),
        "fresh-role": ("role_id", "role_" + str(uuid4())),
        "fresh-claim": ("role_claim_id", "role_claim_" + str(uuid4())),
    }[case]
    decision = replace(
        _decision(request, preview), decision="reject" if boundary == "reject" else "approve"
    )
    fresh = replace(request, operation_id="sharing.evidence.fresh")
    before = _counts(engine)
    with _damage(engine, "sharing_job_identity", {key: value}):
        with pytest.raises(SharingError) as refused:
            if boundary == "preview":
                tasks.sharing.preview(fresh, authority=owner)
            else:
                tasks.sharing.decide(decision, authority=owner)
        assert refused.value.code in {"binding_mismatch", "revision_changed"}
        assert _counts(engine) == before
    assert tasks.sharing.decide(decision, authority=owner).state in {"captured", "rejected"}


def test_decision_and_revoke_changed_replay_preserve_one_copy(tmp_path: Path) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    decision = _decision(request, preview)
    approved = tasks.sharing.decide(decision, authority=owner)
    before = _counts(engine)
    for changed_decision in (
        replace(decision, decision="reject"),
        replace(decision, destination_brain_id="brn_" + "b" * 26),
        replace(decision, preview_sha256="c" * 64),
        replace(decision, operation_id="sharing.evidence.second"),
    ):
        with pytest.raises(SharingError):
            tasks.sharing.decide(changed_decision, authority=owner)
        assert _counts(engine) == before
    with pytest.raises(SharingError, match="invalid_arguments"):
        replace(decision, expected_decision_version=1)
    revoke = SharingRevokeRequest(
        operation_id="sharing.evidence.revoke",
        approval_id=approved.approval_id,
        expected_approval_version=1,
        brain_id=request.brain_id,
        issuer_epoch=request.issuer_epoch,
        destination_brain_id=request.brain_id,
        reason="owner_choice",
    )
    revoked = tasks.sharing.revoke(revoke, authority=owner)
    before = _counts(engine)
    for changed_revoke in (
        replace(revoke, reason="source_changed"),
        replace(revoke, expected_approval_version=2),
        replace(revoke, destination_brain_id="brn_" + "b" * 26),
    ):
        with pytest.raises(SharingError):
            tasks.sharing.revoke(changed_revoke, authority=owner)
        assert _counts(engine) == before
    assert tasks.sharing.revoke(revoke, authority=owner) == revoked
    assert tasks.sharing.decide(decision, authority=owner) == approved


@pytest.mark.parametrize("transition", ["head", "route", "withdraw"])
@pytest.mark.parametrize("decision_kind", ["approve", "reject"])
def test_real_source_transition_after_preview_refuses_decision_before_reservation(
    tmp_path: Path, transition: str, decision_kind: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None and tasks.sources is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    decision = replace(_decision(request, preview), decision=decision_kind)
    if transition == "withdraw":
        tasks.sources.withdraw(
            SourceWithdrawRequest(
                operation_id="withdraw.sharing.evidence",
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_lifecycle_version=0,
                brain_id=request.brain_id,
                issuer_epoch=request.issuer_epoch,
                reason_code="owner_choice",
            ),
            authority=owner,
        )
    elif transition == "route":
        space = tasks.inbox.create_space("Synthetic evidence route", delivery_id="synthetic.route")
        tasks.sources.route(
            SourceRouteRequest(
                operation_id="operation_" + str(uuid4()),
                source_id=request.source_id,
                expected_head=request.expected_head,
                expected_route_version=0,
                space_id=space.space_id,
            ),
            authority=owner,
        )
    else:
        with engine._store.connect() as connection:
            envelope = json.loads(
                connection.execute(
                    "SELECT envelope_bytes FROM managed_source_deliveries"
                ).fetchone()[0]
            )
        privacy = _public_submission(tasks).privacy.from_dict(
            envelope["submission"]["capture"]["privacy"]
        )
        capture = replace(
            _public_submission(tasks),
            payload=ReferencePayload(
                "https://example.test/synthetic", "Synthetic next evidence head\n"
            ),
            privacy=privacy,
            delivery_id="synthetic.evidence.next",
        )
        binding = SourceRevisionBinding(**envelope["binding"])
        observation = replace(
            SourceRevisionObservation.from_value(envelope["observation"]),
            admitted_payload_sha256=sha256(
                portable_canonical_json_bytes(capture.payload.to_dict())
            ).hexdigest(),
        )
        tasks.sources.public_revision_sink(binding).submit(
            SourceRevisionObservedDelivery(
                binding=binding,
                submission=SourceRevisionSubmission(
                    capture=capture,
                    namespace=binding.namespace,
                    revision_key="synthetic-evidence-next",
                    canonical_sha256=capture.request_sha256(),
                    expected_head=request.expected_head,
                    ordering={"kind": "predecessor", "revision_key": "synthetic-revision"},
                    expected_control_epoch=0,
                ),
                expected_lifecycle_version=0,
                delivery_id="synthetic.evidence.next.delivery",
                observation=observation,
            )
        )
    before = _counts(engine)
    with pytest.raises(SharingError, match="revision_changed"):
        tasks.sharing.decide(decision, authority=owner)
    assert _counts(engine) == before


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "actor_id",
        "role_id",
        "role_claim_id",
        "brain_id",
        "issuer_epoch",
        "capabilities",
        "extra",
        "epoch-type",
    ],
)
@pytest.mark.parametrize("boundary", ["preview", "approve", "reject"])
def test_rehashed_frozen_job_identity_cannot_replace_independent_job(
    tmp_path: Path, field: str, boundary: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    with engine._store.connect() as connection:
        row = dict(connection.execute("SELECT * FROM sharing_previews").fetchone())
        independent = tuple(connection.execute("SELECT * FROM sharing_job_identity").fetchone())
    frozen = json.loads(row["preview_bytes"])
    identity = frozen["job_identity"]
    identity[
        field
        if field not in {"extra", "epoch-type"}
        else "issuer_epoch"
        if field == "epoch-type"
        else "unexpected"
    ] = (
        True
        if field in {"extra", "epoch-type"}
        else 99
        if field == "issuer_epoch"
        else ["capture.accept", "review.decide"]
        if field == "capabilities"
        else "brn_" + "b" * 26
        if field == "brain_id"
        else field.removesuffix("_id") + "_" + str(uuid4())
    )
    raw = portable_canonical_json_bytes(frozen)
    digest = sha256(b"open-brain-sharing-preview.v1\0" + raw).hexdigest()
    decision = replace(
        _decision(request, preview),
        preview_sha256=digest,
        decision="reject" if boundary == "reject" else "approve",
    )
    before = _counts(engine)
    with _damage(engine, "sharing_previews", {"preview_bytes": raw, "preview_sha256": digest}):
        with pytest.raises(SharingError, match="binding_mismatch"):
            if boundary == "preview":
                tasks.sharing.preview(
                    replace(request, operation_id="sharing.evidence.fresh"), authority=owner
                )
            else:
                tasks.sharing.decide(decision, authority=owner)
        assert _counts(engine) == before
        with engine._store.connect() as connection:
            assert (
                tuple(connection.execute("SELECT * FROM sharing_job_identity").fetchone())
                == independent
            )
    assert (
        tasks.sharing.decide(_decision(request, preview), authority=owner).copy_capture_id
        is not None
    )


def test_copy_uses_exact_quick_text_and_capture_only_frozen_job(tmp_path: Path) -> None:
    tasks, request, owner, text = _managed_source(tmp_path)
    assert tasks.sharing is not None
    preview = tasks.sharing.preview(request, authority=owner)
    approved = tasks.sharing.decide(_decision(request, preview), authority=owner)
    with tasks.sharing._engine._store.connect() as connection:
        frozen = json.loads(
            connection.execute("SELECT preview_bytes FROM sharing_previews").fetchone()[0]
        )
        envelope = JournalEnvelope.from_bytes(
            bytes(
                connection.execute(
                    "SELECT copy_submission_bytes FROM sharing_decisions"
                ).fetchone()[0]
            )
        )
        copied = dict(
            connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (approved.copy_capture_id,)
            ).fetchone()
        )
    assert envelope.submission.action.value == copied["action"] == "quick"
    assert envelope.submission.submission_path.value == copied["submission_path"] == "public_job"
    assert envelope.submission.payload.to_dict() == {"family": "text", "text": text}
    assert envelope.submission.role_claim["capabilities"] == ("capture.accept",)
    assert envelope.submission.actor_id == frozen["job_identity"]["actor_id"]
    assert envelope.submission.role_claim["role_id"] == frozen["job_identity"]["role_id"]
    assert (
        envelope.submission.role_claim["role_claim_id"] == frozen["job_identity"]["role_claim_id"]
    )


@pytest.mark.parametrize(
    "corruption", ["transformation", "source-type", "provenance-type", "json", "utf8"]
)
def test_rehashed_retained_record_cannot_replace_independent_admission(
    tmp_path: Path, corruption: str
) -> None:
    tasks, request, owner, _ = _managed_source(tmp_path)
    assert tasks.sharing is not None
    engine = tasks.sharing._engine
    preview = tasks.sharing.preview(request, authority=owner)
    with engine._store.connect() as connection:
        revision = dict(connection.execute("SELECT * FROM source_revisions").fetchone())
        independent = bytes(
            connection.execute("SELECT submission_json FROM source_intakes").fetchone()[0]
        )
    original = bytes(revision["source_bytes"])
    record = json.loads(original)
    if corruption == "transformation":
        record["provenance"]["transformation_receipts"] = ["synthetic-forged-transformation"]
    elif corruption == "source-type":
        record["source"] = "invalid-source-object"
    elif corruption == "provenance-type":
        record["provenance"] = "invalid-provenance-object"
    raw = (
        b"{"
        if corruption == "json"
        else b"\xff"
        if corruption == "utf8"
        else portable_canonical_json_bytes(record)
    )
    path = tasks.profile.root / revision["source_path"]
    before = _counts(engine)
    try:
        with _damage(
            engine,
            "source_revisions",
            {"source_bytes": raw, "source_sha256": sha256(raw).hexdigest()},
        ):
            path.write_bytes(raw)
            with pytest.raises(SharingError, match="binding_mismatch"):
                tasks.sharing.preview(
                    replace(request, operation_id="sharing.evidence.fresh"), authority=owner
                )
            assert _counts(engine) == before
            with engine._store.connect() as connection:
                assert (
                    bytes(
                        connection.execute("SELECT submission_json FROM source_intakes").fetchone()[
                            0
                        ]
                    )
                    == independent
                )
    finally:
        path.write_bytes(original)
    assert (
        tasks.sharing.decide(_decision(request, preview), authority=owner).copy_capture_id
        is not None
    )
