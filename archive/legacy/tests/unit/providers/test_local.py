import pytest
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.core.policy import BoundaryErrorCode
from open_brain_engine.core.ports import TextModelRequest
from open_brain_engine.providers.base import ProviderFailure

from open_brain_legacy.providers.local import LocalProvider


class _TransportFake:
    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str, float, int]] = []

    def complete(
        self,
        *,
        endpoint: str,
        model: str,
        prompt: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> str:
        self.calls.append((endpoint, model, prompt, timeout_seconds, max_output_bytes))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _request(*, max_output_bytes: int = 16) -> TextModelRequest:
    return TextModelRequest.create(
        request_id="request.local-001",
        purpose="synthetic",
        prompt="Synthetic prompt",
        timeout_seconds=1.0,
        max_output_bytes=max_output_bytes,
    )


def _privacy() -> PrivacyDecision:
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def test_local_provider_uses_only_injected_transport_and_request_bounds() -> None:
    transport = _TransportFake("safe")
    provider = LocalProvider(
        endpoint="local.synthetic",
        model="model.synthetic",
        transport=transport,
    )

    result = provider.complete(_request(), privacy=_privacy())

    assert result.text == "safe"
    assert result.provider_name == "local"
    assert transport.calls == [("local.synthetic", "model.synthetic", "Synthetic prompt", 1.0, 16)]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (TimeoutError(), BoundaryErrorCode.PROVIDER_TIMEOUT),
        (ConnectionError(), BoundaryErrorCode.LOCAL_UNAVAILABLE),
        ("", BoundaryErrorCode.MALFORMED_RESPONSE),
        ("x" * 17, BoundaryErrorCode.OUTPUT_LIMIT),
    ],
)
def test_local_provider_maps_transport_and_output_failures_to_closed_codes(
    response: str | Exception, expected: BoundaryErrorCode
) -> None:
    provider = LocalProvider(
        endpoint="local.synthetic",
        model="model.synthetic",
        transport=_TransportFake(response),
    )

    with pytest.raises(ProviderFailure) as error:
        provider.complete(_request(), privacy=_privacy())

    assert error.value.code is expected
