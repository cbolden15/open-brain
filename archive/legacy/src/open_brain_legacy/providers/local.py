"""Injected local transport implementation with bounded output handling."""

from __future__ import annotations

from typing import Protocol

from open_brain_engine.core.models import PrivacyDecision, ValidationError
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import TextModelRequest, TextModelResult
from open_brain_engine.providers.base import ProviderFailure


class LocalTransport(Protocol):
    def complete(
        self,
        *,
        endpoint: str,
        model: str,
        prompt: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> str: ...


class LocalProvider:
    """A local provider that performs no ambient configuration or I/O setup."""

    def __init__(self, *, endpoint: str, model: str, transport: LocalTransport) -> None:
        if not isinstance(endpoint, str) or not endpoint or endpoint.isspace():
            raise ValidationError("invalid local endpoint")
        if not isinstance(model, str) or not model or model.isspace():
            raise ValidationError("invalid local model")
        self._endpoint = endpoint
        self._model = model
        self._transport = transport

    def complete(self, request: TextModelRequest, *, privacy: PrivacyDecision) -> TextModelResult:
        if not isinstance(request, TextModelRequest) or not isinstance(privacy, PrivacyDecision):
            raise ValidationError("invalid provider request")
        try:
            text = self._transport.complete(
                endpoint=self._endpoint,
                model=self._model,
                prompt=request.prompt,
                timeout_seconds=request.timeout_seconds,
                max_output_bytes=request.max_output_bytes,
            )
        except TimeoutError as error:
            raise ProviderFailure(BoundaryErrorCode.PROVIDER_TIMEOUT) from error
        except (ConnectionError, OSError) as error:
            raise ProviderFailure(BoundaryErrorCode.LOCAL_UNAVAILABLE) from error
        except Exception as error:
            raise ProviderFailure(BoundaryErrorCode.IMPLEMENTATION_FAILURE) from error
        if not isinstance(text, str):
            raise ProviderFailure(BoundaryErrorCode.MALFORMED_RESPONSE)
        try:
            result = TextModelResult(text=text, provider_name="local")
        except ValidationError as error:
            raise ProviderFailure(BoundaryErrorCode.MALFORMED_RESPONSE) from error
        try:
            return result.validate_for(request)
        except ValidationError as error:
            raise ProviderFailure(BoundaryErrorCode.OUTPUT_LIMIT) from error
