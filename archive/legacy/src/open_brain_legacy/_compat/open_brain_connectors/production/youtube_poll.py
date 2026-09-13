# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Replay-safe production composition for YouTube playlist capture ingress."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

from open_brain_engine.engine import (
    CaptureReceipt,
    ContentOrigin,
    PrivacyDecision,
    Provenance,
    PublicJobCaptureSink,
    ReferencePayload,
)

from open_brain_legacy._compat.open_brain.extensions.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureIdentity,
    ConnectorCaptureSink,
    ConnectorManifest,
    ConnectorMetadataLogger,
    ConnectorOutcome,
    ConnectorPayload,
    ConnectorRunContext,
    ConnectorRunEvidence,
    ConnectorRunReceipt,
)
from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeMediaAdapter,
    YouTubeMediaResult,
)
from open_brain_legacy._compat.open_brain_connectors.capture.media import MediaCommand
from open_brain_legacy._compat.open_brain_connectors.capture.poll import (
    FilesystemYouTubePollState,
    PollItemState,
    PollRecord,
    PollRequestDisposition,
    PollRequestOrigin,
    PollRequestResult,
    YouTubePoller,
)

_MAX_CONFIG_BYTES = 64 * 1024
_MAX_SUBSCRIPTIONS = 50
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}


class YouTubePollConfigError(ValueError):
    """A private YouTube poll configuration is absent, unsafe, or malformed."""


@dataclass(frozen=True, slots=True)
class YouTubeSubscription:
    url: str
    privacy: PrivacyDecision

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> YouTubeSubscription:
        if set(value) != {"url", "privacy"}:
            raise YouTubePollConfigError("invalid private YouTube config")
        raw_url = value["url"]
        raw_privacy = value["privacy"]
        if not isinstance(raw_url, str) or not isinstance(raw_privacy, Mapping):
            raise YouTubePollConfigError("invalid private YouTube config")
        try:
            url = ReferencePayload(raw_url).url
            parsed = urlsplit(url)
            privacy = PrivacyDecision.from_dict(_mapping(raw_privacy))
        except (TypeError, ValueError) as error:
            raise YouTubePollConfigError("invalid private YouTube config") from error
        if (
            parsed.hostname not in _YOUTUBE_HOSTS
            or len(parse_qs(parsed.query).get("list", ())) != 1
        ):
            raise YouTubePollConfigError("invalid private YouTube config")
        return cls(url=url, privacy=privacy)

    def to_dict(self) -> dict[str, object]:
        return {"url": self.url, "privacy": self.privacy.to_dict()}


@dataclass(frozen=True, slots=True)
class YouTubePollConfig:
    subscriptions: tuple[YouTubeSubscription, ...]

    @property
    def requires_external_egress(self) -> bool:
        return any(
            subscription.privacy.authority.external_egress for subscription in self.subscriptions
        )

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> YouTubePollConfig:
        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicates)
            value = _mapping(decoded)
            raw_subscriptions = value["subscriptions"]
            if (
                set(value) != {"schema_version", "subscriptions"}
                or value["schema_version"] != 1
                or not isinstance(raw_subscriptions, list)
                or len(raw_subscriptions) > _MAX_SUBSCRIPTIONS
            ):
                raise YouTubePollConfigError("invalid private YouTube config")
            subscriptions = tuple(
                YouTubeSubscription.from_dict(_mapping(item)) for item in raw_subscriptions
            )
            result = cls(subscriptions=subscriptions)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            if isinstance(error, YouTubePollConfigError):
                raise
            raise YouTubePollConfigError("invalid private YouTube config") from error
        if (
            len({subscription.url for subscription in subscriptions}) != len(subscriptions)
            or tuple(sorted(subscriptions, key=lambda item: item.url)) != subscriptions
            or result.canonical_bytes() != payload
        ):
            raise YouTubePollConfigError("invalid private YouTube config")
        return result

    def canonical_bytes(self) -> bytes:
        return _canonical_json_bytes(
            {
                "schema_version": 1,
                "subscriptions": [item.to_dict() for item in self.subscriptions],
            }
        )


@dataclass(frozen=True, slots=True)
class ProductionYouTubePollResult:
    discovered_count: int
    polled_count: int
    stubbed_count: int
    created_count: int
    duplicate_count: int


