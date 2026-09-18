"""Explicit Slack mapping to revision-bound canonical-note append proposals."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

from open_brain_engine.engine import open_local_engine

from open_brain_collector.runner import _open_existing_collector_profile
from open_brain_collector.slack_policy import SlackPolicyStore
from open_brain_connectors.runtime.live_common import LiveSourceError
from open_brain_connectors.runtime.source_intake import SourceRecordIntake
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

_MAX_FRAGMENT_BYTES = 15 * 1024
_UNAVAILABLE_TEXT = {
    "[Slack message deleted]",
    "[Slack message is no longer available from the selected channel]",
}


class SlackPatchProposer:
    """Creates pending append proposals only for owner-approved Slack mappings."""

    def __init__(self, brain_root: Path, policy: SlackPolicyStore) -> None:
        self._brain_root = brain_root
        self._policy = policy

    def propose(
        self,
        selection: SourceResourceSelection,
        records: Sequence[tuple[SourceRecordIntake, str]],
    ) -> tuple[str, ...]:
        if selection.connector_name != "slack" or not records:
            return ()
        try:
            policy = self._policy.policy(selection.connection_id)
        except LiveSourceError as error:
            if error.code == "source_slack_policy_missing":
                return ()
            raise
        if policy["proposal_opt_in"] is not True:
            return ()
        channel = _channel(selection)
        generation = _mapping_generation(policy)
        proposed: list[str] = []
        for thread_id, grouped in _threads(records).items():
            mapping = _mapping_for(
                cast(list[dict[str, object]], policy["mappings"]), channel, grouped
            )
            if mapping is None:
                continue
            capture_ids = tuple(capture_id for _intake, capture_id in grouped)
            if not capture_ids:
                continue
            delivery_id = "slack-patch-" + hashlib.sha256(
                "\x1f".join(
                    (
                        generation,
                        cast(str, mapping["mapping_id"]),
                        cast(str, mapping["page_id"]),
                        *capture_ids,
                    )
                ).encode("utf-8")
            ).hexdigest()
            try:
                tasks = open_local_engine(_open_existing_collector_profile(self._brain_root))
                proposal = tasks.review.propose_append(
                    capture_ids,
                    target_page_id=cast(str, mapping["page_id"]),
                    append_markdown=_fragment(channel, thread_id, grouped),
                    delivery_id=delivery_id,
                )
            except (OSError, ValueError) as error:
                raise LiveSourceError("source_slack_patch_failed") from error
            proposed.extend(item.proposal_id for item in proposal)
        return tuple(proposed)


def _channel(selection: SourceResourceSelection) -> str:
    if selection.resource_type != "channel" or not selection.resource_id.startswith("channel:"):
        raise LiveSourceError("source_invalid_selection")
    return selection.resource_id.removeprefix("channel:")


def _threads(
    records: Sequence[tuple[SourceRecordIntake, str]],
) -> dict[str, tuple[tuple[SourceRecordIntake, str], ...]]:
    grouped: dict[str, list[tuple[SourceRecordIntake, str]]] = {}
    for intake, capture_id in records:
        if intake.text in _UNAVAILABLE_TEXT:
            continue
        external_id = intake.key.external_id
        if external_id.startswith("reply:"):
            thread_id = external_id.removeprefix("reply:").split(":", maxsplit=1)[0]
        elif external_id.startswith("message:"):
            thread_id = external_id.removeprefix("message:")
        else:
            raise LiveSourceError("source_invalid_slack_thread")
        grouped.setdefault(thread_id, []).append((intake, capture_id))
    return {
        thread_id: tuple(sorted(values, key=lambda item: item[0].key.external_id))
        for thread_id, values in sorted(grouped.items())
    }


def _mapping_for(
    mappings: list[dict[str, object]],
    channel: str,
    records: Sequence[tuple[SourceRecordIntake, str]],
) -> dict[str, object] | None:
    text = "\n".join(intake.text for intake, _capture_id in records).casefold()
    matching = [
        mapping
        for mapping in mappings
        if mapping["channel_id"] == channel
        and (mapping["keyword"] is None or cast(str, mapping["keyword"]) in text)
    ]
    specific = [mapping for mapping in matching if mapping["keyword"] is not None]
    selected = specific or matching
    if len(selected) != 1:
        return None
    return selected[0]


def _mapping_generation(policy: Mapping[str, object]) -> str:
    mappings = cast(list[dict[str, object]], policy["mappings"])
    value = "\n".join(
        "\x1f".join(
            (
                cast(str, mapping["mapping_id"]),
                cast(str, mapping["channel_id"]),
                "" if mapping["keyword"] is None else cast(str, mapping["keyword"]),
                cast(str, mapping["page_id"]),
            )
        )
        for mapping in sorted(mappings, key=lambda item: cast(str, item["mapping_id"]))
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fragment(
    channel: str,
    thread_id: str,
    records: Sequence[tuple[SourceRecordIntake, str]],
) -> str:
    lines = [f"### Slack update: {channel} thread {thread_id}", ""]
    for intake, _capture_id in records:
        lines.extend((f"Source: <{intake.url}>", "", *(_quote(intake.text)), ""))
    return _truncate("\n".join(lines).rstrip(), _MAX_FRAGMENT_BYTES)


def _quote(value: str) -> tuple[str, ...]:
    return tuple("> " + line if line else ">" for line in value.splitlines()) or (">",)


def _truncate(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    suffix = "\n\n> [Slack update truncated]"
    room = maximum_bytes - len(suffix.encode("utf-8"))
    return encoded[:room].decode("utf-8", errors="ignore").rstrip() + suffix
