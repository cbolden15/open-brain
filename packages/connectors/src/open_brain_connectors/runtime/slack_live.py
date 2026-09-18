"""Bounded, read-only Slack channel discovery and capture."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from urllib.parse import urlencode

from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveResource,
    LiveResourcePage,
    LiveSourceError,
    local_source_privacy,
    safe_text,
)
from open_brain_connectors.runtime.live_http import LiveHttpResponse, LiveHttpTransport
from open_brain_connectors.runtime.slack import SlackMessageRecord, SlackSourceAdapter
from open_brain_connectors.runtime.slack_auth import SlackAuth
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import D4_SLACK_SOURCE, SourceResourceSelection

__all__ = ["SlackDiscoveryBatch", "SlackSourceClient"]

_API = "https://slack.com/api/"
_MAX_PAGE = 25
_MAX_KNOWN_IDENTITIES = 500
_MAX_DISCOVERY_CHANNELS = 100
_MAX_DISCOVERY_MESSAGES = 100_000


@dataclass(frozen=True, slots=True)
class SlackDiscoveryBatch:
    """One resumable metadata-only slice of a Slack channel scoring scan."""

    checkpoint: dict[str, object] | None
    has_more: bool
    fetch_candidates: tuple[dict[str, object], ...] = ()
    suggestions: tuple[dict[str, object], ...] = ()

    def __post_init__(self) -> None:
        if (
            self.checkpoint is not None
            and type(self.checkpoint) is not dict
            or type(self.has_more) is not bool
            or type(self.fetch_candidates) is not tuple
            or type(self.suggestions) is not tuple
            or len(self.fetch_candidates) > 1
            or len(self.suggestions) > 1
        ):
            raise LiveSourceError("source_invalid_discovery")


class SlackSourceClient:
    """Read only the explicitly selected Slack public or private channel."""

    def __init__(self, auth: SlackAuth, *, http: LiveHttpTransport | None = None) -> None:
        if not isinstance(auth, SlackAuth):
            raise LiveSourceError("invalid_slack_auth")
        self._auth = auth
        self._http = http or LiveHttpTransport()
        self._adapter = SlackSourceAdapter()

    def resources(self, connection_id: str, *, cursor: str | None = None) -> LiveResourcePage:
        if cursor is not None:
            safe_text(cursor, maximum=8192)
        payload = self._api(
            connection_id,
            "conversations.list",
            {
                "exclude_archived": "true",
                "limit": "100",
                "types": "public_channel,private_channel",
                **({"cursor": cursor} if cursor else {}),
            },
        )
        channels = payload.get("channels")
        if not isinstance(channels, list) or len(channels) > 100:
            raise LiveSourceError("invalid_provider_response")
        resources: list[LiveResource] = []
        for item in channels:
            if not isinstance(item, Mapping):
                raise LiveSourceError("invalid_provider_response")
            if item.get("is_archived") is True:
                continue
            channel_id = _channel_id(item.get("id"))
            name = _channel_name(item.get("name"))
            resources.append(LiveResource(f"channel:{channel_id}", name, "channel"))
        return LiveResourcePage(tuple(resources), _next_cursor(payload))

    def discover(
        self,
        connection_id: str,
        policy: Mapping[str, object],
        checkpoint: dict[str, object] | None,
    ) -> SlackDiscoveryBatch:
        """Score one bounded channel/history page without reading capture content into intake."""
        account = safe_text(connection_id, maximum=128)
        config = _discovery_policy(policy)
        state = _discovery_checkpoint(account, checkpoint, config["lookback_seconds"])
        active = cast(dict[str, object] | None, state["active_channel"])
        if active is None and not cast(list[object], state["pending_channels"]):
            if state["list_complete"]:
                return SlackDiscoveryBatch(None, False)
            payload = self._api(
                account,
                "conversations.list",
                {
                    "exclude_archived": "true",
                    "limit": str(_MAX_DISCOVERY_CHANNELS),
                    "types": "public_channel,private_channel",
                    **({"cursor": cast(str, state["list_cursor"])} if state["list_cursor"] else {}),
                },
            )
            channels = payload.get("channels")
            if not isinstance(channels, list) or len(channels) > _MAX_DISCOVERY_CHANNELS:
                raise LiveSourceError("invalid_provider_response")
            decoded = tuple(_discovery_channel(item) for item in channels)
            state["pending_channels"] = [
                item
                for item, raw in zip(decoded, channels, strict=True)
                if cast(Mapping[str, object], raw).get("is_archived") is not True
            ]
            state["list_cursor"] = _next_cursor(payload)
            state["list_complete"] = state["list_cursor"] is None
            return SlackDiscoveryBatch(
                state, bool(state["pending_channels"]) or not state["list_complete"]
            )
        if active is None:
            pending = cast(list[dict[str, object]], state["pending_channels"])
            active, state["pending_channels"] = pending[0], pending[1:]
            state["active_channel"] = active
        payload = self._api(
            account,
            "conversations.history",
            {
                "channel": cast(str, active["channel_id"]),
                "limit": "100",
                "oldest": cast(str, state["oldest"]),
                **(
                    {"cursor": cast(str, active["history_cursor"])}
                    if active["history_cursor"]
                    else {}
                ),
            },
        )
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) > 100:
            raise LiveSourceError("invalid_provider_response")
        active["message_count"] = cast(int, active["message_count"]) + len(messages)
        if active["message_count"] > _MAX_DISCOVERY_MESSAGES:
            raise LiveSourceError("source_discovery_limit")
        next_history = _next_cursor(payload)
        if next_history is not None:
            active["history_cursor"] = next_history
            state["active_channel"] = active
            return SlackDiscoveryBatch(state, True)
        candidate = _scored_channel(active, config)
        state["active_channel"] = None
        has_more = bool(state["pending_channels"]) or not cast(bool, state["list_complete"])
        result_state: dict[str, object] | None = state if has_more else None
        if candidate["channel_id"] in config["allowlist"]:
            return SlackDiscoveryBatch(result_state, has_more, fetch_candidates=(candidate,))
        if candidate["score"] >= config["threshold"]:
            return SlackDiscoveryBatch(result_state, has_more, suggestions=(candidate,))
        return SlackDiscoveryBatch(result_state, has_more)

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch:
        channel_id = _selection_channel(selection)
        floor = _date_floor(options)
        state = _checkpoint(selection, checkpoint)
        if state["pending_removals"]:
            return self._pending_removals(selection, state)
        if cast(dict[str, object], state["scan"])["cutoff"] is None:
            state = _begin_scan(state)
        if state["thread"] is not None:
            return self._fetch_thread(selection, channel_id, state)
        payload = self._api(
            selection.connection_id,
            "conversations.history",
            {
                "channel": channel_id,
                "limit": "1",
                "oldest": floor,
                "latest": cast(str, cast(dict[str, object], state["scan"])["cutoff"]),
                **(
                    {"cursor": cast(str, state["history_cursor"])}
                    if state["history_cursor"]
                    else {}
                ),
            },
        )
        messages = payload.get("messages")
        if not isinstance(messages, list) or len(messages) > 1:
            raise LiveSourceError("invalid_provider_response")
        next_history = _next_cursor(payload)
        if not messages:
            if next_history is not None:
                return LiveBatch((), _with_history_cursor(state, next_history), has_more=True)
            return self._complete_scan(selection, state, ())
        message = _message(messages[0], channel_id)
        if _is_deleted(message):
            record = _deleted_record(message, channel_id)
            observed = self._observe(selection, state, (record,), deleted=(record,))
            intakes = (self._intake(selection, record),)
            if next_history is None:
                return self._complete_scan(selection, observed, intakes, ("message_deleted",))
            return LiveBatch(
                intakes,
                _with_history_cursor(observed, next_history),
                True,
                ("message_deleted",),
            )
        root = self._record(message, channel_id)
        if _reply_count(message) == 0:
            observed = self._observe(selection, state, (root,))
            intakes = (self._intake(selection, root),)
            if next_history is None:
                return self._complete_scan(selection, observed, intakes)
            return LiveBatch(intakes, _with_history_cursor(observed, next_history), True)
        thread: dict[str, object] = {
            "emitted_parent": False,
            "next_history_cursor": next_history,
            "replies_cursor": None,
            "root_ts": root.message_ts,
        }
        return self._fetch_thread(
            selection,
            channel_id,
            _with_thread(state, thread),
        )

    def _fetch_thread(
        self,
        selection: SourceResourceSelection,
        channel_id: str,
        state: dict[str, object],
    ) -> LiveBatch:
        thread = cast(dict[str, object], state["thread"])
        root_ts = cast(str, thread["root_ts"])
        cursor = cast(str | None, thread["replies_cursor"])
        payload = self._api(
            selection.connection_id,
            "conversations.replies",
            {
                "channel": channel_id,
                "limit": str(_MAX_PAGE),
                "ts": root_ts,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        values = payload.get("messages")
        if not isinstance(values, list) or len(values) > _MAX_PAGE:
            raise LiveSourceError("invalid_provider_response")
        emitted_parent = cast(bool, thread["emitted_parent"])
        records: list[SlackMessageRecord] = []
        deleted: list[SlackMessageRecord] = []
        notices: list[str] = []
        for value in values:
            message = _message(value, channel_id)
            if message.get("ts") == root_ts and emitted_parent:
                continue
            if _is_deleted(message):
                records.append(_deleted_record(message, channel_id))
                deleted.append(records[-1])
                notices.append("message_deleted")
            else:
                records.append(self._record(message, channel_id))
        if len(records) > _MAX_PAGE:
            raise LiveSourceError("invalid_provider_response")
        observed = self._observe(selection, state, tuple(records), deleted=tuple(deleted))
        intakes = tuple(self._intake(selection, record) for record in records)
        next_replies = _next_cursor(payload)
        if next_replies is None:
            next_history = cast(str | None, thread["next_history_cursor"])
            if next_history is None:
                return self._complete_scan(
                    selection, observed, intakes, tuple(sorted(set(notices)))
                )
            next_state = _with_history_cursor(observed, next_history)
            has_more = True
        else:
            next_state = _with_thread(
                observed,
                {
                    "emitted_parent": True,
                    "next_history_cursor": thread["next_history_cursor"],
                    "replies_cursor": next_replies,
                    "root_ts": root_ts,
                },
            )
            has_more = True
        return LiveBatch(
            intakes,
            next_state,
            has_more=has_more,
            notices=tuple(sorted(set(notices))),
        )

    def _observe(
        self,
        selection: SourceResourceSelection,
        state: dict[str, object],
        records: tuple[SlackMessageRecord, ...],
        *,
        deleted: tuple[SlackMessageRecord, ...] = (),
    ) -> dict[str, object]:
        scan = _scan(state)
        known = _known_map(scan["known"])
        seen = set(cast(list[str], scan["seen"]))
        deleted_ids = {self._intake(selection, record).key.external_id for record in deleted}
        for record in records:
            intake = self._intake(selection, record)
            external_id = intake.key.external_id
            seen.add(external_id)
            if external_id in deleted_ids:
                known.pop(external_id, None)
            else:
                known[external_id] = intake.key.revision_id
        if len(known) > _MAX_KNOWN_IDENTITIES or len(seen) > _MAX_KNOWN_IDENTITIES:
            raise LiveSourceError("scan_state_too_large")
        return _with_scan(state, _scan_value(scan["cutoff"], known, seen))

    def _complete_scan(
        self,
        selection: SourceResourceSelection,
        state: dict[str, object],
        intakes: tuple[SourceRecordIntake, ...],
        notices: tuple[str, ...] = (),
    ) -> LiveBatch:
        scan = _scan(state)
        known = _known_map(scan["known"])
        seen = set(cast(list[str], scan["seen"]))
        missing = tuple(sorted(external_id for external_id in known if external_id not in seen))
        retained = {
            external_id: revision for external_id, revision in known.items() if external_id in seen
        }
        room = _MAX_PAGE - len(intakes)
        removals = tuple(
            self._intake(
                selection,
                _missing_record(
                    channel_id=_selection_channel(selection),
                    external_id=external_id,
                    revision_id=known[external_id],
                ),
            )
            for external_id in missing[:room]
        )
        pending = [
            {"external_id": external_id, "revision_id": known[external_id]}
            for external_id in missing[room:]
        ]
        complete = _with_scan(state, _scan_value(None, retained, set()))
        complete["history_cursor"] = None
        complete["thread"] = None
        complete["pending_removals"] = pending
        return LiveBatch(
            intakes + removals,
            complete,
            has_more=bool(pending),
            notices=tuple(sorted(set((*notices, *("message_deleted" for _ in removals))))),
        )

    def _pending_removals(
        self, selection: SourceResourceSelection, state: dict[str, object]
    ) -> LiveBatch:
        pending = cast(list[dict[str, str]], state["pending_removals"])
        take, remaining = pending[:_MAX_PAGE], pending[_MAX_PAGE:]
        intakes = tuple(
            self._intake(
                selection,
                _missing_record(
                    _selection_channel(selection), item["external_id"], item["revision_id"]
                ),
            )
            for item in take
        )
        next_state = dict(state)
        next_state["pending_removals"] = remaining
        return LiveBatch(intakes, next_state, bool(remaining), ("message_deleted",))

    def _intake(
        self, selection: SourceResourceSelection, record: SlackMessageRecord
    ) -> SourceRecordIntake:
        return self._adapter.intake(selection, record, privacy=local_source_privacy())

    def _record(self, value: Mapping[str, object], channel_id: str) -> SlackMessageRecord:
        ts = _timestamp(value.get("ts"))
        user = value.get("user")
        author = user if type(user) is str and user else "unknown"
        mapped: dict[str, object] = {
            "channel_id": channel_id,
            "edited_ts": _revision(value, ts),
            "permalink": _permalink(channel_id, ts),
            "text": value.get("text"),
            "ts": ts,
            "user": author,
        }
        if value.get("thread_ts") is not None:
            mapped["thread_ts"] = _timestamp(value.get("thread_ts"))
        return self._adapter.record_from_rest(mapped)

    def _api(self, connection_id: str, method: str, query: dict[str, str]) -> dict[str, object]:
        token = self._auth.access_token(connection_id)
        response = self._http.request(
            "GET",
            _API + method + "?" + urlencode(query),
            headers={"authorization": f"Bearer {token}"},
        )
        return _api_payload(response)


def _api_payload(response: LiveHttpResponse) -> dict[str, object]:
    if response.status == 429:
        raise LiveSourceError("rate_limited", retry_after_seconds=response.retry_after_seconds())
    if response.status >= 500:
        raise LiveSourceError("provider_unavailable")
    if response.status != 200:
        raise LiveSourceError("provider_unavailable")
    payload = response.json()
    if payload.get("ok") is True:
        return payload
    error = payload.get("error")
    if error in {"channel_not_found", "not_in_channel", "no_permission", "missing_scope"}:
        raise LiveSourceError("not_allowed")
    if error in {"invalid_auth", "account_inactive", "token_revoked"}:
        raise LiveSourceError("auth_required")
    if error in {"message_not_found", "thread_not_found"}:
        raise LiveSourceError("retention_expired")
    if error == "ratelimited":
        raise LiveSourceError("rate_limited", retry_after_seconds=response.retry_after_seconds())
    raise LiveSourceError("provider_failed")


def _selection_channel(selection: SourceResourceSelection) -> str:
    if (
        type(selection) is not SourceResourceSelection
        or selection.connector_name != D4_SLACK_SOURCE
        or selection.resource_type != "channel"
        or not selection.resource_id.startswith("channel:")
    ):
        raise LiveSourceError("invalid_selection")
    return _channel_id(selection.resource_id.removeprefix("channel:"))


def _date_floor(options: dict[str, object]) -> str:
    if type(options) is not dict or set(options) != {"date_floor"}:
        raise LiveSourceError("invalid_options")
    value = options["date_floor"]
    if type(value) is not str:
        raise LiveSourceError("invalid_options")
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise LiveSourceError("invalid_options") from error
    if date.tzinfo is None:
        raise LiveSourceError("invalid_options")
    moment = date.astimezone(UTC)
    if moment.year < 2000 or moment > datetime.now(UTC):
        raise LiveSourceError("invalid_options")
    return f"{moment.timestamp():.6f}"


def _discovery_policy(value: Mapping[str, object]) -> dict[str, object]:
    required = {
        "activity_weight",
        "allowlist",
        "keyword_weight",
        "keywords",
        "lookback_seconds",
        "threshold",
    }
    if not isinstance(value, Mapping) or set(value) != required:
        raise LiveSourceError("invalid_discovery_policy")
    numbers: dict[str, int] = {}
    for field in ("activity_weight", "keyword_weight", "threshold"):
        item = value[field]
        if type(item) is not int or not 0 <= item <= 100_000:
            raise LiveSourceError("invalid_discovery_policy")
        numbers[field] = item
    lookback = value["lookback_seconds"]
    if type(lookback) is not int or not 3_600 <= lookback <= 604_800:
        raise LiveSourceError("invalid_discovery_policy")
    allowlist = value["allowlist"]
    keywords = value["keywords"]
    if (
        not isinstance(allowlist, list)
        or len(allowlist) > 500
        or not isinstance(keywords, list)
        or len(keywords) > 64
    ):
        raise LiveSourceError("invalid_discovery_policy")
    try:
        normalized_allowlist = {_channel_id(item) for item in allowlist}
        normalized_keywords = tuple(safe_text(item, maximum=120).casefold() for item in keywords)
    except LiveSourceError:
        raise LiveSourceError("invalid_discovery_policy") from None
    if len(normalized_allowlist) != len(allowlist) or len(set(normalized_keywords)) != len(
        keywords
    ):
        raise LiveSourceError("invalid_discovery_policy")
    return {
        **numbers,
        "allowlist": normalized_allowlist,
        "keywords": normalized_keywords,
        "lookback_seconds": lookback,
    }


def _discovery_checkpoint(
    connection_id: str, checkpoint: dict[str, object] | None, lookback_seconds: int
) -> dict[str, object]:
    if checkpoint is None:
        return {
            "active_channel": None,
            "connection_id": connection_id,
            "list_complete": False,
            "list_cursor": None,
            "oldest": f"{time.time() - lookback_seconds:.6f}",
            "pending_channels": [],
            "schema_version": 1,
        }
    if type(checkpoint) is not dict or set(checkpoint) != {
        "active_channel",
        "connection_id",
        "list_complete",
        "list_cursor",
        "oldest",
        "pending_channels",
        "schema_version",
    }:
        raise LiveSourceError("invalid_discovery_checkpoint")
    if (
        checkpoint["schema_version"] != 1
        or checkpoint["connection_id"] != connection_id
        or type(checkpoint["list_complete"]) is not bool
        or checkpoint["list_cursor"] is not None
        and type(checkpoint["list_cursor"]) is not str
    ):
        raise LiveSourceError("invalid_discovery_checkpoint")
    _timestamp(checkpoint["oldest"])
    pending = checkpoint["pending_channels"]
    if not isinstance(pending, list) or len(pending) > _MAX_DISCOVERY_CHANNELS:
        raise LiveSourceError("invalid_discovery_checkpoint")
    for item in pending:
        _valid_discovery_channel(item)
    active = checkpoint["active_channel"]
    if active is not None:
        _valid_discovery_channel(active)
    return dict(checkpoint)


def _discovery_channel(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise LiveSourceError("invalid_provider_response")
    channel = _channel_id(value.get("id"))
    name = _channel_name(value.get("name"))
    topic_value = value.get("topic")
    topic = topic_value.get("value") if isinstance(topic_value, Mapping) else None
    if topic is not None and (type(topic) is not str or len(topic) > 512 or "\x00" in topic):
        raise LiveSourceError("invalid_provider_response")
    return {
        "channel_id": channel,
        "history_cursor": None,
        "message_count": 0,
        "name": name,
        "topic": topic,
    }


def _valid_discovery_channel(value: object) -> None:
    if not isinstance(value, dict) or set(value) != {
        "channel_id",
        "history_cursor",
        "message_count",
        "name",
        "topic",
    }:
        raise LiveSourceError("invalid_discovery_checkpoint")
    _channel_id(value["channel_id"])
    _channel_name(value["name"])
    if value["topic"] is not None and (
        type(value["topic"]) is not str
        or len(cast(str, value["topic"])) > 512
        or "\x00" in cast(str, value["topic"])
    ):
        raise LiveSourceError("invalid_discovery_checkpoint")
    if value["history_cursor"] is not None and type(value["history_cursor"]) is not str:
        raise LiveSourceError("invalid_discovery_checkpoint")
    if (
        type(value["message_count"]) is not int
        or not 0 <= value["message_count"] <= _MAX_DISCOVERY_MESSAGES
    ):
        raise LiveSourceError("invalid_discovery_checkpoint")


def _scored_channel(channel: dict[str, object], policy: dict[str, object]) -> dict[str, object]:
    topic = cast(str | None, channel["topic"])
    haystack = (cast(str, channel["name"]) + "\n" + (topic or "")).casefold()
    keyword_hits = sum(
        haystack.count(keyword) for keyword in cast(tuple[str, ...], policy["keywords"])
    )
    message_count = cast(int, channel["message_count"])
    score = message_count * cast(int, policy["activity_weight"]) + keyword_hits * cast(
        int, policy["keyword_weight"]
    )
    return {
        "channel_id": channel["channel_id"],
        "keyword_hits": keyword_hits,
        "message_count": message_count,
        "name": channel["name"],
        "score": score,
        "topic": channel["topic"],
    }


def _checkpoint(
    selection: SourceResourceSelection, checkpoint: dict[str, object] | None
) -> dict[str, object]:
    if checkpoint is None:
        return _state(selection, None)
    if type(checkpoint) is not dict or set(checkpoint) != {
        "connection_id",
        "history_cursor",
        "pending_removals",
        "resource_id",
        "scan",
        "schema_version",
        "thread",
    }:
        raise LiveSourceError("invalid_checkpoint")
    if (
        checkpoint.get("schema_version") != 1
        or checkpoint.get("connection_id") != selection.connection_id
        or checkpoint.get("resource_id") != selection.resource_id
        or checkpoint.get("history_cursor") is not None
        and type(checkpoint.get("history_cursor")) is not str
    ):
        raise LiveSourceError("invalid_checkpoint")
    _scan(checkpoint)
    pending = checkpoint.get("pending_removals")
    if (
        not isinstance(pending, list)
        or len(pending) > _MAX_KNOWN_IDENTITIES
        or any(
            not isinstance(item, dict)
            or set(item) != {"external_id", "revision_id"}
            or not _valid_external_id(item["external_id"])
            or not _valid_revision_id(item["revision_id"])
            for item in pending
        )
    ):
        raise LiveSourceError("invalid_checkpoint")
    thread = checkpoint.get("thread")
    if thread is not None:
        if not isinstance(thread, dict) or set(thread) != {
            "emitted_parent",
            "next_history_cursor",
            "replies_cursor",
            "root_ts",
        }:
            raise LiveSourceError("invalid_checkpoint")
        if (
            type(thread["emitted_parent"]) is not bool
            or thread["next_history_cursor"] is not None
            and type(thread["next_history_cursor"]) is not str
            or thread["replies_cursor"] is not None
            and type(thread["replies_cursor"]) is not str
        ):
            raise LiveSourceError("invalid_checkpoint")
        _timestamp(thread["root_ts"])
    return checkpoint


def _state(
    selection: SourceResourceSelection,
    history_cursor: str | None,
    thread: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "connection_id": selection.connection_id,
        "history_cursor": history_cursor,
        "pending_removals": [],
        "resource_id": selection.resource_id,
        "scan": _scan_value(None, {}, set()),
        "schema_version": 1,
        "thread": thread,
    }


def _with_history_cursor(state: dict[str, object], history_cursor: str) -> dict[str, object]:
    next_state = dict(state)
    next_state["history_cursor"] = history_cursor
    next_state["thread"] = None
    return next_state


def _with_thread(state: dict[str, object], thread: dict[str, object]) -> dict[str, object]:
    next_state = dict(state)
    next_state["thread"] = thread
    return next_state


def _with_scan(state: dict[str, object], scan: dict[str, object]) -> dict[str, object]:
    next_state = dict(state)
    next_state["scan"] = scan
    return next_state


def _begin_scan(state: dict[str, object]) -> dict[str, object]:
    scan = _scan(state)
    return _with_scan(
        state,
        _scan_value(f"{time.time():.6f}", _known_map(scan["known"]), set()),
    )


def _scan(state: dict[str, object]) -> dict[str, object]:
    value = state.get("scan")
    if not isinstance(value, dict) or set(value) != {"cutoff", "known", "seen"}:
        raise LiveSourceError("invalid_checkpoint")
    cutoff = value["cutoff"]
    if cutoff is not None:
        _timestamp(cutoff)
    known = value["known"]
    seen = value["seen"]
    if (
        not isinstance(known, list)
        or not isinstance(seen, list)
        or len(known) > _MAX_KNOWN_IDENTITIES
        or len(seen) > _MAX_KNOWN_IDENTITIES
        or any(
            not isinstance(item, dict)
            or set(item) != {"external_id", "revision_id"}
            or not _valid_external_id(item["external_id"])
            or not _valid_revision_id(item["revision_id"])
            for item in known
        )
        or any(not _valid_external_id(item) for item in seen)
        or len({cast(str, item["external_id"]) for item in known}) != len(known)
        or len(set(cast(list[str], seen))) != len(seen)
    ):
        raise LiveSourceError("invalid_checkpoint")
    return value


def _scan_value(cutoff: object, known: Mapping[str, str], seen: set[str]) -> dict[str, object]:
    return {
        "cutoff": cutoff,
        "known": [
            {"external_id": external_id, "revision_id": revision_id}
            for external_id, revision_id in sorted(known.items())
        ],
        "seen": sorted(seen),
    }


def _known_map(value: object) -> dict[str, str]:
    if not isinstance(value, list):
        raise LiveSourceError("invalid_checkpoint")
    return {
        cast(str, item["external_id"]): cast(str, item["revision_id"])
        for item in cast(list[dict[str, object]], value)
    }


def _valid_external_id(value: object) -> bool:
    if type(value) is not str:
        return False
    if value.startswith("message:"):
        try:
            _timestamp(value.removeprefix("message:"))
        except LiveSourceError:
            return False
        return True
    if value.startswith("reply:"):
        parts = value.removeprefix("reply:").split(":")
        if len(parts) != 2:
            return False
        try:
            _timestamp(parts[0])
            _timestamp(parts[1])
        except LiveSourceError:
            return False
        return True
    return False


def _valid_revision_id(value: object) -> bool:
    return type(value) is str and 1 <= len(value) <= 128 and "\x00" not in value


def _message(value: object, channel_id: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LiveSourceError("invalid_provider_response")
    _timestamp(value.get("ts"))
    return value


def _channel_id(value: object) -> str:
    if type(value) is not str or not value or len(value) > 128 or not value.isascii():
        raise LiveSourceError("invalid_provider_response")
    if not value.replace("_", "").replace("-", "").isalnum():
        raise LiveSourceError("invalid_provider_response")
    return value


def _channel_name(value: object) -> str:
    if type(value) is not str or not value or len(value) > 512 or "\x00" in value:
        raise LiveSourceError("invalid_provider_response")
    return value


def _timestamp(value: object) -> str:
    if type(value) is not str or not value or len(value) > 32:
        raise LiveSourceError("invalid_provider_response")
    head, dot, tail = value.partition(".")
    if not dot or not head.isdecimal() or not tail.isdecimal():
        raise LiveSourceError("invalid_provider_response")
    return value


def _reply_count(value: Mapping[str, object]) -> int:
    count = value.get("reply_count", 0)
    if type(count) is not int or not 0 <= count <= 100_000:
        raise LiveSourceError("invalid_provider_response")
    return count


def _revision(value: Mapping[str, object], fallback: str) -> str:
    edited = value.get("edited")
    if isinstance(edited, Mapping) and edited.get("ts") is not None:
        return _timestamp(edited.get("ts"))
    return fallback


def _is_deleted(value: Mapping[str, object]) -> bool:
    return value.get("subtype") == "message_deleted"


def _deleted_record(value: Mapping[str, object], channel_id: str) -> SlackMessageRecord:
    prior = value.get("previous_message")
    if not isinstance(prior, Mapping):
        raise LiveSourceError("retention_expired")
    ts = _timestamp(prior.get("ts"))
    revision = _revision(value, _timestamp(value.get("ts")))
    return SlackMessageRecord(
        channel_id=channel_id,
        message_ts=ts,
        revision_id=revision,
        permalink=_permalink(channel_id, ts),
        author_id="deleted",
        text="[Slack message deleted]",
        thread_ts=(
            _timestamp(prior.get("thread_ts")) if prior.get("thread_ts") is not None else None
        ),
    )


def _missing_record(channel_id: str, external_id: str, revision_id: str) -> SlackMessageRecord:
    if external_id.startswith("message:"):
        ts = external_id.removeprefix("message:")
        thread_ts = None
    elif external_id.startswith("reply:"):
        root_ts, ts = external_id.removeprefix("reply:").split(":", maxsplit=1)
        thread_ts = root_ts
    else:  # The checkpoint validator prevents this branch for durable state.
        raise LiveSourceError("invalid_checkpoint")
    _timestamp(ts)
    if thread_ts is not None:
        _timestamp(thread_ts)
    return SlackMessageRecord(
        channel_id=channel_id,
        message_ts=ts,
        revision_id=f"unavailable:{revision_id}",
        permalink=_permalink(channel_id, ts),
        author_id="unavailable",
        text="[Slack message is no longer available from the selected channel]",
        thread_ts=thread_ts,
    )


def _permalink(channel_id: str, ts: str) -> str:
    return f"https://slack.com/archives/{channel_id}/p{ts.replace('.', '')}"


def _next_cursor(payload: Mapping[str, object]) -> str | None:
    metadata = payload.get("response_metadata", {})
    if not isinstance(metadata, Mapping):
        raise LiveSourceError("invalid_provider_response")
    cursor = metadata.get("next_cursor", "")
    if cursor == "":
        return None
    if type(cursor) is not str or len(cursor) > 8192 or "\x00" in cursor:
        raise LiveSourceError("invalid_provider_response")
    return cursor
