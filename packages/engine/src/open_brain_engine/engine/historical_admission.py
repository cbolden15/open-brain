"""Stored evidence checks for the future fenced owner reconciliation task."""

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import PrivacyDecision, PrivacyTier
from open_brain_engine.portable.v1 import validate_portable_write
from open_brain_engine.storage.filesystem import StorageError, read_confined

from .consent_contracts import ConsentContractError, ProviderConsentState
from .contracts import FilePayload, LocalEngineContext, ReferencePayload, TextPayload
from .historical_contracts import (
    HistoricalBaselineRequest,
    HistoricalCopyRelationRequest,
    HistoricalDestination,
    HistoricalSourceCAS,
    RetainedCaptureEvidence,
    _unique_object,
)
from .sharing_contracts import SharingError


@dataclass(frozen=True, slots=True)
class HistoricalConsentSnapshot:
    """Trusted runtime input, never a request field or Portable consent grant.

    The app loads the existing Brain-bound owner-only consent store. The engine
    independently checks its current provider/tier projection. A pending link
    must reload this snapshot before commit, not cache it as historical approval.
    """

    destination: HistoricalDestination
    state: ProviderConsentState


def require_historical_provider_consent(
    snapshot: HistoricalConsentSnapshot,
    request: HistoricalCopyRelationRequest,
) -> None:
    if (
        type(request) is not HistoricalCopyRelationRequest
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
    request: HistoricalCopyRelationRequest,
    baseline: HistoricalBaselineRequest,
) -> None:
    """Check current original/copy correspondence without making an approval.

    The private owner caller must first prove authentic old approval bytes and
    explicit provider coverage for approval_evidence_sha256. That attestation is
    distinct from the separately reloaded current provider-consent snapshot.
    """
    if (
        type(request) is not HistoricalCopyRelationRequest
        or type(baseline) is not HistoricalBaselineRequest
        or request.baseline_operation_id != baseline.operation_id
        or request.destination != baseline.destination
        or request.source_cas != baseline.source_cas
    ):
        raise SharingError("binding_mismatch")
    verify_historical_baseline_evidence(connection, profile, baseline)
    request.copy_source_cas.require_active()
    verify_historical_source_cas(connection, request.copy_source_cas)
    copy = verify_retained_capture(
        connection, profile, request.retained_copy, request.copy_source_cas.source_id
    )
    verify_historical_relation_payload(baseline, copy)


