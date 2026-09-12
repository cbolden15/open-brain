"""Disabled-by-default transcription behind an injected provider."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from open_brain_engine.core.models import PrivacyDecision, RawAssetRef, ValidationError
from open_brain_engine.core.policy import BoundaryErrorCode, BoundaryResult
from open_brain_engine.providers.base import OptionalExtraUnavailable, ProviderFailure

MAX_TRANSCRIPTION_MEDIA_BYTES = 50 * 1024 * 1024
MAX_TRANSCRIPTION_OUTPUT_BYTES = 2 * 1024 * 1024
_REQUEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


@dataclass(frozen=True, slots=True)
class TranscriptionRequest:
    request_id: str
    asset: RawAssetRef
    timeout_seconds: float
    max_output_bytes: int

    @classmethod
    def create(
        cls,
        *,
        request_id: str,
        asset: RawAssetRef,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> TranscriptionRequest:
        if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
            raise ValidationError("invalid transcription request")
        if (
            not isinstance(asset, RawAssetRef)
            or not asset.media_type.startswith(("audio/", "video/"))
            or asset.byte_length < 1
            or asset.byte_length > MAX_TRANSCRIPTION_MEDIA_BYTES
            or not isinstance(timeout_seconds, int | float)
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 600
            or not isinstance(max_output_bytes, int)
            or isinstance(max_output_bytes, bool)
            or not 0 < max_output_bytes <= MAX_TRANSCRIPTION_OUTPUT_BYTES
        ):
            raise ValidationError("invalid transcription request")
        return cls(request_id, asset, float(timeout_seconds), max_output_bytes)


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    provider_name: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not isinstance(self.provider_name, str):
            raise ValidationError("invalid transcription result")
        normalized_text = unicodedata.normalize("NFC", self.text).replace("\r\n", "\n").replace(
            "\r", "\n"
        )
        normalized_provider = unicodedata.normalize("NFC", self.provider_name)
        if (
            not normalized_text.strip()
            or len(normalized_text.encode("utf-8")) > MAX_TRANSCRIPTION_OUTPUT_BYTES
            or not _REQUEST_ID.fullmatch(normalized_provider)
        ):
            raise ValidationError("invalid transcription result")
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "provider_name", normalized_provider)

    def validate_for(self, request: TranscriptionRequest) -> TranscriptionResult:
        if len(self.text.encode("utf-8")) > request.max_output_bytes:
            raise ValidationError("transcription output exceeds limit")
        return self


class TranscriptionProvider(Protocol):
    def transcribe(
        self, request: TranscriptionRequest, *, privacy: PrivacyDecision
    ) -> TranscriptionResult: ...


class TranscriptionService:
    """Invoke one explicit provider only when the capability is enabled."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        provider_factory: Callable[[], TranscriptionProvider] | None = None,
        cloud_provider: bool = True,
    ) -> None:
        if not isinstance(enabled, bool) or not isinstance(cloud_provider, bool):
            raise ValidationError("invalid transcription configuration")
        self._enabled = enabled
        self._provider_factory = provider_factory
        self._cloud_provider = cloud_provider

    def transcribe(
        self,
        request: TranscriptionRequest,
        *,
        privacy: PrivacyDecision,
    ) -> BoundaryResult[TranscriptionResult]:
        if not isinstance(request, TranscriptionRequest) or not isinstance(
            privacy, PrivacyDecision
        ):
            raise ValidationError("invalid transcription request")
        if not self._enabled:
            return BoundaryResult(None, BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE)
        if self._cloud_provider and not privacy.authority.cloud:
            return BoundaryResult(None, BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED)
        if self._provider_factory is None:
            return BoundaryResult(None, BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE)
        try:
            provider = self._provider_factory()
        except OptionalExtraUnavailable:
            return BoundaryResult(None, BoundaryErrorCode.OPTIONAL_EXTRA_UNAVAILABLE)
        except ProviderFailure as error:
            return BoundaryResult(None, error.code)
        except Exception:
            return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
        try:
            result = provider.transcribe(request, privacy=privacy)
        except ProviderFailure as error:
            return BoundaryResult(None, error.code)
        except TimeoutError:
            return BoundaryResult(None, BoundaryErrorCode.PROVIDER_TIMEOUT)
        except (ConnectionError, OSError):
            return BoundaryResult(None, BoundaryErrorCode.LOCAL_UNAVAILABLE)
        except Exception:
            return BoundaryResult(None, BoundaryErrorCode.IMPLEMENTATION_FAILURE)
        if not isinstance(result, TranscriptionResult):
            return BoundaryResult(None, BoundaryErrorCode.MALFORMED_RESPONSE)
        try:
            return BoundaryResult(result.validate_for(request), None)
        except ValidationError:
            return BoundaryResult(None, BoundaryErrorCode.OUTPUT_LIMIT)


__all__ = [
    "MAX_TRANSCRIPTION_MEDIA_BYTES",
    "TranscriptionProvider",
    "TranscriptionRequest",
    "TranscriptionResult",
    "TranscriptionService",
]
