"""Stored evidence checks for the future fenced owner reconciliation task."""

import json
import sqlite3
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, PrivacyTier
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.storage.filesystem import StorageError, read_confined

from .consent_contracts import ConsentContractError, ProviderConsentState
from .contracts import LocalEngineContext, ReferencePayload, TextPayload
from .historical_admission import HistoricalConsentSnapshot, verify_historical_source_cas
from .historical_admission import verify_historical_baseline_payload as _v1_baseline_payload
from .historical_contracts import HistoricalBaselineRequest, _unique_object
from .historical_contracts_v2 import (
    HistoricalBaselineRequestV2,
    HistoricalCopyRelationRequestV2,
    RetainedCaptureEvidenceV2,
)
from .sharing_contracts import SharingError


def require_historical_provider_consent(
    snapshot: HistoricalConsentSnapshot,
    request: HistoricalCopyRelationRequestV2,
) -> None:
    if (
        type(request) is not HistoricalCopyRelationRequestV2
        or type(snapshot) is not HistoricalConsentSnapshot
        or type(snapshot.state) is not ProviderConsentState
        or snapshot.destination != request.destination
    ):
        raise SharingError("binding_mismatch")
    try:
        for provider in request.provider_ids:
            active = tuple(
                item
                for item in snapshot.state.records
                if item.provider_id == provider and item.active
            )
            if len(active) != 1:
                raise ConsentContractError("consent_unavailable")
            consent = snapshot.state.active_consent(
                consent_id=active[0].consent_id,
                provider_id=provider,
                authorization_generation=snapshot.state.authorization_generation,
            )
            if PrivacyTier.PUBLIC not in consent.allowed_tiers:
                raise ConsentContractError("consent_unavailable")
    except ConsentContractError:
        raise SharingError("unsupported_capability") from None


def verify_historical_relation_evidence(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    request: HistoricalCopyRelationRequestV2,
    baseline: HistoricalBaselineRequest | HistoricalBaselineRequestV2,
) -> None:
    """Check current original/copy correspondence without making an approval.

    The private owner caller must first prove authentic old approval bytes and
    explicit provider coverage for approval_evidence_sha256. That attestation is
    distinct from the separately reloaded current provider-consent snapshot.
    """
    if (
        type(request) is not HistoricalCopyRelationRequestV2
        or type(baseline) not in (HistoricalBaselineRequest, HistoricalBaselineRequestV2)
        or request.baseline_operation_id != baseline.operation_id
        or request.destination != baseline.destination
        or request.source_cas != baseline.source_cas
    ):
        raise SharingError("binding_mismatch")
    if isinstance(baseline, HistoricalBaselineRequest):
        from .historical_admission import verify_historical_baseline_evidence as verify_v1

        verify_v1(connection, profile, baseline)
    else:
        verify_historical_baseline_evidence(connection, profile, baseline)
    request.copy_source_cas.require_active()
    verify_historical_source_cas(connection, request.copy_source_cas)
    copy = verify_retained_capture(
        connection, profile, request.retained_copy, request.copy_source_cas.source_id
    )
    path = connection.execute(
        "SELECT submission_path FROM captures WHERE capture_id=?",
        (request.retained_copy.capture_id,),
    ).fetchone()
    if path is None or path[0] != "public_job":
        raise SharingError("binding_mismatch")
    verify_historical_relation_payload(baseline, copy)


def verify_historical_relation_payload(
    baseline: HistoricalBaselineRequest | HistoricalBaselineRequestV2, copy: dict[str, Any]
) -> None:
    """Immutable correspondence only; never current eligibility or consent."""
    original = baseline.observed_delivery.submission.capture
    privacy = PrivacyDecision.from_dict(copy["privacy"])
    if (
        original.privacy.tier is not PrivacyTier.PUBLIC
        or original.privacy.authority.cloud
        or original.privacy.authority.external_egress
        or privacy.tier is not PrivacyTier.PUBLIC
        or not privacy.authority.external_egress
    ):
        raise SharingError("unsupported_capability")
    payload = original.payload
    if isinstance(payload, TextPayload):
        text = payload.text
        expected = payload.to_dict()
    elif isinstance(payload, ReferencePayload) and payload.supplied_text is not None:
        text = payload.supplied_text
        expected = (
            TextPayload(text).to_dict()
            if copy["payload"].get("family") == "text"
            else payload.to_dict()
        )
    else:
        raise SharingError("unsupported_capability")
    if copy["payload"] != expected:
        raise SharingError("binding_mismatch")
    if len(text) > 65536:
        raise SharingError("response_too_large")
    if has_redaction_finding(text):
        raise SharingError("unsupported_capability")


