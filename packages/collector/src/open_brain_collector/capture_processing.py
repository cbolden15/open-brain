"""Bounded, recoverable processing after immutable raw capture acceptance."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from open_brain_collector.capture_contracts import Development, SourceObservation, digest
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

Extractor = Callable[[SourceObservation], tuple[Development, ...]]
DerivedCapture = Callable[[Development, SourceObservation, str], str]


@dataclass(frozen=True, slots=True)
class ProcessingPolicy:
    provider: str = "local"
    allowed_tiers: tuple[str, ...] = ("public", "work", "personal")
    max_input_bytes: int = 262_144
    max_developments: int = 25
    max_calls: int = 25
    external_processing: bool = False

    def __post_init__(self) -> None:
        if (type(self.provider) is not str or not self.provider or len(self.provider) > 100
            or type(self.allowed_tiers) is not tuple or not self.allowed_tiers
            or any(t not in {"public", "work", "personal"} for t in self.allowed_tiers)
            or any(type(n) is not int or not 1 <= n <= 262_144 for n in
                   (self.max_input_bytes, self.max_developments, self.max_calls))
            or type(self.external_processing) is not bool
            or (self.provider != "local" and not self.external_processing)):
            raise LiveSourceError("processing_invalid_policy")


def local_extract(observation: SourceObservation) -> tuple[Development, ...]:
    """Extract explicit labeled statements only; preserve their exact source wording."""
    results = []
    for line in observation.text.splitlines():
        match = re.fullmatch(
            r"(Requested|Proposed|Committed|Approved|Decision|Correction):\s+(.{1,4000})", line
        )
        if match is None:
            continue
        results.append(Development(
            claim=match[2], kind=match[1].lower(), actor="unspecified", subject=match[2],
            environment="", event_time=observation.message_ts,
            supporting_capture_ids=(observation.capture_id,),
            supporting_revision_ids=(observation.revision_id,),
            uncertainty="Actor and scope require review", quote=line,
        ))
    return tuple(results)


class CaptureProcessingStore:
    def __init__(self, store: PrivateJsonStore, *, max_observations: int = 1000,
                 max_bytes: int = 2_097_152) -> None:
        if (type(max_observations) is not int or not 1 <= max_observations <= 10_000
            or type(max_bytes) is not int or not 1024 <= max_bytes <= 4_194_304):
            raise LiveSourceError("processing_invalid_limits")
        self.store = store
        self.max_observations = max_observations
        self.max_bytes = max_bytes

    def _load(self) -> dict[str, object]:
        value = self.store.read("capture-processing-v1.json")
        if value is None:
            return {"schema_version": 1, "observations": {}, "operations": {}}
        if (not isinstance(value, dict) or set(value) !=
            {"schema_version", "observations", "operations"} or value["schema_version"] != 1
            or not isinstance(value["observations"], dict)
            or not isinstance(value["operations"], dict)):
            raise LiveSourceError("processing_invalid_state")
        return cast(dict[str, object], value)

    def _save(self, state: dict[str, object]) -> None:
        bounded_json(state, self.max_bytes)
        self.store.write("capture-processing-v1.json", state)

    def admit(self, observation: SourceObservation) -> str:
        with self.store.lock("capture-processing"):
            state = self._load()
            observations = cast(dict[str, dict[str, object]], state["observations"])
            key = observation.observation_id
            if key in observations:
                previous = SourceObservation.from_dict(observations[key]["observation"])
                # Discovery time/context are nonidentity metadata; source bytes must agree.
                if (previous.text, previous.capture_id, previous.privacy) != (
                    observation.text, observation.capture_id, observation.privacy
                ):
                    raise LiveSourceError("processing_revision_conflict")
                return key
            if len(observations) >= self.max_observations:
                raise LiveSourceError("processing_queue_full")
            observations[key] = {"observation": observation.to_dict(), "state": "pending",
                                 "operation_ids": []}
            self._save(state)
            return key

    def process(self, policy: ProcessingPolicy, capture: DerivedCapture, *,
                extractor: Extractor | None = None,
                postprocess: Callable[[Development, str, str], None] | None = None
                ) -> dict[str, object]:
        if not isinstance(policy, ProcessingPolicy) or not callable(capture):
            raise LiveSourceError("processing_invalid_policy")
        if extractor is not None and policy.provider == "local":
            raise LiveSourceError("processing_extractor_requires_policy")
        if policy.provider != "local" and extractor is None:
            raise LiveSourceError("processing_provider_unavailable")
        extract = local_extract if extractor is None else extractor
        counts = {"processed": 0, "captures": 0, "held": 0, "budget_exhausted": False}
        calls = 0
        input_bytes = 0
        with self.store.lock("capture-processing", timeout_seconds=0):
            state = self._load()
            observations = cast(dict[str, dict[str, object]], state["observations"])
            operations = cast(dict[str, dict[str, object]], state["operations"])
            for key, entry in observations.items():
                observation = SourceObservation.from_dict(entry["observation"])
                if entry["state"] == "complete":
                    continue
                if (observation.ai_excluded or observation.privacy.tier.value not in
                    policy.allowed_tiers or (policy.external_processing and not
                    (observation.privacy.authority.cloud and
                     observation.privacy.authority.external_egress))):
                    counts["held"] += 1
                    continue
                if entry["state"] == "pending":
                    size = len((observation.text + observation.thread_context).encode())
                    if calls >= policy.max_calls or input_bytes + size > policy.max_input_bytes:
                        counts["budget_exhausted"] = True
                        break
                    calls += 1
                    input_bytes += size
                    developments = extract(observation)
                    if (type(developments) is not tuple or
                        len(developments) > policy.max_developments
                        or any(type(d) is not Development for d in developments)):
                        raise LiveSourceError("processing_invalid_extraction")
                    ids = []
                    for development in developments:
                        if (development.quote not in observation.text or
                            development.supporting_capture_ids != (observation.capture_id,) or
                            development.supporting_revision_ids != (observation.revision_id,)):
                            raise LiveSourceError("processing_invalid_evidence")
                        material = development.to_dict()
                        material.pop("supporting_capture_ids")
                        material.pop("supporting_revision_ids")
                        operation_id = "development:" + digest([
                            observation.workspace_id, observation.conversation_id,
                            observation.message_ts, material,
                        ])
                        ids.append(operation_id)
                        operations.setdefault(operation_id, {
                            "observation_id": key, "development": development.to_dict(),
                            "capture_id": None, "postprocessed": False,
                        })
                    entry["operation_ids"] = ids
                    entry["state"] = "prepared"
                    self._save(state)  # Reserve immutable operations before external acceptance.
                for operation_id in cast(list[str], entry["operation_ids"]):
                    operation = operations[operation_id]
                    if operation["capture_id"] is None:
                        development = Development.from_dict(operation["development"])
                        bound_observation = SourceObservation.from_dict(
                            observations[cast(str, operation["observation_id"])]["observation"]
                        )
                        capture_id = capture(development, bound_observation, operation_id)
                        if type(capture_id) is not str or not capture_id or len(capture_id) > 512:
                            raise LiveSourceError("processing_invalid_receipt")
                        operation["capture_id"] = capture_id
                        self._save(state)
                        counts["captures"] += 1
                    if postprocess is not None and not operation["postprocessed"]:
                        postprocess(Development.from_dict(operation["development"]),
                                    cast(str, operation["capture_id"]), operation_id)
                        operation["postprocessed"] = True
                        self._save(state)
                entry["state"] = "complete"
                self._save(state)
                counts["processed"] += 1
        return cast(dict[str, object], counts)
