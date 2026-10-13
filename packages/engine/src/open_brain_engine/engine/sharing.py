"""Owner decisions and current eligibility for separately captured managed copies."""
# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import timedelta
from enum import StrEnum
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.storage.filesystem import read_confined

from .consent_contracts import EgressMode
from .contracts import (
    CaptureReceipt,
    CaptureSubmission,
    JournalEnvelope,
    LocalEngineContext,
    PublicJobCaptureContext,
    ReferencePayload,
    TextPayload,
)
from .normalization import _timestamp
from .sharing_contracts import (
    SharingDecisionReceipt,
    SharingDecisionRequest,
    SharingError,
    SharingInspection,
    SharingInspectRequest,
    SharingPreview,
    SharingPreviewRequest,
    SharingRevokeReceipt,
    SharingRevokeRequest,
    parse_sharing_request,
)
from .source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionReceipt,
    SourceRevisionSubmission,
)
from .source_observation import SourceRevisionObservation
from .t03_contracts import EffectiveAuthority, T03Error

if TYPE_CHECKING:
    from .local import BrainEngine


MARKER_PREFIX = "urn:open-brain:sharing-copy:v1:"
NORMALIZATION_VERSIONS = ("saved-markdown-continuous.v1", "saved-markdown-continuous.v2")
SEMANTIC_PROVIDER_IDS = {
    "openai_api": "openai",
    "anthropic_api": "anthropic",
    "gemini_api": "gemini",
}
_MAX_RESPONSE_BYTES = 240_000


class EligibilityMode(StrEnum):
    OWNER_CURRENT = "owner-current"
    OWNER_HISTORY = "owner-history"
    LOCAL_CURRENT = "local-current"
    LOCAL_HISTORY = "local-history"
    EXTERNAL_READ = "external-read"
    SEMANTIC_RELEASE = "semantic-release"


def _canonical(value: object) -> bytes:
    return portable_canonical_json_bytes(value)


def _hash(domain: bytes, value: bytes) -> str:
    return sha256(domain + b"\0" + value).hexdigest()


def _json(raw: bytes | str) -> dict[str, Any]:
    value = json.loads(raw)
    if type(value) is not dict:
        raise SharingError("binding_mismatch")
    return value


def _owner_local(authority: EffectiveAuthority) -> None:
    if (
        not isinstance(authority, EffectiveAuthority)
        or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
    ):
        raise SharingError("unsupported_capability")


def _identity(connection: sqlite3.Connection, brain_id: str, issuer_epoch: int) -> None:
    row = connection.execute(
        "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
    ).fetchone()
    if row is None or (row["brain_id"], row["issuer_epoch"]) != (brain_id, issuer_epoch):
        raise SharingError("revision_changed")


def _job_context(connection: sqlite3.Connection, engine: BrainEngine) -> PublicJobCaptureContext:
    row = connection.execute("SELECT * FROM sharing_job_identity WHERE singleton=1").fetchone()
    if row is None:
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        if identity is None:
            raise SharingError("operation_pending")
        actor_id = "actor_" + str(uuid4())
        role_id = "role_" + str(uuid4())
        claim_id = "role_claim_" + str(uuid4())
        connection.execute(
            "INSERT INTO sharing_job_identity VALUES(1,?,?,?,?,?,?)",
            (
                engine.profile.tenant_id,
                actor_id,
                role_id,
                claim_id,
                identity["brain_id"],
                identity["issuer_epoch"],
            ),
        )
        row = connection.execute("SELECT * FROM sharing_job_identity WHERE singleton=1").fetchone()
    assert row is not None
    _identity(connection, row["brain_id"], row["issuer_epoch"])
    try:
        for field, prefix in (
            ("actor_id", "actor_"), ("role_id", "role_"), ("role_claim_id", "role_claim_"),
        ):
            value = row[field]
            identifier = UUID(value.removeprefix(prefix))
            if identifier.version != 4 or value != prefix + str(identifier):
                raise ValueError("invalid sharing job identity")
        context = PublicJobCaptureContext(
            tenant_id=row["tenant_id"],
            actor_id=row["actor_id"],
            role_claim={
                "tenant_id": row["tenant_id"],
                "actor_id": row["actor_id"],
                "role_id": row["role_id"],
                "role_claim_id": row["role_claim_id"],
                "capabilities": ["capture.accept"],
            },
        )
        context.validate_profile(engine.profile)
        identity = _job_identity(context, row["brain_id"], row["issuer_epoch"])
        for preview in connection.execute("SELECT preview_bytes FROM sharing_previews"):
            if _canonical(_json(bytes(preview["preview_bytes"]))["job_identity"]) != _canonical(identity):
                raise SharingError("binding_mismatch")
        return context
    except KeyError, AttributeError, TypeError, ValueError:
        raise SharingError("binding_mismatch") from None


def _job_identity(
    context: PublicJobCaptureContext, brain_id: str, issuer_epoch: int
) -> dict[str, Any]:
    return {
        "tenant_id": context.tenant_id,
        "actor_id": context.actor_id,
        "role_id": context.role_claim["role_id"],
        "role_claim_id": context.role_claim["role_claim_id"],
        "capabilities": ["capture.accept"],
        "brain_id": brain_id,
        "issuer_epoch": issuer_epoch,
    }


def _copy_privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="sharing-copy.v1",
        authority=Authority(cloud=False, external_egress=True),
    )


def _original_evidence(
    connection: sqlite3.Connection, engine: BrainEngine, request: SharingPreviewRequest
) -> dict[str, Any]:
    try:
        return _read_original_evidence(connection, engine, request)
    except SharingError:
        raise
    except AttributeError, KeyError, TypeError, ValueError, OSError:
        raise SharingError("binding_mismatch") from None


