"""Owner-exclusive reconciliation over existing retained capture evidence."""

from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.storage.locks import FileLease

from .contracts import LocalEngineContext
from .historical_admission import HistoricalConsentSnapshot, verify_historical_source_cas
from .historical_contracts import HistoricalDestination
from .historical_dispatch import (
    BASELINE_TYPES,
    CLAIM_TYPES,
    RELATION_TYPES,
    REVOCATION_TYPES,
    BaselineRequest,
    ClaimRequest,
    Receipt,
    RelationRequest,
    RevocationRequest,
    VersionedHistoricalTransitionStore,
    create_historical_transition,
    decode_receipt,
    require_historical_provider_consent,
    verify_historical_baseline_evidence,
    verify_historical_relation_evidence,
    verify_retained_capture,
)
from .historical_fence import HistoricalPendingFence
from .historical_projection import (
    historical_projection_rows,
    verify_versioned_historical_projection,
)
from .historical_recovery import _historical_transaction, _recover_historical
from .historical_registry import (
    HistoricalClaimMembership,
    HistoricalClaimRegistry,
    HistoricalRegistryStore,
)
from .local_schema import open_local_database_read_only
from .normalization import _utc_now
from .runtime_admission import HeldRuntimeAdmission
from .sharing import _owner_local
from .sharing_contracts import SharingError
from .t03_contracts import EffectiveAuthority


def register_historical_claim(
    profile: LocalEngineContext,
    request: ClaimRequest,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime] = _utc_now,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Receipt:
    """Persist denial, never capture, approve, move or reactivate existing content.

    A completed exact retry returns its immutable receipt before present-source
    eligibility checks. That receipt is not publication or protected custody.
    """
    if type(request) not in CLAIM_TYPES:
        raise SharingError("invalid_arguments")
    return _register_existing(
        profile,
        request,
        authority=authority,
        admission=admission,
        validate_before_write=validate_before_write,
        clock=clock,
        checkpoint=checkpoint,
    )


def adopt_historical_baseline(
    profile: LocalEngineContext,
    request: BaselineRequest,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime] = _utc_now,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Receipt:
    """Bind a separately observed revision without recapturing the retained owner record.

    The trusted private caller verifies installed upstream binding/raw-file
    evidence. The engine verifies current destination, profile, source CAS,
    retained identity and exact transformed payload under exclusive admission.
    This grants neither public-copy authority nor independently protected custody.
    """
    if type(request) not in BASELINE_TYPES:
        raise SharingError("invalid_arguments")
    return _register_existing(
        profile,
        request,
        authority=authority,
        admission=admission,
        validate_before_write=validate_before_write,
        clock=clock,
        checkpoint=checkpoint,
    )


def link_historical_copy(
    profile: LocalEngineContext,
    request: RelationRequest,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    load_consent: Callable[[], HistoricalConsentSnapshot],
    clock: Callable[[], datetime] = _utc_now,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Receipt:
    """Reconcile an existing copy after private authentic approval verification.

    This never approves a new capture. The trusted runtime reloads existing
    Brain-bound provider consent; no consent or owner override is request JSON.
    Completed replay is receipt truth even if current output is now denied.
    """
    if type(request) not in RELATION_TYPES:
        raise SharingError("invalid_arguments")
    return _register_existing(
        profile,
        request,
        authority=authority,
        admission=admission,
        validate_before_write=validate_before_write,
        load_consent=load_consent,
        clock=clock,
        checkpoint=checkpoint,
    )


def revoke_historical_copy(
    profile: LocalEngineContext,
    request: RevocationRequest,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime] = _utc_now,
    checkpoint: Callable[[str], None] = lambda _stage: None,
) -> Receipt:
    """Revoke an exact existing relation without removing claims or restoring content.

    The original source may have advanced or retired. Its present CAS is checked,
    while the receipt preserves the relation's original historical identities.
    """
    if type(request) not in REVOCATION_TYPES:
        raise SharingError("invalid_arguments")
    return _register_existing(
        profile,
        request,
        authority=authority,
        admission=admission,
        validate_before_write=validate_before_write,
        clock=clock,
        checkpoint=checkpoint,
    )


