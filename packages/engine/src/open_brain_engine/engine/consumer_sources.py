"""Stable consumer sources over checked shared lifecycle custody.

Source staging retains immutable revisions; only group activation changes an
existing head. These helpers require the caller's writer transaction and do not
grant owner, profile, account, publication, or privacy authority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from hashlib import sha256
from typing import TYPE_CHECKING, cast

from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical

from .capture_lifecycle_contracts import LifecycleError, canonical_bytes, validate_digest
from .capture_lifecycle_journal import _atomic
from .consumer_contracts import namespace_identifier
from .consumer_journal import consumer_items
from .consumer_segments import ConsumerSegmentEnvelope, ConsumerSourceBinding
from .privacy_projection import parse_effective_privacy_json

if TYPE_CHECKING:
    from .capture_lifecycle_journal import LifecycleJournal, _State
    from .t03_contracts import EffectiveAuthority


def _transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise LifecycleError("local_operation_failed")


def _namespace(namespace_id: str, conversation_key: str, segment_key: str) -> bytes:
    namespace_identifier(namespace_id)
    validate_digest(conversation_key)
    validate_digest(segment_key)
    return canonical_bytes(
        {
            "connector_name": "consumer_chat",
            "connection_id": namespace_id,
            "resource_id": conversation_key,
            "external_id": segment_key,
        }
    )


def consumer_namespace_bytes(binding: ConsumerSourceBinding) -> bytes:
    if type(binding) is not ConsumerSourceBinding:
        raise LifecycleError("invalid_arguments")
    return _namespace(binding.namespace_id, binding.conversation_key, binding.segment_key)


def read_consumer_source_binding(
    connection: sqlite3.Connection, *, namespace_id: str, conversation_key: str, segment_key: str
) -> ConsumerSourceBinding | None:
    """Read one current source preimage; absence is different from damaged lineage."""
    raw = _namespace(namespace_id, conversation_key, segment_key)
    owned = not connection.in_transaction
    try:
        if owned:
            connection.execute("BEGIN")
        row = connection.execute(
            "SELECT n.namespace_json,n.source_id,s.head_capture_id,s.head_version,"
            "s.route_version,s.lifecycle,s.availability,s.space_id,l.lifecycle_version,"
            "p.effective_privacy_json,g.control_epoch "
            "FROM source_namespaces n LEFT JOIN logical_sources s USING(source_id) "
            "LEFT JOIN source_lifecycle_state l USING(source_id) "
            "LEFT JOIN source_revision_privacy p ON p.capture_id=s.head_capture_id "
            "CROSS JOIN engine_generations g WHERE n.namespace_sha256=?",
            (sha256(raw).hexdigest(),),
        ).fetchone()
        if row is None:
            return None
        if row["namespace_json"] != raw.decode() or row["effective_privacy_json"] is None:
            raise LifecycleError("invalid_evidence")
        parse_effective_privacy_json(row["effective_privacy_json"])
        return ConsumerSourceBinding(
            namespace_id=namespace_id,
            conversation_key=conversation_key,
            segment_key=segment_key,
            source_id=row["source_id"],
            expected_head=row["head_capture_id"],
            head_version=row["head_version"],
            route_version=row["route_version"],
            lifecycle_version=row["lifecycle_version"],
            lifecycle=row["lifecycle"],
            availability=row["availability"],
            space_id=row["space_id"],
            effective_privacy_sha256=sha256(row["effective_privacy_json"].encode()).hexdigest(),
            control_epoch=row["control_epoch"],
        )
    except ValueError, TypeError, AttributeError:
        raise LifecycleError("invalid_evidence") from None
    finally:
        if owned and connection.in_transaction:
            connection.rollback()


def _retained_segment(
    connection: sqlite3.Connection,
    capture_id: str,
    *,
    journal: LifecycleJournal | None = None,
    state: _State | None = None,
) -> ConsumerSegmentEnvelope | None:
    from .capture_lifecycle import decode_lifecycle_segment, open_lifecycle_journal

    row = connection.execute(
        "SELECT * FROM consumer_segment_bindings WHERE capture_id=?", (capture_id,)
    ).fetchone()
    if row is None:
        return None
    journal = open_lifecycle_journal(connection) if journal is None else journal
    state = journal._state() if state is None else state
    manifest = state.staged.get((row["operation_id"], row["group_key"]))
    if (
        manifest is None
        or (row["operation_id"], row["group_key"], capture_id) not in state.custodied
    ):
        raise LifecycleError("invalid_evidence")
    binding = next((item for item in manifest.segments if item.capture_id == capture_id), None)
    if binding is None or binding.request_sha256 != row["envelope_sha256"]:
        raise LifecycleError("invalid_evidence")
    raw = state.blobs[binding.request_sha256]
    segment = decode_lifecycle_segment(journal, row["operation_id"], raw)
    if not isinstance(segment, ConsumerSegmentEnvelope) or (
        segment.binding != binding
        or segment.conversation_key != row["group_key"]
        or segment.conversation_revision_sha256 != row["revision_sha256"]
        or segment.conversation_revision_sha256 != manifest.revision_sha256
        or segment.segment_index != row["segment_index"]
        or segment.normalized_sha256 != row["normalized_sha256"]
        or segment.source.source_id != row["source_id"]
        or consumer_namespace_bytes(segment.source) != bytes(row["namespace_bytes"])
        or canonical_bytes(segment.source.value()) != bytes(row["expected_source_bytes"])
        or state.blobs[binding.body_sha256] != segment.body
    ):
        raise LifecycleError("invalid_evidence")
    for key, kind, expected in (
        (binding.request_sha256, "segment", raw),
        (segment.normalized_sha256, "normalized", None),
    ):
        retained = connection.execute(
            "SELECT kind,object_bytes FROM consumer_intent_objects WHERE sha256=?", (key,)
        ).fetchone()
        if (
            retained is None
            or retained["kind"] != kind
            or (
                sha256(retained["object_bytes"]).hexdigest() != key
                or expected is not None
                and bytes(retained["object_bytes"]) != expected
            )
        ):
            raise LifecycleError("invalid_evidence")
    return segment


def _owned_placeholder(
    connection: sqlite3.Connection, segment: ConsumerSegmentEnvelope, current: ConsumerSourceBinding
) -> bool:
    binding = segment.source
    if binding.expected_head is not None or (
        current.source_id != binding.source_id
        or current.expected_head != segment.identities.capture_id
        or current.head_version != 1
        or current.route_version != 0
        or current.lifecycle_version != 0
        or current.lifecycle != "active"
        or current.availability != "available"
        or current.space_id is not None
        or current.control_epoch != binding.control_epoch
    ):
        return False
    source = connection.execute(
        "SELECT historical_only FROM logical_sources WHERE source_id=?", (binding.source_id,)
    ).fetchone()
    count = connection.execute(
        "SELECT count(*) FROM source_revisions WHERE source_id=?", (binding.source_id,)
    ).fetchone()[0]
    privacy = connection.execute(
        "SELECT privacy_json FROM captures WHERE capture_id=?", (segment.identities.capture_id,)
    ).fetchone()
    from .privacy_projection import effective_privacy_json, project_retained_privacy_evidence

    expected_privacy = effective_privacy_json(
        project_retained_privacy_evidence(
            [canonical(segment.envelope.admitted_privacy.to_dict()).decode()]
        )
    )
    return bool(
        source is not None
        and source["historical_only"] == 1
        and count == 1
        and privacy is not None
        and privacy["privacy_json"]
        == canonical(segment.envelope.admitted_privacy.to_dict()).decode()
        and current.effective_privacy_sha256 == sha256(expected_privacy.encode()).hexdigest()
        and _retained_segment(connection, segment.identities.capture_id) == segment
    )


def require_consumer_source_preimages(
    connection: sqlite3.Connection, segments: Sequence[ConsumerSegmentEnvelope]
) -> None:
    """Check selected source CAS; only exact custodied own creations excuse absence."""
    from .capture_lifecycle import open_lifecycle_journal

    _transaction(connection)
    state = open_lifecycle_journal(connection)._state()
    source_ids: set[str] = set()
    namespaces: set[bytes] = set()
    for segment in segments:
        if type(segment) is not ConsumerSegmentEnvelope:
            raise LifecycleError("invalid_arguments")
        binding = segment.source
        if segment.conversation_key in state.barriers:
            raise LifecycleError("retracted")
        raw = consumer_namespace_bytes(binding)
        if binding.source_id in source_ids or raw in namespaces:
            raise LifecycleError("invalid_arguments")
        source_ids.add(binding.source_id)
        namespaces.add(raw)
        epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
        if epoch != binding.control_epoch:
            raise LifecycleError("revision_changed")
        current = read_consumer_source_binding(
            connection,
            namespace_id=binding.namespace_id,
            conversation_key=binding.conversation_key,
            segment_key=binding.segment_key,
        )
        if current is None:
            if (
                binding.expected_head is not None
                or connection.execute(
                    "SELECT 1 FROM logical_sources WHERE source_id=?", (binding.source_id,)
                ).fetchone()
                is not None
            ):
                raise LifecycleError("revision_changed")
        elif current != binding and not _owned_placeholder(connection, segment, current):
            raise LifecycleError("revision_changed")
        if binding.lifecycle != "active" or binding.availability != "available":
            raise LifecycleError("revision_changed")


def register_consumer_capture(
    connection: sqlite3.Connection, row: sqlite3.Row, payload: bytes
) -> bool:
    """Register custodied immutable evidence, preserving an existing current head."""
    _transaction(connection)
    if connection.execute("PRAGMA user_version").fetchone()[0] != 20:
        return False
    segment = _retained_segment(connection, row["capture_id"])
    if segment is None:
        return False
    from .capture_lifecycle import _matching_row, open_lifecycle_journal

    _matching_row(row, segment, open_lifecycle_journal(connection))
    if row["stage"] != 3 or not row["source_path"]:
        raise LifecycleError("operation_pending")
    record = json.loads(payload)
    submission = segment.envelope.submission
    expected_role = dict(submission.role_claim)
    expected_role["capabilities"] = list(
        cast(tuple[str, ...], submission.role_claim["capabilities"])
    )
    if (
        canonical(record) != payload
        or record["capture_id"] != row["capture_id"]
        or record["actor_id"] != submission.actor_id
        or record["tenant_id"] != submission.tenant_id
        or record["role_claim"] != expected_role
        or record["payload"] != submission.payload.to_dict()
        or record["privacy"] != segment.envelope.admitted_privacy.to_dict()
        or record["source"] != {"origin": "third_party", "reference": submission.source_reference}
        or record["provenance"]
        != {
            "content_origin": "third_party",
            "owner_context": "automation_absent",
            "source_ref": submission.source_reference,
            "transformation_receipts": [],
        }
    ):
        raise LifecycleError("invalid_evidence")
    require_consumer_source_preimages(connection, (segment,))
    binding = segment.source
    raw = consumer_namespace_bytes(binding)
    existing = connection.execute(
        "SELECT * FROM source_revisions WHERE capture_id=?", (row["capture_id"],)
    ).fetchone()
    if existing is not None:
        if existing["source_id"] != binding.source_id or bytes(existing["source_bytes"]) != payload:
            raise LifecycleError("invalid_evidence")
        return True
    if binding.expected_head is None:
        connection.execute(
            "INSERT INTO logical_sources VALUES(?,?,1,NULL,0,1,'active','available')",
            (binding.source_id, row["capture_id"]),
        )
        connection.execute(
            "INSERT INTO source_namespaces VALUES(?,?,?)",
            (sha256(raw).hexdigest(), raw.decode(), binding.source_id),
        )
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM source_revisions WHERE source_id=?",
        (binding.source_id,),
    ).fetchone()[0]
    connection.execute(
        "INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            row["capture_id"],
            binding.source_id,
            sequence,
            binding.expected_head,
            row["source_path"],
            sha256(payload).hexdigest(),
            payload,
            row["request_sha256"],
            segment.conversation_revision_sha256,
            canonical({"kind": "unordered"}).decode(),
            row["accepted_at"],
        ),
    )
    connection.execute(
        "INSERT INTO source_aliases VALUES(?,?,?)",
        (row["delivery_id"], binding.source_id, row["request_sha256"]),
    )
    return True


def activate_consumer_sources(
    connection: sqlite3.Connection,
    segments: Sequence[ConsumerSegmentEnvelope],
    *,
    previous_capture_ids: Sequence[str] = (),
) -> None:
    """Publish a complete checked head set; the caller commits group membership with it."""
    with _atomic(connection):
        _activate_consumer_sources(connection, segments, previous_capture_ids=previous_capture_ids)


def _activate_consumer_sources(
    connection: sqlite3.Connection,
    segments: Sequence[ConsumerSegmentEnvelope],
    *,
    previous_capture_ids: Sequence[str],
) -> None:
    _transaction(connection)
    require_consumer_source_preimages(connection, segments)
    current_sources = {segment.source.source_id for segment in segments}
    for segment in segments:
        if _retained_segment(connection, segment.identities.capture_id) != segment:
            raise LifecycleError("invalid_evidence")
        revision = connection.execute(
            "SELECT source_id FROM source_revisions WHERE capture_id=?",
            (segment.identities.capture_id,),
        ).fetchone()
        if revision is None or revision["source_id"] != segment.source.source_id:
            raise LifecycleError("operation_pending")
        connection.execute(
            "UPDATE logical_sources SET head_capture_id=?,historical_only=0,"
            "head_version=head_version+? WHERE source_id=?",
            (
                segment.identities.capture_id,
                int(segment.source.expected_head is not None),
                segment.source.source_id,
            ),
        )
        connection.execute(
            "UPDATE captures SET space_id=? WHERE capture_id=?",
            (segment.source.space_id, segment.identities.capture_id),
        )
    for capture_id in previous_capture_ids:
        row = connection.execute(
            "SELECT s.source_id,s.head_capture_id FROM source_revisions r "
            "JOIN logical_sources s USING(source_id) WHERE r.capture_id=?",
            (capture_id,),
        ).fetchone()
        if (
            row is not None
            and row["source_id"] not in current_sources
            and (row["head_capture_id"] == capture_id)
        ):
            connection.execute(
                "UPDATE logical_sources SET historical_only=1 WHERE source_id=?",
                (row["source_id"],),
            )


def consumer_group_sources_visible(
    connection: sqlite3.Connection,
    capture_id: str,
    *,
    history: bool = False,
    journal: LifecycleJournal | None = None,
    authority: EffectiveAuthority | None = None,
) -> bool:
    """Additional source-state closure; callers still enforce per-authority privacy."""
    from .capture_lifecycle import open_lifecycle_journal

    _transaction(connection)
    journal = open_lifecycle_journal(connection) if journal is None else journal
    state = journal._state()
    if not state.permits(capture_id, history=history):
        return False
    if history:
        return True
    bound = state.capture_bindings.get(capture_id)
    if bound is None:
        return True
    group, revision = bound
    manifest = state.revisions[bound]
    consumer = any(
        item.group_key == group
        and item.revision_sha256 == revision
        and (operation, group) in state.completed
        and consumer_items(state.request(operation)) is not None
        for (operation, _key), item in state.staged.items()
    )
    if not consumer:
        return True
    for member in manifest.segments:
        segment = _retained_segment(connection, member.capture_id, journal=journal, state=state)
        if segment is None:
            return False
        row = connection.execute(
            "SELECT s.*,p.effective_privacy_json FROM logical_sources s "
            "JOIN source_revisions r USING(source_id) "
            "JOIN source_revision_privacy p USING(capture_id) "
            "JOIN source_namespaces n USING(source_id) WHERE r.capture_id=? "
            "AND n.namespace_sha256=? AND n.namespace_json=?",
            (
                member.capture_id,
                sha256(consumer_namespace_bytes(segment.source)).hexdigest(),
                consumer_namespace_bytes(segment.source).decode(),
            ),
        ).fetchone()
        if row is None or (
            row["source_id"] != segment.source.source_id
            or row["head_capture_id"] != member.capture_id
            or row["historical_only"]
            or row["lifecycle"] != "active"
            or row["availability"] != "available"
        ):
            return False
        privacy = parse_effective_privacy_json(row["effective_privacy_json"])
        if authority is not None:
            from .consent_contracts import EgressMode

            if not authority.permits_space(row["space_id"]) or not authority.permits_read_tier(
                privacy.tier
            ) or (
                authority.egress_mode is EgressMode.EXTERNAL_PROVIDER
                and not privacy.authority.external_egress
            ):
                return False
    return True
