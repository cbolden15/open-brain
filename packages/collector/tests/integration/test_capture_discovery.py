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
    assert discovery.calls == 0
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


def test_completed_coverage_catches_up_after_seven_days_and_freezes_pages(tmp_path: Path) -> None:
    now = [1791900000]
    calls: list[dict[str, object]] = []

    class Pages(Discovery):
        def discover(self, *args: object, **kwargs: object) -> SlackMessageDiscoveryBatch:
            calls.append(kwargs)
            more = len(calls) == 2
            return SlackMessageDiscoveryBatch((), {"page": 2} if more else None, more, ())

    store = coordinator(tmp_path, config(), Source(), Pages(), lambda *args: None)
    store.clock = lambda: now[0]
    store.run(ACCOUNT)
    first_until = calls[0]["latest"]
    now[0] += 7 * 86400
    assert store.run(ACCOUNT)["has_more"] is True
    assert store._load(ACCOUNT)["covered_until"] == first_until
    assert calls[1]["oldest"] == f"{1791900000 - 259200:.6f}"
    frozen = calls[1]["latest"]
    assert frozen != first_until
    now[0] += 2 * 86400
    assert store.run(ACCOUNT)["has_more"] is False
    assert calls[2]["oldest"] == calls[1]["oldest"]
    assert calls[2]["latest"] == frozen
    state = store._load(ACCOUNT)
    assert state["covered_until"] == frozen
    assert state["completed_at"] == now[0]


def test_legacy_discovery_restart_preserves_hints_and_conservative_coverage(tmp_path: Path) -> None:
    now = 1791900000
    store = coordinator(tmp_path, config(), Source(), Discovery(), lambda *args: None)
    store.run(ACCOUNT)
    legacy = store._load(ACCOUNT)
    legacy = {
        key: value
        for key, value in legacy.items()
        if key not in {"covered_until", "latest", "retry_at"}
    }
    legacy.update(
        schema_version=1,
        oldest=f"{now - 7 * 86400:.6f}",
        completed_at=now,
        has_more=False,
        checkpoint=None,
    )
    store.store.write(store._name(ACCOUNT), legacy)
    migrated = store._load(ACCOUNT)
    assert migrated["schema_version"] == 2
    assert migrated["covered_until"] == legacy["oldest"]
    assert migrated["completed_at"] is None
    assert migrated["has_more"] is True
    assert migrated["candidates"] == legacy["candidates"]
    assert migrated["sources"] == legacy["sources"]
    assert migrated["sources"]


def test_discovery_retry_deadline_does_not_advance_coverage_or_fetch_early(tmp_path: Path) -> None:
    now = [1791900000]

    class RateLimited(Discovery):
        def discover(self, *args: object, **kwargs: object) -> SlackMessageDiscoveryBatch:
            self.calls += 1
            if self.calls == 1:
                raise LiveSourceError("rate_limited", retry_after_seconds=30)
            return SlackMessageDiscoveryBatch((), None, False, ())

    discovery = RateLimited()
    store = coordinator(tmp_path, config(), Source(), discovery, lambda *args: None)
    store.clock = lambda: now[0]
    result = store.run(ACCOUNT)
    assert result["outcome"] == "deferred"
    assert result["retry_at"] == now[0] + 30
    assert store._load(ACCOUNT)["covered_until"] is None
    now[0] += 29
    assert store.run(ACCOUNT)["outcome"] == "deferred"
    assert discovery.calls == 1
    now[0] += 1
    assert store.run(ACCOUNT)["outcome"] == "completed"
    assert discovery.calls == 2


def test_permitted_dm_discovery_requires_owner_classification_before_development(
    tmp_path: Path,
) -> None:
    from open_brain_engine.engine import open_local_engine

    from open_brain.profile import compile_single_user_local
    from open_brain_collector.live_manager import LiveSourceManager, PriorityRuntime
    from open_brain_connectors.runtime.live_common import LiveAccount
    from open_brain_connectors.runtime.live_http import LiveHttpTransport
    from open_brain_connectors.runtime.slack_auth import SlackAuth, slack_user_scopes

    message = {"ts": "1791900000.000001", "user": "UOWNER", "text": "Decision: Dmacceptanceneedle"}

    class Auth(SlackAuth):
        def accounts(self) -> tuple[LiveAccount, ...]:
            return (LiveAccount("slack", ACCOUNT, "Synthetic", slack_user_scopes(("dm",)), None),)

        def access_token(self, connection_id: str) -> str:
            return "synthetic"

    class Response:
        status = 200

        def __init__(self, payload: dict[str, object]) -> None:
            self.payload = payload

        def json(self) -> dict[str, object]:
            return self.payload

    class Transport:
        urls: list[str] = []
        payloads: list[dict[str, object]] = [
            {"ok": True, "channels": [{"id": "DSYNTHETIC"}, {"id": "DEXCLUDED"}]},
            {"ok": True, "messages": [message]},
            {"ok": True, "messages": [message]},
            {"ok": True, "messages": [message]},
            {"ok": True, "messages": [message]},
        ]

        def request(self, method: str, url: str, **kwargs: object) -> Response:
            self.urls.append(url)
            return Response(self.payloads.pop(0))

    transport = Transport()
    auth = Auth(tmp_path / "accounts")

    class Runtime(PriorityRuntime):
        def auth(self, provider: str) -> SlackAuth:
            return auth

        def client(self, provider: str) -> SlackSourceClient:
            return SlackSourceClient(auth, http=cast(LiveHttpTransport, transport))

    profile = compile_single_user_local(tmp_path / "brain")
    runtime = Runtime(tmp_path / "live", http=cast(LiveHttpTransport, transport))
    manager = LiveSourceManager(tmp_path / "live", profile.root, runtime=runtime)
    manager.capture._clock = lambda: 1791900010
    manager.dispatch(
        "sources.capture_setup",
        {
            "enabled": True,
            "ai_exclusions_verified": True,
            "owner_id": "UOWNER",
            "capabilities": ["dm"],
            "permitted_conversations": ["DSYNTHETIC"],
            "excluded_conversations": ["DEXCLUDED"],
        },
    )
    manager.dispatch("sources.capture_discover", {"connection_id": ACCOUNT})
    result = manager.dispatch("sources.capture_discover", {"connection_id": ACCOUNT})
    source_id = cast(list[str], result["source_ids"])[0]
    source = cast(list[dict[str, object]], manager.capture.status()["sources"])[0]
    assert source["status"] == "disabled"
    preview = manager.capture.preview(source_id)
    with pytest.raises(LiveSourceError, match="source_privacy_unresolved"):
        manager.capture.apply(source_id, cast(str, preview["preview_id"]))
    tasks = open_local_engine(profile)
    assert not tasks.retrieval.search("Dmacceptanceneedle")
    options = {**cast(dict[str, object], source["options"]), "privacy_tier": "personal"}
    manager.capture.configure(
        source_id,
        SourceResourceSelection("slack", ACCOUNT, "channel:DSYNTHETIC", "channel"),
        options,
        reset=True,
    )
    manager.capture.control(source_id, "enable")
    assert manager.capture.sync_due()[0]["captured_count"] == 1
    assert manager.dispatch("sources.capture_process", {})["captures"] == 1
    assert manager.workflow.status()["development_count"] == 1
    assert all("DEXCLUDED" not in url for url in transport.urls)
    assert transport.payloads == []
