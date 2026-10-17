"""Owner-approved durable intake over shared lifecycle and real captures.

The successor catalog admits this seam at schema20. Provider mappings remain
inactive; this module neither parses archives nor accepts caller authority.
"""

from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from open_brain_engine.core.models import CaptureWhyOrigin, ContentOrigin, Provenance

from .capture_lifecycle import open_lifecycle_journal
from .capture_lifecycle_contracts import _uuid4, canonical_bytes
from .capture_recovery import CaptureReservationIdentity
from .consumer_contracts import build_conversation, conversation_key
from .consumer_intents import PreparedConsumerConversation
from .consumer_journal import LifecycleJournalV2, consumer_items
from .consumer_operations import PreparedConsumerOperation, segment_delivery_id
from .consumer_plans import ConsumerImportPlan
from .consumer_profiles import (
    ConsumerIntakeError,
    ConsumerProfileRecord,
    consumer_capture_context,
    require_active_consumer_profile,
)
from .consumer_segments import ConsumerSegmentEnvelope, ConsumerSourceBinding
from .consumer_sources import (
    consumer_namespace_bytes,
    read_consumer_source_binding,
    require_consumer_source_preimages,
)
from .contracts import CaptureFault, CaptureSubmission, TextPayload, owner_privacy_for_tier
from .normalization import _timestamp
from .paging import read_snapshot
from .t03_contracts import EffectiveAuthority

if TYPE_CHECKING:
    from .local import BrainEngine


def _successor(connection: sqlite3.Connection) -> LifecycleJournalV2:
    journal = open_lifecycle_journal(connection)
    if not isinstance(journal, LifecycleJournalV2):
        raise ConsumerIntakeError("operation_pending")
    return journal