def _read_original_evidence(
    connection: sqlite3.Connection, engine: BrainEngine, request: SharingPreviewRequest
) -> dict[str, Any]:
    source = connection.execute(
        "SELECT s.*,l.lifecycle_version FROM logical_sources s "
        "JOIN source_lifecycle_state l USING(source_id) WHERE s.source_id=?",
        (request.source_id,),
    ).fetchone()
    if source is None:
        raise SharingError("not_found")
    if (
        source["head_capture_id"] != request.expected_head
        or source["head_version"] != request.expected_head_version
        or source["route_version"] != request.expected_route_version
        or source["lifecycle_version"] != request.expected_lifecycle_version
        or source["lifecycle"] != "active"
        or source["availability"] != "available"
        or source["historical_only"]
    ):
        raise SharingError("revision_changed")
    revision = connection.execute(
        "SELECT r.*,c.payload_json,c.privacy_json,c.provenance_json,c.source_reference,"
        "c.submission_path,c.source_origin,c.stage,c.action,c.actor_id,c.role_claim_json,"
        "c.delivery_id AS capture_delivery_id,c.request_sha256 AS capture_request_sha256 "
        "FROM source_revisions r JOIN captures c USING(capture_id) "
        "WHERE r.capture_id=? AND r.source_id=?",
        (request.expected_head, request.source_id),
    ).fetchone()
    if revision is None or revision["stage"] != 3 or revision["submission_path"] != "public_job":
        raise SharingError("binding_mismatch")
    managed = connection.execute(
        "SELECT * FROM managed_source_deliveries WHERE source_id=? AND receipt_json IS NOT NULL "
        "ORDER BY rowid DESC",
        (request.source_id,),
    ).fetchall()
    matched = None
    for candidate in managed:
        try:
            receipt = _json(candidate["receipt_json"])["source_receipt"]
            if receipt["capture_id"] == request.expected_head and receipt["outcome"] == "captured":
                matched = candidate
                break
        except KeyError, TypeError, ValueError:
            continue
    if matched is None:
        raise SharingError("binding_mismatch")
    envelope = _json(bytes(matched["envelope_bytes"]))
    if (
        envelope.get("dto_version") != 2
        or envelope.get("binding", {}).get("namespace", {}).get("connector_name")
        != "saved_markdown"
    ):
        raise SharingError("binding_mismatch")
    observation = envelope.get("observation")
    if (
        type(observation) is not dict
        or observation.get("normalization_version") not in NORMALIZATION_VERSIONS
    ):
        raise SharingError("binding_mismatch")
    if matched["envelope_sha256"] != sha256(bytes(matched["envelope_bytes"])).hexdigest():
        raise SharingError("binding_mismatch")
    if (matched["destination_brain_id"], matched["issuer_epoch"]) != (
        request.brain_id,
        request.issuer_epoch,
    ):
        raise SharingError("revision_changed")
    raw = read_confined(
        root=engine.profile.root,
        relative=revision["source_path"],
        expected_root_identity=engine.profile.root_identity,
    )
    if (
        raw is None
        or raw != bytes(revision["source_bytes"])
        or sha256(raw).hexdigest() != revision["source_sha256"]
    ):
        raise SharingError("binding_mismatch")
    record = _json(raw)
    try:
        SourceRevisionObservation.from_value(observation).validate_capture(record)
    except T03Error:
        raise SharingError("binding_mismatch") from None
    payload = _json(bytes(revision["payload_json"]))
    privacy = PrivacyDecision.from_dict(_json(revision["privacy_json"]))
    if (
        record.get("capture_id") != request.expected_head
        or record.get("payload") != payload
        or record.get("privacy") != privacy.to_dict()
        or privacy.tier is not PrivacyTier.PUBLIC
        or privacy.authority.cloud
        or privacy.authority.external_egress
        or privacy.policy_version != observation.get("privacy_policy_version")
        or sha256(_canonical(privacy.to_dict())).hexdigest()
        != observation.get("privacy_policy_sha256")
        or payload.get("kind") != "reference"
        or type(payload.get("supplied_text")) is not str
        or record.get("source", {}).get("origin") != "third_party"
        or revision["source_origin"] != "third_party"
        or record.get("source", {}).get("reference") != revision["source_reference"]
        or {
            key: record.get("provenance", {}).get(key)
            for key in ("source_ref", "content_origin", "owner_context")
        }
        != _json(revision["provenance_json"])
    ):
        raise SharingError("binding_mismatch")
    # Rebuild the complete supported envelope, then bind it to the independently
    # retained intake and accepted capture. A rehashed envelope is not admission.
    claimed = envelope["submission"]
    capture_value = claimed["capture"]
    context = PublicJobCaptureContext(
        tenant_id=capture_value["tenant_id"],
        actor_id=capture_value["actor_id"],
        role_claim=capture_value["role_claim"],
    )
    context.validate_profile(engine.profile)
    capture = CaptureSubmission.for_public_job(
        context=context,
        payload=ReferencePayload(
            capture_value["payload"]["url"], capture_value["payload"]["supplied_text"]
        ),
        delivery_id=claimed["delivery_id"],
        source_origin=capture_value["source_origin"],
        source_reference=capture_value["source_reference"],
        provenance=Provenance.from_dict(capture_value["provenance"]),
        privacy=PrivacyDecision.from_dict(capture_value["privacy"]),
        intent=capture_value["intent"],
        title=capture_value["title"],
    )
    submission = SourceRevisionSubmission(
        capture=capture,
        namespace=claimed["namespace"],
        revision_key=claimed["revision_key"],
        canonical_sha256=claimed["canonical_sha256"],
        expected_head=claimed["expected_head"],
        ordering=claimed["ordering"],
        expected_control_epoch=claimed["expected_control_epoch"],
        dto_version=claimed["dto_version"],
    )
    delivery = SourceRevisionObservedDelivery(
        binding=SourceRevisionBinding(**envelope["binding"]),
        submission=submission,
        expected_lifecycle_version=envelope["expected_lifecycle_version"],
        delivery_id=envelope["delivery_id"],
        observation=SourceRevisionObservation.from_value(observation),
        dto_version=envelope["dto_version"],
    )
    namespace_sha = sha256(submission.namespace_bytes()).hexdigest()
    intake = connection.execute(
        "SELECT * FROM source_intakes WHERE source_id=? AND namespace_sha256=? AND revision_key=?",
        (request.source_id, namespace_sha, submission.revision_key),
    ).fetchone()
    namespace = connection.execute(
        "SELECT * FROM source_namespaces WHERE source_id=?", (request.source_id,)
    ).fetchone()
    retained = _json(matched["receipt_json"])
    if set(retained) != {"source_receipt"} or intake is None or namespace is None:
        raise SharingError("binding_mismatch")
    managed_receipt = SourceRevisionReceipt(**retained["source_receipt"])
    intake_receipt = SourceRevisionReceipt(**_json(intake["receipt_json"]))
    plan = _json(intake["plan_json"])
    adoption = intake["delivery_id"].startswith("adoption.")
    expected_plan = {} if adoption else {
        "namespace_json": submission.namespace_bytes().decode("utf-8"),
        "ordering": dict(submission.ordering),
        "expected_head": submission.expected_head,
        "predecessor_capture_id": revision["predecessor_capture_id"],
        "promote": True,
        "control_epoch": submission.expected_control_epoch,
        "legacy_delivery_id": capture.delivery_id,
    }
    if (
        delivery.custody_bytes() != bytes(matched["envelope_bytes"])
        or submission.custody_bytes() != bytes(intake["submission_json"])
        or namespace["namespace_sha256"] != namespace_sha
        or namespace["namespace_json"].encode("utf-8") != submission.namespace_bytes()
        or matched["delivery_id"] != delivery.delivery_id
        or matched["source_delivery_id"] != capture.delivery_id
        or matched["expected_head"] != submission.expected_head
        or matched["expected_lifecycle_version"] != delivery.expected_lifecycle_version
        or delivery.expected_lifecycle_version > request.expected_lifecycle_version
        or (delivery.binding.destination_brain_id, delivery.binding.issuer_epoch)
        != (matched["destination_brain_id"], matched["issuer_epoch"])
        or managed_receipt != intake_receipt
        or managed_receipt.source_id != request.source_id
        or managed_receipt.capture_id != request.expected_head
        or managed_receipt.outcome != "captured"
        or type(managed_receipt.control_epoch) is not int
        or managed_receipt.control_epoch != submission.expected_control_epoch
        or managed_receipt.custody_id is not None
        or intake["request_sha256"] != capture.request_sha256()
        or revision["request_sha256"] != capture.request_sha256()
        or revision["capture_request_sha256"] != capture.request_sha256()
        or revision["capture_delivery_id"] != (capture.delivery_id if adoption else intake["delivery_id"])
        or _canonical(plan) != _canonical(expected_plan)
        or revision["revision_key"] != submission.revision_key
        or _json(revision["ordering_json"]) != dict(submission.ordering)
        or revision["action"] != "quick"
        or revision["actor_id"] != capture.actor_id
        or _json(revision["role_claim_json"]) != capture_value["role_claim"]
        or capture.request_value() != capture_value
        or record["provenance"] != dict(capture.provenance.to_dict(), transformation_receipts=[])
        or any(record[key] != capture_value[key] for key in (
            "schema_version", "payload", "privacy", "tenant_id", "actor_id", "role_claim",
            "intent", "capture_why",
        ))
    ):
        raise SharingError("binding_mismatch")
    text = payload["supplied_text"]
    if TextPayload(text).text != text or len(text.encode("utf-8")) > 65_536:
        raise SharingError("response_too_large")
    if has_redaction_finding(text):
        raise SharingError("unsupported_capability")
    return {
        "source": dict(source),
        "revision": dict(revision),
        "managed": dict(matched),
        "observation": observation,
        "text": text,
        "privacy": privacy.to_dict(),
    }


