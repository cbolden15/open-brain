"""Exact historical baseline lookup, distinct from normal delivery custody."""

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.storage.filesystem import RootIdentity

from .contracts import LocalEngineContext
from .historical_admission import verify_historical_source_cas
from .historical_contracts import HistoricalReceipt, HistoricalSourceCAS
from .historical_source import historical_baseline_for_namespace
from .source_intake import (
    SourceRevisionBinding,
    SourceRevisionDelivery,
    SourceRevisionDeliveryReceipt,
    SourceRevisionObservedDelivery,
    SourceRevisionReceipt,
)
from .t03_contracts import T03Error


@dataclass(frozen=True, slots=True)
class HistoricalBaselineTemplate:
    """Body-free reconstruction coordinates, never checkpoint authority.

    Rebuild with the incoming capture and observation, then perform exact
    lookup. These historical coordinates do not assert current eligibility.
    """

    revision_key: str
    capture_delivery_id: str
    delivery_id: str
    expected_head: str | None
    ordering: Mapping[str, object]
    expected_control_epoch: int
    expected_lifecycle_version: int
    observed_envelope_sha256: str


def historical_baseline_template(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    binding: SourceRevisionBinding,
) -> HistoricalBaselineTemplate | None:
    from open_brain_engine.core.ids import portable_canonical_json_bytes

    baseline = historical_baseline_for_namespace(
        connection,
        profile,
        sha256(portable_canonical_json_bytes(dict(binding.namespace))).hexdigest(),
        binding=binding,
    )
    if baseline is None:
        return None
    delivery = baseline.observed_delivery
    submission = delivery.submission
    return HistoricalBaselineTemplate(
        revision_key=submission.revision_key,
        capture_delivery_id=submission.capture.delivery_id,
        delivery_id=delivery.delivery_id,
        expected_head=submission.expected_head,
        ordering=submission.ordering,
        expected_control_epoch=submission.expected_control_epoch,
        expected_lifecycle_version=delivery.expected_lifecycle_version,
        observed_envelope_sha256=delivery.envelope_sha256,
    )


@dataclass(frozen=True, slots=True)
class HistoricalBaselineDuplicate:
    """A revalidated observation, not a normal capture or protection receipt.

    Root identity and selection generation are local checkpoint witnesses, not
    serialized historical authority. A checkpoint must recompute this result.
    """

    receipt: HistoricalReceipt
    source_cas: HistoricalSourceCAS
    observed_envelope_sha256: str
    selection_generation: str
    root_identity: RootIdentity

    @property
    def capture_id(self) -> str:
        return self.receipt.original_capture_id

    @property
    def source_id(self) -> str:
        return self.receipt.source_id


def _validate_generation(selection_generation: str) -> None:
    if (
        type(selection_generation) is not str
        or not selection_generation
        or any(ord(char) < 32 or ord(char) == 127 for char in selection_generation)
    ):
        raise T03Error("invalid_arguments")
    try:
        if len(selection_generation.encode("utf-8")) > 1024:
            raise T03Error("invalid_arguments")
    except UnicodeEncodeError:
        raise T03Error("invalid_arguments") from None


@dataclass(frozen=True, slots=True)
class CurrentRevisionCheckpoint:
    """Current source witness, distinct from immutable terminal acceptance."""

    receipt: SourceRevisionDeliveryReceipt
    source_cas: HistoricalSourceCAS
    selection_generation: str
    root_identity: RootIdentity


