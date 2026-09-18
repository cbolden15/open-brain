"""Owner-maintained, private policy for Slack channel discovery."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json, safe_text
from open_brain_connectors.runtime.live_storage import PrivateJsonStore

_SCHEMA_VERSION = 1
_MAX_CHANNELS = 500
_MAX_KEYWORDS = 64
_MAX_MAPPINGS = 256
_MAX_SUGGESTIONS = 500
_PAGE_ID = re.compile(r"page_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_CHANNEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_MAPPING_ID = re.compile(r"mapping_[0-9a-f]{32}")


def channel_id(value: object) -> str:
    if type(value) is not str or _CHANNEL_ID.fullmatch(value) is None:
        raise LiveSourceError("source_invalid_channel")
    return value


def page_id(value: object) -> str:
    if type(value) is not str or _PAGE_ID.fullmatch(value) is None:
        raise LiveSourceError("source_invalid_page")
    return value


def connection_id(value: object) -> str:
    result = safe_text(value, maximum=128)
    if not result.startswith("account:"):
        raise LiveSourceError("source_invalid_connection")
    return result


def _keyword(value: object) -> str:
    return safe_text(value, maximum=120).casefold()


def _default_policy() -> dict[str, object]:
    return {
        "activity_weight": 1,
        "allowlist": [],
        "discovery": {"checkpoint": None, "completed_at": None},
        "keyword_weight": 20,
        "keywords": [],
        "lookback_seconds": 86_400,
        "mappings": [],
        "pending_suggestions": [],
        "proposal_opt_in": False,
        "threshold": 20,
    }


class SlackPolicyStore:
    """Atomic private policy state, partitioned by the connected Slack account."""

    def __init__(self, root: Path) -> None:
        self._store = PrivateJsonStore(root / "slack-policy")

    def setup(self, account: object, values: Mapping[str, object]) -> dict[str, object]:
        selected = connection_id(account)
        policy = self._update(selected, lambda current: _configured(current, values))
        return _public_policy(selected, policy)

    def status(self) -> dict[str, object]:
        state = self._load()
        policies = cast(dict[str, dict[str, object]], state["policies"])
        return {
            "configured_accounts": len(policies),
            "pending_suggestions": sum(
                len(cast(list[object], item["pending_suggestions"])) for item in policies.values()
            ),
            "allowlisted_channels": sum(
                len(cast(list[object], item["allowlist"])) for item in policies.values()
            ),
            "mappings": sum(
                len(cast(list[object], item["mappings"])) for item in policies.values()
            ),
        }

    def policy(self, account: object) -> dict[str, object]:
        selected = connection_id(account)
        policy = cast(dict[str, object], self._load()["policies"]).get(selected)
        if not isinstance(policy, dict):
            raise LiveSourceError("source_slack_policy_missing")
        _validate_policy(policy)
        return policy

    def mappings(self, account: object) -> dict[str, object]:
        selected = connection_id(account)
        policy = self.policy(selected)
        return {"connection_id": selected, "mappings": policy["mappings"]}

    def add_mapping(
        self,
        account: object,
        selected_channel: object,
        selected_page: object,
        keyword: object | None,
        *,
        page_exists: Callable[[str], bool],
    ) -> dict[str, object]:
        selected = connection_id(account)
        channel = channel_id(selected_channel)
        page = page_id(selected_page)
        if not page_exists(page):
            raise LiveSourceError("source_unknown_page")
        match_keyword = None if keyword is None else _keyword(keyword)

        def add(current: dict[str, object]) -> dict[str, object]:
            mappings = cast(list[dict[str, object]], current["mappings"])
            if len(mappings) >= _MAX_MAPPINGS:
                raise LiveSourceError("source_slack_policy_limit")
            if any(
                item["channel_id"] == channel and item["keyword"] == match_keyword
                for item in mappings
            ):
                raise LiveSourceError("source_slack_mapping_exists")
            added = {
                "channel_id": channel,
                "keyword": match_keyword,
                "mapping_id": "mapping_" + uuid.uuid4().hex,
                "page_id": page,
            }
            current["mappings"] = [*mappings, added]
            return current

        policy = self._update(selected, add)
        return next(
            item
            for item in cast(list[dict[str, object]], policy["mappings"])
            if item["channel_id"] == channel and item["keyword"] == match_keyword
        )

    def remove_mapping(self, account: object, selected_mapping: object) -> dict[str, object]:
        selected = connection_id(account)
        if type(selected_mapping) is not str or _MAPPING_ID.fullmatch(selected_mapping) is None:
            raise LiveSourceError("source_invalid_mapping")

        def remove(current: dict[str, object]) -> dict[str, object]:
            mappings = cast(list[dict[str, object]], current["mappings"])
            remaining = [item for item in mappings if item["mapping_id"] != selected_mapping]
            if len(remaining) == len(mappings):
                raise LiveSourceError("source_unknown_mapping")
            current["mappings"] = remaining
            return current

        self._update(selected, remove)
        return {"connection_id": selected, "mapping_id": selected_mapping, "status": "removed"}

    def suggestions(self, account: object) -> dict[str, object]:
        selected = connection_id(account)
        policy = self.policy(selected)
        return {"connection_id": selected, "suggestions": policy["pending_suggestions"]}

    def approve_suggestion(self, account: object, selected_channel: object) -> dict[str, object]:
        return self._decide_suggestion(account, selected_channel, approve=True)

    def dismiss_suggestion(self, account: object, selected_channel: object) -> dict[str, object]:
        return self._decide_suggestion(account, selected_channel, approve=False)

    def record_discovery(
        self,
        account: object,
        checkpoint: dict[str, object] | None,
        suggestions: tuple[dict[str, object], ...],
        *,
        completed_at: int | None,
    ) -> dict[str, object]:
        selected = connection_id(account)

        def record(current: dict[str, object]) -> dict[str, object]:
            discovery = cast(dict[str, object], current["discovery"])
            discovery["checkpoint"] = checkpoint
            discovery["completed_at"] = completed_at
            current["discovery"] = discovery
            allowlist = set(cast(list[str], current["allowlist"]))
            existing = {
                cast(str, item["channel_id"]): item
                for item in cast(list[dict[str, object]], current["pending_suggestions"])
            }
            for suggestion in suggestions:
                channel = channel_id(suggestion.get("channel_id"))
                if channel not in allowlist:
                    existing[channel] = suggestion
            if len(existing) > _MAX_SUGGESTIONS:
                raise LiveSourceError("source_slack_policy_limit")
            current["pending_suggestions"] = [existing[key] for key in sorted(existing)]
            return current

        return self._update(selected, record)

    def _decide_suggestion(
        self, account: object, selected_channel: object, *, approve: bool
    ) -> dict[str, object]:
        selected = connection_id(account)
        channel = channel_id(selected_channel)

        def decide(current: dict[str, object]) -> dict[str, object]:
            suggestions = cast(list[dict[str, object]], current["pending_suggestions"])
            if not any(item["channel_id"] == channel for item in suggestions):
                raise LiveSourceError("source_unknown_suggestion")
            current["pending_suggestions"] = [
                item for item in suggestions if item["channel_id"] != channel
            ]
            if approve:
                allowlist = cast(list[str], current["allowlist"])
                if channel not in allowlist:
                    current["allowlist"] = sorted([*allowlist, channel])
            return current

        self._update(selected, decide)
        return {
            "channel_id": channel,
            "connection_id": selected,
            "status": "approved" if approve else "dismissed",
        }

    def _update(
        self, account: str, mutate: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        with self._store.lock("policy"):
            state = self._load()
            policies = cast(dict[str, dict[str, object]], state["policies"])
            current = dict(cast(dict[str, object], policies.get(account, _default_policy())))
            _validate_policy(current)
            updated = mutate(current)
            _validate_policy(updated)
            policies[account] = updated
            state["policies"] = policies
            self._store.write("policy.json", state)
            return updated

    def _load(self) -> dict[str, object]:
        value = self._store.read("policy.json")
        if value is None:
            return {"schema_version": _SCHEMA_VERSION, "policies": {}}
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "policies"}
            or value.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(value.get("policies"), dict)
            or len(cast(dict[object, object], value["policies"])) > 32
        ):
            raise LiveSourceError("source_invalid_slack_policy")
        for account, policy in cast(dict[object, object], value["policies"]).items():
            connection_id(account)
            if not isinstance(policy, dict):
                raise LiveSourceError("source_invalid_slack_policy")
            _validate_policy(policy)
        bounded_json(value, 262_144)
        return cast(dict[str, object], value)


def _configured(current: dict[str, object], values: Mapping[str, object]) -> dict[str, object]:
    allowed = {
        "activity_weight",
        "allowlist",
        "keyword_weight",
        "keywords",
        "lookback_seconds",
        "proposal_opt_in",
        "threshold",
    }
    if set(values) - allowed:
        raise LiveSourceError("source_invalid_arguments")
    updated = dict(current)
    for field, value in values.items():
        if field == "allowlist":
            if not isinstance(value, list) or len(value) > _MAX_CHANNELS:
                raise LiveSourceError("source_invalid_arguments")
            channels = sorted({channel_id(item) for item in value})
            if len(channels) != len(value):
                raise LiveSourceError("source_invalid_arguments")
            updated[field] = channels
        elif field == "keywords":
            if not isinstance(value, list) or len(value) > _MAX_KEYWORDS:
                raise LiveSourceError("source_invalid_arguments")
            keywords = sorted({_keyword(item) for item in value})
            if len(keywords) != len(value):
                raise LiveSourceError("source_invalid_arguments")
            updated[field] = keywords
        elif field == "proposal_opt_in":
            if type(value) is not bool:
                raise LiveSourceError("source_invalid_arguments")
            updated[field] = value
        elif field == "lookback_seconds":
            if type(value) is not int or not 3_600 <= value <= 604_800:
                raise LiveSourceError("source_invalid_arguments")
            updated[field] = value
        elif field in {"activity_weight", "keyword_weight", "threshold"}:
            if type(value) is not int or not 0 <= value <= 100_000:
                raise LiveSourceError("source_invalid_arguments")
            updated[field] = value
    return updated


def _validate_policy(value: dict[str, object]) -> None:
    expected = set(_default_policy())
    if set(value) != expected:
        raise LiveSourceError("source_invalid_slack_policy")
    _configured(
        _default_policy(),
        {key: value[key] for key in expected - {"discovery", "mappings", "pending_suggestions"}},
    )
    discovery = value["discovery"]
    if not isinstance(discovery, dict) or set(discovery) != {"checkpoint", "completed_at"}:
        raise LiveSourceError("source_invalid_slack_policy")
    if discovery["completed_at"] is not None and (
        type(discovery["completed_at"]) is not int or discovery["completed_at"] < 0
    ):
        raise LiveSourceError("source_invalid_slack_policy")
    if discovery["checkpoint"] is not None:
        bounded_json(discovery["checkpoint"], 65_536)
    mappings = value["mappings"]
    if not isinstance(mappings, list) or len(mappings) > _MAX_MAPPINGS:
        raise LiveSourceError("source_invalid_slack_policy")
    seen: set[tuple[str, str | None]] = set()
    for item in mappings:
        if not isinstance(item, dict) or set(item) != {
            "channel_id",
            "keyword",
            "mapping_id",
            "page_id",
        }:
            raise LiveSourceError("source_invalid_slack_policy")
        channel = channel_id(item["channel_id"])
        keyword = item["keyword"]
        if keyword is not None:
            keyword = _keyword(keyword)
        if type(item["mapping_id"]) is not str or _MAPPING_ID.fullmatch(item["mapping_id"]) is None:
            raise LiveSourceError("source_invalid_slack_policy")
        page_id(item["page_id"])
        if (channel, keyword) in seen:
            raise LiveSourceError("source_invalid_slack_policy")
        seen.add((channel, keyword))
    suggestions = value["pending_suggestions"]
    if not isinstance(suggestions, list) or len(suggestions) > _MAX_SUGGESTIONS:
        raise LiveSourceError("source_invalid_slack_policy")
    for item in suggestions:
        if not isinstance(item, dict) or set(item) != {
            "channel_id",
            "keyword_hits",
            "message_count",
            "name",
            "score",
            "topic",
        }:
            raise LiveSourceError("source_invalid_slack_policy")
        channel_id(item["channel_id"])
        safe_text(item["name"], maximum=512)
        if item["topic"] is not None:
            safe_text(item["topic"], maximum=512)
        if any(
            type(item[field]) is not int or item[field] < 0
            for field in ("keyword_hits", "message_count", "score")
        ):
            raise LiveSourceError("source_invalid_slack_policy")


def _public_policy(account: str, policy: dict[str, object]) -> dict[str, object]:
    return {
        "connection_id": account,
        "allowlisted_channels": len(cast(list[object], policy["allowlist"])),
        "mapping_count": len(cast(list[object], policy["mappings"])),
        "pending_suggestion_count": len(cast(list[object], policy["pending_suggestions"])),
        "proposal_opt_in": policy["proposal_opt_in"],
        "status": "configured",
    }
