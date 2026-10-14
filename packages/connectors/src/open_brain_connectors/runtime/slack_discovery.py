"""Opt-in discovery hints. A hint never grants capture or processing authority."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.live_http import LiveHttpTransport
from open_brain_connectors.runtime.slack_auth import SlackAuth, slack_user_scopes
from open_brain_connectors.runtime.slack_live import (
    SlackSourceClient,
    _channel_id,
    _next_cursor,
    _timestamp,
)


@dataclass(frozen=True, slots=True)
class SlackMessageCandidate:
    """Provider identity requiring policy admission and canonical record fetching."""

    connection_id: str
    conversation_id: str
    message_ts: str
    thread_ts: str | None = None


@dataclass(frozen=True, slots=True)
class SlackMessageDiscoveryBatch:
    candidates: tuple[SlackMessageCandidate, ...]
    checkpoint: dict[str, object] | None
    has_more: bool
    notices: tuple[str, ...]


class SlackMessageDiscoveryClient:
    def __init__(self, auth: SlackAuth, *, http: LiveHttpTransport | None = None) -> None:
        self._auth = auth
        self._client = SlackSourceClient(auth, http=http)

    def discover(
        self,
        connection_id: str,
        *,
        owner_id: str,
        capabilities: tuple[str, ...],
        excluded_conversations: tuple[str, ...],
        ai_exclusions_verified: bool,
        oldest: str,
        latest: str | None = None,
        checkpoint: dict[str, object] | None = None,
    ) -> SlackMessageDiscoveryBatch:
        """One search or DM-history page, with explicit incomplete-coverage evidence."""
        scopes = slack_user_scopes(capabilities)
        if not re.fullmatch(r"[A-Z0-9]{2,128}", owner_id):
            raise LiveSourceError("invalid_owner_identity")
        floor = _timestamp(oldest)
        ceiling = None if latest is None else _timestamp(latest)
        if ceiling is not None and float(ceiling) <= float(floor):
            raise LiveSourceError("invalid_discovery_policy")
        excluded = {_channel_id(item) for item in excluded_conversations}
        if type(ai_exclusions_verified) is not bool:
            raise LiveSourceError("invalid_discovery_policy")
        if not ai_exclusions_verified:
            return SlackMessageDiscoveryBatch((), None, False, ("unsupported_ai_exclusions",))
        if not capabilities:
            return SlackMessageDiscoveryBatch((), None, False, ("discovery_disabled",))
        accounts = {account.connection_id: account for account in self._auth.accounts()}
        if connection_id not in accounts or not set(scopes).issubset(
            accounts[connection_id].scopes
        ):
            raise LiveSourceError("not_allowed")
        state: dict[str, object] = {
            "connection_id": connection_id,
            "capabilities": list(capabilities),
            "oldest": floor,
            "owner_id": owner_id,
            "excluded": sorted(excluded),
            "index": 0,
            "page": 1,
            "cursor": None,
            "list_cursor": None,
            "pending": [],
            "list_complete": False,
            **({"latest": ceiling} if ceiling is not None else {}),
        }
        if checkpoint is not None:
            if (
                set(checkpoint) != set(state)
                or any(
                    checkpoint[key] != state[key]
                    for key in ("connection_id", "capabilities", "oldest", "owner_id", "excluded")
                )
                or checkpoint.get("latest") != ceiling
            ):
                raise LiveSourceError("invalid_checkpoint")
            state = dict(checkpoint)
        index = state["index"]
        page = state["page"]
        if type(index) is not int or not 0 <= index < len(capabilities):
            raise LiveSourceError("invalid_checkpoint")
        if type(page) is not int or not 1 <= page <= 100:
            raise LiveSourceError("invalid_checkpoint")
        capability = capabilities[index]
        notices = ("incomplete_search_coverage", "untracked_older_threads_unsupported")
        candidates: list[SlackMessageCandidate] = []
        more = False
        if capability in {"owner_messages", "mentions"}:
            search_date = (
                datetime.fromtimestamp(float(floor), UTC).date() - timedelta(days=1)
            ).isoformat()
            query = f"from:<@{owner_id}>" if capability == "owner_messages" else f"<@{owner_id}>"
            if ceiling is not None:
                search_until = (
                    datetime.fromtimestamp(float(ceiling), UTC).date() + timedelta(days=1)
                ).isoformat()
                query += f" before:{search_until}"
            payload = self._client._api(
                connection_id,
                "search.messages",
                {
                    "query": f"{query} after:{search_date}",
                    "count": "100",
                    "page": str(page),
                    "sort": "timestamp",
                    "sort_dir": "asc",
                },
            )
            messages = payload.get("messages")
            if not isinstance(messages, dict) or not isinstance(messages.get("matches"), list):
                raise LiveSourceError("invalid_provider_response")
            matches = messages["matches"]
            if len(matches) > 100:
                raise LiveSourceError("invalid_provider_response")
            for raw in matches:
                if not isinstance(raw, dict) or not isinstance(raw.get("channel"), dict):
                    raise LiveSourceError("invalid_provider_response")
                conversation = _channel_id(raw["channel"].get("id"))
                # Search can include DMs: admission requires their separate capability.
                kind = raw["channel"].get("is_im")
                if (conversation.startswith("D") or kind is True) and "dm" not in capabilities:
                    continue
                if raw["channel"].get("is_mpim") is True and "group_dm" not in capabilities:
                    continue
                timestamp = float(_timestamp(raw.get("ts")))
                if (
                    conversation not in excluded
                    and timestamp > float(floor)
                    and (ceiling is None or timestamp <= float(ceiling))
                ):
                    candidates.append(
                        SlackMessageCandidate(
                            connection_id,
                            conversation,
                            _timestamp(raw.get("ts")),
                            _timestamp(raw["thread_ts"]) if raw.get("thread_ts") else None,
                        )
                    )
            paging = messages.get("paging", {})
            if not isinstance(paging, dict) or type(paging.get("pages", 1)) is not int:
                raise LiveSourceError("invalid_provider_response")
            more = page < min(paging.get("pages", 1), 100)
            state["page"] = page + 1 if more else 1
        else:
            pending = state["pending"]
            if not isinstance(pending, list) or len(pending) > 100:
                raise LiveSourceError("invalid_checkpoint")
            if not pending and state["list_complete"] is not True:
                cursor = state["list_cursor"]
                if cursor is not None and type(cursor) is not str:
                    raise LiveSourceError("invalid_checkpoint")
                payload = self._client._api(
                    connection_id,
                    "conversations.list",
                    {
                        "types": "im" if capability == "dm" else "mpim",
                        "limit": "100",
                        **({"cursor": cursor} if cursor else {}),
                    },
                )
                channels = payload.get("channels")
                if not isinstance(channels, list) or len(channels) > 100:
                    raise LiveSourceError("invalid_provider_response")
                pending = cast(list[object], [])
                for channel in channels:
                    if not isinstance(channel, dict):
                        raise LiveSourceError("invalid_provider_response")
                    conversation = _channel_id(channel.get("id"))
                    if conversation not in excluded:
                        pending.append(conversation)
                state["pending"] = pending
                state["list_cursor"] = _next_cursor(payload)
                state["list_complete"] = state["list_cursor"] is None
                return SlackMessageDiscoveryBatch((), state, True, notices)
            if pending:
                conversation = _channel_id(pending[0])
                payload = self._client._api(
                    connection_id,
                    "conversations.history",
                    {
                        "channel": conversation,
                        "oldest": floor,
                        "limit": "15",
                        **({"latest": ceiling} if ceiling is not None else {}),
                        **({"cursor": cast(str, state["cursor"])} if state["cursor"] else {}),
                    },
                )
                values = payload.get("messages")
                if not isinstance(values, list) or len(values) > 15:
                    raise LiveSourceError("invalid_provider_response")
                for raw in values:
                    if not isinstance(raw, dict):
                        raise LiveSourceError("invalid_provider_response")
                    timestamp = float(_timestamp(raw.get("ts")))
                    if (
                        timestamp <= float(floor)
                        or ceiling is not None
                        and timestamp > float(ceiling)
                    ):
                        continue
                    candidates.append(
                        SlackMessageCandidate(
                            connection_id,
                            conversation,
                            _timestamp(raw.get("ts")),
                            _timestamp(raw["thread_ts"]) if raw.get("thread_ts") else None,
                        )
                    )
                cursor = _next_cursor(payload)
                state["cursor"] = cursor
                if cursor is None:
                    state["pending"] = pending[1:]
                more = bool(state["pending"]) or state["list_complete"] is not True
        if not more:
            state.update(
                index=index + 1,
                page=1,
                cursor=None,
                list_cursor=None,
                pending=[],
                list_complete=False,
            )
        has_more = more or index + 1 < len(capabilities)
        return SlackMessageDiscoveryBatch(
            tuple(candidates), state if has_more else None, has_more, notices
        )
