"""Atomic, pure transition planning for Brain Protocol v1."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Literal, cast

from open_brain_engine.protocol.freeze import RESOURCE_LIMITS
from open_brain_engine.protocol.identifiers import validate_identifier

from .model import (
    ActiveLabelSetSnapshot,
    Artifact,
    BrainState,
    Commit,
    CommitBatch,
    Decision,
    DeliveryBinding,
    Effect,
    EffectReceipt,
    LedgerItem,
    LedgerReceipt,
    Proposal,
    Purge,
    PurgeTransition,
    Record,
    ResourceLimitError,
    Revision,
    lineage_identifier,
    processor_output_identity,
)
from .policy import propagated_compartments

BatchLimitError = ResourceLimitError


class LedgerTransitionError(ValueError):
    """A batch violates a frozen semantic invariant."""


class RevisionConflict(LedgerTransitionError):
    """A decision did not name the current proposal revision."""

    def __init__(
        self,
        proposal_id: str,
        expected_revision_id: str,
        actual_revision_id: str | None,
    ) -> None:
        self.proposal_id = proposal_id
        self.expected_revision_id = expected_revision_id
        self.actual_revision_id = actual_revision_id
        super().__init__(
            f"revision conflict for {proposal_id}: expected {expected_revision_id}, "
            f"actual {actual_revision_id}"
        )


@dataclass(frozen=True, slots=True)
class ReplayResult:
    receipt: LedgerReceipt
    kind: Literal["replayed"] = "replayed"


@dataclass(frozen=True, slots=True)
class DeliveryPurged:
    brain_id: str
    delivery_id: str
    kind: Literal["delivery_purged"] = "delivery_purged"
    code: Literal["delivery_purged"] = "delivery_purged"


@dataclass(frozen=True, slots=True)
class DigestConflict:
    brain_id: str
    delivery_id: str
    kind: Literal["conflict"] = "conflict"
    code: Literal["digest_conflict"] = "digest_conflict"


DeliveryResolution = ReplayResult | DeliveryPurged | DigestConflict | None


@dataclass(frozen=True, slots=True)
class BatchApplication:
    """A validated, immutable plan; persistence and cursor allocation happen in W2."""

    base_state: BrainState
    batch: CommitBatch
    active_label_sets: ActiveLabelSetSnapshot
    artifacts: Mapping[str, Artifact]
    proposal_heads: Mapping[str, str]
    artifact_heads: Mapping[str, str]
    origin_heads: Mapping[str, str]
    effects: Mapping[str, Effect]
    purges: Mapping[str, Purge]
    superseded_record_ids: frozenset[str]
    suppressed_ids: frozenset[str]
    tombstoned_ids: frozenset[str]
    purge_pending_ids: frozenset[str]

    def __post_init__(self) -> None:
        for name in (
            "artifacts",
            "proposal_heads",
            "artifact_heads",
            "origin_heads",
            "effects",
            "purges",
        ):
            object.__setattr__(self, name, MappingProxyType(dict(getattr(self, name))))
        for name in (
            "superseded_record_ids",
            "suppressed_ids",
            "tombstoned_ids",
            "purge_pending_ids",
        ):
            object.__setattr__(self, name, frozenset(getattr(self, name)))

    @property
    def new_records(self) -> tuple[Record, ...]:
        return tuple(item for item in self.batch.items if isinstance(item, Record))

    def bind_commit(self, commit: Commit) -> Commit:
        """Validate the exact W2 commit materialization for this ordered batch."""
        batch = _validated_batch(self.batch)
        try:
            validated = replace(commit)
        except (AttributeError, TypeError, ValueError) as error:
            raise LedgerTransitionError("commit materialization is invalid") from error
        if (
            validated.brain_id != batch.brain_id
            or validated.delivery_id != batch.delivery_id
            or validated.digest != batch.digest
            or validated.issuer_epoch != batch.issuer_epoch
            or validated.policy_digest != batch.policy_digest
            or validated.sequencer_epoch != batch.sequencer_epoch
            or validated.item_refs != batch.item_refs
        ):
            raise LedgerTransitionError(
                "commit materialization does not exactly bind the validated batch"
            )
        return validated

    def to_state(self, accepted_records: Iterable[Record] = ()) -> BrainState:
        """Materialize the pure plan after W2 supplies accepted encrypted record values."""
        accepted = {record.record_id: record for record in accepted_records}
        expected = {record.record_id: record for record in self.new_records}
        if set(accepted) != set(expected):
            raise LedgerTransitionError("accepted record set does not match the validated batch")
        for record_id, stored in accepted.items():
            pending = expected[record_id]
            if stored.ciphertext_state == "pending" or not _same_record_semantics(pending, stored):
                raise LedgerTransitionError("accepted record does not match validated input")
        records = {**self.base_state.records, **accepted}
        revisions = dict(self.base_state.revisions)
        proposals = dict(self.base_state.proposals)
        decisions = dict(self.base_state.decisions)
        for item in self.batch.items:
            if isinstance(item, Revision):
                revisions[item.revision_id] = item
            elif isinstance(item, Proposal):
                proposals[item.proposal_revision_id] = item
            elif isinstance(item, Decision):
                decisions[item.decision_id] = item
        return BrainState(
            brain_id=self.base_state.brain_id,
            records=records,
            artifacts=self.artifacts,
            revisions=revisions,
            proposals=proposals,
            proposal_heads=self.proposal_heads,
            decisions=decisions,
            effects=self.effects,
            purges=self.purges,
            commits=self.base_state.commits,
            receipts=self.base_state.receipts,
            deliveries=self.base_state.deliveries,
            artifact_heads=self.artifact_heads,
            origin_heads=self.origin_heads,
            superseded_record_ids=self.superseded_record_ids,
            suppressed_ids=self.suppressed_ids,
            tombstoned_ids=self.tombstoned_ids,
            purge_pending_ids=self.purge_pending_ids,
            purged_deliveries=self.base_state.purged_deliveries,
        )


CommitEvaluation = (
    BatchApplication | ReplayResult | DeliveryPurged | DigestConflict | RevisionConflict
)


def resolve_delivery(
    state: BrainState,
    brain_id: str,
    delivery_id: str,
    digest: str,
) -> DeliveryResolution:
    """Resolve one Brain-bound idempotency key without allocating a cursor."""
    state = _validated_state(state)
    validate_identifier(brain_id, role="brain")
    validate_identifier(delivery_id, role="delivery", brain_id=brain_id)
    _validate_digest(digest)
    if brain_id != state.brain_id:
        raise LedgerTransitionError("delivery lookup crosses Brain boundary")
    if delivery_id in state.purged_deliveries:
        return DeliveryPurged(brain_id, delivery_id)
    binding = state.deliveries.get(delivery_id)
    if binding is None:
        return None
    _validate_binding(binding)
    if binding.digest == digest:
        return ReplayResult(binding.receipt)
    return DigestConflict(brain_id, delivery_id)


def evaluate_batch(
    state: BrainState,
    batch: CommitBatch,
    active_label_sets: ActiveLabelSetSnapshot,
) -> CommitEvaluation:
    """Return closed replay/conflict variants or a complete validation plan."""
    resolution = resolve_delivery(state, batch.brain_id, batch.delivery_id, batch.digest)
    if resolution is not None:
        return resolution
    try:
        return apply_batch(state, batch, active_label_sets)
    except RevisionConflict as error:
        return error


def apply_batch(
    state: BrainState,
    batch: CommitBatch,
    active_label_sets: ActiveLabelSetSnapshot,
) -> BatchApplication:
    """Validate the whole proposed state before returning an immutable persistence plan."""
    state = _validated_state(state)
    batch = _validated_batch(batch)
    snapshot = _validated_snapshot(active_label_sets)
    if batch.brain_id != state.brain_id or snapshot.brain_id != state.brain_id:
        raise LedgerTransitionError("commit state, batch, and capacity snapshot must share a Brain")
    if resolve_delivery(state, batch.brain_id, batch.delivery_id, batch.digest) is not None:
        raise LedgerTransitionError("delivery is already resolved; use evaluate_batch")

    new = _partition_new_items(state, batch)
    records = {**state.records, **new.records}
    revisions = {**state.revisions, **new.revisions}
    proposals = {**state.proposals, **new.proposals}
    decisions = {**state.decisions, **new.decisions}
    provenance_items = (*state.provenance_items(), *new.provenance_items)
    parents, children = _validated_graph(provenance_items, records, revisions)

    purge_targets = {transition.target_record_id for transition in new.purge_transitions}
    forbidden = (
        frozenset(state.tombstoned_ids)
        | frozenset(state.purge_pending_ids)
        | purge_targets
    )
    _validate_new_provenance(new.provenance_items, records, revisions, parents, forbidden)
    _validate_processor_outputs(state.processor_output_items(), new.processor_output_items)

    artifacts = dict(state.artifacts)
    for proposal in new.proposals.values():
        artifacts.setdefault(
            proposal.artifact_id,
            Artifact(state.brain_id, proposal.artifact_id),
        )
    for revision in new.revisions.values():
        artifacts.setdefault(
            revision.artifact_id,
            Artifact(state.brain_id, revision.artifact_id),
        )
    proposal_heads = _validate_proposals(state, new.proposals, revisions)
    _validate_decisions(state, new.decisions, proposals, proposal_heads)
    artifact_heads = _validate_revisions(state, new.revisions, proposals, decisions)
    effects = _validate_effects(state, new.effect_receipts)

    origin_heads, superseded = _apply_supersession(state, new.records)
    suppressed = set(state.suppressed_ids)
    tombstoned = set(state.tombstoned_ids)
    purge_pending = set(state.purge_pending_ids)
    purges = _validate_purges(
        state,
        new.purge_transitions,
        records,
        revisions,
        proposals,
        decisions,
        effects,
        parents,
        children,
        suppressed,
        tombstoned,
        purge_pending,
    )
    post_snapshot = _post_batch_capacity(
        state,
        snapshot,
        new.records,
        superseded,
        suppressed,
        tombstoned,
        purge_pending,
    )
    return BatchApplication(
        base_state=state,
        batch=batch,
        active_label_sets=post_snapshot,
        artifacts=artifacts,
        proposal_heads=proposal_heads,
        artifact_heads=artifact_heads,
        origin_heads=origin_heads,
        effects=effects,
        purges=purges,
        superseded_record_ids=frozenset(superseded),
        suppressed_ids=frozenset(suppressed),
        tombstoned_ids=frozenset(tombstoned),
        purge_pending_ids=frozenset(purge_pending),
    )


@dataclass(frozen=True, slots=True)
class _NewItems:
    records: Mapping[str, Record]
    revisions: Mapping[str, Revision]
    proposals: Mapping[str, Proposal]
    decisions: Mapping[str, Decision]
    effect_receipts: Mapping[str, EffectReceipt]
    purge_transitions: tuple[PurgeTransition, ...]

    @property
    def provenance_items(self) -> tuple[Record | Revision | Proposal | EffectReceipt, ...]:
        return (
            *self.records.values(),
            *self.revisions.values(),
            *self.proposals.values(),
            *self.effect_receipts.values(),
        )

    @property
    def processor_output_items(
        self,
    ) -> tuple[Record | Revision | Proposal | EffectReceipt, ...]:
        return (
            *self.records.values(),
            *self.revisions.values(),
            *self.proposals.values(),
            *(
                receipt
                for receipt in self.effect_receipts.values()
                if receipt.reconciles_receipt_id is None
            ),
        )


def _partition_new_items(state: BrainState, batch: CommitBatch) -> _NewItems:
    records: dict[str, Record] = {}
    revisions: dict[str, Revision] = {}
    proposals: dict[str, Proposal] = {}
    decisions: dict[str, Decision] = {}
    effect_receipts: dict[str, EffectReceipt] = {}
    purges: list[PurgeTransition] = []
    purge_keys: set[tuple[str, str]] = set()
    known_receipts = {
        receipt.receipt_id for effect in state.effects.values() for receipt in effect.receipts
    }
    known_receipts.update(state.receipts)
    for item in batch.items:
        if isinstance(item, Record):
            if item.ciphertext_state != "pending":
                raise LedgerTransitionError("commit rejects already-encrypted record input")
            _insert_unique(records, item.record_id, item, state.records, "record")
        elif isinstance(item, Revision):
            _insert_unique(revisions, item.revision_id, item, state.revisions, "revision")
        elif isinstance(item, Proposal):
            _insert_unique(
                proposals,
                item.proposal_revision_id,
                item,
                state.proposals,
                "proposal revision",
            )
        elif isinstance(item, Decision):
            _insert_unique(decisions, item.decision_id, item, state.decisions, "decision")
        elif isinstance(item, EffectReceipt):
            if item.receipt_id in known_receipts:
                raise LedgerTransitionError("effect receipt identity is immutable")
            _insert_unique(
                effect_receipts,
                item.receipt_id,
                item,
                {},
                "effect receipt",
            )
        elif isinstance(item, PurgeTransition):
            key = (item.purge_id, item.subject_id)
            if key in purge_keys:
                raise LedgerTransitionError("purge subject already has a resolution")
            purge_keys.add(key)
            purges.append(item)
        else:  # pragma: no cover - the batch constructor rejects unknown items.
            raise LedgerTransitionError("unknown ledger item")
    if ({*state.revisions, *revisions}).intersection({*state.proposals, *proposals}):
        raise LedgerTransitionError(
            "revision and proposal-revision identities must be distinct"
        )
    return _NewItems(records, revisions, proposals, decisions, effect_receipts, tuple(purges))


def _insert_unique[K, V](
    pending: dict[K, V], key: K, value: V, existing: Mapping[K, object], label: str
) -> None:
    if key in pending or key in existing:
        raise LedgerTransitionError(f"{label} identity is immutable")
    pending[key] = value


def _validated_graph(
    items: Iterable[Record | Revision | Proposal | EffectReceipt],
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for item in items:
        child_id = lineage_identifier(item)
        parents.setdefault(child_id, set()).update(item.provenance.source_ids)
        for source_id in item.provenance.source_record_ids:
            if source_id not in records:
                raise LedgerTransitionError("provenance source record is unknown or cross-Brain")
        for source_id in item.provenance.source_revision_ids:
            if source_id not in revisions:
                raise LedgerTransitionError("provenance source revision is unknown or cross-Brain")
        for source_id in item.provenance.source_ids:
            children.setdefault(source_id, set()).add(child_id)
    visiting: set[str] = set()
    complete: set[str] = set()

    def visit(node: str) -> None:
        if node in complete:
            return
        if node in visiting:
            raise LedgerTransitionError("provenance graph contains a cycle")
        visiting.add(node)
        for parent in parents.get(node, ()):
            visit(parent)
        visiting.remove(node)
        complete.add(node)

    for node in sorted(parents):
        visit(node)
    return (
        {node: frozenset(values) for node, values in parents.items()},
        {node: frozenset(values) for node, values in children.items()},
    )


def _ancestors(entity_id: str, parents: Mapping[str, frozenset[str]]) -> frozenset[str]:
    queue = deque(sorted(parents.get(entity_id, ())))
    seen = {entity_id}
    result: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        result.add(current)
        queue.extend(sorted(parents.get(current, ())))
    return frozenset(result)


def _descendants(entity_id: str, children: Mapping[str, frozenset[str]]) -> frozenset[str]:
    queue = deque(sorted(children.get(entity_id, ())))
    seen = {entity_id}
    result: set[str] = set()
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        result.add(current)
        queue.extend(sorted(children.get(current, ())))
    return frozenset(result)


def _validate_new_provenance(
    items: Iterable[Record | Revision | Proposal | EffectReceipt],
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    parents: Mapping[str, frozenset[str]],
    forbidden: frozenset[str] | set[str],
) -> None:
    for item in items:
        sources = item.provenance.source_ids
        if not sources:
            continue
        ancestors = _ancestors(lineage_identifier(item), parents)
        blocked = ancestors.intersection(forbidden)
        if blocked:
            reason = "tombstoned or purge-pending"
            raise LedgerTransitionError(f"provenance names a {reason} ancestor")
        source_labels: list[tuple[str, ...]] = []
        for source_id in item.provenance.source_record_ids:
            source_labels.append(tuple(records[source_id].compartments))
        for source_id in item.provenance.source_revision_ids:
            source_labels.append(tuple(revisions[source_id].compartments))
        required = propagated_compartments(*source_labels)
        if tuple(item.compartments) != required:
            raise LedgerTransitionError("derived output compartments must equal provenance union")


def _validate_processor_outputs(
    existing: Iterable[Record | Revision | Proposal | EffectReceipt],
    new: Iterable[Record | Revision | Proposal | EffectReceipt],
) -> None:
    seen: set[str] = set()
    for item in existing:
        if item.provenance.derivation == "processor":
            identity = processor_output_identity(item.provenance)
            if identity in seen:
                raise LedgerTransitionError("authoritative state repeats a processor output")
            seen.add(identity)
    for item in new:
        if item.provenance.derivation == "processor":
            identity = processor_output_identity(item.provenance)
            if identity in seen:
                raise LedgerTransitionError("processor output identity already exists")
            seen.add(identity)


def _validate_proposals(
    state: BrainState,
    new: Mapping[str, Proposal],
    revisions: Mapping[str, Revision],
) -> dict[str, str]:
    heads = dict(state.proposal_heads)
    by_proposal: dict[str, list[Proposal]] = {}
    for proposal in new.values():
        by_proposal.setdefault(proposal.proposal_id, []).append(proposal)
        if proposal.base_revision_id is not None:
            base = revisions.get(proposal.base_revision_id)
            if base is None:
                raise LedgerTransitionError("proposal base revision is unknown")
            if base.artifact_id != proposal.artifact_id:
                raise LedgerTransitionError("proposal base belongs to another artifact")
    for proposal_id, versions in by_proposal.items():
        if len(versions) != 1:
            raise LedgerTransitionError("one batch cannot ambiguously advance a proposal twice")
        proposal = versions[0]
        prior_versions = [
            prior for prior in state.proposals.values() if prior.proposal_id == proposal_id
        ]
        if prior_versions and any(
            prior.artifact_id != proposal.artifact_id
            or prior.base_revision_id != proposal.base_revision_id
            or prior.content_schema != proposal.content_schema
            or prior.compartments != proposal.compartments
            or prior.provenance != proposal.provenance
            for prior in prior_versions
        ):
            raise LedgerTransitionError("proposal revision changed immutable proposal identity")
        heads[proposal_id] = proposal.proposal_revision_id
    return heads


def _validate_decisions(
    state: BrainState,
    new: Mapping[str, Decision],
    proposals: Mapping[str, Proposal],
    proposal_heads: Mapping[str, str],
) -> None:
    decided = {
        (decision.proposal_id, decision.expected_proposal_revision_id)
        for decision in state.decisions.values()
    }
    for decision in new.values():
        actual = proposal_heads.get(decision.proposal_id)
        if actual is None:
            raise LedgerTransitionError("decision proposal is unknown")
        if actual != decision.expected_proposal_revision_id:
            raise RevisionConflict(
                decision.proposal_id,
                decision.expected_proposal_revision_id,
                actual,
            )
        proposal = proposals[actual]
        if tuple(decision.compartments) != tuple(proposal.compartments):
            raise LedgerTransitionError("decision compartments must equal proposal compartments")
        key = (decision.proposal_id, decision.expected_proposal_revision_id)
        if key in decided:
            raise RevisionConflict(
                decision.proposal_id,
                decision.expected_proposal_revision_id,
                actual,
            )
        decided.add(key)


def _validate_revisions(
    state: BrainState,
    new: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    decisions: Mapping[str, Decision],
) -> dict[str, str]:
    heads = dict(state.artifact_heads)
    grouped: dict[str, list[Revision]] = {}
    for revision in new.values():
        if revision.base_revision_id is not None:
            base = {**state.revisions, **new}.get(revision.base_revision_id)
            if base is None or base.artifact_id != revision.artifact_id:
                raise LedgerTransitionError(
                    "revision base belongs to another artifact or is unknown"
                )
        grouped.setdefault(revision.artifact_id, []).append(revision)
    for artifact_id, pending_values in grouped.items():
        pending = {revision.revision_id: revision for revision in pending_values}
        current = heads.get(artifact_id)
        while pending:
            candidates = [
                revision
                for revision in pending.values()
                if revision.base_revision_id == current
            ]
            if len(candidates) != 1:
                linked = next(
                    (
                        revision
                        for revision in pending.values()
                        if revision.accepted_from_proposal_id is not None
                    ),
                    None,
                )
                if linked is not None:
                    raise RevisionConflict(
                        cast(str, linked.accepted_from_proposal_id),
                        linked.base_revision_id or linked.revision_id,
                        current,
                    )
                raise LedgerTransitionError("artifact revision chain is ambiguous or stale")
            revision = candidates[0]
            _validate_revision_approval(revision, current, proposals, decisions)
            current = revision.revision_id
            del pending[revision.revision_id]
        heads[artifact_id] = cast(str, current)
    return heads


def _validate_revision_approval(
    revision: Revision,
    current: str | None,
    proposals: Mapping[str, Proposal],
    decisions: Mapping[str, Decision],
) -> None:
    proposal_id = revision.accepted_from_proposal_id
    decision_id = revision.decision_id
    if current is None and proposal_id is None and decision_id is None:
        return
    if proposal_id is None or decision_id is None:
        raise LedgerTransitionError("non-genesis revision requires proposal and decision links")
    decision = decisions.get(decision_id)
    if decision is None or decision.proposal_id != proposal_id or decision.outcome != "accepted":
        raise LedgerTransitionError("revision decision link is not an accepted decision")
    proposal = proposals.get(decision.expected_proposal_revision_id)
    if (
        proposal is None
        or proposal.proposal_id != proposal_id
        or proposal.artifact_id != revision.artifact_id
        or proposal.base_revision_id != revision.base_revision_id
        or proposal.content_schema != revision.content_schema
        or proposal.compartments != revision.compartments
        or proposal.provenance != revision.provenance
        or proposal.body != revision.body
    ):
        raise LedgerTransitionError("accepted revision does not match its proposal")


def _validate_effects(
    state: BrainState,
    new: Mapping[str, EffectReceipt],
) -> dict[str, Effect]:
    effects = dict(state.effects)
    seen_effects: set[str] = set()
    for receipt in new.values():
        if receipt.effect_id in seen_effects:
            raise LedgerTransitionError("one batch cannot advance an effect more than once")
        seen_effects.add(receipt.effect_id)
        current = effects.get(receipt.effect_id)
        if current is None:
            if receipt.reconciles_receipt_id is not None:
                raise LedgerTransitionError("initial effect receipt cannot reconcile a predecessor")
            effects[receipt.effect_id] = Effect(
                receipt.brain_id,
                receipt.effect_id,
                receipt.external_identity,
                (receipt,),
            )
            continue
        previous = current.current
        if previous.outcome != "unknown":
            raise LedgerTransitionError("external effect is already resolved")
        if receipt.reconciles_receipt_id != previous.receipt_id:
            raise LedgerTransitionError("effect reconciliation does not name the current receipt")
        if receipt.outcome == "unknown":
            raise LedgerTransitionError("effect reconciliation requires a terminal outcome")
        if (
            receipt.external_identity != current.external_identity
            or receipt.compartments != previous.compartments
            or receipt.provenance != previous.provenance
        ):
            raise LedgerTransitionError("effect reconciliation binding changed")
        effects[receipt.effect_id] = Effect(
            current.brain_id,
            current.effect_id,
            current.external_identity,
            (*current.receipts, receipt),
        )
    return effects


def _apply_supersession(
    state: BrainState,
    new: Mapping[str, Record],
) -> tuple[dict[str, str], set[str]]:
    heads = dict(state.origin_heads)
    superseded = set(state.superseded_record_ids)
    seen_origins: set[str] = set()
    for record in new.values():
        if record.origin_id is None:
            continue
        if record.origin_id in seen_origins:
            raise LedgerTransitionError("one batch cannot supersede one origin twice")
        seen_origins.add(record.origin_id)
        prior = heads.get(record.origin_id)
        if prior is not None:
            if prior not in record.provenance.source_record_ids:
                raise LedgerTransitionError(
                    "origin supersession must cite the current origin head"
                )
            superseded.add(prior)
        heads[record.origin_id] = record.record_id
    return heads, superseded


def _validate_purges(
    state: BrainState,
    transitions: Iterable[PurgeTransition],
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    decisions: Mapping[str, Decision],
    effects: Mapping[str, Effect],
    parents: Mapping[str, frozenset[str]],
    children: Mapping[str, frozenset[str]],
    suppressed: set[str],
    tombstoned: set[str],
    purge_pending: set[str],
) -> dict[str, Purge]:
    pending_transitions = tuple(transitions)
    purges = dict(state.purges)
    if not pending_transitions:
        return purges

    effect_receipts = {
        receipt.receipt_id: receipt
        for effect in effects.values()
        for receipt in effect.receipts
    }
    grouped: dict[str, list[PurgeTransition]] = {}
    targets = {purge_id: purge.target_record_id for purge_id, purge in purges.items()}
    for transition in pending_transitions:
        if transition.target_record_id not in state.records:
            raise LedgerTransitionError("purge target must be an existing authoritative record")
        grouped.setdefault(transition.purge_id, []).append(transition)
        prior_target = targets.setdefault(transition.purge_id, transition.target_record_id)
        if prior_target != transition.target_record_id:
            raise LedgerTransitionError("purge identity is already bound to another target")

    closures = {
        purge_id: _purge_closure(
            target_record_id,
            records,
            revisions,
            proposals,
            effect_receipts,
            children,
        )
        for purge_id, target_record_id in targets.items()
    }
    closure_ids = sorted(closures)
    for index, purge_id in enumerate(closure_ids):
        for other_id in closure_ids[index + 1 :]:
            if closures[purge_id].intersection(closures[other_id]):
                raise LedgerTransitionError("distinct purge closures overlap")

    for purge_id, values in grouped.items():
        existing = purges.get(purge_id)
        if existing is None:
            target_record_id = targets[purge_id]
            if target_record_id in tombstoned or target_record_id in purge_pending:
                raise LedgerTransitionError("purge target is already inactive")
            if not any(
                transition.subject_id == target_record_id
                and transition.subject_kind == "record"
                and transition.resolution == "purge"
                for transition in values
            ):
                raise LedgerTransitionError(
                    "new purge requires an initiating target purge resolution"
                )
            purges[purge_id] = Purge(state.brain_id, purge_id, target_record_id)
            suppressed.update(closures[purge_id])
            purge_pending.update(closures[purge_id])

    for purge_id in sorted(grouped):
        purge = purges[purge_id]
        additions = grouped[purge_id]
        all_resolution_ids = set(purge.resolutions) | {
            transition.subject_id for transition in additions
        }
        replacement_ids = {
            cast(str, transition.replacement_record_id)
            for transition in (*purge.resolutions.values(), *additions)
            if transition.resolution == "replace"
        }
        if replacement_ids.intersection(all_resolution_ids):
            raise LedgerTransitionError(
                "an approved replacement cannot also be a purge resolution subject"
            )
        for transition in sorted(
            additions,
            key=lambda value: (value.subject_id, value.subject_kind),
        ):
            if transition.subject_id in purge.resolutions:
                raise LedgerTransitionError("purge subject already has a resolution")
            if transition.subject_id not in closures[purge_id]:
                raise LedgerTransitionError(
                    "purge subject is outside the target provenance closure"
                )
            _validate_subject_kind(
                transition,
                records,
                revisions,
                proposals,
                effect_receipts,
            )
            if (
                transition.subject_id == purge.target_record_id
                and transition.resolution != "purge"
            ):
                raise LedgerTransitionError("purge target cannot be retained or replaced")
            if transition.resolution in {"retain", "replace"}:
                _validate_purge_review(
                    transition,
                    records,
                    revisions,
                    proposals,
                    decisions,
                    effect_receipts,
                )
            if transition.resolution == "replace":
                replacement_id = cast(str, transition.replacement_record_id)
                replacement = records.get(replacement_id)
                if replacement is None or replacement_id == transition.subject_id:
                    raise LedgerTransitionError("purge replacement record is invalid")
                if (
                    replacement.provenance.derivation != "replacement"
                    or not _ancestors(replacement_id, parents).intersection(
                        _purge_subject_lineage_ids(proposals, transition.subject_id)
                    )
                ):
                    raise LedgerTransitionError(
                        "replacement is not provenance-bound to its subject"
                    )
                tombstoned.add(transition.subject_id)
                suppressed.discard(replacement_id)
                purge_pending.discard(replacement_id)
            elif transition.resolution == "purge":
                tombstoned.add(transition.subject_id)
            else:
                if transition.subject_id in tombstoned:
                    raise LedgerTransitionError("a tombstoned subject cannot be retained")
                suppressed.discard(transition.subject_id)
            purge_pending.discard(transition.subject_id)
            resolutions = {**purge.resolutions, transition.subject_id: transition}
            purge = Purge(
                purge.brain_id,
                purge.purge_id,
                purge.target_record_id,
                resolutions,
            )
            purges[purge_id] = purge
    return purges


def _purge_closure(
    target_record_id: str,
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    effect_receipts: Mapping[str, EffectReceipt],
    children: Mapping[str, frozenset[str]],
) -> frozenset[str]:
    nodes = {target_record_id, *_descendants(target_record_id, children)}
    return frozenset(
        {
            *(node for node in nodes if node in records or node in revisions),
            *(
                proposal.proposal_id
                for proposal in proposals.values()
                if proposal.proposal_revision_id in nodes
            ),
            *(receipt_id for receipt_id in effect_receipts if receipt_id in nodes),
        }
    )


def _validate_subject_kind(
    transition: PurgeTransition,
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    effect_receipts: Mapping[str, EffectReceipt],
) -> None:
    matches = {
        "record": transition.subject_id in records,
        "revision": transition.subject_id in revisions,
        "proposal": any(
            proposal.proposal_id == transition.subject_id for proposal in proposals.values()
        ),
        "effect_receipt": transition.subject_id in effect_receipts,
    }
    if not matches[transition.subject_kind]:
        raise LedgerTransitionError("purge subject kind does not match its identity")


def _purge_subject_lineage_ids(
    proposals: Mapping[str, Proposal],
    subject_id: str,
) -> frozenset[str]:
    proposal_revisions = {
        proposal.proposal_revision_id
        for proposal in proposals.values()
        if proposal.proposal_id == subject_id
    }
    return frozenset({subject_id, *proposal_revisions})


def _subject_compartments(
    subject_kind: str,
    subject_id: str,
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    effect_receipts: Mapping[str, EffectReceipt],
) -> tuple[str, ...]:
    if subject_kind == "record":
        return tuple(records[subject_id].compartments)
    if subject_kind == "revision":
        return tuple(revisions[subject_id].compartments)
    if subject_kind == "effect_receipt":
        return tuple(effect_receipts[subject_id].compartments)
    return next(
        tuple(proposal.compartments)
        for proposal in proposals.values()
        if proposal.proposal_id == subject_id
    )


def _validate_purge_review(
    transition: PurgeTransition,
    records: Mapping[str, Record],
    revisions: Mapping[str, Revision],
    proposals: Mapping[str, Proposal],
    decisions: Mapping[str, Decision],
    effect_receipts: Mapping[str, EffectReceipt],
) -> None:
    review = decisions.get(cast(str, transition.review_decision_id))
    if review is None or review.outcome != "accepted":
        raise LedgerTransitionError("purge resolution requires an accepted review decision")
    proposal = proposals.get(review.expected_proposal_revision_id)
    if proposal is None or proposal.proposal_id != review.proposal_id:
        raise LedgerTransitionError("purge review decision is not bound to an exact proposal")
    expected_body = {
        "kind": "purge_resolution_review",
        "purge_id": transition.purge_id,
        "subject_kind": transition.subject_kind,
        "subject_id": transition.subject_id,
        "resolution": transition.resolution,
        "replacement_record_id": transition.replacement_record_id,
    }
    if dict(proposal.body) != expected_body:
        raise LedgerTransitionError("purge review does not name the exact resolution intent")
    if proposal.provenance.derivation != "owner" or proposal.provenance.source_ids:
        raise LedgerTransitionError("purge review must use owner provenance without sources")
    subject_labels = _subject_compartments(
        transition.subject_kind,
        transition.subject_id,
        records,
        revisions,
        proposals,
        effect_receipts,
    )
    expected_labels = subject_labels
    if transition.resolution == "replace":
        replacement = records.get(cast(str, transition.replacement_record_id))
        if replacement is None:
            raise LedgerTransitionError("purge replacement record is invalid")
        expected_labels = propagated_compartments(subject_labels, replacement.compartments)
    if proposal.compartments != expected_labels:
        raise LedgerTransitionError("purge review compartments do not match reviewed values")


def _post_batch_capacity(
    state: BrainState,
    snapshot: ActiveLabelSetSnapshot,
    new_records: Mapping[str, Record],
    superseded: set[str],
    suppressed: set[str],
    tombstoned: set[str],
    purge_pending: set[str],
) -> ActiveLabelSetSnapshot:
    expected_before = _active_record_counts(state.records, state)
    supplied = {labels: count for labels, count in snapshot.counts.items() if count > 0}
    if supplied != expected_before:
        raise LedgerTransitionError("capacity snapshot does not match authoritative active records")
    records = {**state.records, **new_records}
    inactive = superseded | suppressed | tombstoned | purge_pending
    counts: dict[tuple[str, ...], int] = {}
    for record_id, record in records.items():
        if record_id in inactive:
            continue
        labels = tuple(record.compartments)
        counts[labels] = counts.get(labels, 0) + 1
    _validate_capacity(counts, snapshot.max_active_label_sets)
    return ActiveLabelSetSnapshot(state.brain_id, counts, snapshot.max_active_label_sets)


def _active_record_counts(
    records: Mapping[str, Record],
    state: BrainState,
) -> dict[tuple[str, ...], int]:
    inactive = (
        frozenset(state.superseded_record_ids)
        | frozenset(state.suppressed_ids)
        | frozenset(state.tombstoned_ids)
        | frozenset(state.purge_pending_ids)
    )
    counts: dict[tuple[str, ...], int] = {}
    for record_id, record in records.items():
        if record_id in inactive:
            continue
        labels = tuple(record.compartments)
        counts[labels] = counts.get(labels, 0) + 1
    return counts


def _validate_capacity(counts: Mapping[tuple[str, ...], int], active_limit: int) -> None:
    active = sum(count > 0 for count in counts.values())
    if active > active_limit:
        raise ResourceLimitError(
            "active_label_set_limit",
            observed=active,
            limit=active_limit,
            corrective_action="reduce_active_label_sets",
        )
    for count in counts.values():
        if count > RESOURCE_LIMITS.records_per_label_set:
            raise ResourceLimitError(
                "records_per_label_set_limit",
                observed=count,
                limit=RESOURCE_LIMITS.records_per_label_set,
                corrective_action="purge_or_supersede_records",
            )


def _validated_state(state: BrainState) -> BrainState:
    try:
        return BrainState(
            brain_id=state.brain_id,
            records=state.records,
            artifacts=state.artifacts,
            revisions=state.revisions,
            proposals=state.proposals,
            proposal_heads=state.proposal_heads,
            decisions=state.decisions,
            effects=state.effects,
            purges=state.purges,
            commits=state.commits,
            receipts=state.receipts,
            deliveries=state.deliveries,
            artifact_heads=state.artifact_heads,
            origin_heads=state.origin_heads,
            superseded_record_ids=state.superseded_record_ids,
            suppressed_ids=state.suppressed_ids,
            tombstoned_ids=state.tombstoned_ids,
            purge_pending_ids=state.purge_pending_ids,
            purged_deliveries=state.purged_deliveries,
        )
    except (AttributeError, TypeError, ValueError) as error:
        if isinstance(error, ResourceLimitError):
            raise
        raise LedgerTransitionError("authoritative Brain state is invalid") from error


def _validated_batch(batch: CommitBatch) -> CommitBatch:
    try:
        items = tuple(_validated_item(item) for item in batch.items)
        return CommitBatch(
            brain_id=batch.brain_id,
            delivery_id=batch.delivery_id,
            digest=batch.digest,
            sequencer_epoch=batch.sequencer_epoch,
            issuer_epoch=batch.issuer_epoch,
            policy_digest=batch.policy_digest,
            items=items,
            schema_version=batch.schema_version,
        )
    except (AttributeError, TypeError, ValueError) as error:
        if isinstance(error, ResourceLimitError):
            raise
        raise LedgerTransitionError("commit batch value is invalid") from error


def _validated_item(item: LedgerItem) -> LedgerItem:
    if isinstance(item, Record):
        return replace(item)
    if isinstance(item, Revision):
        return replace(item)
    if isinstance(item, Proposal):
        return replace(item)
    if isinstance(item, Decision):
        return replace(item)
    if isinstance(item, EffectReceipt):
        return replace(item)
    if isinstance(item, PurgeTransition):
        return replace(item)
    raise LedgerTransitionError("commit batch contains an unknown item")


def _validated_snapshot(snapshot: ActiveLabelSetSnapshot) -> ActiveLabelSetSnapshot:
    try:
        return ActiveLabelSetSnapshot(
            snapshot.brain_id,
            snapshot.counts,
            snapshot.max_active_label_sets,
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise LedgerTransitionError("active-label-set snapshot is invalid") from error


def _validate_binding(binding: DeliveryBinding) -> None:
    try:
        DeliveryBinding(binding.brain_id, binding.delivery_id, binding.digest, binding.receipt)
    except (AttributeError, TypeError, ValueError) as error:
        raise LedgerTransitionError("delivery binding is invalid") from error


def _validate_digest(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise LedgerTransitionError("commit digest must be lowercase SHA-256")


def _same_record_semantics(pending: Record, stored: Record) -> bool:
    return (
        pending.brain_id == stored.brain_id
        and pending.record_id == stored.record_id
        and pending.record_type == stored.record_type
        and pending.content_schema == stored.content_schema
        and pending.producer_principal_id == stored.producer_principal_id
        and pending.origin_id == stored.origin_id
        and pending.captured_at == stored.captured_at
        and pending.observed_at == stored.observed_at
        and pending.compartments == stored.compartments
        and pending.provenance == stored.provenance
        and pending.body == stored.body
    )