def _load(journal: LifecycleJournalV2, operation_id: str) -> PreparedConsumerOperation | None:
    row = journal.connection.execute(
        "SELECT * FROM consumer_approvals WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None

    def fetch(key: str) -> bytes:
        value = journal.connection.execute(
            "SELECT object_bytes FROM consumer_intent_objects WHERE sha256=?",
            (key,),
        ).fetchone()
        if value is None:
            raise ConsumerIntakeError("invalid_evidence")
        return bytes(value[0])

    prepared = PreparedConsumerOperation.from_loader(fetch(row["root_sha256"]), fetch)
    request = prepared.request
    if (
        prepared.intent_sha256 != row["root_sha256"]
        or request.operation_id != operation_id
        or request.canonical_bytes != bytes(row["request_bytes"])
        or request.request_sha256 != row["request_sha256"]
        or journal._state().requests.get(operation_id) != request
    ):
        raise ConsumerIntakeError("invalid_evidence")
    for key, (kind, raw) in prepared.inventory().items():
        actual = journal.connection.execute(
            "SELECT kind,object_bytes FROM consumer_intent_objects WHERE sha256=?",
            (key,),
        ).fetchone()
        if actual is None or tuple(actual) != (kind, raw):
            raise ConsumerIntakeError("invalid_evidence")
    expected_bindings = []
    for item in prepared.items:
        if item["planned_outcome"] not in {"new", "replace_current"}:
            continue
        conversation = prepared.conversation(item)
        assert conversation is not None
        for segment in conversation.segments:
            expected_bindings.append(
                (
                    segment.identities.capture_id,
                    operation_id,
                    segment.conversation_key,
                    segment.conversation_revision_sha256,
                    segment.segment_index,
                    segment.source.source_id,
                    consumer_namespace_bytes(segment.source),
                    canonical_bytes(segment.source.value()),
                    segment.normalized_sha256,
                    sha256(segment.to_bytes()).hexdigest(),
                )
            )
    actual_bindings = [
        tuple(row)
        for row in journal.connection.execute(
            "SELECT * FROM consumer_segment_bindings WHERE operation_id=? ORDER BY capture_id",
            (operation_id,),
        )
    ]
    if actual_bindings != sorted(expected_bindings):
        raise ConsumerIntakeError("invalid_evidence")
    return prepared


def _check_scope(journal: LifecycleJournalV2, prepared: PreparedConsumerOperation) -> bool:
    scope, plan = prepared.scope, prepared.plan
    row = journal.connection.execute(
        "SELECT * FROM consumer_scopes WHERE scope_id=? OR (provider=? AND label=?)",
        (scope["scope_id"], plan.provider, scope["label"]),
    ).fetchone()
    if row is None:
        if not scope["proposed"]:
            raise ConsumerIntakeError("scope_conflict")
    elif (row["scope_id"], row["provider"], row["label"], row["namespace_id"]) != (
        scope["scope_id"],
        plan.provider,
        scope["label"],
        scope["namespace_id"],
    ):
        raise ConsumerIntakeError("scope_conflict")
    archive = journal.connection.execute(
        "SELECT scope_id FROM consumer_archive_bindings WHERE provider=? AND archive_sha256=?",
        (plan.provider, plan.archive_sha256),
    ).fetchone()
    if archive is not None and archive[0] != scope["scope_id"]:
        raise ConsumerIntakeError("scope_conflict")
    return row is not None


def _publish_scope(
    journal: LifecycleJournalV2,
    prepared: PreparedConsumerOperation,
    *,
    admission: bool,
) -> None:
    exists = _check_scope(journal, prepared)
    scope, plan = prepared.scope, prepared.plan
    if not exists:
        if not admission:
            return
        journal.connection.execute(
            "INSERT INTO consumer_scopes VALUES(?,?,?,?,?)",
            (
                scope["scope_id"],
                plan.provider,
                scope["label"],
                scope["namespace_id"],
                plan.operation_id,
            ),
        )
    journal.connection.execute(
        "INSERT OR IGNORE INTO consumer_archive_bindings VALUES(?,?,?,?)",
        (plan.provider, plan.archive_sha256, scope["scope_id"], plan.operation_id),
    )


def _sync_outcomes(journal: LifecycleJournalV2) -> None:
    """A checked projection; the shared events remain the outcome authority."""
    for operation, request in journal._state().requests.items():
        if (
            journal.connection.execute(
                "SELECT 1 FROM consumer_approvals WHERE operation_id=?",
                (operation,),
            ).fetchone()
            is None
        ):
            continue
        ordinals = {
            item.group_key: index for index, item in enumerate(consumer_items(request) or ())
        }
        for item in journal.conversation_outcomes(operation):
            if item["outcome"] == "pending":
                continue
            raw = canonical_bytes({"operation_id": operation, **item})
            expected = (
                operation,
                item["group_key"],
                ordinals[cast(str, item["group_key"])],
                item["outcome"],
                raw,
                sha256(raw).hexdigest(),
            )
            prior = journal.connection.execute(
                "SELECT * FROM consumer_outcomes WHERE operation_id=? AND group_key=?",
                (operation, item["group_key"]),
            ).fetchone()
            if prior is not None and tuple(prior) != expected:
                raise ConsumerIntakeError("invalid_evidence")
            if prior is None:
                journal.connection.execute(
                    "INSERT INTO consumer_outcomes VALUES(?,?,?,?,?,?)", expected
                )


class ConsumerIntakeTasks:
    def __init__(self, engine: BrainEngine) -> None:
        self._engine = engine

    def _owner(self, authority: EffectiveAuthority) -> None:
        self._engine.lifecycle._owner(authority)
        if authority.space_ids is not None:
            raise ConsumerIntakeError("invalid_arguments")

    @contextmanager
    def _transaction(self, authority: EffectiveAuthority) -> Iterator[LifecycleJournalV2]:
        self._owner(authority)
        with self._engine.lifecycle._transaction(authority) as journal:
            if not isinstance(journal, LifecycleJournalV2):
                raise ConsumerIntakeError("operation_pending")
            yield journal

    def _accepted(
        self, plan: ConsumerImportPlan, authority: EffectiveAuthority
    ) -> dict[str, object] | None:
        self._owner(authority)
        with read_snapshot(self._engine) as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] < 20:
                return None
            journal = _successor(connection)
            return self._reconcile(journal, plan)

    def _reconcile(
        self,
        journal: LifecycleJournalV2,
        plan: ConsumerImportPlan,
    ) -> dict[str, object] | None:
        prior = _load(journal, plan.operation_id)
        if prior is None:
            return None
        if prior.plan.input_sha256 != plan.input_sha256:
            raise ConsumerIntakeError("operation_conflict")
        prior.validate_profile(self._engine.profile)
        return self._status(journal, prior)

    @staticmethod
    def _active(prepared: PreparedConsumerOperation) -> None:
        plan = prepared.plan
        actual = require_active_consumer_profile(plan.provider, plan.profile_id)
        if actual.record_sha256 != prepared.value()["profile_sha256"]:
            raise ConsumerIntakeError("unsupported_profile")

    def _prepare(
        self,
        journal: LifecycleJournalV2,
        plan: ConsumerImportPlan,
        record: ConsumerProfileRecord,
        principal: str,
    ) -> PreparedConsumerOperation:
        connection = journal.connection
        if plan.scope_id is None:
            if connection.execute(
                "SELECT 1 FROM consumer_scopes WHERE provider=? AND label=?",
                (plan.provider, plan.new_scope_label),
            ).fetchone():
                raise ConsumerIntakeError("scope_conflict")
            scope = {
                "scope_id": "scope_" + str(uuid4()),
                "namespace_id": str(uuid4()),
                "label": plan.new_scope_label,
                "proposed": True,
            }
        else:
            row = connection.execute(
                "SELECT * FROM consumer_scopes WHERE scope_id=? AND provider=?",
                (plan.scope_id, plan.provider),
            ).fetchone()
            if row is None:
                raise ConsumerIntakeError("scope_conflict")
            scope = {
                "scope_id": row["scope_id"],
                "namespace_id": row["namespace_id"],
                "label": row["label"],
                "proposed": False,
            }
        known = connection.execute(
            "SELECT scope_id FROM consumer_archive_bindings WHERE provider=? AND archive_sha256=?",
            (plan.provider, plan.archive_sha256),
        ).fetchone()
        if known is not None and known[0] != scope["scope_id"]:
            raise ConsumerIntakeError("scope_conflict")
        control = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
        state, items = journal._state(), []
        payloads = {
            **plan.objects(),
            plan.input_sha256: plan.to_bytes(),
            record.record_sha256: record.to_bytes(),
        }
        context, engine = consumer_capture_context(self._engine.profile), self._engine
        namespace = cast(str, scope["namespace_id"])
        for raw_input in plan.conversations:
            key = conversation_key(plan.provider, namespace, raw_input.provider_conversation_id)
            expected, prepared = state.current.get(key), None
            revision, reference = None, None
            if not raw_input.messages:
                outcome = "no_text"
            else:
                submissions: list[CaptureSubmission] = []

                def factory(
                    index: int,
                    body: str,
                    source: str,
                    title: str,
                    submissions: list[CaptureSubmission] = submissions,
                ) -> CaptureSubmission:
                    submission = CaptureSubmission.for_public_job(
                        context=context,
                        payload=TextPayload(body),
                        delivery_id=f"consumer.pending.{plan.operation_id}.{index}",
                        source_origin=ContentOrigin.THIRD_PARTY,
                        source_reference=source,
                        provenance=Provenance.create(
                            source_ref=source,
                            content_origin=ContentOrigin.THIRD_PARTY,
                            owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
                        ),
                        privacy=owner_privacy_for_tier(plan.privacy_tier),
                        title=title,
                    )
                    submissions.append(submission)
                    return submission

                normalized = build_conversation(
                    provider=plan.provider,
                    profile_id=plan.profile_id,
                    import_namespace_id=namespace,
                    provider_conversation_id=raw_input.provider_conversation_id,
                    title=raw_input.title,
                    messages=[
                        {
                            "provider_message_id": m.provider_message_id,
                            "role": m.role,
                            "text": m.text,
                            "occurred_at": m.occurred_at,
                        }
                        for m in raw_input.messages
                    ],
                    omissions=raw_input.omissions,
                    submission_factory=factory,
                )
                revision = normalized.conversation_revision_sha256
                segments = []
                for index, descriptor in enumerate(normalized.segments):
                    submission = replace(
                        submissions[index],
                        delivery_id=segment_delivery_id(
                            journal.brain_id,
                            plan.operation_id,
                            key,
                            revision,
                            index,
                            descriptor.capture_request_sha256,
                        ),
                    )
                    envelope = engine._prepare_journal_submission(submission, _lifecycle=True)
                    source = read_consumer_source_binding(
                        connection,
                        namespace_id=namespace,
                        conversation_key=key,
                        segment_key=descriptor.segment_key,
                    ) or ConsumerSourceBinding(
                        namespace,
                        key,
                        descriptor.segment_key,
                        "source_" + str(uuid4()),
                        None,
                        0,
                        0,
                        0,
                        "active",
                        "available",
                        None,
                        None,
                        control,
                    )
                    segments.append(
                        ConsumerSegmentEnvelope(
                            key,
                            revision,
                            sha256(normalized.to_bytes()).hexdigest(),
                            index,
                            descriptor.segment_key,
                            envelope,
                            CaptureReservationIdentity.allocate(
                                canonical=False,
                                accepted_at=_timestamp(engine._clock()),
                            ),
                            source,
                        )
                    )
                prepared = PreparedConsumerConversation(
                    record,
                    normalized,
                    raw_input.provider_conversation_id.encode().hex(),
                    tuple(m.provider_message_id.encode().hex() for m in raw_input.messages),
                    tuple(segments),
                )
                prepared.validate_profile(engine.profile)
                payloads.update(prepared.objects())
                payloads[prepared.intent_sha256] = prepared.to_bytes()
                if key in state.barriers:
                    outcome = "retracted"
                elif any(
                    s.source.lifecycle != "active" or s.source.availability != "available"
                    for s in segments
                ):
                    outcome = "conflict"
                elif expected == revision:
                    outcome, reference = "unchanged", journal.activation_receipt(key, revision)
                    current = state.revisions[key, revision]
                    if (
                        reference is None
                        or len(current.segments) != len(segments)
                        or any(
                            connection.execute(
                                "SELECT privacy_json FROM captures WHERE capture_id=?",
                                (old.capture_id,),
                            ).fetchone()[0]
                            != canonical_bytes(new.envelope.admitted_privacy.to_dict()).decode()
                            for old, new in zip(current.segments, segments, strict=True)
                        )
                    ):
                        outcome, reference = "conflict", None
                elif expected is None:
                    outcome = "new"
                elif (key, revision) in state.revisions:
                    # A retained historical revision cannot acquire fresh capture
                    # identities or membership through an importer replay.
                    outcome = "conflict"
                else:
                    outcome = "replace_current" if plan.replace_current else "conflict"
                    # A preexisting owner group cannot be adopted as consumer identity.
                    current = state.revisions[key, expected]
                    if any(
                        connection.execute(
                            "SELECT 1 FROM consumer_segment_bindings WHERE capture_id=?",
                            (s.capture_id,),
                        ).fetchone()
                        is None
                        for s in current.segments
                    ):
                        outcome = "conflict"
            items.append(
                {
                    "item_token": "citem_" + str(uuid4()),
                    "group_key": key,
                    "revision_sha256": revision,
                    "planned_outcome": outcome,
                    "reference_receipt_sha256": reference,
                    "intent_sha256": None if prepared is None else prepared.intent_sha256,
                    "input_sha256": raw_input.input_sha256,
                    "expected_revision_sha256": expected,
                }
            )
        return PreparedConsumerOperation.from_values(
            {
                "contract": "ob.consumer-operation-intent.v1",
                "operation_id": plan.operation_id,
                "brain_id": journal.brain_id,
                "issuer_epoch": journal.issuer_epoch,
                "principal_id": principal,
                "provider": plan.provider,
                "profile_sha256": record.record_sha256,
                "input_sha256": plan.input_sha256,
                "scope": scope,
                "archive_sha256": plan.archive_sha256,
                "privacy_tier": plan.privacy_tier.value,
                "replace_current": plan.replace_current,
                "session_id": "capsess_" + str(uuid4()),
                "eligibility_generation": journal.eligibility_generation,
                "control_epoch": control,
                "items": sorted(items, key=lambda item: cast(str, item["group_key"])),
            },
            payloads,
        )

    @staticmethod
    def _preview_value(
        prepared: PreparedConsumerOperation, identity: str, expires: str
    ) -> dict[str, object]:
        counts = dict.fromkeys(
            (
                "conversations",
                "new",
                "unchanged",
                "changed",
                "conflict",
                "retracted",
                "no_text",
                "segments",
                "omitted_parts",
            ),
            0,
        )
        items = []
        pages = 0
        for item in prepared.items:
            conversation = prepared.conversation(item)
            outcome = cast(str, item["planned_outcome"])
            public_outcome = "changed" if outcome == "replace_current" else outcome
            omissions = (
                dict(conversation.normalized.omissions)
                if conversation is not None
                else dict(
                    next(
                        c.omissions
                        for c in prepared.plan.conversations
                        if c.input_sha256 == item["input_sha256"]
                    )
                )
            )
            segments = 0 if conversation is None else len(conversation.segments)
            counts["conversations"] += 1
            counts[public_outcome] += 1
            counts["segments"] += segments
            counts["omitted_parts"] += sum(omissions.values())
            pages += segments
            items.append(
                {
                    "item_token": item["item_token"],
                    "outcome": public_outcome,
                    "segments": segments,
                    "omissions": omissions,
                }
            )
        items.sort(key=lambda item: item["outcome"] not in {"conflict", "retracted"})
        plan = prepared.plan
        return {
            "dto_version": 1,
            "status": "preview",
            "preview_id": identity,
            "expires_at": expires,
            "provider": plan.provider,
            "profile_id": plan.profile_id,
            "account_scope_id": prepared.scope["scope_id"],
            "counts": counts,
            "items": items[:100],
            "items_omitted": max(0, len(items) - 100),
            "body_pages": pages,
            "history_retained": True,
        }

    def _preview(
        self,
        journal: LifecycleJournalV2,
        identity: str,
        authority: EffectiveAuthority,
    ) -> tuple[PreparedConsumerOperation, str]:
        row = journal.connection.execute(
            "SELECT * FROM consumer_previews WHERE preview_id=? AND principal_id=?",
            (identity, authority.principal_id),
        ).fetchone()
        if row is None:
            raise ConsumerIntakeError("preview_not_found")
        if row["expires_at"] <= _timestamp(self._engine._clock()):
            raise ConsumerIntakeError("preview_expired")
        records = {
            key: (kind, bytes(raw))
            for key, kind, raw in journal.connection.execute(
                "SELECT sha256,kind,object_bytes FROM consumer_preview_objects WHERE preview_id=?",
                (identity,),
            )
        }
        objects = {key: raw for key, (_, raw) in records.items()}
        root = bytes(row["root_bytes"])
        if objects.pop(row["root_sha256"], None) != root:
            raise ConsumerIntakeError("invalid_evidence")
        prepared = PreparedConsumerOperation(root, objects)
        if records != prepared.inventory():
            raise ConsumerIntakeError("invalid_evidence")
        if (
            prepared.intent_sha256,
            prepared.request.operation_id,
            prepared.plan.input_sha256,
            prepared.request.brain_id,
            prepared.request.issuer_epoch,
        ) != (
            row["root_sha256"],
            row["operation_id"],
            row["input_sha256"],
            row["brain_id"],
            row["issuer_epoch"],
        ) or (row["brain_id"], row["issuer_epoch"]) != (journal.brain_id, journal.issuer_epoch):
            raise ConsumerIntakeError("preview_changed")
        prepared.validate_profile(self._engine.profile)
        return prepared, row["expires_at"]

    def preview_consumer_import(
        self,
        plan: ConsumerImportPlan,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        if type(plan) is not ConsumerImportPlan:
            raise ConsumerIntakeError()
        prior = self._accepted(plan, authority)
        if prior is not None:
            return prior
        record = require_active_consumer_profile(plan.provider, plan.profile_id)
        with self._transaction(authority) as journal:
            prior = self._reconcile(journal, plan)
            if prior is not None:
                return prior
            prior_preview = journal.connection.execute(
                "SELECT preview_id,input_sha256,expires_at FROM consumer_previews "
                "WHERE operation_id=?",
                (plan.operation_id,),
            ).fetchone()
            if prior_preview is not None:
                if prior_preview["input_sha256"] != plan.input_sha256:
                    raise ConsumerIntakeError("operation_conflict")
                if prior_preview["expires_at"] > _timestamp(self._engine._clock()):
                    prepared, expires = self._preview(
                        journal, prior_preview["preview_id"], authority
                    )
                    return self._preview_value(prepared, prior_preview["preview_id"], expires)
            now = self._engine._clock()
            journal.connection.execute(
                "DELETE FROM consumer_previews WHERE expires_at<=?", (_timestamp(now),)
            )
            if (
                journal.connection.execute(
                    "SELECT count(*) FROM consumer_previews WHERE principal_id=?",
                    (authority.principal_id,),
                ).fetchone()[0]
                >= 16
            ):
                raise ConsumerIntakeError("operation_pending")
            prepared = self._prepare(journal, plan, record, authority.principal_id)
            self._engine._refuse_on_storage_watermark()
            identity, expires = (
                "chatprev_" + secrets.token_urlsafe(32),
                _timestamp(now + timedelta(hours=1)),
            )
            journal.connection.execute(
                "INSERT INTO consumer_previews VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    plan.operation_id,
                    authority.principal_id,
                    journal.brain_id,
                    journal.issuer_epoch,
                    _timestamp(now),
                    expires,
                    plan.input_sha256,
                    prepared.intent_sha256,
                    prepared.raw,
                ),
            )
            for key, (kind, raw) in prepared.inventory().items():
                journal.connection.execute(
                    "INSERT INTO consumer_preview_objects VALUES(?,?,?,?)",
                    (identity, key, kind, raw),
                )
            return self._preview_value(prepared, identity, expires)

    def consumer_import_preview_body(
        self,
        preview_id: str,
        body_page: int,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        self._owner(authority)
        if type(body_page) is not int or body_page < 0:
            raise ConsumerIntakeError()
        with read_snapshot(self._engine) as connection:
            prepared, _ = self._preview(_successor(connection), preview_id, authority)
            pages = [
                (item, conversation, segment)
                for item in prepared.items
                if (conversation := prepared.conversation(item)) is not None
                for segment in conversation.segments
            ]
            if body_page >= len(pages):
                raise ConsumerIntakeError()
            item, conversation, segment = pages[body_page]
            value = {
                "dto_version": 1,
                "status": "preview_body",
                "preview_id": preview_id,
                "body_page": body_page,
                "body_pages": len(pages),
                "item_token": item["item_token"],
                "segment_index": segment.segment_index,
                "title": conversation.normalized.title,
                "body": segment.body.decode(),
                "omissions": dict(conversation.normalized.omissions),
                "next_body_page": body_page + 1 if body_page + 1 < len(pages) else None,
            }
            if len(canonical_bytes(value)) > 1024 * 1024:
                raise ConsumerIntakeError("conversation_limit")
            return value

    def commit_consumer_import(
        self,
        preview_id: str,
        operation_id: str,
        rebuilt_plan: ConsumerImportPlan,
        *,
        authority: EffectiveAuthority,
    ) -> dict[str, object]:
        if (
            type(rebuilt_plan) is not ConsumerImportPlan
            or operation_id != rebuilt_plan.operation_id
        ):
            raise ConsumerIntakeError()
        prior = self._accepted(rebuilt_plan, authority)
        if prior is not None:
            return prior
        require_active_consumer_profile(rebuilt_plan.provider, rebuilt_plan.profile_id)
        with self._transaction(authority) as journal:
            prior = self._reconcile(journal, rebuilt_plan)
            if prior is not None:
                return prior
            prepared, _ = self._preview(journal, preview_id, authority)
            if prepared.plan.input_sha256 != rebuilt_plan.input_sha256:
                raise ConsumerIntakeError("preview_changed")
            self._active(prepared)
            _check_scope(journal, prepared)
            if prepared.request.issuer_epoch != journal.issuer_epoch:
                raise ConsumerIntakeError("revision_changed")
            for item in prepared.items:
                if (
                    journal._state().current.get(cast(str, item["group_key"]))
                    != item["expected_revision_sha256"]
                ):
                    raise ConsumerIntakeError("revision_changed")
                conversation = prepared.conversation(item)
                if conversation is not None and item["planned_outcome"] in {
                    "new",
                    "replace_current",
                    "unchanged",
                }:
                    require_consumer_source_preimages(journal.connection, conversation.segments)
            request = prepared.request
            journal.reserve(request)
            for key, (kind, raw) in prepared.inventory().items():
                prior_object = journal.connection.execute(
                    "SELECT kind,object_bytes FROM consumer_intent_objects WHERE sha256=?",
                    (key,),
                ).fetchone()
                if prior_object is not None and tuple(prior_object) != (kind, raw):
                    raise ConsumerIntakeError("invalid_evidence")
                journal.connection.execute(
                    "INSERT OR IGNORE INTO consumer_intent_objects VALUES(?,?,?)", (key, kind, raw)
                )
            journal.connection.execute(
                "INSERT INTO consumer_approvals VALUES(?,?,?,?)",
                (
                    operation_id,
                    prepared.intent_sha256,
                    request.canonical_bytes,
                    request.request_sha256,
                ),
            )
            for group in journal._state().intended_groups(operation_id).values():
                journal.stage_group(operation_id, group)
            for item in prepared.items:
                if item["planned_outcome"] not in {"new", "replace_current"}:
                    continue
                conversation = prepared.conversation(item)
                assert conversation is not None
                for segment in conversation.segments:
                    journal.connection.execute(
                        "INSERT INTO consumer_segment_bindings VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            segment.identities.capture_id,
                            operation_id,
                            segment.conversation_key,
                            segment.conversation_revision_sha256,
                            segment.segment_index,
                            segment.source.source_id,
                            consumer_namespace_bytes(segment.source),
                            canonical_bytes(segment.source.value()),
                            segment.normalized_sha256,
                            sha256(segment.to_bytes()).hexdigest(),
                        ),
                    )
        self._engine._fault(CaptureFault.AFTER_CONSUMER_INTENT)
        return self.recover_consumer_import(operation_id, authority=authority)

    def recover_consumer_import(
        self, operation_id: str, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _uuid4(operation_id)
        self._owner(authority)
        with read_snapshot(self._engine) as connection:
            journal = _successor(connection)
            prepared = _load(journal, operation_id)
            if prepared is None:
                raise ConsumerIntakeError("operation_not_found")
            prepared.validate_profile(self._engine.profile)
            if journal.status(operation_id)["status"] in {"committed", "halted"}:
                return self._status(journal, prepared)
            if prepared.request.issuer_epoch != journal.issuer_epoch:
                raise ConsumerIntakeError("revision_changed")
        self._active(prepared)
        for item in prepared.items:
            key = cast(str, item["group_key"])
            with self._transaction(authority) as journal:
                if journal.status(operation_id)["status"] in {"committed", "halted"}:
                    return self._status(journal, prepared)
                if (operation_id, key) in journal._state().outcomes:
                    continue
                _check_scope(journal, prepared)
                if item["planned_outcome"] not in {"new", "replace_current"}:
                    if item["planned_outcome"] == "unchanged":
                        conversation = prepared.conversation(item)
                        assert conversation is not None
                        require_consumer_source_preimages(journal.connection, conversation.segments)
                    journal.record_outcome(operation_id, key, cast(str, item["planned_outcome"]))
                    _publish_scope(journal, prepared, admission=False)
                    _sync_outcomes(journal)
                    continue
                if key in journal._state().barriers:
                    journal.record_outcome(operation_id, key, "retracted")
                    _sync_outcomes(journal)
                    continue
                conversation = prepared.conversation(item)
                assert conversation is not None
                require_consumer_source_preimages(journal.connection, conversation.segments)
            for segment in conversation.segments:
                self._engine.lifecycle._stage_consumer_segment(
                    prepared.request, key, segment, authority=authority
                )
                self._engine._fault(CaptureFault.AFTER_CONSUMER_SEGMENT)
            self._engine._fault(CaptureFault.CONSUMER_READY)
            with self._transaction(authority) as journal:
                if journal.status(operation_id)["status"] in {"committed", "halted"}:
                    return self._status(journal, prepared)
                if key in journal._state().barriers:
                    journal.record_outcome(operation_id, key, "retracted")
                else:
                    _publish_scope(journal, prepared, admission=True)
                    self._engine.lifecycle._activate_group_in_transaction(
                        journal,
                        prepared.request,
                        key,
                        session_id=cast(str, prepared.value()["session_id"]),
                    )
                _sync_outcomes(journal)
            self._engine._fault(CaptureFault.AFTER_CONSUMER_ACTIVATION)
        with self._transaction(authority) as journal:
            if journal.status(operation_id)["status"] not in {"committed", "halted"}:
                journal.finish(operation_id)
            _sync_outcomes(journal)
            return self._status(journal, prepared)

    @staticmethod
    def _status(
        journal: LifecycleJournalV2, prepared: PreparedConsumerOperation
    ) -> dict[str, object]:
        operation = prepared.request.operation_id
        status = journal.status(operation)
        outcomes = {item["group_key"]: item for item in journal.conversation_outcomes(operation)}
        counts = dict.fromkeys(
            (
                "conversations",
                "committed",
                "unchanged",
                "conflict",
                "retracted",
                "no_text",
                "halted",
                "pending",
                "segments_materialized",
            ),
            0,
        )
        items = []
        for item in prepared.items:
            outcome = outcomes.get(
                item["group_key"], {"outcome": "pending", "receipt_sha256": None}
            )
            counts["conversations"] += 1
            counts[cast(str, outcome["outcome"])] += 1
            receipt = outcome["receipt_sha256"]
            items.append(
                {
                    "item_token": item["item_token"],
                    "outcome": outcome["outcome"],
                    "receipt_id": None if receipt is None else "creceipt_" + cast(str, receipt),
                }
            )
        counts["segments_materialized"] = journal.connection.execute(
            "SELECT count(*) FROM consumer_segment_bindings b JOIN captures c USING(capture_id) "
            "WHERE b.operation_id=? AND c.stage=3",
            (operation,),
        ).fetchone()[0]
        total = journal.connection.execute(
            "SELECT count(*) FROM consumer_segment_bindings WHERE operation_id=?",
            (operation,),
        ).fetchone()[0]
        current = status["status"]
        if current not in {"committed", "halted"}:
            current = (
                "ready"
                if total and counts["segments_materialized"] == total
                else "materializing"
                if counts["segments_materialized"]
                else current
            )
        items.sort(key=lambda item: item["outcome"] not in {"conflict", "retracted", "halted"})
        return {
            "dto_version": 1,
            "status": current,
            "operation_id": operation,
            "session_id": status["session_id"],
            "counts": counts,
            "items": items[:100],
            "items_omitted": max(0, len(items) - 100),
            "history_retained": True,
        }

    def consumer_import_status(
        self, operation_id: str, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _uuid4(operation_id)
        self._owner(authority)
        with read_snapshot(self._engine) as connection:
            journal = _successor(connection)
            prepared = _load(journal, operation_id)
            if prepared is None:
                raise ConsumerIntakeError("operation_not_found")
            prepared.validate_profile(self._engine.profile)
            return self._status(journal, prepared)

    def halt_consumer_import(
        self, operation_id: str, *, authority: EffectiveAuthority
    ) -> dict[str, object]:
        _uuid4(operation_id)
        with self._transaction(authority) as journal:
            prepared = _load(journal, operation_id)
            if prepared is None:
                raise ConsumerIntakeError("operation_not_found")
            prepared.validate_profile(self._engine.profile)
            journal.halt(operation_id)
            _sync_outcomes(journal)
            return self._status(journal, prepared)
