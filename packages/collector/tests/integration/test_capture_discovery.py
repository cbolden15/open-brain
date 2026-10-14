from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import pytest

from open_brain_collector.capture_discovery import CaptureDiscoveryCoordinator
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.slack import SlackMessageRecord
from open_brain_connectors.runtime.slack_discovery import (
    SlackMessageCandidate,
    SlackMessageDiscoveryBatch,
    SlackMessageDiscoveryClient,
)
from open_brain_connectors.runtime.slack_live import SlackSourceClient
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

ACCOUNT = "account:synthetic"


def config() -> dict[str, object]:
    return {
        "enabled": True,
        "ai_exclusions_verified": True,
        "owner_id": "UOWNER",
        "capabilities": ["mentions"],
        "excluded_conversations": [],
        "permitted_conversations": ["CONE"],
    }


class Discovery:
    calls = 0

    def discover(self, *args: object, **kwargs: object) -> SlackMessageDiscoveryBatch:
        self.calls += 1
        return SlackMessageDiscoveryBatch(
            (SlackMessageCandidate(ACCOUNT, "CONE", "1791900001.000001", "1600000000.000001"),),
            None,
            False,
            ("incomplete_search_coverage",),
        )


class Source:
    calls = 0
    hook: Callable[[], None] | None = None

    def resolve_candidate(self, *args: object, **kwargs: object) -> SlackMessageRecord:
        self.calls += 1
        if self.hook is not None:
            self.hook()
        return SlackMessageRecord(
            "CONE",
            "1791900001.000001",
            "1791900001.000001",
            "https://slack.com/archives/CONE/p1791900001000001",
            "UOWNER",
            "synthetic",
            "1600000000.000001",
        )


def coordinator(
    tmp_path: Path,
    current: dict[str, object],
    source: Source,
    discovery: Discovery,
    configure: Callable[[str, SourceResourceSelection, dict[str, object]], object],
) -> CaptureDiscoveryCoordinator:
    return CaptureDiscoveryCoordinator(
        tmp_path,
        configuration=lambda: current,
        discover=cast(SlackMessageDiscoveryClient, discovery),
        source=cast(SlackSourceClient, source),
        configure=configure,
        clock=lambda: 1791900000,
    )


def test_full_hints_and_old_root_are_durable_before_configure(tmp_path: Path) -> None:
    current = config()
    source, discovery = Source(), Discovery()
    scheduled = []
    holder: list[CaptureDiscoveryCoordinator] = []

    def configure(
        source_id: str, selection: SourceResourceSelection, options: dict[str, object]
    ) -> None:
        store = holder[0]
        state = cast(dict[str, Any], store.store.read(store._name(ACCOUNT)))
        assert state["candidates"][0]["message_ts"] == "1791900001.000001"
        assert state["candidates"][0]["thread_ts"] == "1600000000.000001"
        assert state["checkpoint"] is None
        assert state["sources"][0]["configured"] is False
        scheduled.append((source_id, selection, options))

    store = coordinator(tmp_path, current, source, discovery, configure)
    holder.append(store)
    result = store.run(ACCOUNT)
    assert result["configured_sources"] == 1
    assert scheduled[0][2]["tracked_thread"] == "1600000000.000001"
    assert scheduled[0][2]["privacy_tier"] == "unknown"
    assert store.sources(ACCOUNT)[0]["root_ts"] == "1600000000.000001"
    assert store.run(ACCOUNT)["outcome"] == "skipped"
    assert discovery.calls == source.calls == 1


def test_revocation_before_and_during_canonical_fetch(tmp_path: Path) -> None:
    current = config()
    current["excluded_conversations"] = ["CONE"]
    source, discovery = Source(), Discovery()
    scheduled = []
    store = coordinator(tmp_path, current, source, discovery, lambda *args: scheduled.append(args))
    assert store.run(ACCOUNT)["configured_sources"] == 0
    assert source.calls == 0
    current["excluded_conversations"] = []
    source.hook = lambda: current.update(excluded_conversations=["CONE"])
    assert store.run(ACCOUNT)["configured_sources"] == 0
    assert source.calls == 1
    assert scheduled == []


def test_lost_configure_ack_retries_frozen_full_hint(tmp_path: Path) -> None:
    current = config()
    source, discovery = Source(), Discovery()
    attempts = []

    def configure(*args: object) -> None:
        attempts.append(args)
        if len(attempts) == 1:
            raise RuntimeError("synthetic lost ack")

    store = coordinator(tmp_path, current, source, discovery, configure)
    with pytest.raises(RuntimeError):
        store.run(ACCOUNT)
    assert store.run(ACCOUNT)["configured_sources"] == 1
    assert attempts[0] == attempts[1]
    assert discovery.calls == 1


def test_unverified_and_invalid_state_make_no_provider_calls(tmp_path: Path) -> None:
    current = config()
    current["ai_exclusions_verified"] = False
    source, discovery = Source(), Discovery()
    store = coordinator(tmp_path, current, source, discovery, lambda *args: None)
    assert store.run(ACCOUNT)["outcome"] == "skipped"
    assert source.calls == discovery.calls == 0
    current["ai_exclusions_verified"] = True
    store.store.write(
        store._name(ACCOUNT),
        {"candidates": [asdict(SlackMessageCandidate(ACCOUNT, "CONE", "bad", None))]},
    )
    with pytest.raises(LiveSourceError, match="capture_discovery_invalid_state"):
        store.run(ACCOUNT)