def _register_existing(
    profile: LocalEngineContext,
    request: BaselineRequest | ClaimRequest | RevocationRequest | RelationRequest,
    *,
    authority: EffectiveAuthority,
    admission: HeldRuntimeAdmission,
    validate_before_write: Callable[[], None],
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None],
    load_consent: Callable[[], HistoricalConsentSnapshot] | None = None,
) -> Receipt:
    _owner_local(authority)
    request.canonical_bytes()
    if isinstance(request, BASELINE_TYPES):
        kind, outcome, role = "baseline", "baseline_adopted", "baseline_original"
        evidence, capture_cas = request.retained_original, request.source_cas
    elif isinstance(request, CLAIM_TYPES):
        kind, outcome, role = "claim", "denied_claim_recorded", request.claim_role
        evidence, capture_cas = request.retained_capture, request.capture_source_cas
    elif isinstance(request, REVOCATION_TYPES):
        kind, outcome, role = "revocation", "historical_copy_revoked", None
        evidence, capture_cas = None, None
    else:
        kind, outcome, role = "relation", "historical_copy_linked", None
        evidence, capture_cas = None, None

    def validate_consent(relation: RelationRequest) -> None:
        if load_consent is None:
            raise SharingError("unsupported_capability")
        require_historical_provider_consent(load_consent(), relation)

    def validate() -> None:
        admission.validate(profile)
        if admission.live_peer_count:
            raise SharingError("operation_pending")
        validate_before_write()

    validate()
    connection = open_local_database_read_only(profile)
    connection.close()
    lease = FileLease(
        profile.root / ".open-brain",
        "engine-" + sha256(profile.owner_actor_id.encode("utf-8")).hexdigest()[:32],
        clock=clock,
        validate_acquire=validate,
        parent_root_identity=profile.root_identity,
    )
    with lease.acquire_shared_writer():
        fence = HistoricalPendingFence(profile.root, profile.root_identity)
        retained = VersionedHistoricalTransitionStore(
            profile.root, profile.root_identity
        ).read_optional(request.operation_id)
        if retained is not None and retained.request.canonical_bytes() != request.canonical_bytes():
            raise SharingError("binding_mismatch")
        pending = fence.pending()
        if pending is not None:
            if pending != retained:
                raise SharingError("operation_pending")
        else:
            with _historical_transaction(profile, validate) as connection:
                identity = connection.execute(
                    "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
                ).fetchone()
                if identity is None or request.destination != HistoricalDestination(
                    brain_id=identity[0], issuer_epoch=identity[1]
                ):
                    raise SharingError("binding_mismatch")
                registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(
                    request.destination
                )
                fence.assert_settled(registry)
                records = verify_versioned_historical_projection(connection, profile, registry)
                completed = next(
                    (item for item in records if item.request.operation_id == request.operation_id),
                    None,
                )
                if completed is not None:
                    if completed != retained:
                        raise SharingError("binding_mismatch")
                    return completed.receipt
                if registry.generation != request.expected_claim_generation:
                    raise SharingError("revision_changed")
                verify_historical_source_cas(connection, request.source_cas)
                copy_capture_id: str | None
                relation_version: int | None
                if isinstance(request, BASELINE_TYPES):
                    verify_historical_baseline_evidence(connection, profile, request)
                elif isinstance(request, CLAIM_TYPES):
                    assert capture_cas is not None and evidence is not None
                    verify_historical_source_cas(connection, capture_cas)
                    verify_retained_capture(connection, profile, evidence, capture_cas.source_id)
                if isinstance(request, RELATION_TYPES):
                    baseline = next(
                        (
                            item.request
                            for item in records
                            if item.request.operation_id == request.baseline_operation_id
                        ),
                        None,
                    )
                    if not isinstance(baseline, BASELINE_TYPES):
                        raise SharingError("binding_mismatch")
                    verify_historical_relation_evidence(connection, profile, request, baseline)
                    validate_consent(request)
                    original_capture_id = baseline.retained_original.capture_id
                    copy_capture_id = request.retained_copy.capture_id
                    relation_version = 1
                    proposed = registry
                elif isinstance(request, REVOCATION_TYPES):
                    relation = next(
                        (
                            item.request
                            for item in records
                            if item.request.operation_id == request.relation_operation_id
                        ),
                        None,
                    )
                    if (
                        not isinstance(relation, RELATION_TYPES)
                        or request.expected_relation_version != 1
                        or request.source_cas.source_id != relation.source_cas.source_id
                        or any(
                            isinstance(item.request, REVOCATION_TYPES)
                            and item.request.relation_operation_id == request.relation_operation_id
                            for item in records
                        )
                    ):
                        raise SharingError("binding_mismatch")
                    original_capture_id = relation.source_cas.expected_head
                    copy_capture_id = relation.retained_copy.capture_id
                    relation_version = 2
                    proposed = registry
                else:
                    assert evidence is not None and capture_cas is not None and role is not None
                    membership = HistoricalClaimMembership(
                        capture_id=evidence.capture_id,
                        source_id=request.source_cas.source_id,
                        capture_source_id=capture_cas.source_id,
                        claim_role=role,
                    )
                    proposed = registry.register(membership)
                    original_capture_id = (
                        evidence.capture_id
                        if role == "baseline_original"
                        else request.source_cas.expected_head
                    )
                    copy_capture_id = evidence.capture_id if role == "historical_copy" else None
                    relation_version = None
                if proposed == registry:
                    proposed = HistoricalClaimRegistry._create(
                        registry.destination, registry.generation + 1, registry.memberships
                    )
                if retained is None:
                    instant = clock()
                    if instant.tzinfo is None or instant.utcoffset() is None:
                        raise SharingError("invalid_arguments")
                    body: dict[str, object] = {
                        "dto_version": request.dto_version,
                        "operation_id": request.operation_id,
                        "request_sha256": request.request_sha256,
                        "destination": request.destination.value(),
                        "source_id": request.source_cas.source_id,
                        "original_capture_id": original_capture_id,
                        "copy_capture_id": copy_capture_id,
                        "outcome": outcome,
                        "claim_generation": proposed.generation,
                        "relation_version": relation_version,
                        "recorded_at": instant.astimezone(UTC).isoformat().replace("+00:00", "Z"),
                    }
                    body["receipt_sha256"] = sha256(
                        f"open-brain-historical-receipt.v{request.dto_version}\0".encode()
                        + portable_canonical_json_bytes(body)
                    ).hexdigest()
                    retained = create_historical_transition(
                        request=request,
                        previous=registry,
                        proposed=proposed,
                        receipt=decode_receipt(body, request.dto_version),
                    )
                elif retained.previous != registry or retained.proposed != proposed:
                    raise SharingError("binding_mismatch")
                historical_projection_rows(proposed, (*records, retained))
                checkpoint(f"historical_{kind}_validated")
                validate()
                fence.prepare(retained)
                checkpoint(f"historical_{kind}_pending")
        result = _recover_historical(
            profile,
            validate_before_write=validate,
            checkpoint=checkpoint,
            validate_relation_consent=validate_consent,
        )
        if retained is None or result != retained.receipt:
            raise SharingError("binding_mismatch")
        return result