def _preview_from_row(row: sqlite3.Row) -> SharingPreview:
    if (
        _hash(b"open-brain-sharing-preview.v1", bytes(row["preview_bytes"]))
        != row["preview_sha256"]
    ):
        raise SharingError("binding_mismatch")
    value = _json(bytes(row["preview_bytes"]))
    return SharingPreview(
        preview_id=row["preview_id"],
        preview_sha256=row["preview_sha256"],
        source_id=value["source_id"],
        original_capture_id=value["original_capture_id"],
        text=value["text"],
        text_sha256=value["text_sha256"],
        expires_at=row["expires_at"],
        provider_ids=tuple(value["provider_ids"]),
        brain_id=value["brain_id"],
        issuer_epoch=value["issuer_epoch"],
    )


def _receipt(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["receipt_sha256"] = _hash(b"open-brain-sharing-receipt.v1", _canonical(result))
    if len(_canonical(result)) > _MAX_RESPONSE_BYTES:
        raise SharingError("response_too_large")
    return result


class SharingTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def recover_pending(self) -> int:
        """Resume only durable copy reservations after ordinary journal recovery."""
        connection = self._engine._store.connect()
        try:
            rows = connection.execute(
                "SELECT approval_id FROM sharing_decisions "
                "WHERE decision='approve' AND copy_capture_id IS NULL ORDER BY approval_id"
            ).fetchall()
        finally:
            connection.close()
        completed = 0
        for row in rows:
            receipt = self._complete_approval(row["approval_id"])
            completed += int(receipt.copy_capture_id is not None)
        return completed

    def preview(
        self, request: SharingPreviewRequest, *, authority: EffectiveAuthority
    ) -> SharingPreview:
        _owner_local(authority)
        if not isinstance(request, SharingPreviewRequest):
            raise SharingError("invalid_arguments")
        engine = self._engine
        with (
            engine._writer_lease.acquire_shared_writer(),
            engine._store.transaction() as connection,
        ):
            _identity(connection, request.brain_id, request.issuer_epoch)
            previous = connection.execute(
                "SELECT * FROM sharing_previews WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            request_bytes = _canonical(request.value())
            if previous is not None:
                if (
                    bytes(previous["request_bytes"]) != request_bytes
                    or previous["request_sha256"] != request.request_sha256
                ):
                    raise SharingError("invalid_arguments")
                return _preview_from_row(previous)
            evidence = _original_evidence(connection, engine, request)
            context = _job_context(connection, engine)
            preview_id = str(uuid4())
            now = engine._clock()
            expires_at = _timestamp(now + timedelta(minutes=15))
            marker = f"{MARKER_PREFIX}{preview_id}:{request.expected_head.removeprefix('capture_')}"
            text = evidence["text"]
            value = {
                "dto_version": 1,
                "preview_id": preview_id,
                "source_id": request.source_id,
                "original_capture_id": request.expected_head,
                "source_sha256": evidence["revision"]["source_sha256"],
                "payload_sha256": sha256(bytes(evidence["revision"]["payload_json"])).hexdigest(),
                "privacy_sha256": sha256(_canonical(evidence["privacy"])).hexdigest(),
                "provenance_sha256": sha256(
                    evidence["revision"]["provenance_json"].encode("utf-8")
                ).hexdigest(),
                "managed_delivery_id": evidence["managed"]["delivery_id"],
                "managed_envelope_sha256": evidence["managed"]["envelope_sha256"],
                "observation": evidence["observation"],
                "head_version": request.expected_head_version,
                "lifecycle_version": request.expected_lifecycle_version,
                "route_version": request.expected_route_version,
                "space_id": evidence["source"]["space_id"],
                "text": text,
                "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
                "marker": marker,
                "copy_privacy": _copy_privacy().to_dict(),
                "provider_ids": list(request.provider_ids),
                "brain_id": request.brain_id,
                "issuer_epoch": request.issuer_epoch,
                "job_identity": _job_identity(context, request.brain_id, request.issuer_epoch),
                "created_at": _timestamp(now),
                "expires_at": expires_at,
            }
            preview_bytes = _canonical(value)
            if len(preview_bytes) > _MAX_RESPONSE_BYTES:
                raise SharingError("response_too_large")
            digest = _hash(b"open-brain-sharing-preview.v1", preview_bytes)
            connection.execute(
                "INSERT INTO sharing_previews(preview_id,operation_id,request_bytes,request_sha256,"
                "preview_bytes,preview_sha256,source_id,original_capture_id,expires_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    preview_id,
                    request.operation_id,
                    request_bytes,
                    request.request_sha256,
                    preview_bytes,
                    digest,
                    request.source_id,
                    request.expected_head,
                    expires_at,
                ),
            )
            return _preview_from_row(
                connection.execute(
                    "SELECT * FROM sharing_previews WHERE preview_id=?", (preview_id,)
                ).fetchone()
            )

    def inspect(
        self, request: SharingInspectRequest, *, authority: EffectiveAuthority
    ) -> SharingInspection:
        _owner_local(authority)
        if not isinstance(request, SharingInspectRequest):
            raise SharingError("invalid_arguments")
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM sharing_previews WHERE preview_id=?", (request.subject_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT p.* FROM sharing_decisions d JOIN sharing_previews p USING(preview_id) "
                    "WHERE d.approval_id=?",
                    (request.subject_id,),
                ).fetchone()
            if row is None:
                raise SharingError("not_found")
            preview = _preview_from_row(row)
            if len(_canonical(asdict(preview))) > _MAX_RESPONSE_BYTES:
                raise SharingError("response_too_large")
            decision = connection.execute(
                "SELECT * FROM sharing_decisions WHERE preview_id=?", (preview.preview_id,)
            ).fetchone()
            revoked = (
                None
                if decision is None
                else connection.execute(
                    "SELECT * FROM sharing_revocations WHERE approval_id=?",
                    (decision["approval_id"],),
                ).fetchone()
            )
            result = SharingInspection(
                preview=preview,
                decision=None if decision is None else decision["decision"],
                approval_id=None if decision is None else decision["approval_id"],
                copy_capture_id=None if decision is None else decision["copy_capture_id"],
                revoked=revoked is not None,
                decision_receipt=None
                if decision is None
                else _json(bytes(decision["receipt_bytes"])),
                revoke_receipt=None if revoked is None else _json(bytes(revoked["receipt_bytes"])),
            )
            if len(_canonical(asdict(result))) > _MAX_RESPONSE_BYTES:
                raise SharingError("response_too_large")
            return result
        finally:
            connection.close()

    def decide(
        self, request: SharingDecisionRequest, *, authority: EffectiveAuthority
    ) -> SharingDecisionReceipt:
        _owner_local(authority)
        if not isinstance(request, SharingDecisionRequest):
            raise SharingError("invalid_arguments")
        engine = self._engine
        with (
            engine._writer_lease.acquire_shared_writer(),
            engine._store.transaction() as connection,
        ):
            _identity(connection, request.brain_id, request.issuer_epoch)
            previous = connection.execute(
                "SELECT * FROM sharing_decisions WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            request_bytes = _canonical(request.value())
            if previous is not None:
                if (
                    bytes(previous["request_bytes"]) != request_bytes
                    or previous["request_sha256"] != request.request_sha256
                ):
                    raise SharingError("invalid_arguments")
                approval_id = previous["approval_id"]
            else:
                row = connection.execute(
                    "SELECT * FROM sharing_previews WHERE preview_id=?", (request.preview_id,)
                ).fetchone()
                if row is None:
                    raise SharingError("not_found")
                if row["imported"]:
                    raise SharingError("preview_expired")
                if connection.execute(
                    "SELECT 1 FROM sharing_decisions WHERE preview_id=?", (request.preview_id,)
                ).fetchone():
                    raise SharingError("binding_mismatch")
                preview = _preview_from_row(row)
                if (
                    preview.preview_sha256 != request.preview_sha256
                    or request.destination_brain_id != preview.brain_id
                ):
                    raise SharingError("binding_mismatch")
                if _timestamp(engine._clock()) >= preview.expires_at:
                    raise SharingError("preview_expired")
                frozen = _json(bytes(row["preview_bytes"]))
                original_request = SharingPreviewRequest(**_json(bytes(row["request_bytes"])))
                evidence = _original_evidence(connection, engine, original_request)
                context = _job_context(connection, engine)
                if (
                    evidence["revision"]["source_sha256"] != frozen["source_sha256"]
                    or sha256(bytes(evidence["revision"]["payload_json"])).hexdigest()
                    != frozen["payload_sha256"]
                    or sha256(_canonical(evidence["privacy"])).hexdigest()
                    != frozen["privacy_sha256"]
                    or sha256(evidence["revision"]["provenance_json"].encode("utf-8")).hexdigest()
                    != frozen["provenance_sha256"]
                    or evidence["managed"]["envelope_sha256"] != frozen["managed_envelope_sha256"]
                    or evidence["source"]["space_id"] != frozen["space_id"]
                    or evidence["text"] != frozen["text"]
                    or evidence["observation"] != frozen["observation"]
                    or evidence["managed"]["delivery_id"] != frozen["managed_delivery_id"]
                    or _canonical(_job_identity(context, request.brain_id, request.issuer_epoch))
                    != _canonical(frozen["job_identity"])
                ):
                    raise SharingError("binding_mismatch")
                approval_id = str(uuid4())
                delivery_id: str | None = None
                envelope_bytes: bytes | None = None
                submission_sha: str | None = None
                if request.decision == "approve":
                    marker = frozen["marker"]
                    submission = CaptureSubmission.for_public_job(
                        context=context,
                        payload=TextPayload(preview.text),
                        delivery_id="sharing-copy.v1:placeholder",
                        source_origin=ContentOrigin.THIRD_PARTY,
                        source_reference=marker,
                        provenance=Provenance.create(
                            source_ref=marker,
                            content_origin=ContentOrigin.THIRD_PARTY,
                            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
                        ),
                        privacy=_copy_privacy(),
                    )
                    submission_sha = submission.request_sha256()
                    delivery_id = f"sharing-copy.v1:{approval_id}:{submission_sha}"
                    submission = CaptureSubmission.for_public_job(
                        context=context,
                        payload=TextPayload(preview.text),
                        delivery_id=delivery_id,
                        source_origin=ContentOrigin.THIRD_PARTY,
                        source_reference=marker,
                        provenance=submission.provenance,
                        privacy=submission.privacy,
                    )
                    envelope_bytes = JournalEnvelope(submission, submission.privacy).to_bytes()
                result = _receipt(
                    {
                        "dto_version": 1,
                        "brain_id": request.brain_id,
                        "issuer_epoch": request.issuer_epoch,
                        "operation_id": request.operation_id,
                        "request_sha256": request.request_sha256,
                        "preview_id": request.preview_id,
                        "approval_id": approval_id,
                        "decision": request.decision,
                        "state": "rejected" if request.decision == "reject" else "pending",
                        "approval_version": 1,
                        "destination_brain_id": request.destination_brain_id,
                        "copy_delivery_id": delivery_id,
                        "copy_capture_id": None,
                    }
                )
                if request.decision == "approve":
                    # Reserve the larger terminal response before accepting custody.
                    terminal = dict(
                        result,
                        state="history_only",
                        copy_capture_id="capture_00000000-0000-4000-8000-000000000000",
                    )
                    if len(_canonical(terminal)) > _MAX_RESPONSE_BYTES:
                        raise SharingError("response_too_large")
                connection.execute(
                    "INSERT INTO sharing_decisions VALUES(?,?,?,?,?, ?,1,?,?,?,?,?,?)",
                    (
                        approval_id,
                        request.operation_id,
                        request.preview_id,
                        request_bytes,
                        request.request_sha256,
                        request.decision,
                        request.destination_brain_id,
                        delivery_id,
                        envelope_bytes,
                        submission_sha,
                        None,
                        _canonical(result),
                    ),
                )
        if request.decision == "approve":
            return self._complete_approval(approval_id)
        return self._decision_receipt(approval_id)

    def _decision_receipt(self, approval_id: str) -> SharingDecisionReceipt:
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT receipt_bytes FROM sharing_decisions WHERE approval_id=?", (approval_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise SharingError("not_found")
        return SharingDecisionReceipt(**_json(bytes(row["receipt_bytes"])))

    def _complete_approval(self, approval_id: str) -> SharingDecisionReceipt:
        engine = self._engine
        connection = engine._store.connect()
        try:
            row = connection.execute(
                "SELECT * FROM sharing_decisions WHERE approval_id=?", (approval_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["decision"] != "approve":
            raise SharingError("not_found")
        if row["copy_capture_id"] is not None:
            return self._decision_receipt(approval_id)
        envelope = JournalEnvelope.from_bytes(bytes(row["copy_submission_bytes"]))
        if envelope.submission.request_sha256() != row["copy_submission_sha256"]:
            raise SharingError("binding_mismatch")
        try:
            outcome = engine.capture.submit(envelope.submission)
        except ValueError, RuntimeError:
            # The durable decision remains pending for exact foreground retry.
            return self._decision_receipt(approval_id)
        if not isinstance(outcome, CaptureReceipt):
            return self._decision_receipt(approval_id)
        with (
            engine._writer_lease.acquire_shared_writer(),
            engine._store.transaction() as connection,
        ):
            row = connection.execute(
                "SELECT * FROM sharing_decisions WHERE approval_id=?", (approval_id,)
            ).fetchone()
            if row is None:
                raise SharingError("operation_pending")
            if row["copy_capture_id"] is not None:
                return SharingDecisionReceipt(**_json(bytes(row["receipt_bytes"])))
            preview_row = connection.execute(
                "SELECT * FROM sharing_previews WHERE preview_id=?", (row["preview_id"],)
            ).fetchone()
            if preview_row is None:
                raise SharingError("binding_mismatch")
            frozen = _json(bytes(preview_row["preview_bytes"]))
            capture = connection.execute(
                "SELECT * FROM captures WHERE capture_id=? AND delivery_id=? AND stage=3",
                (outcome.capture_id, row["copy_delivery_id"]),
            ).fetchone()
            revision = connection.execute(
                "SELECT source_bytes,source_sha256 FROM source_revisions WHERE capture_id=?",
                (outcome.capture_id,),
            ).fetchone()
            copy_record = None if revision is None else _json(bytes(revision["source_bytes"]))
            if (
                capture is None
                or revision is None
                or capture["request_sha256"] != row["copy_submission_sha256"]
                or capture["source_reference"] != frozen["marker"]
                or capture["source_origin"] != "third_party"
                or capture["submission_path"] != "public_job"
                or capture["actor_id"] != envelope.submission.actor_id
                or _json(capture["role_claim_json"])
                != _json(_canonical(dict(envelope.submission.role_claim)))
                or _json(capture["provenance_json"]) != envelope.submission.provenance.to_dict()
                or _json(capture["privacy_json"]) != frozen["copy_privacy"]
                or bytes(capture["payload_json"])
                != _canonical(TextPayload(frozen["text"]).to_dict())
                or sha256(bytes(revision["source_bytes"])).hexdigest()
                != revision["source_sha256"]
                or copy_record is None
                or copy_record.get("payload") != TextPayload(frozen["text"]).to_dict()
                or copy_record.get("privacy") != frozen["copy_privacy"]
                or copy_record.get("source", {}).get("reference") != frozen["marker"]
            ):
                raise SharingError("binding_mismatch")
            connection.execute(
                "INSERT INTO sharing_links VALUES(?,?,?,?,?,?)",
                (
                    approval_id,
                    frozen["original_capture_id"],
                    outcome.capture_id,
                    frozen["marker"],
                    row["copy_submission_sha256"],
                    _timestamp(engine._clock()),
                ),
            )
            source = connection.execute(
                "SELECT s.*,l.lifecycle_version FROM logical_sources s "
                "JOIN source_lifecycle_state l "
                "USING(source_id) WHERE s.source_id=?",
                (frozen["source_id"],),
            ).fetchone()
            current = source is not None and (
                source["head_capture_id"] == frozen["original_capture_id"]
                and source["head_version"] == frozen["head_version"]
                and source["route_version"] == frozen["route_version"]
                and source["lifecycle_version"] == frozen["lifecycle_version"]
                and source["lifecycle"] == "active"
                and source["availability"] == "available"
                and connection.execute(
                    "SELECT 1 FROM sharing_revocations WHERE approval_id=?", (approval_id,)
                ).fetchone()
                is None
            )
            retained = _json(bytes(row["receipt_bytes"]))
            retained.pop("receipt_sha256")
            retained.update(
                state="captured" if current else "history_only", copy_capture_id=outcome.capture_id
            )
            result = _receipt(retained)
            connection.execute(
                "UPDATE sharing_decisions SET copy_capture_id=?,receipt_bytes=? "
                "WHERE approval_id=?",
                (outcome.capture_id, _canonical(result), approval_id),
            )
            return SharingDecisionReceipt(**result)

    def revoke(
        self, request: SharingRevokeRequest, *, authority: EffectiveAuthority
    ) -> SharingRevokeReceipt:
        _owner_local(authority)
        if not isinstance(request, SharingRevokeRequest):
            raise SharingError("invalid_arguments")
        engine = self._engine
        with (
            engine._writer_lease.acquire_shared_writer(),
            engine._store.transaction() as connection,
        ):
            _identity(connection, request.brain_id, request.issuer_epoch)
            previous = connection.execute(
                "SELECT * FROM sharing_revocations WHERE operation_id=?", (request.operation_id,)
            ).fetchone()
            request_bytes = _canonical(request.value())
            if previous is not None:
                if (
                    bytes(previous["request_bytes"]) != request_bytes
                    or previous["request_sha256"] != request.request_sha256
                ):
                    raise SharingError("invalid_arguments")
                return SharingRevokeReceipt(**_json(bytes(previous["receipt_bytes"])))
            approval = connection.execute(
                "SELECT * FROM sharing_decisions WHERE approval_id=?", (request.approval_id,)
            ).fetchone()
            if approval is None or approval["decision"] != "approve":
                raise SharingError("not_found")
            if approval["destination_brain_id"] != request.destination_brain_id:
                raise SharingError("binding_mismatch")
            if (
                approval["approval_version"] != request.expected_approval_version
                or connection.execute(
                    "SELECT 1 FROM sharing_revocations WHERE approval_id=?", (request.approval_id,)
                ).fetchone()
            ):
                raise SharingError("revision_changed")
            result = _receipt(
                {
                    "dto_version": 1,
                    "brain_id": request.brain_id,
                    "issuer_epoch": request.issuer_epoch,
                    "operation_id": request.operation_id,
                    "request_sha256": request.request_sha256,
                    "approval_id": request.approval_id,
                    "approval_version": request.expected_approval_version + 1,
                    "destination_brain_id": request.destination_brain_id,
                    "state": "revoked",
                }
            )
            connection.execute(
                "INSERT INTO sharing_revocations VALUES(?,?,?,?,?,?,?,?)",
                (
                    request.operation_id,
                    request.approval_id,
                    request_bytes,
                    request.request_sha256,
                    request.expected_approval_version + 1,
                    request.reason,
                    _timestamp(engine._clock()),
                    _canonical(result),
                ),
            )
            connection.execute(
                "UPDATE sharing_decisions SET approval_version=? WHERE approval_id=?",
                (
                    request.expected_approval_version + 1,
                    request.approval_id,
                ),
            )
            return SharingRevokeReceipt(**result)


def _managed_copy_identity(
    connection: sqlite3.Connection, capture_id: str, revision: sqlite3.Row
) -> bool:
    """Any independent custody signal keeps a damaged copy on managed policy."""
    if (revision["source_reference"] or "").startswith(MARKER_PREFIX) or (
        revision["delivery_id"] or ""
    ).startswith("sharing-copy.v1:"):
        return True
    if connection.execute(
        "SELECT 1 FROM sharing_links WHERE copy_capture_id=? "
        "UNION ALL SELECT 1 FROM sharing_decisions WHERE copy_capture_id=? LIMIT 1",
        (capture_id, capture_id),
    ).fetchone() is not None:
        return True
    try:
        retained = _json(bytes(revision["source_bytes"]))
        provenance = _json(revision["provenance_json"])
        return any(
            isinstance(value, str) and value.startswith(MARKER_PREFIX)
            for value in (
                retained.get("source", {}).get("reference"),
                retained.get("provenance", {}).get("source_ref"),
                provenance.get("source_ref"),
            )
        )
    except AttributeError, TypeError, ValueError, UnicodeError:
        # Malformed retained evidence is not permission to use ordinary policy.
        return True


def sharing_eligible(
    connection: sqlite3.Connection,
    capture_id: str,
    *,
    mode: EligibilityMode,
    provider_id: str | None = None,
    brain_id: str | None = None,
    issuer_epoch: int | None = None,
    profile: LocalEngineContext | None = None,
) -> bool:
    """One managed-source decision, reused before content-bearing projection."""
    revision = connection.execute(
        "SELECT r.source_id,r.source_bytes,r.source_sha256,c.source_reference,c.payload_json,"
        "c.privacy_json,c.provenance_json,c.role_claim_json,c.actor_id,c.source_origin,"
        "c.submission_path,c.request_sha256,c.stage,c.delivery_id "
        "FROM source_revisions r LEFT JOIN captures c USING(capture_id) WHERE r.capture_id=?",
        (capture_id,),
    ).fetchone()
    if revision is None:
        return False
    marker = revision["source_reference"] or ""
    managed_original = (
        connection.execute(
            "SELECT 1 FROM managed_source_deliveries WHERE source_id=? "
            "AND receipt_json IS NOT NULL "
            "AND json_extract(receipt_json,'$.source_receipt.capture_id')=? LIMIT 1",
            (revision["source_id"], capture_id),
        ).fetchone()
        is not None
    )
    if mode is EligibilityMode.OWNER_HISTORY:
        return True
    if mode is EligibilityMode.OWNER_CURRENT:
        source = connection.execute(
            "SELECT head_capture_id,lifecycle,availability FROM logical_sources WHERE source_id=?",
            (revision["source_id"],),
        ).fetchone()
        return (
            source is not None
            and source["head_capture_id"] == capture_id
            and source["lifecycle"] == "active"
            and source["availability"] == "available"
        )
    from .historical_visibility import historical_capture_visibility

    historical = historical_capture_visibility(
        connection,
        profile,
        capture_id,
        local_history=mode in (EligibilityMode.LOCAL_HISTORY, EligibilityMode.LOCAL_CURRENT),
        provider_id=provider_id,
        brain_id=brain_id,
        issuer_epoch=issuer_epoch,
    )
    if historical is not None:
        return historical
    managed_copy = _managed_copy_identity(connection, capture_id, revision)
    local_mode = mode in (EligibilityMode.LOCAL_HISTORY, EligibilityMode.LOCAL_CURRENT)
    if local_mode:
        source = connection.execute(
            "SELECT head_capture_id,lifecycle,availability FROM logical_sources WHERE source_id=?",
            (revision["source_id"],),
        ).fetchone()
        if (
            source is None
            or source["lifecycle"] != "active"
            or source["availability"] != "available"
        ):
            return False
        if managed_original:
            return bool(source["head_capture_id"] == capture_id)
        if mode is EligibilityMode.LOCAL_CURRENT and source["head_capture_id"] != capture_id:
            return False
        if not managed_copy:
            # A local history grant still permits older ordinary source revisions.
            return True
        identity = connection.execute(
            "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
        ).fetchone()
        if identity is None:
            return False
        brain_id, issuer_epoch = identity["brain_id"], identity["issuer_epoch"]
    if managed_original:
        return False
    if not managed_copy:
        return True
    if (
        not local_mode
        and provider_id is None
        or brain_id is None
        or issuer_epoch is None
    ):
        return False
    row = connection.execute(
        "SELECT l.*,d.*,p.preview_bytes,p.preview_sha256,p.source_id AS original_source_id "
        "FROM sharing_links l JOIN sharing_decisions d USING(approval_id) "
        "JOIN sharing_previews p USING(preview_id) WHERE l.copy_capture_id=?",
        (capture_id,),
    ).fetchone()
    if row is None or row["decision"] != "approve" or row["copy_capture_id"] != capture_id:
        return False
    if connection.execute(
        "SELECT 1 FROM sharing_revocations WHERE approval_id=?", (row["approval_id"],)
    ).fetchone():
        return False
    try:
        frozen = _json(bytes(row["preview_bytes"]))
        decision_request = parse_sharing_request(
            bytes(row["request_bytes"]), SharingDecisionRequest
        )
        receipt = _json(bytes(row["receipt_bytes"]))
        receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
        privacy = PrivacyDecision.from_dict(_json(revision["privacy_json"]))
        envelope = JournalEnvelope.from_bytes(bytes(row["copy_submission_bytes"]))
        job = connection.execute(
            "SELECT * FROM sharing_job_identity WHERE singleton=1"
        ).fetchone()
        original = connection.execute(
            "SELECT r.source_bytes,r.source_sha256,c.payload_json,c.privacy_json "
            "FROM source_revisions r JOIN captures c USING(capture_id) "
            "WHERE r.capture_id=? AND r.source_id=?",
            (frozen["original_capture_id"], row["original_source_id"]),
        ).fetchone()
        if (
            _canonical(decision_request.value()) != bytes(row["request_bytes"])
            or decision_request.request_sha256 != row["request_sha256"]
            or decision_request.operation_id != row["operation_id"]
            or decision_request.preview_id != row["preview_id"]
            or decision_request.preview_sha256 != row["preview_sha256"]
            or decision_request.decision != "approve"
            or decision_request.destination_brain_id != brain_id
            or (decision_request.brain_id, decision_request.issuer_epoch)
            != (brain_id, issuer_epoch)
            or row["approval_version"] != 1
            or set(receipt) != {
                "dto_version", "brain_id", "issuer_epoch",
                "operation_id", "request_sha256", "preview_id", "approval_id",
                "decision", "state", "approval_version", "destination_brain_id",
                "copy_delivery_id", "copy_capture_id", "receipt_sha256",
            }
            or receipt["receipt_sha256"]
            != _hash(b"open-brain-sharing-receipt.v1", _canonical(receipt_body))
            or receipt["operation_id"] != row["operation_id"]
            or receipt["request_sha256"] != row["request_sha256"]
            or type(receipt["dto_version"]) is not int
            or receipt["dto_version"] != 1
            or receipt["brain_id"] != brain_id
            or type(receipt["issuer_epoch"]) is not int
            or receipt["issuer_epoch"] != issuer_epoch
            or receipt["preview_id"] != row["preview_id"]
            or receipt["approval_id"] != row["approval_id"]
            or receipt["decision"] != "approve"
            or receipt["state"] != "captured"
            or receipt["approval_version"] != 1
            or receipt["destination_brain_id"] != brain_id
            or receipt["copy_delivery_id"] != row["copy_delivery_id"]
            or receipt["copy_capture_id"] != capture_id
            or _hash(b"open-brain-sharing-preview.v1", bytes(row["preview_bytes"]))
            != row["preview_sha256"]
            or not local_mode
            and provider_id not in frozen["provider_ids"]
            or (brain_id, issuer_epoch) != (frozen["brain_id"], frozen["issuer_epoch"])
            or marker != frozen["marker"]
            or marker != row["marker"]
            or row["original_capture_id"] != frozen["original_capture_id"]
            or row["submission_sha256"] != revision["request_sha256"]
            or row["copy_submission_sha256"] != revision["request_sha256"]
            or row["copy_delivery_id"] != revision["delivery_id"]
            or revision["stage"] != 3
            or revision["source_origin"] != "third_party"
            or revision["submission_path"] != "public_job"
            or revision["actor_id"] != envelope.submission.actor_id
            or _json(revision["role_claim_json"])
            != _json(_canonical(dict(envelope.submission.role_claim)))
            or _json(revision["provenance_json"]) != envelope.submission.provenance.to_dict()
            or sha256(bytes(revision["source_bytes"])).hexdigest() != revision["source_sha256"]
            or _json(bytes(revision["source_bytes"])).get("payload")
            != TextPayload(frozen["text"]).to_dict()
            or _json(bytes(revision["source_bytes"])).get("privacy") != privacy.to_dict()
            or _json(bytes(revision["source_bytes"])).get("source", {}).get("reference")
            != marker
            or envelope.submission.request_sha256() != row["copy_submission_sha256"]
            or envelope.submission.delivery_id != row["copy_delivery_id"]
            or envelope.submission.source_reference != marker
            or envelope.submission.payload.to_dict() != TextPayload(frozen["text"]).to_dict()
            or envelope.submission.privacy.to_dict() != privacy.to_dict()
            or job is None
            or envelope.submission.actor_id != job["actor_id"]
            or envelope.submission.role_claim["role_id"] != job["role_id"]
            or envelope.submission.role_claim["role_claim_id"] != job["role_claim_id"]
            or original is None
            or sha256(bytes(original["source_bytes"])).hexdigest() != original["source_sha256"]
            or original["source_sha256"] != frozen["source_sha256"]
            or sha256(bytes(original["payload_json"])).hexdigest() != frozen["payload_sha256"]
            or sha256(_canonical(_json(original["privacy_json"]))).hexdigest()
            != frozen["privacy_sha256"]
            or _json(original["privacy_json"]).get("authority")
            != {"cloud": False, "external_egress": False}
            or privacy.to_dict() != frozen["copy_privacy"]
            or bytes(revision["payload_json"]) != _canonical(TextPayload(frozen["text"]).to_dict())
            or row["destination_brain_id"] != brain_id
        ):
            return False
        source = connection.execute(
            "SELECT s.*,l.lifecycle_version FROM logical_sources s JOIN source_lifecycle_state l "
            "USING(source_id) WHERE s.source_id=?",
            (row["original_source_id"],),
        ).fetchone()
        return source is not None and (
            source["head_capture_id"] == frozen["original_capture_id"]
            and source["head_version"] == frozen["head_version"]
            and source["route_version"] == frozen["route_version"]
            and source["lifecycle_version"] == frozen["lifecycle_version"]
            and source["space_id"] == frozen["space_id"]
            and source["lifecycle"] == "active"
            and source["availability"] == "available"
        )
    except KeyError, TypeError, ValueError, UnicodeError:
        return False


def require_capture_eligibility(
    connection: sqlite3.Connection,
    capture_id: str,
    authority: EffectiveAuthority,
    *,
    history: bool = False,
    profile: LocalEngineContext | None = None,
) -> None:
    if authority.egress_mode is EgressMode.EXTERNAL_PROVIDER:
        mode = EligibilityMode.EXTERNAL_READ
    elif authority.owner:
        mode = EligibilityMode.OWNER_HISTORY if history else EligibilityMode.OWNER_CURRENT
    else:
        mode = EligibilityMode.LOCAL_HISTORY if history else EligibilityMode.LOCAL_CURRENT
    if not sharing_eligible(
        connection,
        capture_id,
        mode=mode,
        provider_id=authority.provider_id,
        brain_id=authority.brain_id,
        issuer_epoch=authority.issuer_epoch,
        profile=profile,
    ):
        from .t03_contracts import T03Error

        raise T03Error("not_found")


def external_candidate_clause(
    alias: str, authority: EffectiveAuthority
) -> tuple[str, tuple[object, ...]]:
    """Filter managed source candidates before FTS ranking or cursor digests."""
    if authority.egress_mode is not EgressMode.EXTERNAL_PROVIDER:
        return "1", ()
    if (
        authority.provider_id is None
        or authority.brain_id is None
        or authority.issuer_epoch is None
    ):
        return "0", ()
    if alias not in {"d", "a", "m"}:
        raise ValueError("invalid search alias")
    capture = f"{alias}.capture_id"
    clause = f"""
    NOT EXISTS (
      SELECT 1 FROM source_revisions original
      JOIN managed_source_deliveries managed ON managed.source_id=original.source_id
      WHERE original.capture_id={capture} AND managed.receipt_json IS NOT NULL
        AND json_extract(managed.receipt_json,'$.source_receipt.capture_id')={capture}
    )
    AND (
      NOT EXISTS (SELECT 1 FROM captures marker_capture WHERE marker_capture.capture_id={capture}
        AND marker_capture.source_reference LIKE 'urn:open-brain:sharing-copy:v1:%')
      OR EXISTS (
        SELECT 1 FROM sharing_links link
        JOIN sharing_decisions decision ON decision.approval_id=link.approval_id
        JOIN sharing_previews preview ON preview.preview_id=decision.preview_id
        JOIN logical_sources original_source ON original_source.source_id=preview.source_id
        JOIN source_lifecycle_state lifecycle ON lifecycle.source_id=original_source.source_id
        WHERE link.copy_capture_id={capture} AND decision.copy_capture_id={capture}
          AND decision.decision='approve' AND decision.approval_version=1
          AND NOT EXISTS (SELECT 1 FROM sharing_revocations revocation
            WHERE revocation.approval_id=decision.approval_id)
          AND original_source.head_capture_id=preview.original_capture_id
          AND original_source.head_version=json_extract(CAST(preview.preview_bytes AS TEXT),'$.head_version')
          AND original_source.route_version=json_extract(CAST(preview.preview_bytes AS TEXT),'$.route_version')
          AND lifecycle.lifecycle_version=json_extract(CAST(preview.preview_bytes AS TEXT),'$.lifecycle_version')
          AND original_source.space_id IS json_extract(CAST(preview.preview_bytes AS TEXT),'$.space_id')
          AND original_source.lifecycle='active' AND original_source.availability='available'
          AND preview.preview_sha256 IS NOT NULL
          AND decision.destination_brain_id=?
          AND json_extract(CAST(preview.preview_bytes AS TEXT),'$.brain_id')=?
          AND json_extract(CAST(preview.preview_bytes AS TEXT),'$.issuer_epoch')=?
          AND EXISTS (SELECT 1 FROM json_each(CAST(preview.preview_bytes AS TEXT),'$.provider_ids')
            WHERE value=?)
      )
    )
    """
    return clause, (
        authority.brain_id,
        authority.brain_id,
        authority.issuer_epoch,
        authority.provider_id,
    )


def external_canonical_clause(
    alias: str, authority: EffectiveAuthority
) -> tuple[str, tuple[object, ...]]:
    if authority.egress_mode is not EgressMode.EXTERNAL_PROVIDER:
        return "1", ()
    if alias not in {"d", "a"}:
        raise ValueError("invalid canonical search alias")
    # The snapshot callback uses RecordProjector's exact current-publication
    # resolver, including confined retained-byte matching for legacy imports.
    current_members = (
        f"m.page_id={alias}.result_id AND "
        f"m.publication_id=sharing_current_publication({alias}.result_id)"
    )
    return (
        f"({alias}.record_type!='canonical' OR (EXISTS ("
        "SELECT 1 FROM canonical_revision_members m "
        f"WHERE {current_members}) AND NOT EXISTS ("
        "SELECT 1 FROM canonical_revision_members m "
        f"WHERE {current_members} AND sharing_visible(m.capture_id)=0)))",
        (),
    )


def validate_reserved_copy_submission(engine: BrainEngine, submission: CaptureSubmission) -> bool:
    """A marker or delivery prefix cannot enter the journal without matching custody."""
    marked = submission.source_reference.startswith(MARKER_PREFIX)
    reserved = submission.delivery_id.startswith("sharing-copy.v1:")
    if not marked and not reserved:
        return False
    if not marked or not reserved:
        raise ValueError("invalid reserved sharing copy")
    connection = engine._store.connect()
    try:
        row = connection.execute(
            "SELECT d.copy_submission_bytes,d.copy_submission_sha256,p.preview_bytes "
            "FROM sharing_decisions d JOIN sharing_previews p USING(preview_id) "
            "WHERE d.copy_delivery_id=? AND d.decision='approve'",
            (submission.delivery_id,),
        ).fetchone()
    finally:
        connection.close()
    if row is None or (
        row["copy_submission_sha256"] != submission.request_sha256()
        or _json(bytes(row["preview_bytes"]))["marker"] != submission.source_reference
        or JournalEnvelope.from_bytes(bytes(row["copy_submission_bytes"])).submission != submission
    ):
        raise ValueError("invalid reserved sharing copy")
    return True


__all__ = [
    "EligibilityMode",
    "MARKER_PREFIX",
    "SEMANTIC_PROVIDER_IDS",
    "SharingTasks",
    "require_capture_eligibility",
    "sharing_eligible",
    "external_candidate_clause",
    "external_canonical_clause",
    "validate_reserved_copy_submission",
]
