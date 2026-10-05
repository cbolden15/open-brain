"""Owner-only planned capture replay on an imported, identity-preserving baseline.

The caller authenticates the independently retained latest head. A decoded plan
is not a grant, an imported consent, or a receipt-protection acknowledgement.
Managed and destination-bound operations require their own complete replay plans.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from hashlib import sha256
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import narrowest_tier
from open_brain_engine.portable.v5 import _manifest
from open_brain_engine.portable.v8 import PORTABLE_V8_SCHEMA_CATALOG_DIGEST
from open_brain_engine.portable.v9 import PORTABLE_V9_SCHEMA_CATALOG_DIGEST
from open_brain_engine.storage.filesystem import read_confined

from .capture_recovery import CaptureRecoveryPlan
from .consent_contracts import EgressMode
from .contracts import CaptureReceipt, CaptureSubmissionPath, FilePayload
from .historical_recovery import require_historical_snapshot_settled
from .portability import _manifest_digest, _validate_ready_record
from .recovery_journal import RecoveryBaseline, RecoveryHead, RecoveryRecord, verify_recovery_chain
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


def _reservation_values(plan: CaptureRecoveryPlan) -> dict[str, object]:
    return {key: value for key, value in asdict(plan.identities).items() if key != "canonical"}


def require_matching_capture(row: sqlite3.Row, plan: CaptureRecoveryPlan) -> None:
    """Delivery/request alone omit original privacy and authority metadata."""
    submission = plan.envelope.submission
    role = dict(submission.role_claim)
    role["capabilities"] = list(cast(tuple[str, ...], role["capabilities"]))
    expected = {
        **_reservation_values(plan),
        "delivery_id": submission.delivery_id,
        "request_sha256": submission.request_sha256(),
        "payload_family": submission.payload.family,
        "payload_json": portable_canonical_json_bytes(submission.payload.to_dict()),
        "search_text": submission.payload.search_text(),
        "file_bytes": submission.payload.data
        if isinstance(submission.payload, FilePayload)
        else None,
        "source_origin": submission.durable_source_origin(),
        "source_reference": submission.source_reference,
        "space_id": submission.space_id,
        "intent": None if submission.intent is None else submission.intent.value,
        "capture_why": submission.capture_why,
        "action": submission.action.value,
        "title": submission.title,
        "actor_id": submission.actor_id,
        "submission_path": submission.submission_path.value,
        "role_claim_json": portable_canonical_json_bytes(role).decode(),
        "privacy_json": portable_canonical_json_bytes(
            plan.envelope.admitted_privacy.to_dict()
        ).decode(),
        "provenance_json": portable_canonical_json_bytes(submission.provenance.to_dict()).decode(),
    }
    if any(row[key] != value for key, value in expected.items()):
        raise ValueError("capture recovery conflicts with retained capture")


def _baseline_manifest(engine: BrainEngine, baseline: RecoveryBaseline) -> set[str]:
    def read_json(relative: str, limit: int) -> dict[str, object]:
        raw = read_confined(
            root=engine.profile.root,
            relative=relative,
            expected_root_identity=engine.profile.root_identity,
            maximum_bytes=limit,
        )
        if raw is None:
            raise ValueError("capture recovery requires imported baseline")
        value = json.loads(raw)
        if type(value) is not dict or portable_canonical_json_bytes(value) != raw:
            raise ValueError("invalid capture recovery baseline evidence")
        return cast(dict[str, object], value)

    manifest = read_json("portable-manifest.json", 16 * 1024 * 1024)
    version = manifest.get("schema_version")
    if type(version) is not int or version not in (8, 9):
        raise ValueError("capture recovery requires Portable8 or Portable9")
    catalog = (
        PORTABLE_V8_SCHEMA_CATALOG_DIGEST if version == 8 else PORTABLE_V9_SCHEMA_CATALOG_DIGEST
    )
    paths = _manifest(manifest, version=version, catalog=catalog)
    ready = read_json(".open-brain/state/portability-ready.json", 65536)
    _validate_ready_record(manifest, import_id=cast(str, ready.get("import_id")), ready=ready)
    if (
        _manifest_digest(manifest) != baseline.artifact_sha256
        or manifest["tenant_id"] != engine.profile.tenant_id
    ):
        raise ValueError("capture recovery baseline mismatch")
    return set(paths)


def _preflight_captures(
    connection: sqlite3.Connection,
    plans: tuple[CaptureRecoveryPlan, ...],
    paths: set[str],
) -> None:
    seen_deliveries: dict[str, bytes] = {}
    seen_ids: dict[str, bytes] = {}
    for plan in plans:
        raw = plan.to_bytes()
        delivery = plan.envelope.submission.delivery_id
        identifiers = {
            value
            for name, value in _reservation_values(plan).items()
            if name != "accepted_at" and isinstance(value, str)
        }
        if delivery in seen_deliveries and seen_deliveries[delivery] != raw:
            raise ValueError("conflicting capture recovery deliveries")
        if any(identity in seen_ids and seen_ids[identity] != raw for identity in identifiers):
            raise ValueError("conflicting capture recovery identities")
        seen_deliveries[delivery] = raw
        seen_ids.update(dict.fromkeys(identifiers, raw))
        existing = connection.execute(
            "SELECT * FROM captures WHERE delivery_id=?",
            (delivery,),
        ).fetchone()
        if existing is not None:
            require_matching_capture(existing, plan)
            continue
        if any(PurePosixPath(path).stem in identifiers for path in paths):
            raise ValueError("capture recovery collides with baseline record")
        # Fixed catalog columns only. Cross-column receipt collisions must also
        # refuse: SQL's per-column UNIQUE constraints alone do not enforce them.
        columns = {
            "captures": tuple(key for key in _reservation_values(plan) if key != "accepted_at"),
            "proposals": ("proposal_id", "receipt_id", "page_id"),
            "decisions": ("decision_id", "decision_receipt_id", "page_id", "publication_id"),
            "space_operations": ("receipt_id",),
            "route_operations": ("receipt_id",),
            "review_page_heads": ("page_id", "publication_id"),
        }
        values = tuple(sorted(identifiers))
        placeholders = ",".join("?" for _ in values)
        for table, names in columns.items():
            for name in names:
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE {name} IN ({placeholders}) LIMIT 1",
                        values,
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("capture recovery identity collision")


def _require_completed_sources(
    engine: BrainEngine,
    connection: sqlite3.Connection,
    captures: tuple[CaptureRecoveryPlan, ...],
) -> None:
    """A matching completed row cannot stand in for missing physical records."""
    for plan in captures:
        row = connection.execute(
            "SELECT * FROM captures WHERE delivery_id=?",
            (plan.envelope.submission.delivery_id,),
        ).fetchone()
        if row is None or row["stage"] < 3:
            continue
        expected = portable_canonical_json_bytes(engine._capture_record(row))
        source = read_confined(
            root=engine.profile.root,
            relative=row["source_path"],
            expected_root_identity=engine.profile.root_identity,
            maximum_bytes=len(expected),
        )
        if source != expected:
            raise ValueError("owner recovery completed source mismatch")
        if row["file_bytes"] is not None:
            original = row["file_bytes"]
            digest = sha256(original).hexdigest()
            blob = read_confined(
                root=engine.profile.root,
                relative=f"sources/blobs/sha256/{digest[:2]}/{digest}",
                expected_root_identity=engine.profile.root_identity,
                maximum_bytes=max(1, len(original)),
            )
            if blob != original:
                raise ValueError("owner recovery completed blob mismatch")


def replay_owner_capture_chain(
    engine: BrainEngine,
    records: tuple[RecoveryRecord, ...],
    *,
    expected_head: RecoveryHead,
    authority: EffectiveAuthority,
    dependency_payloads: tuple[bytes, ...] = (),
) -> tuple[CaptureReceipt, ...]:
    """Preflight the whole chain, then resume established capture stage machines.

    This is a local engine seam, not a CLI/remote restoration endpoint. The
    imported manifest and ready record bind the baseline; ordinary historical
    fencing and the writer lease still apply. No new IDs are allocated.
    """
    if (
        type(authority) is not EffectiveAuthority
        or not authority.owner
        or authority.egress_mode is not EgressMode.OWNER_LOCAL
        or authority.principal_id != engine.profile.owner_actor_id
        or type(expected_head) is not RecoveryHead
    ):
        raise ValueError("capture recovery requires local owner")
    baseline = expected_head.baseline
    if authority.brain_id is not None and (
        authority.brain_id != baseline.brain_id or authority.issuer_epoch != baseline.issuer_epoch
    ):
        raise ValueError("capture recovery authority destination mismatch")
    verify_recovery_chain(
        baseline,
        records,
        expected_head=expected_head,
        dependency_payloads=dependency_payloads,
    )
    plans = tuple(CaptureRecoveryPlan.from_record(record) for record in records)
    if any(
        plan.envelope.submission.submission_path is not CaptureSubmissionPath.OWNER
        for plan in plans
    ):
        raise ValueError("unsupported capture recovery operation")
    engine._assert_root()
    with engine._writer_lease_bounded():
        engine._assert_root()
        if engine._validate_mutation_authority is not None:
            engine._validate_mutation_authority()
        paths = _baseline_manifest(engine, baseline)
        with engine._store.connect() as connection:
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1",
            ).fetchone()
            if identity is None or tuple(identity) != (baseline.brain_id, baseline.issuer_epoch):
                raise ValueError("capture recovery destination mismatch")
            require_historical_snapshot_settled(connection, engine.profile)
            _preflight_captures(connection, plans, paths)
            _require_completed_sources(engine, connection, plans)
        for plan in plans:
            current = engine._prepare_journal_submission(plan.envelope.submission)
            retained = plan.envelope.admitted_privacy
            if narrowest_tier(retained.tier, current.admitted_privacy.tier) != retained.tier:
                raise ValueError("capture recovery requires narrower current privacy")
        engine._refuse_on_storage_watermark()
        return tuple(
            engine._materialize_capture_locked(
                plan.envelope.submission,
                admitted_privacy=plan.envelope.admitted_privacy,
                recovery_plan=plan,
            )
            for plan in plans
        )