class YouTubePollCheckpoint:
    """Opaque connector checkpoint backed by the existing durable poll state."""

    connector_name = "youtube"
    __slots__ = ("__budget", "__checkpoint_committed", "__evidence", "__state")

    def __init__(
        self,
        state: FilesystemYouTubePollState,
        *,
        budget: ConnectorBudget | None = None,
        evidence: ConnectorRunEvidence | None = None,
    ) -> None:
        if (
            not isinstance(state, FilesystemYouTubePollState)
            or (budget is not None and type(budget) is not ConnectorBudget)
            or (evidence is not None and type(evidence) is not ConnectorRunEvidence)
        ):
            raise ValueError("invalid YouTube poll checkpoint")
        self.__state = state
        self.__budget = budget
        self.__evidence = evidence
        self.__checkpoint_committed = False

    @property
    def budget(self) -> ConnectorBudget | None:
        return self.__budget

    @property
    def checkpoint_committed(self) -> bool:
        return self.__checkpoint_committed

    def bind_run(
        self,
        budget: ConnectorBudget,
        evidence: ConnectorRunEvidence,
    ) -> YouTubePollCheckpoint:
        if (
            self.__budget is not None
            or self.__evidence is not None
            or type(budget) is not ConnectorBudget
            or type(evidence) is not ConnectorRunEvidence
        ):
            raise ValueError("invalid YouTube poll checkpoint budget")
        self.__budget = budget
        self.__evidence = evidence
        return self

    def request(
        self,
        record: PollRecord,
        *,
        reserve_create: Callable[[], bool] | None = None,
    ) -> PollRequestResult:
        if self.__budget is None or reserve_create is not None:
            raise ValueError("YouTube discovery budget unavailable")
        return self.__state.request(
            record,
            reserve_create=self.__budget._consume_discovery,
        )

    def records(self) -> tuple[PollRecord, ...]:
        return self.__state.records()

    def claim_next(self, *, now: datetime, lease_seconds: int) -> PollRecord | None:
        return self.__state.claim_next(now=now, lease_seconds=lease_seconds)

    def release(self, claimed: PollRecord) -> None:
        self.__state.release(claimed)

    def replace(self, previous: PollRecord, current: PollRecord) -> None:
        if current.state is PollItemState.ACCEPTED:
            raise ValueError("capture receipt required for checkpoint acceptance")
        self.__state.replace(previous, current)

    def commit_acceptance(
        self,
        previous: PollRecord,
        *,
        delivery_id: str,
        receipt: CaptureReceipt,
    ) -> PollRecord:
        evidence = self.__evidence
        expected_delivery_id = f"connector.youtube.{previous.video_id}"
        if (
            previous.state is not PollItemState.SEEN
            or delivery_id != expected_delivery_id
            or evidence is None
            or not evidence.authorizes_checkpoint(
                delivery_id,
                previous.source_url,
                receipt,
            )
        ):
            raise ValueError("capture receipt required for checkpoint acceptance")
        current = PollRecord.from_dict(
            {
                **previous.to_dict(),
                "capture_id": receipt.capture_id,
                "state": PollItemState.ACCEPTED.value,
            }
        )
        self.__state.replace(previous, current)
        self.__checkpoint_committed = True
        return current


class YouTubeReferenceTransport:
    """The connector receives only approved subscriptions and injected media transport."""

    __slots__ = ("__budget", "__media_adapter", "__subscriptions")
    connector_name = "youtube"

    def __init__(
        self,
        *,
        subscriptions: tuple[YouTubeSubscription, ...],
        media_adapter: YouTubeMediaAdapter,
        budget: ConnectorBudget | None = None,
    ) -> None:
        if (
            not isinstance(subscriptions, tuple)
            or any(not isinstance(item, YouTubeSubscription) for item in subscriptions)
            or not callable(getattr(media_adapter, "playlist_items", None))
            or not callable(getattr(media_adapter, "media", None))
            or (budget is not None and type(budget) is not ConnectorBudget)
        ):
            raise ValueError("invalid YouTube reference transport")
        self.__subscriptions = subscriptions
        self.__media_adapter = media_adapter
        self.__budget = budget

    @property
    def subscriptions(self) -> tuple[YouTubeSubscription, ...]:
        return self.__subscriptions

    @property
    def budget(self) -> ConnectorBudget | None:
        return self.__budget

    def bind_budget(self, budget: ConnectorBudget) -> YouTubeReferenceTransport:
        if self.__budget is not None or type(budget) is not ConnectorBudget:
            raise ValueError("invalid YouTube reference transport budget")
        self.__budget = budget
        return self

    def playlist_items(self, url: str, *, command: MediaCommand) -> tuple[str, ...]:
        if self.__budget is None or not self.__budget._consume_fetch():
            return ()
        return self.__media_adapter.playlist_items(url, command=command)

    def media(self, video_id: str, *, command: MediaCommand) -> YouTubeMediaResult:
        if self.__budget is None or not self.__budget._consume_extraction():
            raise ValueError("YouTube extraction budget exhausted")
        return self.__media_adapter.media(video_id, command=command)


