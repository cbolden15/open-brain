"""Foreground harness memory orchestration over existing local engine tasks."""

from __future__ import annotations

import time
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
    SessionOrigin,
    SourceRouteRequest,
    T03Error,
)

from .harness_batching import indexed_memories
from .harness_memory import MemoryMessage
from .harness_policy import HarnessPolicy, HarnessPolicyError, HarnessSourceDecision
from .harness_retrieval import decision_state, plan_task_query
from .session_consent import DurableProviderConsentStore


@dataclass(frozen=True, slots=True)
class HarnessRecallResult:
    status: str
    memories: tuple[HarnessMemory, ...] = ()
    error_code: str | None = None
    memory_ids: tuple[str, ...] = ()
    memory_states: tuple[str, ...] = ()


class _RecallRefusal(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


_CLOUD_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})
# A verified local-only client reads every tier except secret.
_LOCAL_TIERS = frozenset(PrivacyTier) - {PrivacyTier.SECRET}
_SPACE_PREFIX = "Harness memory "
# Read-only: earlier releases routed local-only records to per-project spaces.
_LEGACY_LOCAL_PREFIX = "Harness local memory "


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:24]


def _operation_id(value: str) -> str:
    return "operation_" + str(UUID(bytes=sha256(value.encode()).digest()[:16], version=4))


def _space_name(policy: HarnessPolicy, decision: HarnessSourceDecision, project: Path) -> str:
    rule = next((rule for rule in policy.sources if rule.source_id == decision.source_id), None)
    identity = str(project.resolve()) if rule is None else f"{rule.source_id}:{rule.project_root}"
    return _SPACE_PREFIX + _digest(identity)


def _excluded_space_names(policy: HarnessPolicy) -> frozenset[str]:
    """Exclusions revoke reads of what was already captured, not only new captures.

    A source-rule space is withheld when its root overlaps an exclusion. An
    unmapped project's space is named by its path digest, so only a project
    excluded exactly can be mapped; nested projects need an owner purge.
    """
    names: set[str] = set()
    for rule in policy.sources:
        if any(
            rule.project_root.is_relative_to(path) or path.is_relative_to(rule.project_root)
            for path in policy.exclusions
        ):
            names.add(_SPACE_PREFIX + _digest(f"{rule.source_id}:{rule.project_root}"))
    for path in policy.exclusions:
        for prefix in (_SPACE_PREFIX, _LEGACY_LOCAL_PREFIX):
            names.add(prefix + _digest(str(path)))
    return frozenset(names)


def _revision_parts(key: str) -> tuple[str, int | None]:
    """Split a revision key into its content part and observation time, if any."""
    base, separator, stamp = key.partition("@")
    return base, int(stamp) if separator and stamp.isdigit() else None


