from __future__ import annotations

import json

import pytest

from open_brain.integrations.ports import PageDocument, PageReadRequest
from open_brain.integrations.ui import UiBindConfig, UiHandler, UiRequest


class RecordingPageReader:
    def __init__(self) -> None:
        self.requests: list[PageReadRequest] = []

    def read(self, request: PageReadRequest) -> PageDocument | None:
        self.requests.append(request)
        return None


class FailingPageReader:
    def __init__(self, detail: str) -> None:
        self._detail = detail

    def read(self, request: PageReadRequest) -> PageDocument | None:
        raise RuntimeError(self._detail)


def test_ui_bind_is_loopback_only_by_default() -> None:
    bind = UiBindConfig()

    assert bind.host == "127.0.0.1"
    assert bind.port == 8788
    assert bind.allow_private_network is False


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "localhost", ".".join(("192", "168", "50", "10")), "203.0.113.10"],
)
def test_ui_bind_rejects_wildcard_names_and_non_loopback_without_opt_in(host: str) -> None:
    with pytest.raises(ValueError, match="unsafe UI bind"):
        UiBindConfig(host=host)


def test_ui_bind_allows_explicit_private_literals_but_never_public_literals() -> None:
    private_host = ".".join(("192", "168", "50", "10"))
    private = UiBindConfig(
        host=private_host,
        port=9000,
        allow_private_network=True,
    )

    assert private.host == private_host
    assert UiBindConfig(host="::1").host == "::1"
    with pytest.raises(ValueError, match="unsafe UI bind"):
        UiBindConfig(host="203.0.113.10", allow_private_network=True)


@pytest.mark.parametrize(
    "values",
    [
        {"port": 0},
        {"port": 65_536},
        {"port": True},
        {"allow_private_network": 1},
    ],
)
def test_ui_bind_rejects_invalid_port_and_opt_in_types(values: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="unsafe UI bind"):
        UiBindConfig(**values)  # type: ignore[arg-type]


def test_authentication_happens_before_page_dispatch() -> None:
    reader = RecordingPageReader()
    handler = UiHandler(expected_bearer_token="synthetic-ui-token", page_reader=reader)

    unauthorized = handler.handle(
        UiRequest(method="GET", path="/pages/page.synthetic-001", headers=())
    )

    assert unauthorized.status == 401
    assert reader.requests == []

    missing = handler.handle(
        UiRequest(
            method="GET",
            path="/pages/page.synthetic-001",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )

    assert missing.status == 404
    assert reader.requests == [PageReadRequest(page_id="page.synthetic-001")]


def test_authenticated_health_is_metadata_only_and_does_not_dispatch() -> None:
    reader = RecordingPageReader()
    handler = UiHandler(expected_bearer_token="synthetic-ui-token", page_reader=reader)

    response = handler.handle(
        UiRequest(
            method="GET",
            path="/health",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )

    assert response.status == 200
    assert json.loads(response.body) == {"status": "ok"}
    assert reader.requests == []


def test_write_methods_are_denied_after_authentication_without_dispatch() -> None:
    reader = RecordingPageReader()
    handler = UiHandler(expected_bearer_token="synthetic-ui-token", page_reader=reader)
    path = "/pages/page.synthetic-001"

    unauthorized = handler.handle(UiRequest(method="POST", path=path, headers=()))
    denied = handler.handle(
        UiRequest(
            method="POST",
            path=path,
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )

    assert unauthorized.status == 401
    assert denied.status == 405
    assert ("Allow", "GET") in denied.headers
    assert reader.requests == []


def test_authenticated_malformed_route_fields_are_rejected_without_dispatch() -> None:
    reader = RecordingPageReader()
    handler = UiHandler(expected_bearer_token="synthetic-ui-token", page_reader=reader)
    authorization = (("Authorization", "Bearer synthetic-ui-token"),)

    malformed_path = handler.handle(
        UiRequest(method="GET", path=object(), headers=authorization)  # type: ignore[arg-type]
    )
    malformed_method = handler.handle(
        UiRequest(method=object(), path="/health", headers=authorization)  # type: ignore[arg-type]
    )

    assert malformed_path.status == 400
    assert malformed_path.body == b"invalid_request"
    assert malformed_method.status == 400
    assert malformed_method.body == b"invalid_request"
    assert reader.requests == []


def test_page_failures_return_only_a_redacted_failure_class() -> None:
    detail = "synthetic private content and raw exception detail"
    handler = UiHandler(
        expected_bearer_token="synthetic-ui-token",
        page_reader=FailingPageReader(detail),
    )

    response = handler.handle(
        UiRequest(
            method="GET",
            path="/pages/page.synthetic-001",
            headers=(("Authorization", "Bearer synthetic-ui-token"),),
        )
    )

    assert response.status == 503
    assert response.body == b"service_unavailable"
    assert detail.encode() not in response.body