def lookup_revision_checkpoint(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    delivery: SourceRevisionDelivery,
    receipt: SourceRevisionDeliveryReceipt,
    *,
    selection_generation: str,
) -> CurrentRevisionCheckpoint:
    _validate_generation(selection_generation)
    source = receipt.source_receipt
    if (
        type(receipt) is not SourceRevisionDeliveryReceipt
        or source is None
        or receipt.outcome not in {"captured", "duplicate"}
        or source.outcome != receipt.outcome
        or source.source_id is None
        or source.capture_id is None
        or receipt.delivery_id != delivery.delivery_id
        or receipt.envelope_sha256 != delivery.envelope_sha256
        or receipt.destination_brain_id != delivery.binding.destination_brain_id
        or type(receipt.issuer_epoch) is not int
        or receipt.issuer_epoch != delivery.binding.issuer_epoch
    ):
        raise T03Error("invalid_arguments")
    namespace_sha = sha256(delivery.submission.namespace_bytes()).hexdigest()
    # Check settled complete authority even when this namespace is ordinary.
    historical_baseline_for_namespace(connection, profile, namespace_sha, binding=delivery.binding)
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    row = connection.execute(
        "SELECT envelope_bytes,receipt_json FROM managed_source_deliveries WHERE delivery_id=?",
        (delivery.delivery_id,),
    ).fetchone()
    if (
        identity is None
        or tuple(identity) != (delivery.binding.destination_brain_id, delivery.binding.issuer_epoch)
        or row is None
        or bytes(row[0]) != delivery.custody_bytes()
        or row[1] is None
        or SourceRevisionReceipt(**json.loads(row[1])["source_receipt"]) != source
    ):
        raise T03Error("revision_changed")
    current = connection.execute(
        "SELECT s.*,l.lifecycle_version,g.control_epoch FROM source_namespaces n "
        "JOIN logical_sources s USING(source_id) "
        "JOIN source_lifecycle_state l USING(source_id) CROSS JOIN engine_generations g "
        "WHERE n.namespace_sha256=?",
        (namespace_sha,),
    ).fetchone()
    revision = connection.execute(
        "SELECT revision_key FROM source_revisions WHERE capture_id=?", (source.capture_id,)
    ).fetchone()
    if (
        current is None
        or current["source_id"] != source.source_id
        or current["head_capture_id"] != source.capture_id
        or current["lifecycle_version"] != delivery.expected_lifecycle_version
        or current["control_epoch"] != delivery.submission.expected_control_epoch
        or type(source.control_epoch) is not int
        or current["control_epoch"] != source.control_epoch
        or revision is None
        or revision[0] != delivery.submission.revision_key
    ):
        raise T03Error("revision_changed")
    cas = HistoricalSourceCAS(
        source_id=current["source_id"],
        expected_head=current["head_capture_id"],
        expected_head_version=current["head_version"],
        expected_route_version=current["route_version"],
        expected_lifecycle_version=current["lifecycle_version"],
        expected_control_epoch=current["control_epoch"],
        expected_lifecycle=current["lifecycle"],
        expected_availability=current["availability"],
        expected_historical_only=bool(current["historical_only"]),
    )
    cas.require_active()
    return CurrentRevisionCheckpoint(receipt, cas, selection_generation, profile.root_identity)


def lookup_historical_baseline(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    delivery: SourceRevisionObservedDelivery,
    *,
    selection_generation: str,
) -> HistoricalBaselineDuplicate | None:
    if type(delivery) is not SourceRevisionObservedDelivery:
        raise T03Error("invalid_arguments")
    _validate_generation(selection_generation)
    raw = delivery.custody_bytes()
    if len(raw) > 65536:
        raise T03Error("invalid_arguments")
    namespace_sha256 = sha256(delivery.submission.namespace_bytes()).hexdigest()
    baseline = historical_baseline_for_namespace(
        connection, profile, namespace_sha256, binding=delivery.binding
    )
    if baseline is None:
        return None
    # Eligibility changes must not turn a bound namespace into an unbound one.
    baseline.source_cas.require_active()
    verify_historical_source_cas(connection, baseline.source_cas)
    if raw != baseline.observed_delivery.custody_bytes():
        return None
    revision = connection.execute(
        "SELECT revision_key FROM source_revisions WHERE capture_id=?",
        (baseline.retained_original.capture_id,),
    ).fetchone()
    if revision is None or revision[0] is not None:
        raise T03Error("revision_changed")
    row = connection.execute(
        "SELECT receipt_json FROM historical_operations WHERE operation_id=? AND kind='baseline'",
        (baseline.operation_id,),
    ).fetchone()
    if row is None:
        raise T03Error("revision_changed")
    receipt = HistoricalReceipt.from_value(json.loads(bytes(row[0])))
    return HistoricalBaselineDuplicate(
        receipt=receipt,
        source_cas=baseline.source_cas,
        observed_envelope_sha256=delivery.envelope_sha256,
        selection_generation=selection_generation,
        root_identity=profile.root_identity,
    )
