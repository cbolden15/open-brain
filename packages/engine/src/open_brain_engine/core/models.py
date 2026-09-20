from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any, Literal, cast

from .ids import (
    AssetId,
    asset_id_for,
    canonical_json_bytes,
    canonicalize_source_url,
    capture_id_for,
    validate_identifier,
)
from .ids import CaptureId as CaptureId


class ValidationError(ValueError):
    """A value violates a closed Phase 2 domain contract."""


class AuthorityBroadeningError(ValidationError):
    """A caller attempted to grant authority beyond the stored maximum."""


class SourceType(StrEnum):
    YOUTUBE = "youtube"
    SOCIAL = "social"
    WEB = "web"
    TEXT = "text"


class ContentKind(StrEnum):
    EVENT = "event"
    ARTICLE = "article"
    PRODUCT = "product"
    PLACE = "place"
    POST = "post"
    VIDEO = "video"
    OTHER = "other"


class CaptureWhyOrigin(StrEnum):
    OWNER_AUTHORED = "owner_authored"
    AUTOMATION_ABSENT = "automation_absent"


class CaptureSource(StrEnum):
    SHORTCUT = "shortcut"
    PLAYLIST = "playlist"
    CLI = "cli"
    INTEGRATION = "integration"


class ContentOrigin(StrEnum):
    OWNER_AUTHORED = "owner_authored"
    THIRD_PARTY = "third_party"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class PrivacyTier(StrEnum):
    PUBLIC = "public"
    WORK = "work"
    PERSONAL = "personal"
    SECRET = "secret"
    UNKNOWN = "unknown"


class PrivacyReason(StrEnum):
    POLICY_PUBLIC = "policy_public"
    POLICY_WORK = "policy_work"
    PERSONAL_LOCAL_ONLY = "personal_local_only"
    PERSONAL_CONFIRMED = "personal_confirmed"
    SECRET_DETECTED = "secret_detected"
    CLASSIFICATION_MISSING = "classification_missing"
    CLASSIFICATION_INVALID = "classification_invalid"
    CLASSIFICATION_AMBIGUOUS = "classification_ambiguous"
    EXPLICIT_LOCAL_ONLY = "explicit_local_only"


class Intent(StrEnum):
    REFERENCE = "reference"
    IDEA = "idea"
    ACTION_CANDIDATE = "action_candidate"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class EffectiveAuthority:
    cloud: bool
    external_egress: bool


@dataclass(frozen=True, slots=True)
class Authority:
    cloud: bool
    external_egress: bool

    def narrow(self, *, cloud: bool, external_egress: bool) -> EffectiveAuthority:
        if cloud and not self.cloud or external_egress and not self.external_egress:
            raise AuthorityBroadeningError("authority may only narrow")
        return EffectiveAuthority(cloud=cloud, external_egress=external_egress)