class HarnessWorkflow:
    """Trusted local host interface; cloud callers never select owner authority.

    Policy B: each capture is tiered when written. Cloud reads use an external
    provider authority, so the core's owner egress policy and the provider's
    durable consent decide visibility. Consent is rechecked before release.
    Only a bridge-verified local-only host may read unshared tiers; secret never.
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
        selection: Sequence[tuple[int, int]] | None = None,
        observed_at: int | None = None,
    ) -> dict[str, object]:
        """Return only delivery metadata; no source text enters acknowledgements.

        ``observed_at`` is when the event was observed, in nanoseconds since the
        epoch. A spooled replay passes its enqueue time, so it never overwrites a
        head revision observed after it. Inline captures are observed now.
        """
        captured: list[str] = []
        deliveries: list[str] = []
        observed = time.time_ns() if observed_at is None else observed_at
        try:
            policy = self.policy()
            authored = "\n".join(message.text for message in messages if message.authored)
            if type(local_only) is not bool:
                return {"status": "refused", "error_code": "invalid_request"}
            decision = policy.capture_decision(
                client, project, authored_text=authored, local_only=local_only
            )
            if decision.tier is PrivacyTier.SECRET:
                # Secret means a credential source: never written, never readable.
                return {"status": "refused", "error_code": "source_denied"}
            if client == "hermes" and any(
                not message.authored or message.source_id is None for message in messages
            ):
                return {"status": "refused", "error_code": "invalid_request"}
            if has_redaction_finding(authored):
                return {"status": "refused", "error_code": "credential_signal"}
            indexed = indexed_memories(messages)
            if selection is not None:
                selected_keys = set(selection)
                if (
                    len(selection) > 32
                    or len(selected_keys) != len(selection)
                    or not selected_keys.issubset({item.key for item in indexed})
                ):
                    return {"status": "refused", "error_code": "invalid_request"}
                indexed = tuple(item for item in indexed if item.key in selected_keys)
            candidates = indexed
            if not candidates:
                return {"status": "success_empty", "capture_ids": []}
            queued = False
            for selected in candidates:
                candidate = selected.candidate
                if self.policy() != policy:
                    return {
                        "status": "refused",
                        "error_code": "policy_changed",
                        "capture_ids": captured,
                        "delivery_ids": deliveries,
                    }
                index = selected.index
                message_id = candidate.source_id or str(candidate.ordinal)
                locator = portable_canonical_json_bytes(
                    [client, session_id, str(project.resolve()), message_id, index]
                ).decode("utf-8")
                identity = _digest(locator)
                source = SOURCE_PREFIX + identity
                memory = HarnessMemory(
                    candidate.kind,
                    candidate.text,
                    next(
                        (
                            message.text
                            for message in messages
                            if message.ordinal == candidate.ordinal
                            and message.authored
                            and message.role == candidate.role
                            and message.source_id == candidate.source_id
                            and candidate.source_excerpt in message.text
                            and len(message.text.encode("utf-8")) <= 8192
                        ),
                        candidate.source_excerpt,
                    ),
                    HarnessCitation(
                        client,
                        client,
                        "project-" + _digest(str(project.resolve())),
                        session_id,
                        candidate.ordinal,
                        candidate.role,
                        None if timestamps is None else timestamps.get(candidate.ordinal),
                        candidate.source_id if client == "hermes" else None,
                    ),
                )
                capture_id, delivery_id = self._capture_revision(
                    client, project, policy, decision, identity, source, memory, observed
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
        observed_at: int | None = None,
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
        privacy = self._privacy(decision)
        payload = encode_memory(memory)
        revision_digest = _digest(
            payload + portable_canonical_json_bytes(privacy.to_dict()).decode("utf-8")
        )
        # A plain tier-keyed revision. Returning to an earlier tier or text is a new
        # revision, so it is keyed by its predecessor; an unchanged head is a no-op.
        # The observation time follows "@" so a stale replay cannot overwrite a
        # newer head, such as one lowered by a later confidentiality marking.
        tiered = decision.tier.value + "-v1-" + revision_digest
        head_base, head_observed = _revision_parts(head.revision_key or "")
        if head.capture_id is not None and (
            head_base == tiered or head_base.startswith(tiered + "-")
        ):
            revision_key = cast(str, head.revision_key)
        elif (
            head.capture_id is not None
            and head_observed is not None
            and observed_at is not None
            and observed_at < head_observed
        ):
            # Superseded: report the newer head as this request's delivery.
            return head.capture_id, "harness.memory." + identity + "." + _digest(
                cast(str, head.revision_key)
            )
        else:
            base = tiered if head.capture_id is None else tiered + "-" + _digest(head.capture_id)
            revision_key = base if observed_at is None else f"{base}@{observed_at}"
        delivery_id = "harness.memory." + identity + "." + _digest(revision_key)
        name = _space_name(policy, decision, project)
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

    def _privacy(self, decision: HarnessSourceDecision) -> PrivacyDecision:
        """Record only the tier; the core's owner egress policy grants any sharing."""
        reasons = {
            PrivacyTier.PUBLIC: PrivacyReason.POLICY_PUBLIC,
            PrivacyTier.WORK: PrivacyReason.POLICY_WORK,
            PrivacyTier.PERSONAL: PrivacyReason.PERSONAL_LOCAL_ONLY,
            PrivacyTier.SECRET: PrivacyReason.SECRET_DETECTED,
            PrivacyTier.UNKNOWN: PrivacyReason.CLASSIFICATION_MISSING,
        }
        return PrivacyDecision.create(
            tier=decision.tier,
            reason=reasons[decision.tier],
            policy_version="open-brain-harness-policy.v1",
            authority=Authority(cloud=False, external_egress=False),
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

    def recall(
        self, query: str, *, provider: str, limit: int = 5,
        read_tiers: frozenset[PrivacyTier] | None = None,
    ) -> HarnessRecallResult:
        try:
            authority, state = self._provider_authority(provider, read_tiers=read_tiers)
            return self._recall(query, authority, limit, state)
        except (HarnessPolicyError, _RecallRefusal) as error:
            return HarnessRecallResult("refused", error_code=error.code)
        except Exception:
            return HarnessRecallResult("unavailable", error_code="recall_unavailable")

    def _harness_spaces(self, *, legacy_local: bool) -> frozenset[str]:
        prefixes = (_SPACE_PREFIX, _LEGACY_LOCAL_PREFIX) if legacy_local else (_SPACE_PREFIX,)
        excluded = _excluded_space_names(self.policy())
        return frozenset(
            space.space_id for space in self.tasks.spaces.spaces()
            if space.name.startswith(prefixes) and space.name not in excluded
        )

    def _permitted(self, capture_id: str, authority: EffectiveAuthority) -> bool:
        """Owner authority ignores read tiers in the engine, so withhold secret here.

        Records written before Policy B can be secret-tier in legacy local spaces.
        """
        if not authority.owner:
            return True
        receipt = cast(CaptureTasks, self.tasks.capture).get(capture_id)
        return receipt is not None and receipt.final_admitted_tier in _LOCAL_TIERS

    def _provider_authority(
        self, provider: str, *, read_tiers: frozenset[PrivacyTier] | None = None,
    ) -> tuple[EffectiveAuthority, ProviderConsentState]:
        """Cloud ceiling: consent tiers within public/work/personal, filtered by the core."""
        if read_tiers is not None and (
            not isinstance(read_tiers, frozenset)
            or any(not isinstance(tier, PrivacyTier) for tier in read_tiers)
        ):
            raise _RecallRefusal("invalid_request")
        state = self._consent.load()
        active = [r for r in state.records if r.active and r.provider_id == provider]
        if len(active) != 1:
            raise _RecallRefusal("consent_unavailable")
        consent = state.active_consent(
            consent_id=active[0].consent_id,
            provider_id=provider,
            authorization_generation=state.authorization_generation,
        )
        tiers = consent.allowed_tiers & _CLOUD_TIERS
        if read_tiers is not None:
            tiers &= read_tiers
            if not tiers:
                raise _RecallRefusal("provider_denied")
        return EffectiveAuthority(
            "harness-" + provider,
            "harness-recall",
            frozenset({"search", "content-read"}),
            self._harness_spaces(legacy_local=False),
            authorization_generation=state.authorization_generation,
            allowed_read_tiers=tiers,
            egress_mode=EgressMode.EXTERNAL_PROVIDER,
            provider_id=provider,
            consent_id=consent.consent_id,
            brain_id=self._consent.brain_id,
            issuer_epoch=self._consent.issuer_epoch,
            session_origin=SessionOrigin.MCP_POLICY,
        ), state

    def _local_authority(self, project: Path) -> EffectiveAuthority:
        if not self._local_only_host:
            raise ValueError("local_only_host_required")
        self.policy().classify(project)  # Exclusions and opt-out still apply.
        return EffectiveAuthority(
            self.tasks.profile.owner_actor_id,
            "harness-local-recall",
            frozenset({"search", "content-read"}),
            self._harness_spaces(legacy_local=True),
            owner=True,
            allowed_read_tiers=_LOCAL_TIERS,
            session_origin=SessionOrigin.MCP_POLICY,
        )

    def recall_local(self, query: str, *, project: Path, limit: int = 5) -> HarnessRecallResult:
        if not self._local_only_host:
            raise ValueError("local_only_host_required")
        try:
            return self._recall(query, self._local_authority(project), limit, None)
        except (HarnessPolicyError, _RecallRefusal) as error:
            return HarnessRecallResult("refused", error_code=error.code)
        except Exception:
            return HarnessRecallResult("unavailable", error_code="recall_unavailable")

    def _recall(
        self,
        query: str,
        authority: EffectiveAuthority,
        limit: int,
        consent: ProviderConsentState | None,
    ) -> HarnessRecallResult:
        if type(limit) is not int or not 1 <= limit <= 5:
            raise ValueError("invalid_limit")
        plan = plan_task_query(query)
        if not plan.search_text:
            self._require_current(consent)
            return HarnessRecallResult("success_empty")
        response = self._search(plan.search_text, authority, consent)
        candidates = [
            (row["record_id"], self._read_memory(row["record_id"], authority, consent))
            for row in response["results"]
            if row["provenance"]["source_origin"] == "third_party"
            and self._permitted(row["record_id"], authority)
        ]
        changes = (
            self._decision_changes(authority, consent)
            if any(memory.kind == "decision" for _, memory in candidates)
            else ()
        )
        ranked = sorted(
            (
                (plan.relevance(memory), capture_id, memory)
                for capture_id, memory in candidates
                if decision_state(memory, changes) == "current" and plan.relevance(memory) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )[:limit]
        self._require_current(consent)
        return HarnessRecallResult(
            "success" if ranked else "success_empty",
            tuple(memory for _, _, memory in ranked),
            memory_ids=tuple(capture_id for _, capture_id, _ in ranked),
            memory_states=tuple("current" for _ in ranked),
        )

    def _require_current(self, consent: ProviderConsentState | None) -> None:
        if not self.consent_current(consent):
            raise _RecallRefusal("authorization_changed")

    def _search(
        self,
        query: str,
        authority: EffectiveAuthority,
        consent: ProviderConsentState | None,
    ) -> dict[str, Any]:
        self._require_current(consent)
        result = cast(
            dict[str, Any],
            self.tasks.retrieval.search_page(
                SearchPageRequest(
                    query=query,
                    limit=100,
                    filters={
                        "space_ids": [],
                        "payload_families": ["text"],
                        "record_types": ["source"],
                    },
                ),
                authority=authority,
            ).to_wire(),
        )
        self._require_current(consent)
        if not result["complete"]:
            raise _RecallRefusal("retrieval_budget_exceeded")
        return result

    def _read_memory(
        self,
        capture_id: str,
        authority: EffectiveAuthority,
        consent: ProviderConsentState | None,
    ) -> HarnessMemory:
        # Capture IDs bind immutable source revisions. Never admit page/owner IDs.
        if (
            not isinstance(capture_id, str)
            or not capture_id.startswith("capture_")
            or not self._permitted(capture_id, authority)
        ):
            raise _RecallRefusal("source_unavailable")
        chunks: list[str] = []
        cursor: str | None = None
        for _ in range(3):
            self._require_current(consent)
            response = cast(
                dict[str, Any],
                self.tasks.retrieval.read_record(
                    RecordReadRequest(
                        record_id=capture_id,
                        expected_revision_id=capture_id,
                        target_bytes=32768,
                        cursor=cursor,
                    ),
                    authority=authority,
                ).to_wire(),
            )
            if (
                response["record"]["record_type"] != "source"
                or response["record"]["provenance"]["source_origin"] != "third_party"
            ):
                raise _RecallRefusal("source_unavailable")
            chunks.append(response["content"]["text"])
            if response["complete"]:
                memory = decode_memory("".join(chunks))
                self._require_current(consent)
                return memory
            cursor = response["next_cursor"]
        raise _RecallRefusal("source_unavailable")

    def _decision_changes(
        self,
        authority: EffectiveAuthority,
        consent: ProviderConsentState | None,
    ) -> tuple[HarnessMemory, ...]:
        # Search is authority-scoped BEFORE matching, counts, or currentness evaluation.
        rows = self._search("changed instead now", authority, consent)["results"]
        return tuple(
            self._read_memory(row["record_id"], authority, consent)
            for row in rows
            if row["provenance"]["source_origin"] == "third_party"
            and self._permitted(row["record_id"], authority)
        )

    def read_context(
        self, capture_id: str, *, provider: str,
        read_tiers: frozenset[PrivacyTier] | None = None,
    ) -> HarnessRecallResult:
        """Resolve only an authorized retained harness source, including historical decisions."""
        try:
            authority, state = self._provider_authority(provider, read_tiers=read_tiers)
            return self._context(capture_id, authority, state)
        except (HarnessPolicyError, _RecallRefusal) as error:
            return HarnessRecallResult("refused", error_code=error.code)
        except T03Error:
            return HarnessRecallResult("refused", error_code="source_unavailable")
        except Exception:
            return HarnessRecallResult("unavailable", error_code="source_read_unavailable")

    def read_context_local(self, capture_id: str, *, project: Path) -> HarnessRecallResult:
        if not self._local_only_host:
            raise ValueError("local_only_host_required")
        try:
            return self._context(capture_id, self._local_authority(project), None)
        except (HarnessPolicyError, _RecallRefusal) as error:
            return HarnessRecallResult("refused", error_code=error.code)
        except T03Error:
            return HarnessRecallResult("refused", error_code="source_unavailable")
        except Exception:
            return HarnessRecallResult("unavailable", error_code="source_read_unavailable")

    def _context(
        self,
        capture_id: str,
        authority: EffectiveAuthority,
        consent: ProviderConsentState | None,
    ) -> HarnessRecallResult:
        memory = self._read_memory(capture_id, authority, consent)
        changes = self._decision_changes(authority, consent) if memory.kind == "decision" else ()
        state = decision_state(memory, changes)
        self._require_current(consent)
        return HarnessRecallResult(
            "success", (memory,), memory_ids=(capture_id,), memory_states=(state,),
        )

    def consent_snapshot(self) -> ProviderConsentState:
        return self._consent.load()

    def consent_current(self, consent: ProviderConsentState | None) -> bool:
        """A revoked or replaced provider consent denies release immediately."""
        return consent is None or self._consent.load() == consent