def verify_historical_baseline_evidence(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    request: HistoricalBaselineRequestV2,
) -> None:
    """Verify retained owner correspondence without relabeling either record.

    The observed upstream root/selection and original-file digest are private
    owner attestations, not canonical-root paths or executable publication grants.
    The private caller must verify its installed binding and original file before
    submitting this request. Engine admission separately holds owner authority,
    destination, generation, namespace uniqueness and the writer fence.
    """
    if type(request) is not HistoricalBaselineRequestV2:
        raise SharingError("invalid_arguments")
    request.source_cas.require_active()
    verify_historical_source_cas(connection, request.source_cas)
    retained = verify_retained_capture(
        connection, profile, request.retained_original, request.source_cas.source_id
    )
    row = connection.execute(
        "SELECT submission_path FROM captures WHERE capture_id=?",
        (request.retained_original.capture_id,),
    ).fetchone()
    if row is None or row[0] not in (None, "owner", "import"):
        raise SharingError("binding_mismatch")
    observed = request.observed_delivery
    capture = observed.submission.capture
    try:
        capture.validate_profile(profile)
    except ValueError:
        raise SharingError("binding_mismatch") from None
    _v1_baseline_payload(cast(Any, request), retained)
    namespace = sha256(observed.submission.namespace_bytes()).hexdigest()
    bound = connection.execute(
        "SELECT source_id FROM source_namespaces WHERE namespace_sha256=?", (namespace,)
    ).fetchone()
    if bound is not None and bound[0] != request.source_cas.source_id:
        raise SharingError("binding_mismatch")


def verify_retained_capture(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    evidence: RetainedCaptureEvidenceV2,
    capture_source_id: str,
) -> dict[str, Any]:
    """Check immutable retained evidence without capturing or rewriting it.

    The enclosing task must independently verify owner authority, destination,
    current source CAS and admission bounds under the writer fence.
    """
    if type(evidence) is not RetainedCaptureEvidenceV2 or not isinstance(
        profile, LocalEngineContext
    ):
        raise SharingError("invalid_arguments")
    try:
        if evidence.correspondence_scheme == "portable_import_projection_v1":
            revision = connection.execute(
                "SELECT * FROM source_revisions WHERE capture_id=?", (evidence.capture_id,)
            ).fetchone()
            capture = connection.execute(
                "SELECT * FROM captures WHERE capture_id=?", (evidence.capture_id,)
            ).fetchone()
            if revision is None or capture is None:
                raise SharingError("binding_mismatch")
            raw = read_confined(
                root=profile.root,
                relative=revision["source_path"],
                expected_root_identity=profile.root_identity,
                maximum_bytes=8 * 1024 * 1024,
            )
            if raw is None or raw != bytes(revision["source_bytes"]):
                raise SharingError("binding_mismatch")
            alias = connection.execute(
                "SELECT 1 FROM source_aliases WHERE delivery_id=?", (evidence.retained_delivery_id,)
            ).fetchone()
            return verify_portable_import_projection(
                evidence,
                dict(capture),
                dict(revision),
                alias is not None,
                raw,
                profile.tenant_id,
                capture_source_id,
            )
        alias = connection.execute(
            "SELECT source_id,evidence_sha256 FROM source_aliases WHERE delivery_id=?",
            (evidence.retained_delivery_id,),
        ).fetchone()
        if alias is None or tuple(alias) != (capture_source_id, evidence.alias_evidence_sha256):
            raise SharingError("binding_mismatch")
        revisions = connection.execute(
            "SELECT r.*,c.stage,c.payload_json,c.privacy_json,"
            "c.source_path AS capture_source_path, "
            "c.request_sha256 AS capture_request_sha256 "
            "FROM source_revisions r JOIN captures c USING(capture_id) "
            "WHERE r.source_id=? AND r.request_sha256=?",
            (capture_source_id, evidence.revision_request_sha256),
        ).fetchall()
        if len(revisions) != 1:
            raise SharingError("binding_mismatch")
        row = revisions[0]
        if (
            row["capture_id"] != evidence.capture_id
            or row["capture_request_sha256"] != evidence.capture_request_sha256
            or row["stage"] != 3
            or row["source_sha256"] != evidence.source_sha256
            or row["source_path"] != row["capture_source_path"]
        ):
            raise SharingError("binding_mismatch")
        if evidence.correspondence_scheme == "direct_revision_alias":
            correspondence = evidence.alias_evidence_sha256 == evidence.revision_request_sha256
        else:
            correspondence = (
                evidence.capture_request_sha256 == evidence.revision_request_sha256
                and evidence.alias_evidence_sha256
                == sha256(
                    portable_canonical_json_bytes(evidence.revision_request_sha256)
                ).hexdigest()
            )
        if not correspondence:
            raise SharingError("binding_mismatch")
        raw = read_confined(
            root=profile.root,
            relative=row["source_path"],
            expected_root_identity=profile.root_identity,
            maximum_bytes=8 * 1024 * 1024,
        )
        if (
            raw is None
            or raw != bytes(row["source_bytes"])
            or sha256(raw).hexdigest() != evidence.source_sha256
        ):
            raise SharingError("binding_mismatch")
        validate_portable_write(row["source_path"], raw, profile.tenant_id)
        record = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
        privacy = PrivacyDecision.from_dict(
            json.loads(row["privacy_json"], object_pairs_hook=_unique_object)
        ).to_dict()
        payload = json.loads(row["payload_json"], object_pairs_hook=_unique_object)
        if (
            record["capture_id"] != evidence.capture_id
            or record["payload"] != payload
            or record["privacy"] != privacy
            or sha256(portable_canonical_json_bytes(privacy)).hexdigest() != evidence.privacy_sha256
        ):
            raise SharingError("binding_mismatch")
        return cast(dict[str, Any], record)
    except KeyError, TypeError, ValueError, UnicodeError, StorageError:
        raise SharingError("binding_mismatch") from None