@dataclass(frozen=True, slots=True)
class PrivacyDecision:
    tier: PrivacyTier
    reason: PrivacyReason
    policy_version: str
    authority: Authority
    confirmation_ref: str | None

    @classmethod
    def create(
        cls,
        *,
        tier: PrivacyTier | str,
        reason: PrivacyReason | str,
        policy_version: str,
        authority: Authority,
        confirmation_ref: str | None = None,
    ) -> PrivacyDecision:
        normalized_tier = _enum(PrivacyTier, tier)
        normalized_reason = _enum(PrivacyReason, reason)
        if not isinstance(policy_version, str) or not policy_version:
            raise ValidationError("invalid privacy decision")
        if not isinstance(authority, Authority):
            raise ValidationError("invalid privacy decision")
        normalized_confirmation_ref = None
        if confirmation_ref is not None:
            if not isinstance(confirmation_ref, str) or not confirmation_ref:
                raise ValidationError("invalid privacy decision")
            normalized_confirmation_ref = _nfc(confirmation_ref)
        expected_tier = {
            PrivacyReason.POLICY_PUBLIC: PrivacyTier.PUBLIC,
            PrivacyReason.POLICY_WORK: PrivacyTier.WORK,
            PrivacyReason.PERSONAL_LOCAL_ONLY: PrivacyTier.PERSONAL,
            PrivacyReason.PERSONAL_CONFIRMED: PrivacyTier.PERSONAL,
            PrivacyReason.SECRET_DETECTED: PrivacyTier.SECRET,
            PrivacyReason.CLASSIFICATION_MISSING: PrivacyTier.UNKNOWN,
            PrivacyReason.CLASSIFICATION_INVALID: PrivacyTier.UNKNOWN,
            PrivacyReason.CLASSIFICATION_AMBIGUOUS: PrivacyTier.UNKNOWN,
            PrivacyReason.EXPLICIT_LOCAL_ONLY: PrivacyTier.PERSONAL,
        }
        local_only_reasons = {
            PrivacyReason.PERSONAL_LOCAL_ONLY,
            PrivacyReason.SECRET_DETECTED,
            PrivacyReason.CLASSIFICATION_MISSING,
            PrivacyReason.CLASSIFICATION_INVALID,
            PrivacyReason.CLASSIFICATION_AMBIGUOUS,
            PrivacyReason.EXPLICIT_LOCAL_ONLY,
        }
        if normalized_tier is not expected_tier[normalized_reason]:
            raise ValidationError("invalid privacy decision")
        if normalized_reason is PrivacyReason.PERSONAL_CONFIRMED:
            if not normalized_confirmation_ref:
                raise ValidationError("invalid privacy decision")
        elif normalized_confirmation_ref is not None:
            raise ValidationError("invalid privacy decision")
        if normalized_reason in local_only_reasons and (
            authority.cloud or authority.external_egress
        ):
            raise ValidationError("invalid privacy decision")
        return cls(
            normalized_tier,
            normalized_reason,
            _nfc(policy_version),
            authority,
            normalized_confirmation_ref,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier.value,
            "reason": self.reason.value,
            "policy_version": self.policy_version,
            "authority": {
                "cloud": self.authority.cloud,
                "external_egress": self.authority.external_egress,
            },
            "confirmation_ref": self.confirmation_ref,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> PrivacyDecision:
        stored = dict(_mapping(value))
        _exact_keys(stored, {"tier", "reason", "policy_version", "authority", "confirmation_ref"})
        authority = dict(_mapping(stored["authority"]))
        _exact_keys(authority, {"cloud", "external_egress"})
        if not isinstance(authority["cloud"], bool) or not isinstance(
            authority["external_egress"], bool
        ):
            raise ValidationError("invalid privacy decision")
        return cls.create(
            tier=cast(str, stored["tier"]),
            reason=cast(str, stored["reason"]),
            policy_version=cast(str, stored["policy_version"]),
            authority=Authority(
                cloud=authority["cloud"], external_egress=authority["external_egress"]
            ),
            confirmation_ref=cast(str | None, stored["confirmation_ref"]),
        )


@dataclass(frozen=True, slots=True)
class Provenance:
    source_ref: str
    content_origin: ContentOrigin
    owner_context: CaptureWhyOrigin

    @classmethod
    def create(
        cls,
        *,
        source_ref: str,
        content_origin: ContentOrigin | str,
        owner_context: CaptureWhyOrigin | str,
    ) -> Provenance:
        if not isinstance(source_ref, str) or not source_ref:
            raise ValidationError("invalid provenance")
        return cls(
            _nfc(source_ref),
            _enum(ContentOrigin, content_origin),
            _enum(CaptureWhyOrigin, owner_context),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "source_ref": self.source_ref,
            "content_origin": self.content_origin.value,
            "owner_context": self.owner_context.value,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> Provenance:
        _exact_keys(value, {"source_ref", "content_origin", "owner_context"})
        return cls.create(
            source_ref=cast(str, value["source_ref"]),
            content_origin=cast(str, value["content_origin"]),
            owner_context=cast(str, value["owner_context"]),
        )


@dataclass(frozen=True, slots=True)
class RawAssetRef:
    asset_id: AssetId
    sha256: str
    media_type: str
    byte_length: int

    @classmethod
    def create(
        cls, *, asset_id: AssetId | str, sha256: str, media_type: str, byte_length: int
    ) -> RawAssetRef:
        try:
            asset_value = validate_identifier(str(asset_id), prefix="asset_")
        except ValueError as error:
            raise ValidationError("invalid asset") from error
        if sha256 != asset_value[6:] or not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValidationError("invalid asset")
        if not isinstance(media_type, str) or not re.fullmatch(
            r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", media_type
        ):
            raise ValidationError("invalid asset")
        if not isinstance(byte_length, int) or isinstance(byte_length, bool) or byte_length < 0:
            raise ValidationError("invalid asset")
        return cls(AssetId(asset_value), sha256, media_type, byte_length)

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": str(self.asset_id),
            "sha256": self.sha256,
            "media_type": self.media_type,
            "byte_length": self.byte_length,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> RawAssetRef:
        _exact_keys(value, {"asset_id", "sha256", "media_type", "byte_length"})
        return cls.create(
            asset_id=cast(str, value["asset_id"]),
            sha256=cast(str, value["sha256"]),
            media_type=cast(str, value["media_type"]),
            byte_length=cast(int, value["byte_length"]),
        )


@dataclass(frozen=True, slots=True)
class CaptureEnvelope:
    schema_version: Literal[1]
    capture_id: CaptureId
    source_type: SourceType
    content_kind: ContentKind
    source_url: str | None
    title: str | None
    shared_text: str
    captured_at: datetime
    capture_why: str
    capture_why_origin: CaptureWhyOrigin
    capture_source: CaptureSource
    provenance: Provenance
    raw_assets: tuple[RawAssetRef, ...]
    privacy_decision: PrivacyDecision

    @classmethod
    def create(
        cls,
        *,
        source_type: SourceType | str,
        content_kind: ContentKind | str,
        source_url: str | None,
        title: str | None,
        shared_text: str,
        captured_at: datetime,
        capture_why: str,
        capture_why_origin: CaptureWhyOrigin | str,
        capture_source: CaptureSource | str,
        provenance: Provenance,
        raw_assets: tuple[RawAssetRef, ...],
        privacy_decision: PrivacyDecision,
        capture_id: CaptureId | str | None = None,
    ) -> CaptureEnvelope:
        normalized_source = _enum(SourceType, source_type)
        normalized_kind = _enum(ContentKind, content_kind)
        normalized_origin = _enum(CaptureWhyOrigin, capture_why_origin)
        normalized_capture_source = _enum(CaptureSource, capture_source)
        if (
            not isinstance(shared_text, str)
            or not isinstance(provenance, Provenance)
            or not isinstance(privacy_decision, PrivacyDecision)
        ):
            raise ValidationError("invalid capture")
        text = _nfc(shared_text).replace("\r\n", "\n").replace("\r", "\n")
        normalized_title = None if title is None or _nfc(title) == "" else _nfc(title)
        timestamp = _utc_datetime(captured_at)
        normalized_url = None if source_url is None else canonicalize_source_url(source_url)
        if normalized_source is SourceType.TEXT:
            if normalized_url is not None or not text.strip():
                raise ValidationError("invalid capture")
            expected_ref = "urn:open-brain:text:sha256:" + sha256(text.encode("utf-8")).hexdigest()
        else:
            if normalized_url is None:
                raise ValidationError("invalid capture")
            expected_ref = normalized_url
        if provenance.source_ref != expected_ref:
            raise ValidationError("invalid capture")
        reason = _validate_capture_why(
            capture_why, normalized_origin, normalized_capture_source, provenance
        )
        if not isinstance(raw_assets, tuple) or any(
            not isinstance(asset, RawAssetRef) for asset in raw_assets
        ):
            raise ValidationError("invalid capture")
        if tuple(sorted(raw_assets, key=lambda asset: str(asset.asset_id))) != raw_assets or len(
            {asset.asset_id for asset in raw_assets}
        ) != len(raw_assets):
            raise ValidationError("invalid capture")
        identity = {
            "identity_version": 1,
            "capture_source": normalized_capture_source.value,
            "capture_why": reason,
            "capture_why_origin": normalized_origin.value,
            "content_kind": normalized_kind.value,
            "content_origin": provenance.content_origin.value,
            "shared_text_sha256": sha256(text.encode("utf-8")).hexdigest(),
            "source_ref": provenance.source_ref,
            "source_type": normalized_source.value,
        }
        derived_id = capture_id_for(identity)
        if capture_id is not None:
            try:
                validate_identifier(str(capture_id), prefix="cap_")
            except ValueError as error:
                raise ValidationError("invalid capture") from error
            if str(capture_id) != derived_id:
                raise ValidationError("invalid capture")
        return cls(
            1,
            derived_id,
            normalized_source,
            normalized_kind,
            normalized_url,
            normalized_title,
            text,
            timestamp,
            reason,
            normalized_origin,
            normalized_capture_source,
            provenance,
            raw_assets,
            privacy_decision,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "capture_id": str(self.capture_id),
            "source_type": self.source_type.value,
            "content_kind": self.content_kind.value,
            "source_url": self.source_url,
            "title": self.title,
            "shared_text": self.shared_text,
            "captured_at": _timestamp(self.captured_at),
            "capture_why": self.capture_why,
            "capture_why_origin": self.capture_why_origin.value,
            "capture_source": self.capture_source.value,
            "provenance": self.provenance.to_dict(),
            "raw_assets": [asset.to_dict() for asset in self.raw_assets],
            "privacy_decision": self.privacy_decision.to_dict(),
        }

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CaptureEnvelope:
        _exact_keys(
            value,
            {
                "schema_version",
                "capture_id",
                "source_type",
                "content_kind",
                "source_url",
                "title",
                "shared_text",
                "captured_at",
                "capture_why",
                "capture_why_origin",
                "capture_source",
                "provenance",
                "raw_assets",
                "privacy_decision",
            },
        )
        if value["schema_version"] != 1 or not isinstance(value["raw_assets"], list):
            raise ValidationError("invalid capture")
        return cls.create(
            capture_id=cast(str, value["capture_id"]),
            source_type=cast(str, value["source_type"]),
            content_kind=cast(str, value["content_kind"]),
            source_url=cast(str | None, value["source_url"]),
            title=cast(str | None, value["title"]),
            shared_text=cast(str, value["shared_text"]),
            captured_at=_parse_timestamp(cast(str, value["captured_at"])),
            capture_why=cast(str, value["capture_why"]),
            capture_why_origin=cast(str, value["capture_why_origin"]),
            capture_source=cast(str, value["capture_source"]),
            provenance=Provenance.from_dict(_mapping(value["provenance"])),
            raw_assets=tuple(
                RawAssetRef.from_dict(_mapping(asset)) for asset in value["raw_assets"]
            ),
            privacy_decision=PrivacyDecision.from_dict(_mapping(value["privacy_decision"])),
        )

    @classmethod
    def from_canonical_bytes(cls, payload: bytes) -> CaptureEnvelope:
        try:
            decoded = json.loads(payload.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise ValidationError("invalid canonical capture") from error
        result = cls.from_dict(_mapping(decoded))
        if result.canonical_bytes() != payload:
            raise ValidationError("non-canonical capture")
        return result


@dataclass(frozen=True, slots=True)
class RawAssetBlob:
    ref: RawAssetRef
    data: bytes

    @classmethod
    def create(cls, *, ref: RawAssetRef, data: bytes) -> RawAssetBlob:
        if (
            not isinstance(ref, RawAssetRef)
            or not isinstance(data, bytes)
            or asset_id_for(data) != ref.asset_id
            or len(data) != ref.byte_length
        ):
            raise ValidationError("invalid asset blob")
        return cls(ref, data)


@dataclass(frozen=True, slots=True)
class RawCapture:
    envelope: CaptureEnvelope
    assets: tuple[RawAssetBlob, ...]

    @classmethod
    def create(cls, *, envelope: CaptureEnvelope, assets: tuple[RawAssetBlob, ...]) -> RawCapture:
        if (
            not isinstance(envelope, CaptureEnvelope)
            or not isinstance(assets, tuple)
            or any(not isinstance(asset, RawAssetBlob) for asset in assets)
            or {asset.ref.asset_id for asset in assets}
            != {ref.asset_id for ref in envelope.raw_assets}
            or tuple(sorted(assets, key=lambda asset: str(asset.ref.asset_id))) != assets
            or len({asset.ref.asset_id for asset in assets}) != len(assets)
        ):
            raise ValidationError("invalid raw capture")
        return cls(envelope, assets)


def _enum(enum_type: type[StrEnum], value: StrEnum | str) -> Any:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as error:
        raise ValidationError("invalid enum") from error


def _nfc(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("invalid text")
    return unicodedata.normalize("NFC", value)


def _validate_capture_why(
    value: str, origin: CaptureWhyOrigin, source: CaptureSource, provenance: Provenance
) -> str:
    text = _nfc(value)
    separators = {"\r", "\n", "\u0085", "\u2028", "\u2029"}
    if origin is CaptureWhyOrigin.AUTOMATION_ABSENT:
        if (
            source is not CaptureSource.PLAYLIST
            or text
            or provenance.owner_context is not CaptureWhyOrigin.AUTOMATION_ABSENT
        ):
            raise ValidationError("invalid capture reason")
        return text
    if (
        not text
        or text.isspace()
        or len(text) > 280
        or any(separator in text for separator in separators)
        or provenance.owner_context is not CaptureWhyOrigin.OWNER_AUTHORED
    ):
        raise ValidationError("invalid capture reason")
    return text


def _utc_datetime(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("invalid timestamp")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return _utc_datetime(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
    ):
        raise ValidationError("invalid timestamp")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValidationError("invalid object")
    return value


def _exact_keys(value: Mapping[str, object], expected: set[str]) -> None:
    if set(value) != expected:
        raise ValidationError("invalid fields")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError("duplicate key")
        result[key] = value
    return result