class YouTubeReferenceConnector:
    """The synthetic reference proof, constrained to capture-only connector authority."""

    manifest = ConnectorManifest(
        schema_version=1,
        name="youtube",
        version="1",
        payloads=(ConnectorPayload.REFERENCE_OR_FILE,),
        schedules=("JOB-029",),
        secrets=(),
        action_authorities=(),
        external_egress=True,
    )

    def run(self, context: ConnectorRunContext) -> ConnectorRunReceipt:
        if (
            not isinstance(context, ConnectorRunContext)
            or context.capture_identity.connector_name != "youtube"
            or context.capture_identity.job_id != "JOB-029"
            or not isinstance(context.transport, YouTubeReferenceTransport)
            or not isinstance(context.checkpoint, YouTubePollCheckpoint)
        ):
            raise ValueError("invalid YouTube connector context")
        transport = context.transport
        checkpoint = context.checkpoint
        discovered = 0
        for subscription in transport.subscriptions:
            if not subscription.privacy.authority.external_egress:
                continue
            maximum = min(
                context.budget.remaining_discoveries,
                context.budget.remaining_extractions,
                context.budget.remaining_submissions,
                500,
            )
            if maximum < 1:
                break
            poller = YouTubePoller(
                state=checkpoint,
                media_adapter=transport,
                max_playlist_items=maximum,
                clock=context.clock,
            )
            requests = poller.request_playlist(
                subscription.url,
                privacy=subscription.privacy,
                requested_at=context.clock(),
            )
            created_requests = sum(
                request.disposition is PollRequestDisposition.CREATED for request in requests
            )
            discovered += created_requests

        stubbed = 0
        poller = YouTubePoller(
            state=checkpoint,
            media_adapter=transport,
            max_playlist_items=1,
            clock=context.clock,
        )
        while context.budget.remaining_extractions:
            pending = next(
                (
                    record
                    for record in checkpoint.records()
                    if record.state is PollItemState.REQUESTED
                    and record.privacy.authority.external_egress
                ),
                None,
            )
            if pending is None:
                break
            result = poller.poll_one(privacy=pending.privacy)
            if result is None:
                break
            stubbed += result.record.state is PollItemState.STUBBED

        created = 0
        duplicates = 0
        checkpoint_committed = False
        for record in checkpoint.records():
            if (
                record.state is not PollItemState.SEEN
                or record.origin is not PollRequestOrigin.PLAYLIST
                or context.budget.remaining_submissions < 1
            ):
                continue
            payload = _reference_payload(record)
            delivery_id = f"connector.youtube.{record.video_id}"
            receipt = context.capture_sink.submit(
                payload,
                delivery_id=delivery_id,
                source_origin=ContentOrigin.THIRD_PARTY,
                source_reference=payload.url,
                provenance=Provenance.create(
                    source_ref=payload.url,
                    content_origin=ContentOrigin.THIRD_PARTY,
                    owner_context="automation_absent",
                ),
                privacy=(
                    record.reclassification.replacement
                    if record.reclassification is not None
                    else record.privacy
                ),
                title=record.extraction.metadata.title if record.extraction is not None else None,
            )
            checkpoint.commit_acceptance(
                record,
                delivery_id=delivery_id,
                receipt=receipt,
            )
            checkpoint_committed = True
            if receipt.duplicate:
                duplicates += 1
            else:
                created += 1
        context.metadata_logger.record("youtube.poll.completed")
        if not any(
            (
                discovered,
                context.budget.fetched_count,
                context.budget.extracted_count,
                context.budget.submitted_count,
                stubbed,
                created,
                duplicates,
            )
        ):
            return ConnectorRunReceipt.empty(
                self.manifest.name,
                metadata_count=context.metadata_logger.count,
            )
        return ConnectorRunReceipt(
            connector_name=self.manifest.name,
            outcome=ConnectorOutcome.COMPLETED,
            failure_code=None,
            discovered_count=discovered,
            fetched_count=context.budget.fetched_count,
            extracted_count=context.budget.extracted_count,
            submitted_count=context.budget.submitted_count,
            stubbed_count=stubbed,
            created_count=created,
            duplicate_count=duplicates,
            checkpoint_committed=checkpoint_committed,
            metadata_count=context.metadata_logger.count,
        )