def verify_portable_import_projection(
    evidence: RetainedCaptureEvidenceV2,
    capture: dict[str, Any],
    revision: dict[str, Any],
    alias_present: bool,
    raw: bytes,
    tenant_id: str,
    source_id: str,
) -> dict[str, Any]:
    """Prove supported canonical import projection, never sender transport evidence."""
    try:
        if type(evidence) is not RetainedCaptureEvidenceV2 or (
            evidence.correspondence_scheme != "portable_import_projection_v1"
            or evidence.revision_request_sha256 is not None
            or evidence.alias_evidence_sha256 is not None
            or alias_present
            or revision["request_sha256"] is not None
            or revision["revision_key"] is not None
            or revision["ordering_json"] is not None
            or revision["source_id"] != source_id
            or revision["capture_id"] != evidence.capture_id
            or capture["capture_id"] != evidence.capture_id
            or capture["stage"] != 3
            or capture["submission_path"] != "import"
            or capture["delivery_id"] != evidence.retained_delivery_id
            or evidence.retained_delivery_id
            != "import.capture." + sha256(evidence.capture_id.encode()).hexdigest()
            or capture["request_sha256"] != evidence.capture_request_sha256
            or evidence.capture_request_sha256 != evidence.source_sha256
            or revision["source_sha256"] != evidence.source_sha256
            or sha256(raw).hexdigest() != evidence.source_sha256
            or capture["source_path"] != revision["source_path"]
        ):
            raise SharingError("binding_mismatch")
        validate_portable_write(revision["source_path"], raw, tenant_id)
        record = json.loads(raw.decode("utf-8", "strict"), object_pairs_hook=_unique_object)
        if (
            portable_canonical_json_bytes(record) != raw
            or record["capture_id"] != evidence.capture_id
        ):
            raise SharingError("binding_mismatch")
        if (
            record["tenant_id"] != tenant_id
            or record["payload"].get("family") != "text"
            or capture["payload_family"] != "text"
            or capture["file_bytes"] is not None
            or capture["payload_json"] != portable_canonical_json_bytes(record["payload"])
            or capture["search_text"] != record["payload"]["text"]
            or capture["privacy_json"] != portable_canonical_json_bytes(record["privacy"]).decode()
            or sha256(portable_canonical_json_bytes(record["privacy"])).hexdigest()
            != evidence.privacy_sha256
        ):
            raise SharingError("binding_mismatch")
        for column, field in (("provenance_json", "provenance"), ("role_claim_json", "role_claim")):
            if capture[column] != portable_canonical_json_bytes(record[field]).decode():
                raise SharingError("binding_mismatch")
        for field in ("actor_id", "intent", "capture_why", "accepted_at"):
            if capture[field] != record[field]:
                raise SharingError("binding_mismatch")
        if (
            capture["source_origin"] != record["source"]["origin"]
            or capture["source_reference"] != record["source"]["reference"]
        ):
            raise SharingError("binding_mismatch")
        accepted = [item for item in record["receipt_refs"] if item["kind"] == "capture_accepted"]
        if (
            len(accepted) != 1
            or accepted[0]["receipt_id"] != evidence.accepted_receipt_id
            or (capture["accepted_receipt_id"] != evidence.accepted_receipt_id)
        ):
            raise SharingError("binding_mismatch")
        return cast(dict[str, Any], record)
    except KeyError, TypeError, ValueError, UnicodeError:
        raise SharingError("binding_mismatch") from None
