"""Durable full-identity Slack hints, separate from owner capture authorization."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.slack_discovery import (
    SlackMessageDiscoveryClient,
)
from open_brain_connectors.runtime.slack_live import SlackSourceClient, _channel_id, _timestamp
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

Configure = Callable[[str, SourceResourceSelection, dict[str, object]], object]


class CaptureDiscoveryCoordinator:
    def __init__(
        self,
        root: Path,
        *,
        configuration: Callable[[], dict[str, object]],
        discover: SlackMessageDiscoveryClient,
        source: SlackSourceClient,
        configure: Configure,
        clock: Callable[[], int] = lambda: int(time.time()),
    ) -> None:
        self.store = PrivateJsonStore(root / "capture-discovery")
        self.configuration = configuration
        self.discover = discover
        self.source = source
        self.configure = configure
        self.clock = clock

    @staticmethod
    def _name(account: str) -> str:
        if type(account) is not str or not account.startswith("account:"):
            raise LiveSourceError("source_invalid_connection")
        return "discovery-" + hashlib.sha256(account.encode()).hexdigest() + ".json"

    def _load(self, account: str) -> dict[str, object]:
        value = self.store.read(self._name(account))
        if value is None:
            return {
                "schema_version": 1,
                "generation": None,
                "oldest": None,
                "completed_at": None,
                "checkpoint": None,
                "has_more": True,
                "candidates": [],
                "sources": [],
            }
        required = {
            "schema_version",
            "generation",
            "oldest",
            "completed_at",
            "checkpoint",
            "has_more",
            "candidates",
            "sources",
        }
        if (
            not isinstance(value, dict)
            or set(value) != required
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or type(value["has_more"]) is not bool
            or not isinstance(value["candidates"], list)
            or len(value["candidates"]) > 500
            or not isinstance(value["sources"], list)
            or len(value["sources"]) > 500
            or value["checkpoint"] is not None
            and not isinstance(value["checkpoint"], dict)
        ):
            raise LiveSourceError("capture_discovery_invalid_state")
        if value["generation"] is not None and (
            type(value["generation"]) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", value["generation"])
        ):
            raise LiveSourceError("capture_discovery_invalid_state")
        for item in value["candidates"]:
            if (
                not isinstance(item, dict)
                or set(item)
                != {"connection_id", "conversation_id", "message_ts", "thread_ts", "state"}
                or item["connection_id"] != account
                or item["state"] not in {"pending", "configured", "excluded", "unsupported"}
            ):
                raise LiveSourceError("capture_discovery_invalid_state")
            _channel_id(item["conversation_id"])
            _timestamp(item["message_ts"])
            if item["thread_ts"] is not None:
                _timestamp(item["thread_ts"])
        for source in value["sources"]:
            if (
                not isinstance(source, dict)
                or set(source)
                != {"source_id", "conversation_id", "root_ts", "generation", "configured"}
                or type(source["source_id"]) is not str
                or not re.fullmatch(r"slack-extra\.[0-9a-f]{24}", source["source_id"])
                or type(source["generation"]) is not str
                or not re.fullmatch(r"[0-9a-f]{64}", source["generation"])
                or type(source["configured"]) is not bool
            ):
                raise LiveSourceError("capture_discovery_invalid_state")
            _channel_id(source["conversation_id"])
            _timestamp(source["root_ts"])
        if value["oldest"] is not None:
            _timestamp(value["oldest"])
        if value["completed_at"] is not None and type(value["completed_at"]) is not int:
            raise LiveSourceError("capture_discovery_invalid_state")
        return cast(dict[str, object], value)

    def sources(self, account: str) -> list[dict[str, object]]:
        return cast(list[dict[str, object]], self._load(account)["sources"])

    @staticmethod
    def _allowed(config: dict[str, object], conversation: str) -> bool:
        return (
            config["enabled"] is True
            and config["ai_exclusions_verified"] is True
            and bool(config["capabilities"])
            and conversation in cast(list[str], config["permitted_conversations"])
            and conversation not in cast(list[str], config["excluded_conversations"])
        )

    def run(self, account: str) -> dict[str, object]:
        name = self._name(account)
        with self.store.lock(name, timeout_seconds=0):
            config = self.configuration()
            if (
                config["enabled"] is not True
                or not config["capabilities"]
                or config["ai_exclusions_verified"] is not True
            ):
                return {
                    "outcome": "skipped",
                    "has_more": False,
                    "notices": ["discovery_disabled_or_exclusions_unverified"],
                }
            generation = hashlib.sha256(bounded_json(config)).hexdigest()
            state = self._load(account)
            if state["generation"] != generation:
                # Retain source registry for root reconciliation, abandon old hints only.
                state.update(
                    generation=generation,
                    oldest=None,
                    completed_at=None,
                    checkpoint=None,
                    has_more=True,
                    candidates=[],
                )
            now = self.clock()
            if type(now) is not int or now < 259200:
                raise LiveSourceError("capture_discovery_invalid_clock")
            if state["completed_at"] is not None and now - cast(int, state["completed_at"]) < 86400:
                return {"outcome": "skipped", "has_more": False, "notices": ["discovery_not_due"]}
            if state["completed_at"] is not None:
                state.update(
                    oldest=None, completed_at=None, checkpoint=None, has_more=True, candidates=[]
                )
            if state["oldest"] is None:
                state["oldest"] = f"{now - 259200:.6f}"
            pending = cast(list[dict[str, object]], state["candidates"])
            notices = ["incomplete_search_coverage", "untracked_older_threads_unsupported"]
            if not any(item["state"] == "pending" for item in pending) and state["has_more"]:
                batch = self.discover.discover(
                    account,
                    owner_id=cast(str, config["owner_id"]),
                    capabilities=tuple(cast(list[str], config["capabilities"])),
                    excluded_conversations=tuple(cast(list[str], config["excluded_conversations"])),
                    ai_exclusions_verified=True,
                    oldest=cast(str, state["oldest"]),
                    checkpoint=cast(dict[str, object] | None, state["checkpoint"]),
                )
                notices = list(batch.notices)
                known = {
                    tuple(item[key] for key in ("conversation_id", "message_ts", "thread_ts"))
                    for item in pending
                }
                for candidate in batch.candidates:
                    identity = (
                        candidate.conversation_id,
                        candidate.message_ts,
                        candidate.thread_ts,
                    )
                    if identity not in known:
                        pending.append({**asdict(candidate), "state": "pending"})
                        known.add(identity)
                if len(pending) > 500:
                    raise LiveSourceError("capture_discovery_capacity")
                state.update(
                    checkpoint=batch.checkpoint, has_more=batch.has_more, candidates=pending
                )
                self.store.write(
                    name, state
                )  # Full hints and cursor precede any scheduling effect.
            configured = 0
            sources = cast(list[dict[str, object]], state["sources"])
            for item in [entry for entry in pending if entry["state"] == "pending"][:25]:
                conversation = cast(str, item["conversation_id"])
                current = self.configuration()
                if (
                    not self._allowed(current, conversation)
                    or hashlib.sha256(bounded_json(current)).hexdigest() != generation
                ):
                    item["state"] = "excluded"
                    continue
                try:
                    record = self.source.resolve_candidate(
                        account,
                        conversation,
                        cast(str, item["message_ts"]),
                        thread_ts=cast(str | None, item["thread_ts"]),
                    )
                except LiveSourceError as error:
                    if error.code != "source_candidate_identity_unsupported":
                        raise
                    item["state"] = "unsupported"
                    notices.append("source_candidate_identity_unsupported")
                    continue
                current = self.configuration()
                if (
                    not self._allowed(current, conversation)
                    or hashlib.sha256(bounded_json(current)).hexdigest() != generation
                ):
                    item["state"] = "excluded"
                    continue
                root = record.thread_ts or record.message_ts
                source_id = (
                    "slack-extra."
                    + hashlib.sha256(
                        bounded_json([account, conversation, root, generation])
                    ).hexdigest()[:24]
                )
                selected = SourceResourceSelection(
                    "slack", account, "channel:" + conversation, "channel"
                )
                options: dict[str, object] = {
                    "tracked_thread": root,
                    "privacy_tier": "unknown",
                    "date_floor": datetime.fromtimestamp(float(cast(str, state["oldest"])), UTC)
                    .isoformat()
                    .replace("+00:00", "Z"),
                }
                registered = next(
                    (entry for entry in sources if entry["source_id"] == source_id), None
                )
                if registered is None:
                    if len(sources) >= 500:
                        raise LiveSourceError("capture_discovery_capacity")
                    # Registry entry precedes configure, so a crash leaves a discoverable retry.
                    sources.append(
                        {
                            "source_id": source_id,
                            "conversation_id": conversation,
                            "root_ts": root,
                            "generation": generation,
                            "configured": False,
                        }
                    )
                    self.store.write(name, state)
                registered = next(entry for entry in sources if entry["source_id"] == source_id)
                if registered["configured"] is not True:
                    self.configure(source_id, selected, options)
                    registered["configured"] = True
                item["state"] = "configured"
                self.store.write(name, state)
                configured += 1
            remaining = any(item["state"] == "pending" for item in pending)
            more = state["has_more"] or remaining
            if not more:
                state["completed_at"] = now
            self.store.write(name, state)
            return {
                "outcome": "completed",
                "configured_sources": configured,
                "candidate_count": len(pending),
                "has_more": more,
                "notices": sorted(set(notices)),
            }