class ProductionYouTubePollRuntime:
    def __init__(
        self,
        *,
        config: YouTubePollConfig,
        state: FilesystemYouTubePollState,
        sink: PublicJobCaptureSink,
        media_adapter: YouTubeMediaAdapter,
        clock: Callable[[], datetime],
    ) -> None:
        if (
            not isinstance(config, YouTubePollConfig)
            or not isinstance(state, FilesystemYouTubePollState)
            or not isinstance(sink, PublicJobCaptureSink)
            or not callable(clock)
        ):
            raise ValueError("invalid production YouTube poll runtime")
        self._config = config
        self._state = state
        self._sink = sink
        self._media_adapter = media_adapter
        self._clock = clock

    @property
    def requires_external_egress(self) -> bool:
        return self._config.requires_external_egress

    def run(self, *, max_items: int = 100) -> ProductionYouTubePollResult:
        if (
            not isinstance(max_items, int)
            or isinstance(max_items, bool)
            or not 1 <= max_items <= 1_000
        ):
            raise ValueError("invalid YouTube poll batch")
        budget = ConnectorBudget(
            ConnectorBudgetLimits(
                max_fetches=_MAX_SUBSCRIPTIONS,
                max_extractions=max_items,
                max_submissions=max_items,
            )
        )
        evidence = ConnectorRunEvidence()
        receipt = YouTubeReferenceConnector().run(
            ConnectorRunContext(
                capture_identity=ConnectorCaptureIdentity(
                    "youtube",
                    "JOB-029",
                    self._sink.context,
                ),
                capture_sink=ConnectorCaptureSink(self._sink, budget, evidence),
                transport=YouTubeReferenceTransport(
                    subscriptions=self._config.subscriptions,
                    media_adapter=self._media_adapter,
                ).bind_budget(budget),
                checkpoint=YouTubePollCheckpoint(self._state).bind_run(
                    budget,
                    evidence,
                ),
                clock=self._clock,
                budget=budget,
                metadata_logger=ConnectorMetadataLogger(),
            )
        )
        return ProductionYouTubePollResult(
            discovered_count=receipt.discovered_count,
            polled_count=receipt.extracted_count,
            stubbed_count=receipt.stubbed_count,
            created_count=receipt.created_count,
            duplicate_count=receipt.duplicate_count,
        )


def load_private_youtube_config(path: Path) -> YouTubePollConfig:
    if not isinstance(path, Path) or not path.is_absolute():
        raise YouTubePollConfigError("invalid private YouTube config")
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_nlink != 1
            or not 0 < metadata.st_size <= _MAX_CONFIG_BYTES
        ):
            raise YouTubePollConfigError("invalid private YouTube config")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_CONFIG_BYTES + 1)
    except YouTubePollConfigError:
        raise
    except OSError as error:
        raise YouTubePollConfigError("invalid private YouTube config") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAX_CONFIG_BYTES:
        raise YouTubePollConfigError("invalid private YouTube config")
    return YouTubePollConfig.from_canonical_bytes(payload)


def compose_production_youtube_poll_runtime(
    *,
    config_path: Path,
    state_root: Path,
    sink: PublicJobCaptureSink,
    media_adapter: YouTubeMediaAdapter,
    clock: Callable[[], datetime],
) -> ProductionYouTubePollRuntime:
    if not isinstance(state_root, Path) or not state_root.is_absolute():
        raise ValueError("invalid production YouTube poll state")
    return ProductionYouTubePollRuntime(
        config=load_private_youtube_config(config_path),
        state=FilesystemYouTubePollState(state_root / "youtube-poll"),
        sink=sink,
        media_adapter=media_adapter,
        clock=clock,
    )


def _reference_payload(record: PollRecord) -> ReferencePayload:
    if record.extraction is None:
        raise ValueError("invalid completed playlist record")
    extraction = record.extraction
    shared_text = extraction.transcript or extraction.text
    if not shared_text.strip() or extraction.assets:
        raise ValueError("invalid completed playlist extraction")
    return ReferencePayload(record.source_url, shared_text)


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise YouTubePollConfigError("invalid private YouTube config")
    return cast(Mapping[str, object], value)


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise YouTubePollConfigError("invalid private YouTube config")
        result[key] = value
    return result


__all__ = [
    "ProductionYouTubePollResult",
    "ProductionYouTubePollRuntime",
    "YouTubePollCheckpoint",
    "YouTubePollConfig",
    "YouTubePollConfigError",
    "YouTubeSubscription",
    "YouTubeReferenceConnector",
    "YouTubeReferenceTransport",
    "compose_production_youtube_poll_runtime",
    "load_private_youtube_config",
]
