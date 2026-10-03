"""Exact historical baseline lookup, distinct from normal delivery custody."""

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256

from open_brain_engine.storage.filesystem import RootIdentity

from .contracts import LocalEngineContext
from .historical_admission import verify_historical_source_cas
from .historical_contracts import HistoricalReceipt, HistoricalSourceCAS
from .historical_source import historical_baseline_for_namespace
from .source_intake import SourceRevisionObservedDelivery
from .t03_contracts import T03Error


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


def lookup_historical_baseline(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    delivery: SourceRevisionObservedDelivery,
    *,
    selection_generation: str,
) -> HistoricalBaselineDuplicate | None:
    if (
        type(delivery) is not SourceRevisionObservedDelivery
        or type(selection_generation) is not str
        or not selection_generation
        or any(ord(char) < 32 or ord(char) == 127 for char in selection_generation)
    ):
        raise T03Error("invalid_arguments")
    try:
        if len(selection_generation.encode("utf-8")) > 1024:
            raise T03Error("invalid_arguments")
    except UnicodeEncodeError:
        raise T03Error("invalid_arguments") from None
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
