"""Verified historical namespace identity and an explicit legacy predecessor key."""

import json
import sqlite3
from hashlib import sha256
from typing import TYPE_CHECKING, Any, cast

from .contracts import LocalEngineContext
from .historical_admission import verify_retained_capture
from .historical_contracts import HistoricalBaselineRequest, HistoricalDestination
from .historical_fence import HistoricalPendingFence
from .historical_projection import verify_historical_projection
from .historical_registry import HistoricalRegistryStore
from .sharing_contracts import SharingError

if TYPE_CHECKING:
    from .source_intake import SourceRevisionBinding


def historical_baseline_for_namespace(
    connection: sqlite3.Connection,
    profile: LocalEngineContext,
    namespace_sha256: str,
    *,
    binding: SourceRevisionBinding | None = None,
) -> HistoricalBaselineRequest | None:
    """Resolve retained identity even after withdrawal, not current eligibility.

    Missing authority must never mean an unbound namespace/new logical source.
    Caller owns an actual SQL snapshot. No authority cache or inferred grant.
    """
    if connection.execute("PRAGMA user_version").fetchone()[0] < 13:
        return None
    identity = connection.execute("SELECT brain_id,issuer_epoch FROM brain_identity").fetchone()
    if identity is None:
        raise SharingError("binding_mismatch")
    destination = HistoricalDestination(brain_id=identity[0], issuer_epoch=identity[1])
    registry = HistoricalRegistryStore(profile.root, profile.root_identity).read(destination)
    HistoricalPendingFence(profile.root, profile.root_identity).assert_settled(registry)
    records = verify_historical_projection(connection, profile, registry)
    matches = tuple(
        item.request
        for item in records
        if isinstance(item.request, HistoricalBaselineRequest)
        and sha256(item.request.observed_delivery.submission.namespace_bytes()).hexdigest()
        == namespace_sha256
    )
    if not matches:
        return None
    if len(matches) != 1:
        raise SharingError("binding_mismatch")
    baseline = matches[0]
    if binding is not None and baseline.observed_delivery.binding != binding:
        raise SharingError("binding_mismatch")
    verify_retained_capture(
        connection, profile, baseline.retained_original, baseline.source_cas.source_id
    )
    return baseline


def historical_source_row(
    connection: sqlite3.Connection, baseline: HistoricalBaselineRequest
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT s.*,l.lifecycle_version FROM logical_sources s "
        "JOIN source_lifecycle_state l USING(source_id) WHERE s.source_id=?",
        (baseline.source_cas.source_id,),
    ).fetchone()
    if row is None:
        raise SharingError("binding_mismatch")
    return cast(sqlite3.Row, row)


def historical_revision_head(
    head: sqlite3.Row | None,
    baseline: HistoricalBaselineRequest | None,
) -> sqlite3.Row | dict[str, Any] | None:
    """Overlay only the verified retained head in memory; never rewrite old SQL."""
    if (
        baseline is None
        or head is None
        or head["capture_id"] != baseline.retained_original.capture_id
    ):
        return head
    if head["revision_key"] is not None:
        raise SharingError("binding_mismatch")
    result = dict(head)
    result["revision_key"] = baseline.observed_delivery.submission.revision_key
    result["ordering_json"] = json.dumps(dict(baseline.observed_delivery.submission.ordering))
    return result