def verify_historical_relation_payload(
    baseline: HistoricalBaselineRequest, copy: dict[str, Any]
) -> None:
    """Immutable correspondence only; never current eligibility or consent."""
    original = baseline.observed_delivery.submission.capture
    privacy = PrivacyDecision.from_dict(copy["privacy"])
    if (
        original.privacy.tier is not PrivacyTier.PUBLIC
        or original.privacy.authority.cloud
        or original.privacy.authority.external_egress
        or privacy.tier is not PrivacyTier.PUBLIC
        or privacy.authority.cloud
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
    if len(text.encode("utf-8")) > 65536:
        raise SharingError("response_too_large")
    if has_redaction_finding(text):
        raise SharingError("unsupported_capability")


def verify_historical_baseline_evidence(
    connection: sqlite3.Connection, profile: LocalEngineContext, request: HistoricalBaselineRequest
) -> None:
    """Verify retained owner correspondence without relabeling either record.

    The observed upstream root/selection and original-file digest are private
    owner attestations, not canonical-root paths or executable publication grants.
    The private caller must verify its installed binding and original file before
    submitting this request. Engine admission separately holds owner authority,
    destination, generation, namespace uniqueness and the writer fence.
    """
    if type(request) is not HistoricalBaselineRequest:
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
    if row is None or row[0] not in (None, "owner"):
        raise SharingError("binding_mismatch")
    observed = request.observed_delivery
    capture = observed.submission.capture
    try:
        capture.validate_profile(profile)
    except ValueError:
        raise SharingError("binding_mismatch") from None
    verify_historical_baseline_payload(request, retained)
    namespace = sha256(observed.submission.namespace_bytes()).hexdigest()
    bound = connection.execute(
        "SELECT source_id FROM source_namespaces WHERE namespace_sha256=?", (namespace,)
    ).fetchone()
    if bound is not None and bound[0] != request.source_cas.source_id:
        raise SharingError("binding_mismatch")


def verify_historical_baseline_payload(
    request: HistoricalBaselineRequest, retained: dict[str, Any]
) -> None:
    """Shared live/Portable immutable payload and transform correspondence."""
    observed = request.observed_delivery
    capture = observed.submission.capture
    payload = capture.payload
    old_payload = retained["payload"]
    if isinstance(payload, TextPayload):
        transformed = payload.text.encode("utf-8")
        expected_payload = payload.to_dict()
    elif isinstance(payload, ReferencePayload):
        if payload.supplied_text is None or capture.source_reference != payload.url:
            raise SharingError("binding_mismatch")
        transformed = payload.supplied_text.encode("utf-8")
        expected_payload = (
            TextPayload(payload.supplied_text).to_dict()
            if old_payload.get("family") == "text"
            else payload.to_dict()
        )
    elif isinstance(payload, FilePayload):
        transformed = payload.data
        expected_payload = payload.to_dict()
    else:
        expected_payload = payload.to_dict()
        transformed = portable_canonical_json_bytes(expected_payload)
    if (
        old_payload != expected_payload
        or sha256(transformed).hexdigest() != observed.observation.transformed_sha256
    ):
        raise SharingError("binding_mismatch")


def verify_historical_source_cas(
    connection: sqlite3.Connection,
    witness: HistoricalSourceCAS,
) -> None:
    """Check all state fields; caller must hold admission and its writer fence.

    This check is eligibility-neutral so denial and revocation can protect inactive
    sources. Positive admission must separately require active availability.
    """
    if type(witness) is not HistoricalSourceCAS:
        raise SharingError("invalid_arguments")
    row = connection.execute(
        "SELECT s.head_capture_id,s.head_version,s.route_version,s.lifecycle,"
        "s.availability,s.historical_only,l.lifecycle_version,g.control_epoch "
        "FROM logical_sources s JOIN source_lifecycle_state l USING(source_id) "
        "CROSS JOIN engine_generations g WHERE s.source_id=? AND g.singleton=1",
        (witness.source_id,),
    ).fetchone()
    expected = (
        witness.expected_head,
        witness.expected_head_version,
        witness.expected_route_version,
        witness.expected_lifecycle,
        witness.expected_availability,
        int(witness.expected_historical_only),
        witness.expected_lifecycle_version,
        witness.expected_control_epoch,
    )
    if row is None or tuple(row) != expected:
        raise SharingError("revision_changed")


def verify_retained_capture(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    evidence: RetainedCaptureEvidence,
    capture_source_id: str,
) -> dict[str, Any]:
    """Check immutable retained evidence without capturing or rewriting it.

    The enclosing task must independently verify owner authority, destination,
    current source CAS and admission bounds under the writer fence.
    """
    if type(evidence) is not RetainedCaptureEvidence or not isinstance(profile, LocalEngineContext):
        raise SharingError("invalid_arguments")
    try:
        alias = connection.execute(
            "SELECT source_id,evidence_sha256 FROM source_aliases WHERE delivery_id=?",
            (evidence.retained_delivery_id,),
        ).fetchone()
        if alias is None or tuple(alias) != (capture_source_id, evidence.retained_request_sha256):
            raise SharingError("binding_mismatch")
        revisions = connection.execute(
            "SELECT r.*,c.stage,c.payload_json,c.privacy_json,c.source_path AS capture_source_path "
            "FROM source_revisions r JOIN captures c USING(capture_id) "
            "WHERE r.source_id=? AND r.request_sha256=?",
            (capture_source_id, evidence.retained_request_sha256),
        ).fetchall()
        if len(revisions) != 1:
            raise SharingError("binding_mismatch")
        row = revisions[0]
        if (
            row["capture_id"] != evidence.capture_id
            or row["stage"] != 3
            or row["source_sha256"] != evidence.source_sha256
            or row["source_path"] != row["capture_source_path"]
        ):
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
