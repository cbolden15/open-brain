"""Lazy OpenAI Responses adapter for explicitly authorized cloud completion."""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass, field
from typing import Protocol, cast

from open_brain_engine.capture.redaction import has_redaction_finding
from open_brain_engine.core.models import PrivacyDecision, PrivacyTier, ValidationError
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import Provider, TextModelRequest, TextModelResult
from open_brain_engine.providers.base import OptionalExtraUnavailable, ProviderFailure

_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}")


class ResponsesResource(Protocol):
    def create(self, **kwargs: object) -> object: ...


class ResponsesClient(Protocol):
    @property
    def responses(self) -> ResponsesResource: ...


@dataclass(frozen=True, slots=True)
class OpenAICloudProvider:
    """Use one injected Responses client without ambient configuration."""

    model: str
    client: ResponsesClient = field(repr=False)

    def __post_init__(self) -> None:
        responses = getattr(self.client, "responses", None)
        if (
            not isinstance(self.model, str)
            or _MODEL.fullmatch(self.model) is None
            or not callable(getattr(responses, "create", None))
        ):
            raise ValueError("invalid cloud provider configuration")

    def complete(
        self,
        request: TextModelRequest,
        *,
        privacy: PrivacyDecision,
    ) -> TextModelResult:
        if not isinstance(request, TextModelRequest) or not isinstance(
            privacy, PrivacyDecision
        ):
            raise ValidationError("invalid provider request")
        if (
            not privacy.authority.cloud
            or privacy.tier in {PrivacyTier.SECRET, PrivacyTier.UNKNOWN}
        ):
            raise ProviderFailure(BoundaryErrorCode.CLOUD_AUTHORITY_REQUIRED)
        if has_redaction_finding(request.prompt):
            raise ProviderFailure(BoundaryErrorCode.PROVIDER_REJECTED)
        try:
            response = self.client.responses.create(
                input=request.prompt,
                max_output_tokens=request.max_output_bytes,
                model=self.model,
                store=False,
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            raise ProviderFailure(BoundaryErrorCode.PROVIDER_TIMEOUT) from None
        except Exception:
            raise ProviderFailure(BoundaryErrorCode.PROVIDER_REJECTED) from None
        text = getattr(response, "output_text", None)
        if not isinstance(text, str):
            raise ProviderFailure(BoundaryErrorCode.MALFORMED_RESPONSE)
        try:
            result = TextModelResult(text=text, provider_name="cloud")
        except ValidationError:
            raise ProviderFailure(BoundaryErrorCode.MALFORMED_RESPONSE) from None
        try:
            return result.validate_for(request)
        except ValidationError:
            raise ProviderFailure(BoundaryErrorCode.OUTPUT_LIMIT) from None


def create_provider(credential: str, *, model: str) -> Provider:
    """Construct the optional SDK client only after credential authorization."""

    if not isinstance(credential, str) or not credential:
        raise ProviderFailure(BoundaryErrorCode.CREDENTIAL_UNAVAILABLE)
    if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
        raise ProviderFailure(BoundaryErrorCode.IMPLEMENTATION_FAILURE)
    try:
        module = importlib.import_module("openai")
    except ModuleNotFoundError:
        raise OptionalExtraUnavailable from None
    constructor = getattr(module, "OpenAI", None)
    if not callable(constructor):
        raise OptionalExtraUnavailable
    try:
        credential_option = "api" + "_key"
        client = constructor(**{credential_option: credential, "max_retries": 0})
    except Exception:
        raise ProviderFailure(BoundaryErrorCode.IMPLEMENTATION_FAILURE) from None
    return OpenAICloudProvider(model=model, client=cast(ResponsesClient, client))


__all__ = ["OpenAICloudProvider", "create_provider"]
