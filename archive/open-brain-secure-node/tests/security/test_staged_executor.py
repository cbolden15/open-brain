from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

import pytest
from open_brain_engine.capture.models import ExtractionFailure, ExtractionState
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.core.ports import (
    FetchRequest,
    FetchResponse,
    StagedExecutionRequest,
    StagedExecutionResult,
)

from open_brain_legacy.capture.extractors.social import SocialExtractionRequest, SocialExtractor

_SYNTHETIC_LEAK = "outside-canary|environment-canary|socket-canary|network-canary|raw-error-canary"


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PUBLIC,
        reason=PrivacyReason.POLICY_PUBLIC,
        policy_version="test-v1",
        authority=Authority(cloud=True, external_egress=True),
    )


@dataclass
class SyntheticFetcher:
    calls: list[FetchRequest] = field(default_factory=list)

    def fetch(self, request: FetchRequest, *, privacy: PrivacyDecision) -> FetchResponse:
        self.calls.append(request)
        return FetchResponse(request.url, 200, "text/html", b"<title>Synthetic</title>")


@dataclass
class SyntheticLeakExecutor:
    shape: str
    calls: int = 0

    def execute(
        self,
        request: StagedExecutionRequest,
        *,
        privacy: PrivacyDecision,
    ) -> StagedExecutionResult:
        del request, privacy
        self.calls += 1
        return StagedExecutionResult(
            text=f"{self.shape}|{_SYNTHETIC_LEAK}",
            produced_assets=(),
        )


@pytest.mark.parametrize("shape", ["image", "text", "prompt"])
def test_arbitrary_staged_executor_injection_is_rejected_without_invocation(shape: str) -> None:
    executor = SyntheticLeakExecutor(shape)
    constructor: Any = SocialExtractor

    with pytest.raises(TypeError):
        constructor(fetcher=SyntheticFetcher(), executor=executor)

    assert executor.calls == 0


def test_public_social_constructor_has_no_executor_dependency() -> None:
    parameters = inspect.signature(SocialExtractor).parameters

    assert "executor" not in parameters
    assert set(parameters) == {
        "fetcher",
        "media_adapter",
        "timeout_seconds",
        "transcription_service",
    }


def test_transcription_request_is_executor_denied_in_closed_mode() -> None:
    fetcher = SyntheticFetcher()

    result = SocialExtractor(fetcher=fetcher).extract(
        SocialExtractionRequest(
            url="https://example.test/synthetic",
            transcribe_audio=True,
        ),
        privacy=_privacy(),
    )

    assert result.state is ExtractionState.FAILED
    assert result.failure is ExtractionFailure.EXECUTOR_DENIED
    assert result.transcript is None
    assert result.assets == ()
    assert fetcher.calls == []
