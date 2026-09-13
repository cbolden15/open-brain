# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Durable requested/seen state for bounded YouTube polling."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from pathlib import Path
from typing import Protocol, cast

from open_brain_engine.engine import (
    CaptureEnvelope,
    ExtractionState,
    NormalizedExtraction,
    PrivacyDecision,
    PrivacyReason,
    SourceType,
    ValidationError,
    canonical_json_bytes,
    validate_identifier,
)

from open_brain_legacy._compat.open_brain_connectors.capture.extractors.youtube import (
    YouTubeExtractionRequest,
    YouTubeExtractor,
    YouTubeMediaAdapter,
    video_id_from_url,
)

_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_DECISION_DIGEST = re.compile(r"[0-9a-f]{64}")
_LEASE_ID = re.compile(r"poll_[0-9a-f]{64}")


class PollItemState(StrEnum):
    REQUESTED = "requested"
    PROCESSING = "processing"
    SEEN = "seen"
    ACCEPTED = "accepted"
    STUBBED = "stubbed"


class PollRequestOrigin(StrEnum):
    DIRECT = "direct"
    PLAYLIST = "playlist"


class PollRequestDisposition(StrEnum):
    CREATED = "created"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class PrivacyReclassificationProof:
    capture_id: str
    prior_decision_digest: str
    replacement: PrivacyDecision
    replacement_decision_digest: str
    authorization_ref: str
    policy_version: str

    @classmethod
    def create(
        cls,
        *,
        capture_id: str,
        prior: PrivacyDecision,
        replacement: PrivacyDecision,
        authorization_ref: str,
        policy_version: str,
    ) -> PrivacyReclassificationProof:
        proof = cls(
            capture_id=capture_id,
            prior_decision_digest=_privacy_digest(prior),
            replacement=replacement,
            replacement_decision_digest=_privacy_digest(replacement),
            authorization_ref=authorization_ref,
            policy_version=policy_version,
        )
        proof.validate_for(
            capture_id=capture_id,
            prior=prior,
            replacement=replacement,
        )
        return proof

    def validate_for(
        self,
        *,
        capture_id: str,
        prior: PrivacyDecision,
        replacement: PrivacyDecision,
    ) -> None:
        try:
            validate_identifier(capture_id, prefix="cap_")
        except ValueError:
            raise ValueError("invalid privacy reclassification proof") from None
        if (
            self.capture_id != capture_id
            or not isinstance(prior, PrivacyDecision)
            or not isinstance(replacement, PrivacyDecision)
            or self.replacement != replacement
            or self.prior_decision_digest != _privacy_digest(prior)
            or self.replacement_decision_digest != _privacy_digest(replacement)
            or not _DECISION_DIGEST.fullmatch(self.prior_decision_digest)
            or not _DECISION_DIGEST.fullmatch(self.replacement_decision_digest)
            or prior.reason
            not in {
                PrivacyReason.CLASSIFICATION_MISSING,
                PrivacyReason.CLASSIFICATION_INVALID,
                PrivacyReason.CLASSIFICATION_AMBIGUOUS,
            }
            or prior.authority.external_egress
            or not replacement.authority.external_egress
            or not isinstance(self.authorization_ref, str)
            or not self.authorization_ref.strip()
            or len(self.authorization_ref) > 256
            or any(ord(character) < 32 for character in self.authorization_ref)
            or self.policy_version != replacement.policy_version
        ):
            raise ValueError("invalid privacy reclassification proof")

    def to_dict(self) -> dict[str, object]:
        return {
            "capture_id": self.capture_id,
            "prior_decision_digest": self.prior_decision_digest,
            "replacement": self.replacement.to_dict(),
            "replacement_decision_digest": self.replacement_decision_digest,
            "authorization_ref": self.authorization_ref,
            "policy_version": self.policy_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PrivacyReclassificationProof:
        if set(value) != {
            "capture_id",
            "prior_decision_digest",
            "replacement",
            "replacement_decision_digest",
            "authorization_ref",
            "policy_version",
        }:
            raise ValueError("invalid privacy reclassification proof")
        return cls(
            capture_id=_string(value["capture_id"]),
            prior_decision_digest=_string(value["prior_decision_digest"]),
            replacement=PrivacyDecision.from_dict(_mapping(value["replacement"])),
            replacement_decision_digest=_string(value["replacement_decision_digest"]),
            authorization_ref=_string(value["authorization_ref"]),
            policy_version=_string(value["policy_version"]),
        )


class PrivacyReclassificationVerifier(Protocol):
    def verify(
        self,
        proof: PrivacyReclassificationProof,
        *,
        capture_id: str,
        prior: PrivacyDecision,
        replacement: PrivacyDecision,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class PollRecord:
    schema_version: int
    video_id: str
    source_url: str
    state: PollItemState
    origin: PollRequestOrigin
    requested_at: datetime
    capture_id: str | None
    capture_why: str
    privacy: PrivacyDecision
    reclassification: PrivacyReclassificationProof | None
    lease_id: str | None
    lease_expires_at: datetime | None
    attempt_count: int
    failure_code: str | None
    extraction: NormalizedExtraction | None

    @classmethod
    def create(
        cls,
        *,
        video_id: str,
        source_url: str,
        state: PollItemState | str,
        origin: PollRequestOrigin | str,
        requested_at: datetime,
        capture_id: str | None,
        capture_why: str,
        privacy: PrivacyDecision,
        reclassification: PrivacyReclassificationProof | None = None,
        lease_id: str | None = None,
        lease_expires_at: datetime | None = None,
        attempt_count: int = 0,
        failure_code: str | None = None,
        extraction: NormalizedExtraction | None = None,
    ) -> PollRecord:
        try:
            normalized_state = PollItemState(state)
            normalized_origin = PollRequestOrigin(origin)
        except (TypeError, ValueError) as error:
            raise ValueError("invalid poll record") from error
        if (
            not isinstance(video_id, str)
            or not _VIDEO_ID.fullmatch(video_id)
            or video_id_from_url(source_url) != video_id
            or not isinstance(privacy, PrivacyDecision)
            or not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 0
            or not isinstance(capture_why, str)
            or len(capture_why) > 2_000
        ):
            raise ValueError("invalid poll record")
        if normalized_origin is PollRequestOrigin.DIRECT:
            try:
                if capture_id is None:
                    raise ValueError
                validate_identifier(capture_id, prefix="cap_")
            except ValueError as error:
                raise ValueError("invalid poll record") from error
            if not capture_why.strip():
                raise ValueError("invalid poll record")
        elif (
            capture_id is not None and normalized_state is not PollItemState.ACCEPTED
        ) or capture_why:
            raise ValueError("invalid poll record")
        if reclassification is not None:
            if capture_id is None or not isinstance(reclassification, PrivacyReclassificationProof):
                raise ValueError("invalid poll record")
            try:
                reclassification.validate_for(
                    capture_id=capture_id,
                    prior=privacy,
                    replacement=reclassification.replacement,
                )
            except ValueError:
                raise ValueError("invalid poll record") from None
        if normalized_state is PollItemState.REQUESTED:
            if (
                extraction is not None
                or lease_id is not None
                or lease_expires_at is not None
                or (attempt_count == 0 and failure_code is not None)
            ):
                raise ValueError("invalid poll record")
        elif normalized_state is PollItemState.PROCESSING:
            if (
                extraction is not None
                or not isinstance(lease_id, str)
                or not _LEASE_ID.fullmatch(lease_id)
                or lease_expires_at is None
            ):
                raise ValueError("invalid poll record")
            lease_expires_at = _utc_datetime(lease_expires_at)
        elif normalized_state in {PollItemState.SEEN, PollItemState.ACCEPTED}:
            if (
                not isinstance(extraction, NormalizedExtraction)
                or extraction.state is not ExtractionState.COMPLETE
                or failure_code is not None
                or attempt_count < 1
                or lease_id is not None
                or lease_expires_at is not None
                or (
                    normalized_state is PollItemState.ACCEPTED
                    and (capture_id is None or not capture_id.startswith("capture_"))
                )
            ):
                raise ValueError("invalid poll record")
        elif (
            extraction is not None
            or not isinstance(failure_code, str)
            or not failure_code
            or attempt_count < 1
            or lease_id is not None
            or lease_expires_at is not None
        ):
            raise ValueError("invalid poll record")
        return cls(
            1,
            video_id,
            source_url,
            normalized_state,
            normalized_origin,
            _utc_datetime(requested_at),
            capture_id,
            capture_why,
            privacy,
            reclassification,
            lease_id,
            lease_expires_at,
            attempt_count,
            failure_code,
            extraction,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "video_id": self.video_id,
            "source_url": self.source_url,
            "state": self.state.value,
            "origin": self.origin.value,
            "requested_at": _timestamp(self.requested_at),
            "capture_id": self.capture_id,
            "capture_why": self.capture_why,
            "privacy": self.privacy.to_dict(),
            "reclassification": (
                None if self.reclassification is None else self.reclassification.to_dict()
            ),
            "lease_id": self.lease_id,
            "lease_expires_at": (
                None if self.lease_expires_at is None else _timestamp(self.lease_expires_at)
            ),
            "attempt_count": self.attempt_count,
            "failure_code": self.failure_code,
            "extraction": None if self.extraction is None else self.extraction.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PollRecord:
        if (
            set(value)
            != {
                "schema_version",
                "video_id",
                "source_url",
                "state",
                "origin",
                "requested_at",
                "capture_id",
                "capture_why",
                "privacy",
                "reclassification",
                "lease_id",
                "lease_expires_at",
                "attempt_count",
                "failure_code",
                "extraction",
            }
            or value["schema_version"] != 1
        ):
            raise ValueError("invalid poll record")
        extraction = value["extraction"]
        return cls.create(
            video_id=_string(value["video_id"]),
            source_url=_string(value["source_url"]),
            state=_string(value["state"]),
            origin=_string(value["origin"]),
            requested_at=_parse_timestamp(_string(value["requested_at"])),
            capture_id=_optional_string(value["capture_id"]),
            capture_why=_string(value["capture_why"]),
            privacy=PrivacyDecision.from_dict(_mapping(value["privacy"])),
            reclassification=(
                None
                if value["reclassification"] is None
                else PrivacyReclassificationProof.from_dict(_mapping(value["reclassification"]))
            ),
            lease_id=_optional_string(value["lease_id"]),
            lease_expires_at=(
                None
                if value["lease_expires_at"] is None
                else _parse_timestamp(_string(value["lease_expires_at"]))
            ),
            attempt_count=_integer(value["attempt_count"]),
            failure_code=_optional_string(value["failure_code"]),
            extraction=None
            if extraction is None
            else NormalizedExtraction.from_dict(_mapping(extraction)),
        )


@dataclass(frozen=True, slots=True)
class PollRequestResult:
    disposition: PollRequestDisposition
    record: PollRecord


@dataclass(frozen=True, slots=True)
class PollRunResult:
    record: PollRecord


class YouTubePollState(Protocol):
    def request(
        self,
        record: PollRecord,
        *,
        reserve_create: Callable[[], bool] | None = None,
    ) -> PollRequestResult: ...

    def records(self) -> tuple[PollRecord, ...]: ...

    def claim_next(self, *, now: datetime, lease_seconds: int) -> PollRecord | None: ...

    def release(self, claimed: PollRecord) -> None: ...

    def replace(self, previous: PollRecord, current: PollRecord) -> None: ...


class FilesystemYouTubePollState:
    """One atomically-published state document for one canonical poller."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path) or not root.is_absolute():
            raise ValueError("poll state root must be absolute")
        self._root = root
        self._path = root / "state.json"
        root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def request(
        self,
        record: PollRecord,
        *,
        reserve_create: Callable[[], bool] | None = None,
    ) -> PollRequestResult:
        if not isinstance(record, PollRecord) or (
            reserve_create is not None and not callable(reserve_create)
        ):
            raise ValueError("invalid poll record")
        with self._locked():
            records = self._load()
            existing = records.get(record.video_id)
            if existing is not None:
                return PollRequestResult(PollRequestDisposition.DUPLICATE, existing)
            if reserve_create is not None and not reserve_create():
                raise ValueError("poll request budget exhausted")
            records[record.video_id] = record
            self._write(records)
            return PollRequestResult(PollRequestDisposition.CREATED, record)

    def get(self, video_id: str) -> PollRecord | None:
        if not isinstance(video_id, str) or not _VIDEO_ID.fullmatch(video_id):
            raise ValueError("invalid video ID")
        with self._locked():
            return self._load().get(video_id)

    def records(self) -> tuple[PollRecord, ...]:
        """Return one validated, stable snapshot without changing poll ownership."""
        with self._locked():
            records = self._load()
            return tuple(records[key] for key in sorted(records))

    def claim_next(self, *, now: datetime, lease_seconds: int) -> PollRecord | None:
        current_time = _utc_datetime(now)
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
        ):
            raise ValueError("invalid poll lease")
        with self._locked():
            records = self._load()
            pending = [
                record
                for record in records.values()
                if record.state is PollItemState.REQUESTED
                or (
                    record.state is PollItemState.PROCESSING
                    and record.lease_expires_at is not None
                    and record.lease_expires_at <= current_time
                )
            ]
            if not pending:
                return None
            previous = min(pending, key=lambda record: (record.requested_at, record.video_id))
            claimed = replace(
                previous,
                state=PollItemState.PROCESSING,
                lease_id="poll_" + secrets.token_hex(32),
                lease_expires_at=current_time + timedelta(seconds=lease_seconds),
            )
            claimed = PollRecord.from_dict(claimed.to_dict())
            records[claimed.video_id] = claimed
            self._write(records)
            return claimed

    def release(self, claimed: PollRecord) -> None:
        if claimed.state is not PollItemState.PROCESSING:
            raise ValueError("invalid poll release")
        self.replace(
            claimed,
            replace(
                claimed,
                state=PollItemState.REQUESTED,
                lease_id=None,
                lease_expires_at=None,
            ),
        )

    def replace(self, previous: PollRecord, current: PollRecord) -> None:
        if previous.video_id != current.video_id:
            raise ValueError("invalid poll transition")
        with self._locked():
            records = self._load()
            if records.get(previous.video_id) != previous:
                raise ValueError("stale poll transition")
            records[current.video_id] = current
            self._write(records)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        descriptor = os.open(self._root / ".poll.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load(self) -> dict[str, PollRecord]:
        if not self._path.exists():
            return {}
        try:
            decoded = json.loads(
                self._path.read_text("utf-8"),
                object_pairs_hook=_reject_duplicates,
            )
            value = _mapping(decoded)
            if set(value) != {"schema_version", "records"} or value["schema_version"] != 1:
                raise ValueError
            raw_records = value["records"]
            if not isinstance(raw_records, list):
                raise ValueError
            records = [PollRecord.from_dict(_mapping(record)) for record in raw_records]
            if records != sorted(records, key=lambda record: record.video_id) or len(
                {record.video_id for record in records}
            ) != len(records):
                raise ValueError
            return {record.video_id: record for record in records}
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as error:
            raise ValueError("invalid poll state") from error

    def _write(self, records: Mapping[str, PollRecord]) -> None:
        payload = canonical_json_bytes(
            {
                "schema_version": 1,
                "records": [records[key].to_dict() for key in sorted(records)],
            }
        )
        temporary = self._root / ("." + secrets.token_hex(16) + ".tmp")
        descriptor = -1
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.replace(temporary, self._path)
            directory = os.open(self._root, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()


class YouTubePoller:
    def __init__(
        self,
        *,
        state: YouTubePollState,
        media_adapter: YouTubeMediaAdapter | None = None,
        max_attempts: int = 3,
        max_playlist_items: int = 50,
        reclassification_verifier: PrivacyReclassificationVerifier | None = None,
        lease_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(max_attempts, int)
            or isinstance(max_attempts, bool)
            or not 1 <= max_attempts <= 20
            or not isinstance(max_playlist_items, int)
            or isinstance(max_playlist_items, bool)
            or not 1 <= max_playlist_items <= 500
            or not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or not 1 <= lease_seconds <= 3600
            or (clock is not None and not callable(clock))
        ):
            raise ValueError("invalid poll configuration")
        self._state = state
        self._extractor = YouTubeExtractor(media_adapter)
        self._max_attempts = max_attempts
        self._max_playlist_items = max_playlist_items
        self._reclassification_verifier = reclassification_verifier
        self._lease_seconds = lease_seconds
        self._clock: Callable[[], datetime] = _utc_now if clock is None else clock

    def request_direct(self, envelope: CaptureEnvelope) -> PollRequestResult:
        if (
            not isinstance(envelope, CaptureEnvelope)
            or envelope.source_type is not SourceType.YOUTUBE
            or envelope.source_url is None
        ):
            raise ValueError("invalid direct YouTube request")
        video_id = video_id_from_url(envelope.source_url)
        if video_id is None:
            raise ValueError("invalid direct YouTube request")
        return self._state.request(
            PollRecord.create(
                video_id=video_id,
                source_url=envelope.source_url,
                state=PollItemState.REQUESTED,
                origin=PollRequestOrigin.DIRECT,
                requested_at=envelope.captured_at,
                capture_id=str(envelope.capture_id),
                capture_why=envelope.capture_why,
                privacy=envelope.privacy_decision,
            )
        )

    def request_playlist(
        self,
        url: str,
        *,
        privacy: PrivacyDecision,
        requested_at: datetime,
    ) -> tuple[PollRequestResult, ...]:
        items = self._extractor.playlist_items(
            url,
            max_items=self._max_playlist_items,
            privacy=privacy,
        )
        results: list[PollRequestResult] = []
        for video_id in items:
            source_url = f"https://www.youtube.com/watch?v={video_id}"
            if video_id_from_url(source_url) is None:
                continue
            results.append(
                self._state.request(
                    PollRecord.create(
                        video_id=video_id,
                        source_url=source_url,
                        state=PollItemState.REQUESTED,
                        origin=PollRequestOrigin.PLAYLIST,
                        requested_at=requested_at,
                        capture_id=None,
                        capture_why="",
                        privacy=privacy,
                    )
                )
            )
        return tuple(results)

    def poll_one(
        self,
        *,
        privacy: PrivacyDecision,
        reclassification: PrivacyReclassificationProof | None = None,
    ) -> PollRunResult | None:
        record = self._state.claim_next(now=self._clock(), lease_seconds=self._lease_seconds)
        if record is None:
            return None
        try:
            proof = reclassification or record.reclassification
            if privacy == record.privacy:
                if reclassification is not None:
                    raise ValueError("invalid privacy reclassification proof")
            else:
                verifier = self._reclassification_verifier
                if proof is None or record.capture_id is None or verifier is None:
                    raise ValueError("poll privacy decision changed")
                proof.validate_for(
                    capture_id=record.capture_id,
                    prior=record.privacy,
                    replacement=privacy,
                )
                if not verifier.verify(
                    proof,
                    capture_id=record.capture_id,
                    prior=record.privacy,
                    replacement=privacy,
                ):
                    raise ValueError("invalid privacy reclassification proof")
            if not privacy.authority.external_egress:
                self._state.release(record)
                return None
            extraction = self._extractor.extract(
                YouTubeExtractionRequest(url=record.source_url),
                privacy=privacy,
            )
        except BaseException:
            with suppress(ValueError):
                self._state.release(record)
            raise
        attempt_count = record.attempt_count + 1
        if extraction.state is ExtractionState.COMPLETE:
            current = replace(
                record,
                state=PollItemState.SEEN,
                attempt_count=attempt_count,
                failure_code=None,
                extraction=extraction,
                reclassification=proof,
                lease_id=None,
                lease_expires_at=None,
            )
        else:
            failure_code = (
                extraction.failure.value
                if extraction.failure is not None
                else extraction.state.value
            )
            current = replace(
                record,
                state=PollItemState.STUBBED
                if attempt_count >= self._max_attempts
                else PollItemState.REQUESTED,
                attempt_count=attempt_count,
                failure_code=failure_code,
                extraction=None,
                reclassification=proof,
                lease_id=None,
                lease_expires_at=None,
            )
        current = PollRecord.from_dict(current.to_dict())
        self._state.replace(record, current)
        return PollRunResult(current)


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid timestamp")
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    try:
        return _utc_datetime(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except (TypeError, ValueError) as error:
        raise ValueError("invalid timestamp") from error


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("invalid mapping")
    return cast(Mapping[str, object], value)


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("invalid integer")
    return value


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _write_all(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short poll-state write")
        remaining = remaining[written:]


def _privacy_digest(decision: PrivacyDecision) -> str:
    if not isinstance(decision, PrivacyDecision):
        raise ValueError("invalid privacy reclassification proof")
    return sha256(canonical_json_bytes(decision.to_dict())).hexdigest()


__all__ = [
    "FilesystemYouTubePollState",
    "PollItemState",
    "PollRecord",
    "PollRequestDisposition",
    "PollRequestResult",
    "PollRunResult",
    "PrivacyReclassificationProof",
    "PrivacyReclassificationVerifier",
    "YouTubePoller",
]
