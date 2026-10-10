"""Foreground harness memory orchestration over existing local engine tasks."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.core.models import (
    Authority,
    CaptureWhyOrigin,
    ContentOrigin,
    PrivacyDecision,
    PrivacyReason,
    PrivacyTier,
    Provenance,
)
from open_brain_engine.engine.capture import CaptureTasks
from open_brain_engine.engine.consent_contracts import EgressMode, ProviderConsentState
from open_brain_engine.engine.contracts import (
    CaptureSubmission,
    EngineTaskSet,
    PublicJobCaptureContext,
    TextPayload,
)
from open_brain_engine.engine.harness_memory import (
    SOURCE_PREFIX,
    HarnessCitation,
    HarnessMemory,
    decode_memory,
    encode_memory,
)
from open_brain_engine.engine.source_intake import (
    SourceRevisionBinding,
    SourceRevisionSubmission,
)
from open_brain_engine.engine.source_lifecycle_contracts import SourceInspectRequest
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    SearchPageRequest,
    SourceRouteRequest,
    T03Error,
)

from .harness_memory import MemoryMessage, select_memories
from .harness_policy import (
    HarnessPolicy,
    HarnessPolicyError,
    HarnessSourceDecision,
    HarnessSourceRule,
)
from .session_consent import DurableProviderConsentStore


@dataclass(frozen=True, slots=True)
class HarnessRecallResult:
    status: str
    memories: tuple[HarnessMemory, ...] = ()
    error_code: str | None = None


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]


def _operation_id(value: str) -> str:
    return "operation_" + str(UUID(bytes=sha256(value.encode()).digest()[:16], version=4))


def _local_space_name(project: Path) -> str:
    return "Harness local memory " + _digest(str(project.resolve()))


def _space_name(policy: HarnessPolicy, decision: HarnessSourceDecision, project: Path) -> str:
    rule = next((rule for rule in policy.sources if rule.source_id == decision.source_id), None)
    identity = str(project.resolve()) if rule is None else f"{rule.source_id}:{rule.project_root}"
    return "Harness memory " + _digest(identity)


def _overlaps(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def _cloud_rule(policy: HarnessPolicy, rule: HarnessSourceRule, provider: str) -> bool:
    tiers = policy.allowed_tiers(provider)
    return (
        not rule.local_only
        and rule.tier in tiers
        and not any(_overlaps(rule.project_root, path) for path in policy.exclusions)
        and not any(
            nested.project_root != rule.project_root
            and nested.project_root.is_relative_to(rule.project_root)
            and (nested.local_only or nested.tier not in tiers)
            for nested in policy.sources
        )
    )


class HarnessWorkflow:
    """Trusted local host interface; cloud callers never select owner authority.

    Policy and durable consent are reloaded per operation and before release.
    A source-rule space is conservatively withheld on overlapping exclusions or
    restrictive nested rules. More granular path revocation is a later seam.
    Reserved local-v1- source revision keys preserve a durable restriction across
    routing failures. They only narrow; shared-v1- never grants provider access.
    """

    def __init__(
        self,
        tasks: EngineTaskSet,
        policy: Callable[[], HarnessPolicy],
        *,
        consent_store: DurableProviderConsentStore,
        local_only_host: bool = False,
    ) -> None:
        if consent_store.brain_id != derive_brain_id(tasks.profile.tenant_id):
            raise ValueError("destination_mismatch")
        self.tasks = tasks
        self.policy = policy
        self._consent = consent_store
        self._local_only_host = local_only_host

    def _owner(self) -> EffectiveAuthority:
        return EffectiveAuthority(
            self.tasks.profile.owner_actor_id,
            "harness-local-operation",
            frozenset(),
            None,
            owner=True,
            allowed_read_tiers=frozenset(PrivacyTier),
        )

    def capture(
        self,
        client: str,
        session_id: str,
        project: Path,
        messages: Sequence[MemoryMessage],
        *,
        timestamps: Mapping[int, str | None] | None = None,
        local_only: bool = False,
    ) -> dict[str, object]:
        """Return only custody metadata; no source text enters acknowledgements."""
        captured: list[str] = []
        deliveries: list[str] = []
        try:
            policy = self.policy()
            authored = "\n".join(message.text for message in messages if message.authored)
            decision = policy.classify(project, authored_text=authored)
            if type(local_only) is not bool:
                return {"status": "refused", "error_code": "invalid_request"}
            if local_only:
                decision = replace(decision, local_only=True)
            if has_redaction_finding(authored):
                return {"status": "refused", "error_code": "credential_signal"}
            candidates = select_memories(messages)
            if not candidates:
                return {"status": "success_empty", "capture_ids": []}
            queued = False
            ordinal_indexes: dict[int, int] = {}
            for candidate in candidates:
                if self.policy() != policy:
                    return {
                        "status": "refused",
                        "error_code": "policy_changed",
                        "capture_ids": captured,
                        "delivery_ids": deliveries,
                    }
                index = ordinal_indexes.get(candidate.ordinal, 0)
                ordinal_indexes[candidate.ordinal] = index + 1
                message_id = candidate.source_id or str(candidate.ordinal)
                locator = portable_canonical_json_bytes(
                    [client, session_id, str(project.resolve()), message_id, index]
                ).decode("utf-8")
                identity = _digest(locator)
                source = SOURCE_PREFIX + identity
                memory = HarnessMemory(
                    candidate.kind,
                    candidate.text,
                    candidate.source_excerpt[:2048],
                    HarnessCitation(
                        client,
                        client,
                        "project-" + _digest(str(project.resolve())),
                        session_id,
                        candidate.ordinal,
                        candidate.role,
                        None if timestamps is None else timestamps.get(candidate.ordinal),
                    ),
                )
                capture_id, delivery_id = self._capture_revision(
                    client,
                    project,
                    policy,
                    decision,
                    identity,
                    source,
                    memory,
                )
                deliveries.append(delivery_id)
                if capture_id is not None:
                    captured.append(capture_id)
                else:
                    queued = True
            return {
                "status": "queued" if queued else "acknowledged",
                "capture_ids": captured,
                "delivery_ids": deliveries,
            }
        except HarnessPolicyError as error:
            return {"status": "refused", "error_code": error.code}
        except Exception:
            return {
                "status": "unavailable",
                "error_code": "capture_unavailable",
                "capture_ids": captured,
                "delivery_ids": deliveries,
            }

    def _capture_revision(
        self,
        client: str,
        project: Path,
        policy: HarnessPolicy,
        decision: HarnessSourceDecision,
        identity: str,
        source: str,
        memory: HarnessMemory,
    ) -> tuple[str | None, str]:
        if self.tasks.sources is None:
            raise ValueError("source_tasks_unavailable")
        namespace = {
            "connector_name": "harness-memory",
            "connection_id": client,
            "resource_id": memory.citation.project_id,
            "external_id": identity,
        }
        binding = SourceRevisionBinding(
            destination_brain_id=self._consent.brain_id,
            issuer_epoch=self._consent.issuer_epoch,
            root_fingerprint=sha256(str(self.tasks.profile.root_identity).encode()).hexdigest(),
            accepted_source_id="harness-project-" + _digest(str(project.resolve())),
            namespace=namespace,
        )
        sink = self.tasks.sources.public_revision_sink(binding)
        head = sink.inspect_head()  # Checks actual Brain identity and issuer epoch.
        if head.capture_id is not None:
            record = cast(
                dict[str, Any],
                self.tasks.retrieval.read_record(
                    RecordReadRequest(
                        record_id=head.capture_id, expected_revision_id=head.capture_id
                    ),
                    authority=self._owner(),
                ).to_wire(),
            )
            previous = decode_memory(record["content"]["text"])
            if (
                previous.kind == memory.kind
                and previous.citation.authorship == memory.citation.authorship
            ):
                # A rotation/display-ordinal or newly discovered timestamp cannot
                # mutate an already accepted candidate's source citation.
                memory = replace(memory, citation=previous.citation)
            head_receipt = cast(CaptureTasks, self.tasks.capture).get(head.capture_id)
            if head_receipt is None:
                raise ValueError("head_receipt_unavailable")
            private = (
                head.revision_key is not None
                and head.revision_key.startswith("local-v1-")
                or record["record"]["space_id"]
                in {
                    space.space_id
                    for space in self.tasks.spaces.spaces()
                    if space.name == _local_space_name(project)
                }
            )
            tiers = list(PrivacyTier)
            retained_tier = head_receipt.final_admitted_tier
            if tiers.index(retained_tier) > tiers.index(decision.tier):
                decision = replace(decision, tier=retained_tier, local_only=True)
            if private:
                decision = replace(decision, local_only=True)
        privacy = self._privacy(policy, decision)
        local = not privacy.authority.external_egress
        payload = encode_memory(memory)
        revision_digest = _digest(
            payload + portable_canonical_json_bytes(privacy.to_dict()).decode("utf-8")
        )
        revision_key = ("local-v1-" if local else "shared-v1-") + revision_digest
        delivery_id = "harness.memory." + identity + "." + revision_digest
        name = _local_space_name(project) if local else _space_name(policy, decision, project)
        space = self.tasks.spaces.create_space(name, delivery_id="harness.space." + _digest(name))
        if head.revision_key == revision_key and head.capture_id is not None:
            self._route(head.capture_id, space.space_id)
            return head.capture_id, delivery_id
        submission = CaptureSubmission.for_public_job(
            context=self._capture_context(client),
            payload=TextPayload(payload),
            delivery_id=delivery_id,
            source_origin=ContentOrigin.THIRD_PARTY,
            source_reference=source,
            provenance=Provenance.create(
                source_ref=source,
                content_origin=ContentOrigin.THIRD_PARTY,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=privacy,
        )
        source_submission = SourceRevisionSubmission(
            capture=submission,
            namespace=namespace,
            revision_key=revision_key,
            canonical_sha256=submission.request_sha256(),
            expected_head=head.capture_id,
            ordering={"kind": "unordered"}
            if head.capture_id is None
            else {"kind": "predecessor", "revision_key": head.revision_key},
            expected_control_epoch=head.control_epoch,
        )
        # Native harness sources use ordinary owner-approved public-job intake.
        # Managed collector deliveries have a distinct sharing-copy authority
        # boundary and must retain their external-original denial.
        try:
            receipt = self.tasks.sources.submit_revision(source_submission)
        except T03Error as error:
            if error.code == "operation_pending":
                return None, delivery_id
            raise
        if receipt.capture_id is None:
            return None, delivery_id
        if receipt.outcome != "captured":
            raise ValueError("revision_not_captured")
        self._route(receipt.capture_id, space.space_id)
        return receipt.capture_id, delivery_id

    def _capture_context(self, client: str) -> PublicJobCaptureContext:
        def identifier(prefix: str) -> str:
            digest = sha256(f"open-brain:harness:{client}:{prefix}".encode()).digest()[:16]
            return prefix + "_" + str(UUID(bytes=digest, version=4))

        actor = identifier("actor")
        return PublicJobCaptureContext.create(
            profile=self.tasks.profile,
            actor_id=actor,
            role_claim={
                "actor_id": actor,
                "capabilities": ["capture.accept"],
                "role_claim_id": identifier("role_claim"),
                "role_id": identifier("role"),
                "tenant_id": self.tasks.profile.tenant_id,
            },
        )

    def _privacy(self, policy: HarnessPolicy, decision: HarnessSourceDecision) -> PrivacyDecision:
        egress = not decision.local_only and any(
            decision.tier in tiers for _, tiers in policy.providers
        )
        reasons = {
            PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
            PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
            PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_CONFIRMED
            if egress
            else PrivacyReason.PERSONAL_LOCAL_ONLY,
            PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
            PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
        }
        return PrivacyDecision.create(
            tier=decision.tier,
            reason=reasons[decision.tier],
            policy_version="open-brain-harness-policy.v1",
            authority=Authority(cloud=egress, external_egress=egress),
            confirmation_ref="owner-source-rule:" + str(decision.source_id)
            if decision.tier is PrivacyTier.PERSONAL and egress
            else None,
        )

    def _route(self, capture_id: str, space_id: str) -> None:
        if self.tasks.sources is None:
            raise ValueError("source_tasks_unavailable")
        authority = self._owner()
        record = cast(
            dict[str, Any],
            self.tasks.retrieval.read_record(
                RecordReadRequest(record_id=capture_id, expected_revision_id=capture_id),
                authority=authority,
            ).to_wire(),
        )
        if record["record"]["space_id"] == space_id:
            return
        source_id = record["record"]["source_id"]
        inspection = self.tasks.sources.inspect(
            SourceInspectRequest(source_id=source_id),
            authority=authority,
        )
        self.tasks.sources.route(
            SourceRouteRequest(
                source_id=source_id,
                space_id=space_id,
                expected_head=inspection.head_capture_id,
                expected_route_version=inspection.route_version,
                operation_id=_operation_id(capture_id + space_id),
            ),
            authority=authority,
        )

    def recall(self, query: str, *, provider: str, limit: int = 5) -> HarnessRecallResult:
        try:
            policy = self.policy()
            if not policy.allowed_tiers(provider):
                return HarnessRecallResult("refused", error_code="provider_denied")
            state = self._consent.load()
            active = [
                record
                for record in state.records
                if record.active and record.provider_id == provider
            ]
            if len(active) != 1:
                return HarnessRecallResult("refused", error_code="consent_unavailable")
            consent = state.active_consent(
                consent_id=active[0].consent_id,
                provider_id=provider,
                authorization_generation=state.authorization_generation,
            )
            names = {
                _space_name(
                    policy,
                    HarnessSourceDecision(rule.source_id, rule.tier, rule.local_only),
                    rule.project_root,
                )
                for rule in policy.sources
                if _cloud_rule(policy, rule, provider)
            }
            spaces = frozenset(
                space.space_id for space in self.tasks.spaces.spaces() if space.name in names
            )
            authority = EffectiveAuthority(
                "harness-" + provider,
                "harness-recall",
                frozenset({"search", "content-read"}),
                spaces,
                authorization_generation=state.authorization_generation,
                allowed_read_tiers=policy.allowed_tiers(provider) & consent.allowed_tiers,
                egress_mode=EgressMode.EXTERNAL_PROVIDER,
                provider_id=provider,
                consent_id=consent.consent_id,
                brain_id=self._consent.brain_id,
                issuer_epoch=self._consent.issuer_epoch,
            )
            return self._recall(query, authority, policy, limit, state)
        except Exception:
            return HarnessRecallResult("unavailable", error_code="recall_unavailable")

    def recall_local(self, query: str, *, project: Path, limit: int = 5) -> HarnessRecallResult:
        if not self._local_only_host:
            raise ValueError("local_only_host_required")
        try:
            policy = self.policy()
            decision = policy.classify(project)
            rule = next(
                (rule for rule in policy.sources if rule.source_id == decision.source_id), None
            )
            if rule is not None and any(
                _overlaps(rule.project_root, path) for path in policy.exclusions
            ):
                return HarnessRecallResult("refused", error_code="source_scope_excluded")
            names = {_space_name(policy, decision, project), _local_space_name(project)}
            spaces = frozenset(
                space.space_id for space in self.tasks.spaces.spaces() if space.name in names
            )
            authority = EffectiveAuthority(
                self.tasks.profile.owner_actor_id,
                "harness-local-recall",
                frozenset({"search", "content-read"}),
                spaces,
                owner=True,
                allowed_read_tiers=frozenset(PrivacyTier),
            )
            return self._recall(query, authority, policy, limit, None)
        except HarnessPolicyError as error:
            return HarnessRecallResult("refused", error_code=error.code)
        except Exception:
            return HarnessRecallResult("unavailable", error_code="recall_unavailable")

    def _recall(
        self,
        query: str,
        authority: EffectiveAuthority,
        policy: HarnessPolicy,
        limit: int,
        consent: ProviderConsentState | None,
    ) -> HarnessRecallResult:
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("invalid_limit")
        rows = cast(
            dict[str, Any],
            self.tasks.retrieval.search_page(
                SearchPageRequest(
                    query=query,
                    limit=limit,
                    filters={
                        "space_ids": [],
                        "payload_families": ["text"],
                        "record_types": ["source"],
                    },
                ),
                authority=authority,
            ).to_wire(),
        )["results"]
        memories: list[HarnessMemory] = []
        for row in rows:
            chunks: list[str] = []
            cursor: str | None = None
            for _ in range(8):
                if not self.authorization_current(policy, consent):
                    return HarnessRecallResult("refused", error_code="authorization_changed")
                response = cast(
                    dict[str, Any],
                    self.tasks.retrieval.read_record(
                        RecordReadRequest(
                            record_id=row["record_id"],
                            expected_revision_id=row["revision_id"],
                            target_bytes=32768,
                            cursor=cursor,
                        ),
                        authority=authority,
                    ).to_wire(),
                )
                chunks.append(response["content"]["text"])
                if response["complete"]:
                    break
                cursor = response["next_cursor"]
            else:
                raise ValueError("record_read_incomplete")
            memories.append(decode_memory("".join(chunks)))
        if not self.authorization_current(policy, consent):
            return HarnessRecallResult("refused", error_code="authorization_changed")
        return HarnessRecallResult("success" if memories else "success_empty", tuple(memories))

    def consent_snapshot(self) -> ProviderConsentState:
        return self._consent.load()

    def authorization_current(
        self, policy: HarnessPolicy, consent: ProviderConsentState | None
    ) -> bool:
        return self.policy() == policy and (consent is None or self._consent.load() == consent)
