"""Private opt-in orchestration for evidence-bound Slack developments."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

from open_brain_engine.engine import (
    CaptureReceipt,
    CaptureSubmission,
    SourceRevisionBinding,
    SourceRevisionSubmission,
    open_local_engine,
)
from open_brain_engine.engine.source_observation import SourceRevisionObservation
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain_collector.capture_contracts import Development, SourceObservation, digest
from open_brain_collector.capture_processing import CaptureProcessingStore, ProcessingPolicy
from open_brain_collector.lifecycle import EngineCaptureSink
from open_brain_collector.runner import (
    _open_existing_collector_profile,
    collector_capture_sink,
    collector_revision_sink,
)
from open_brain_collector.slack_policy import SlackPolicyStore
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json, safe_text
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection


def automation_authority() -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="collector-development-v1",
        session_id="collector-development-v1",
        capabilities=frozenset(
            {
                "development-bind",
                "review-propose",
                "review-read",
                "facet-read",
                "facet-assign",
                "search",
            }
        ),
        space_ids=None,
    )


def owner_authority() -> EffectiveAuthority:
    return EffectiveAuthority(
        principal_id="collector-owner-review",
        session_id="collector-owner-review",
        capabilities=frozenset(),
        space_ids=None,
        owner=True,
    )


def _default() -> dict[str, object]:
    return {
        "schema_version": 1,
        "enabled": False,
        "ai_exclusions_verified": False,
        "excluded_conversations": [],
        "capabilities": [],
        "owner_id": None,
        "permitted_conversations": [],
        "candidate_dimensions": {},
        "max_calls": 25,
        "max_input_bytes": 262_144,
        "max_developments": 25,
    }


def _validate(value: object) -> dict[str, object]:
    from open_brain_connectors.runtime.slack_auth import slack_user_scopes

    if not isinstance(value, dict) or set(value) != set(_default()) or value["schema_version"] != 1:
        raise LiveSourceError("capture_invalid_configuration")
    for key in ("enabled", "ai_exclusions_verified"):
        if type(value[key]) is not bool:
            raise LiveSourceError("capture_invalid_configuration")
    for key in ("excluded_conversations", "permitted_conversations", "capabilities"):
        if not isinstance(value[key], list) or len(value[key]) > 500:
            raise LiveSourceError("capture_invalid_configuration")
        for item in value[key]:
            safe_text(item, maximum=128)
        if len(value[key]) != len(set(value[key])):
            raise LiveSourceError("capture_invalid_configuration")
    slack_user_scopes(tuple(value["capabilities"]))
    if value["capabilities"] and value["owner_id"] is None:
        raise LiveSourceError("capture_owner_identity_required")
    if value["owner_id"] is not None:
        safe_text(value["owner_id"], maximum=128)
    dimensions = value["candidate_dimensions"]
    if not isinstance(dimensions, dict) or set(dimensions) - {"actor", "subject", "environment"}:
        raise LiveSourceError("capture_invalid_configuration")
    for item in dimensions.values():
        safe_text(item, maximum=128)
    ProcessingPolicy(
        max_calls=value["max_calls"],
        max_input_bytes=value["max_input_bytes"],
        max_developments=value["max_developments"],
    )
    bounded_json(value, 65_536)
    return cast(dict[str, object], value)


class CaptureWorkflow:
    def __init__(self, root: Path, brain_root: Path, policy: SlackPolicyStore) -> None:
        self.root = root
        self.brain_root = brain_root
        self.policy = policy
        self.store = PrivateJsonStore(root / "developments")
        self.queue = CaptureProcessingStore(self.store)

    def configuration(self) -> dict[str, object]:
        value = self.store.read("configuration-v1.json")
        return _validate(_default() if value is None else value)

    def configure(self, values: dict[str, object]) -> dict[str, object]:
        if set(values) - set(_default()) or "schema_version" in values:
            raise LiveSourceError("capture_invalid_configuration")
        with self.store.lock("capture-configuration"):
            current = self.configuration()
            updated = _validate({**current, **values})
            self.store.write("configuration-v1.json", updated)
            return updated

    def admit(
        self, selection: SourceResourceSelection, records: Sequence[tuple[SourceRecordIntake, str]]
    ) -> None:
        if selection.connector_name != "slack":
            return
        config = self.configuration()
        if config["enabled"] is not True:
            return
        conversation = selection.resource_id.removeprefix("channel:")
        context = "\n".join(item.text for item, _ in records)
        for intake, capture_id in records:
            message = intake.key.external_id.split(":")[-1]
            self.queue.admit(
                SourceObservation(
                    workspace_id=selection.connection_id,
                    conversation_id=conversation,
                    message_ts=message,
                    revision_id=intake.key.revision_identity(),
                    observed_at=datetime.now(UTC).isoformat(),
                    source_link=intake.url,
                    text=intake.text,
                    thread_context=context,
                    capture_id=capture_id,
                    privacy=intake.privacy,
                    ai_excluded=(
                        config["ai_exclusions_verified"] is not True
                        or conversation in cast(list[str], config["excluded_conversations"])
                    ),
                )
            )

    def status(self) -> dict[str, object]:
        state = self.queue._load()
        observations = cast(dict[str, dict[str, object]], state["observations"])
        pending = [item for item in observations.values()
                   if item["state"] not in {"complete", "superseded"}]
        observed_epochs: list[int] = []
        for item in pending:
            observation = SourceObservation.from_dict(item["observation"])
            try:
                observed = datetime.fromisoformat(observation.observed_at)
                if observed.tzinfo is not None and observed.timestamp() >= 0:
                    observed_epochs.append(int(observed.timestamp()))
            except ValueError:
                pass
        return {
            "schema_version": 1,
            "enabled": self.configuration()["enabled"],
            "observation_count": len(observations),
            "pending_count": len(pending),
            "superseded_count": sum(item["state"] == "superseded"
                                    for item in observations.values()),
            "failure_codes": sorted({cast(str, item["failure_code"])
                                     for item in observations.values() if "failure_code" in item}),
            "oldest_pending_epoch": min(observed_epochs)
            if observed_epochs and len(observed_epochs) == len(pending) else None,
            "development_count": len(cast(dict[str, object], state["operations"])),
        }

    def process(self) -> dict[str, object]:
        config = self.configuration()
        if config["enabled"] is not True:
            return {"outcome": "disabled", **self.status()}

        def preflight(observation: SourceObservation) -> None:
            tasks = open_local_engine(_open_existing_collector_profile(self.brain_root))
            assert tasks.developments is not None
            try:
                tasks.developments.validate_support(
                    (observation.capture_id,), authority=automation_authority(),
                )
            except T03Error as error:
                if error.code == "revision_changed":
                    raise LiveSourceError("processing_evidence_superseded") from None
                raise

        # Recheck current policy before any extraction, including retained work.
        def capture(
            development: Development, observation: SourceObservation, operation_id: str
        ) -> str:
            current = self.configuration()
            if (
                current["enabled"] is not True
                or current["ai_exclusions_verified"] is not True
                or observation.conversation_id in cast(list[str], current["excluded_conversations"])
            ):
                raise LiveSourceError("capture_processing_excluded")
            preflight(observation)
            operations = cast(dict[str, dict[str, Any]], self.queue._load()["operations"])
            operation = operations[operation_id]
            semantic_id = operation.get("semantic_id")
            payload = json.dumps(development.to_dict(), sort_keys=True, ensure_ascii=False)
            intake = SourceRecordIntake(
                SourceRecordKey(
                    "slack_development",
                    observation.workspace_id,
                    "channel:" + observation.conversation_id,
                    operation_id if semantic_id is None else semantic_id,
                    digest(development.to_dict()),
                ),
                observation.source_link,
                payload,
                observation.privacy,
                title=(development.kind + ": " + development.claim)[:200],
            )
            if semantic_id is None:
                # Preserve the exact request of an older queue's uncertain capture.
                receipt = EngineCaptureSink(collector_capture_sink(self.brain_root)).submit(intake)
                if not isinstance(receipt, CaptureReceipt):
                    raise LiveSourceError("capture_development_pending")
                capture_id = receipt.capture_id
            else:
                self._adopt_legacy(semantic_id, operations)
                privacy = intake.privacy.to_dict()
                content_digest = sha256(intake.text.encode()).hexdigest()
                intake = replace(intake, observation=SourceRevisionObservation(
                    original_sha256=content_digest, transformed_sha256=content_digest,
                    normalization_version="slack-development-v1",
                    privacy_policy_version=cast(str, privacy["policy_version"]),
                    privacy_policy_sha256=sha256(bounded_json(privacy)).hexdigest(),
                    admitted_payload_sha256=sha256(bounded_json(intake.payload().to_dict())).hexdigest(),
                ))
                result = collector_revision_sink(
                    self.brain_root, self.root / "development-revisions",
                ).submit(intake)
                if (result.source_receipt is None or result.source_receipt.capture_id is None
                    or result.outcome not in {"captured", "duplicate"}):
                    raise LiveSourceError("capture_development_pending")
                capture_id = result.source_receipt.capture_id
            return capture_id

        if config["ai_exclusions_verified"] is not True:
            return {"outcome": "held", "failure_code": "unsupported_ai_exclusions", **self.status()}
        with self.store.lock("capture-processing", timeout_seconds=0):
            retained = self.queue._load()
            for entry in cast(dict[str, dict[str, object]], retained["observations"]).values():
                observation = cast(dict[str, object], entry["observation"])
                observation["ai_excluded"] = observation["conversation_id"] in cast(
                    list[str], config["excluded_conversations"]
                )
            self.queue._save(retained)
        result = self.queue.process(
            ProcessingPolicy(
                max_calls=cast(int, config["max_calls"]),
                max_input_bytes=cast(int, config["max_input_bytes"]),
                max_developments=cast(int, config["max_developments"]),
            ),
            capture,
            postprocess=self._postprocess,
            preflight=preflight,
        )
        return result

    def _adopt_legacy(self, semantic_id: str, operations: dict[str, dict[str, Any]]) -> None:
        """Bind a pre-revision capture only through the engine's exact-request adoption."""
        legacy = operations.get(semantic_id)
        if legacy is None or "semantic_id" in legacy:
            return
        if legacy["capture_id"] is None:
            raise LiveSourceError("processing_legacy_adoption_conflict")
        development = Development.from_dict(legacy["development"])
        observations = cast(dict[str, dict[str, Any]], self.queue._load()["observations"])
        observation = SourceObservation.from_dict(
            observations[legacy["observation_id"]]["observation"],
        )
        intake = SourceRecordIntake(
            SourceRecordKey("slack_development", observation.workspace_id,
                            "channel:" + observation.conversation_id, semantic_id,
                            digest(development.to_dict())),
            observation.source_link,
            json.dumps(development.to_dict(), sort_keys=True, ensure_ascii=False),
            observation.privacy, title=(development.kind + ": " + development.claim)[:200],
        )
        tasks = open_local_engine(_open_existing_collector_profile(self.brain_root))
        assert tasks.sources is not None
        sink = collector_capture_sink(self.brain_root)
        identity, fingerprint = sink.brain_identity, sink.brain_fingerprint
        if identity is None or fingerprint is None:
            raise LiveSourceError("source_brain_unavailable")
        namespace = {
            "connector_name": intake.key.connector_name, "connection_id": intake.key.connection_id,
            "resource_id": intake.key.resource_id, "external_id": intake.key.external_id,
        }
        binding = SourceRevisionBinding(
            destination_brain_id=identity[0], issuer_epoch=identity[1],
            root_fingerprint=fingerprint,
            accepted_source_id=intake.key.connection_id, namespace=namespace,
        )
        name = "legacy-adoption-" + digest(semantic_id) + ".json"
        reserved = self.store.read(name)
        if reserved is None:
            head = tasks.sources.public_revision_sink(binding).inspect_head()
            reserved = {"control_epoch": head.control_epoch}
            self.store.write(name, reserved)
        if not isinstance(reserved, dict) or type(reserved.get("control_epoch")) is not int:
            raise LiveSourceError("processing_invalid_state")
        capture = CaptureSubmission.for_public_job(
            context=sink.context, payload=intake.payload(), delivery_id=intake.key.delivery_id(),
            source_origin="third_party", source_reference=intake.source_reference,
            provenance=intake.provenance(), privacy=intake.privacy, intent="reference",
            title=intake.title,
        )
        try:
            tasks.sources.submit_revision(SourceRevisionSubmission(
                capture=capture, namespace=namespace, revision_key="legacy-" + digest(semantic_id),
                canonical_sha256=capture.request_sha256(), expected_head=legacy["capture_id"],
                ordering={"kind": "unordered"}, expected_control_epoch=reserved["control_epoch"],
            ))
        except (T03Error, ValueError):
            raise LiveSourceError("processing_legacy_adoption_conflict") from None

    def _postprocess(
        self, development: Development, capture_id: str, operation_id: str,
    ) -> tuple[str, ...]:
        from open_brain_collector.capture_review import CaptureReviewStore

        review = CaptureReviewStore(PrivateJsonStore(self.root / "capture-review"))
        state = self.queue._load()
        operation = cast(dict[str, dict[str, Any]], state["operations"])[operation_id]
        observation = SourceObservation.from_dict(
            cast(dict[str, dict[str, object]], state["observations"])[operation["observation_id"]][
                "observation"
            ]
        )
        config = self.configuration()
        if (
            config["enabled"] is not True
            or config["ai_exclusions_verified"] is not True
            or observation.conversation_id in cast(list[str], config["excluded_conversations"])
        ):
            raise LiveSourceError("capture_processing_excluded")
        tasks = open_local_engine(_open_existing_collector_profile(self.brain_root))
        assert tasks.developments is not None
        try:
            tasks.developments.validate_support(
                development.supporting_capture_ids, authority=automation_authority(),
            )
            tasks.developments.bind(
                capture_id, development.supporting_capture_ids, operation_id=operation_id,
                authority=automation_authority(),
                supports_development_ids=development.supports_development_ids,
                supersedes_development_ids=development.supersedes_development_ids,
            )
        except T03Error as error:
            if error.code == "revision_changed":
                raise LiveSourceError("processing_evidence_changed_pending_recovery") from None
            raise
        self._assign_existing(development, capture_id, operation_id)
        notices: list[str] = []
        for field, dimension in cast(dict[str, str], config["candidate_dimensions"]).items():
            name = cast(str, getattr(development, field))
            if len(name) > 512:
                notices.append("candidate_name_too_long")
                continue
            if name and name != "unspecified":
                review.discover(
                    name,
                    dimension,
                    observation.workspace_id,
                    (capture_id,),
                    priority=10 if development.kind == "committed" else 0,
                )
        policy = self.policy.policy(observation.workspace_id)
        if policy["proposal_opt_in"] is not True:
            return tuple(notices)
        mappings = [
            item
            for item in cast(list[dict[str, object]], policy["mappings"])
            if item["channel_id"] == observation.conversation_id
            and (
                item["keyword"] is None
                or cast(str, item["keyword"]) in development.claim.casefold()
            )
        ]
        specific = [item for item in mappings if item["keyword"] is not None]
        matches = specific or mappings
        if len(matches) != 1:
            return tuple(notices)
        created_at = self._reserve_proposal(operation_id)
        tasks = open_local_engine(_open_existing_collector_profile(self.brain_root))
        proposals = tasks.review.with_authority(automation_authority()).propose_append(
            (capture_id,),
            target_page_id=cast(str, matches[0]["page_id"]),
            append_markdown=f"### {development.kind.capitalize()}\n\n{development.claim}\n\n"
            f"Source: <{observation.source_link}>\n\n> {development.quote}",
            delivery_id="development-proposal-" + digest(operation_id),
        )
        for proposal in proposals:
            review.admit_attention(
                proposal.proposal_id,
                created_at,
                priority=10 if development.kind == "committed" else 0,
                kind="commitment" if development.kind == "committed" else "routine",
            )
        return tuple(notices)

    def _reserve_proposal(self, operation_id: str) -> str:
        with self.store.lock("capture-proposals"):
            loaded = self.store.read("proposal-slots-v1.json")
            if loaded is None:
                slots: dict[str, str] = {}
            elif (
                isinstance(loaded, dict)
                and loaded.get("schema_version") == 1
                and isinstance(loaded.get("slots"), dict)
            ):
                slots = cast(dict[str, str], loaded["slots"])
            else:
                raise LiveSourceError("capture_invalid_proposal_slots")
            if operation_id in slots:
                return slots[operation_id]
            now = datetime.now(UTC)
            if (
                sum(
                    datetime.fromisoformat(value).isocalendar()[:2] == now.isocalendar()[:2]
                    for value in slots.values()
                )
                >= 10
            ):
                raise LiveSourceError("attention_weekly_limit")
            if len(slots) >= 1000:
                raise LiveSourceError("attention_queue_full")
            slots[operation_id] = now.isoformat()
            self.store.write("proposal-slots-v1.json", {"schema_version": 1, "slots": slots})
            return slots[operation_id]

    def _assign_existing(
        self, development: Development, capture_id: str, operation_id: str
    ) -> None:
        from open_brain_engine.engine import facet_contracts as fc

        values = development.existing_facet_ids
        if not values:
            return
        tasks = open_local_engine(_open_existing_collector_profile(self.brain_root))
        assert tasks.facets is not None
        with self.store.lock("capture-facet-assignments"):
            name = "facet-assignment-" + digest(operation_id) + ".json"
            loaded = self.store.read(name)
            if loaded is None:
                state = tasks.facets.assignments_get(
                    fc.AssignmentsGetRequest(record_id=capture_id), authority=automation_authority()
                ).to_wire()
                item = cast(dict[str, Any], state["item"])
                request: dict[str, Any] = {
                    "dto_version": 1,
                    "operation_id": "development-facets-" + digest(operation_id),
                    "record_id": capture_id,
                    "expected_revision_id": item["revision_id"],
                    "expected_version": item["version"],
                    "add_value_ids": list(values),
                    "remove_value_ids": [],
                }
                self.store.write(name, request)
            elif isinstance(loaded, dict):
                request = loaded
            else:
                raise LiveSourceError("capture_invalid_assignment_receipt")
            response = tasks.facets.assignments_patch(
                cast(
                    fc.AssignmentsPatchRequest,
                    fc.request_from_wire("facet.assignment.patch", request),
                ),
                authority=automation_authority(),
            ).to_wire()
            if response["status"] not in {"applied", "duplicate", "unchanged"}:
                raise LiveSourceError("capture_assignment_conflict")
