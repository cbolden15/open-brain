from __future__ import annotations

import hmac
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
from open_brain_engine.engine import CaptureAction, CaptureReceipt, Payload, open_local_engine

from open_brain.capture.http import (
    BODY_LIMIT_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    HttpRequest,
    RequestReadTimeout,
    ShareHttpHandler,
)
from open_brain.profile import compile_single_user_local


@dataclass
class _CaptureSpy:
    calls: list[dict[str, object]] = field(default_factory=list)
    failure: Exception | None = None
    duplicate: bool = False

    def accept(
        self,
        payload: Payload,
        *,
        delivery_id: str,
        action: CaptureAction = CaptureAction.QUICK,
        space_id: str | None = None,
        intent: str | None = None,
        capture_why: str | None = None,
        title: str | None = None,
    ) -> CaptureReceipt:
        if self.failure is not None:
            raise self.failure
        self.calls.append(
            {
                "action": action,
                "capture_why": capture_why,
                "delivery_id": delivery_id,
                "intent": intent,
                "payload": payload,
                "space_id": space_id,
                "title": title,
            }
        )
        return CaptureReceipt(
            capture_id="cap_" + "a" * 64,
            payload_family="reference_or_file",
            state="captured",
            enrichment_state="held",
            space_id=None,
            canonical_path=None,
            duplicate=self.duplicate,
        )


class _Reader:
    def __init__(self, body: bytes, failure: Exception | None = None) -> None:
        self.body = body
        self.failure = failure
        self.calls: list[tuple[int, float]] = []

    def __call__(self, maximum_bytes: int, timeout_seconds: float) -> bytes:
        self.calls.append((maximum_bytes, timeout_seconds))
        if self.failure is not None:
            raise self.failure
        return self.body


def _request(body: bytes, *, authorization: str = "Bearer synthetic-token") -> HttpRequest:
    return HttpRequest(
        method="POST",
        path="/share",
        headers=(
            ("Authorization", authorization),
            ("Content-Length", str(len(body))),
            ("Content-Type", "application/json"),
        ),
    )


def _handler(root: Path, body: bytes, *, calls: list[tuple[int, float]]) -> ShareHttpHandler:
    return ShareHttpHandler(
        expected_bearer_token="synthetic-token",
        capture=open_local_engine(compile_single_user_local(root)).capture,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        body_reader=lambda maximum, timeout: _record_body(calls, maximum, timeout, body),
    )


def _record_body(
    calls: list[tuple[int, float]], maximum: int, timeout: float, body: bytes
) -> bytes:
    calls.append((maximum, timeout))
    return body


def test_share_authenticates_and_bounds_before_reading_the_body(tmp_path: Path) -> None:
    body = b'{"url":"https://example.test/share","why":"Synthetic reason"}'
    calls: list[tuple[int, float]] = []
    handler = _handler(tmp_path / "brain", body, calls=calls)

    unauthorized = handler.handle(_request(body, authorization="Bearer wrong-token"))
    oversized = handler.handle(
        HttpRequest(
            method="POST",
            path="/share",
            headers=(
                ("Authorization", "Bearer synthetic-token"),
                ("Content-Length", str(BODY_LIMIT_BYTES + 1)),
                ("Content-Type", "application/json"),
            ),
        )
    )

    assert (unauthorized.status, unauthorized.body) == (401, b'{"code":"unauthorized"}')
    assert (oversized.status, oversized.body) == (413, b'{"code":"request_too_large"}')
    assert calls == []


def test_share_keeps_the_202_envelope_and_uses_engine_replay(tmp_path: Path) -> None:
    body = json.dumps(
        {
            "url": "https://www.youtube.com/watch?v=synthetic123",
            "why": "Watch this before the planning meeting",
            "text": "A teammate shared this from iOS.",
        },
        separators=(",", ":"),
    ).encode()
    calls: list[tuple[int, float]] = []
    handler = _handler(tmp_path / "brain", body, calls=calls)

    created = handler.handle(_request(body))
    replay = handler.handle(_request(body))
    created_value = json.loads(created.body)
    replay_value = json.loads(replay.body)

    assert created.status == replay.status == 202
    assert created_value["status"] == "queued"
    assert replay_value == {**created_value, "duplicate": True, "status": "duplicate"}
    assert created_value["pipeline"] == "youtube"
    assert created_value["capture_id"] == replay_value["capture_id"]
    assert calls == [(BODY_LIMIT_BYTES, 5.0), (BODY_LIMIT_BYTES, 5.0)]


def _spy_handler(
    reader: _Reader, spy: _CaptureSpy, *, token: str = "synthetic-token"
) -> ShareHttpHandler:
    return ShareHttpHandler(
        expected_bearer_token=token,
        capture=spy,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        body_reader=reader,
    )


def test_share_uses_constant_time_auth_and_refuses_blank_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[bytes, bytes]] = []
    original = hmac.compare_digest

    def compare(left: bytes, right: bytes) -> bool:
        observed.append((left, right))
        return bool(original(left, right))

    monkeypatch.setattr(
        hmac,
        "compare_digest",
        compare,
    )
    body = b'{"url":"https://example.test/share","why":"Synthetic reason"}'
    reader = _Reader(body)
    spy = _CaptureSpy()
    handler = _spy_handler(reader, spy)

    valid = handler.handle(_request(body))
    invalid = [
        handler.handle(_request(body, authorization=value))
        for value in (
            "",
            "Bearer ",
            "Token synthetic-token",
            "Bearer wrong",
            "Bearer non\u00e4scii",
        )
    ]

    assert valid.status == 202
    assert {(response.status, response.body) for response in invalid} == {
        (401, b'{"code":"unauthorized"}')
    }
    assert observed == [(b"synthetic-token", b"synthetic-token"), (b"wrong", b"synthetic-token")]
    assert len(spy.calls) == 1
    with pytest.raises(ValueError):
        _spy_handler(_Reader(body), _CaptureSpy(), token=" ")


@pytest.mark.parametrize("length", [None, "", "-1", "bad", str(BODY_LIMIT_BYTES + 1)])
def test_share_refuses_missing_invalid_or_oversized_lengths_before_reading(
    length: str | None,
) -> None:
    body = b'{"url":"https://example.test/share","why":"Synthetic reason"}'
    reader = _Reader(body)
    spy = _CaptureSpy()
    headers = list(_request(body).headers)
    if length is None:
        headers = [header for header in headers if header[0] != "Content-Length"]
    else:
        headers = [(name, length if name == "Content-Length" else value) for name, value in headers]

    response = _spy_handler(reader, spy).handle(HttpRequest("POST", "/share", tuple(headers)))

    assert response == type(response)(413, b'{"code":"request_too_large"}')
    assert reader.calls == []
    assert spy.calls == []


def test_share_closes_duplicate_lengths_body_and_field_bounds() -> None:
    exact = (
        b'{"url":"https://example.test/share","why":"reason","text":"'
        + b"x"
        * (BODY_LIMIT_BYTES - len(b'{"url":"https://example.test/share","why":"reason","text":""}'))
        + b'"}'
    )
    reader = _Reader(exact)
    spy = _CaptureSpy()
    handler = _spy_handler(reader, spy)
    duplicate = HttpRequest(
        "POST", "/share", _request(exact).headers + (("Content-Length", str(len(exact))),)
    )
    too_long_url = b'{"url":"https://example.test/' + b"x" * 2_100 + b'","why":"reason"}'

    assert handler.handle(_request(exact)).status == 409
    assert handler.handle(_request(exact + b" ")).status == 413
    assert handler.handle(duplicate).status == 413
    assert (
        _spy_handler(_Reader(too_long_url), _CaptureSpy()).handle(_request(too_long_url)).status
        == 413
    )
    assert reader.calls == [(BODY_LIMIT_BYTES, REQUEST_TIMEOUT_SECONDS)]
    assert spy.calls == []


@pytest.mark.parametrize(
    ("http_request", "status", "body"),
    (
        (HttpRequest("POST", "/other", ()), 400, b'{"code":"invalid_request"}'),
        (
            _request(b"{}", authorization="Bearer synthetic-token"),
            400,
            b'{"code":"invalid_request"}',
        ),
        (
            HttpRequest(
                "POST",
                "/share",
                (
                    ("Authorization", "Bearer synthetic-token"),
                    ("Content-Length", "2"),
                    ("Content-Type", "text/plain"),
                ),
            ),
            415,
            b'{"code":"unsupported_media_type"}',
        ),
        (
            _request(b"{", authorization="Bearer synthetic-token"),
            400,
            b'{"code":"invalid_request"}',
        ),
        (
            _request(b"[]", authorization="Bearer synthetic-token"),
            400,
            b'{"code":"invalid_request"}',
        ),
    ),
)
def test_share_accepts_only_closed_json_content_and_path(
    http_request: HttpRequest, status: int, body: bytes
) -> None:
    response = _spy_handler(_Reader(http_request.method.encode()), _CaptureSpy()).handle(
        http_request
    )

    assert (response.status, response.body) == (status, body)


def test_share_timeout_errors_and_capture_failures_are_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "synthetic-secret-marker"
    body = (
        b'{"url":"https://example.test/share","why":"Synthetic reason","text":"'
        + marker.encode()
        + b'"}'
    )
    timeout = _spy_handler(_Reader(body, RequestReadTimeout()), _CaptureSpy()).handle(
        _request(body)
    )
    unavailable = _spy_handler(_Reader(body), _CaptureSpy(failure=RuntimeError(marker))).handle(
        _request(body)
    )
    with caplog.at_level(logging.DEBUG):
        exposed = b"".join((timeout.body, unavailable.body)) + b"\n".join(
            record.getMessage().encode() for record in caplog.records
        )

    assert (timeout.status, timeout.body) == (408, b'{"code":"request_timeout"}')
    assert (unavailable.status, unavailable.body) == (500, b'{"code":"capture_unavailable"}')
    assert marker.encode() not in exposed


def test_share_requires_why_preserves_provenance_and_closes_conflict() -> None:
    body = (
        b'{"url":"https://www.youtube.com/watch?v=synthetic",'
        b'"why":"Keep this source","text":"Synthetic text"}'
    )
    spy = _CaptureSpy()
    response = _spy_handler(_Reader(body), spy).handle(_request(body))
    conflict = _spy_handler(_Reader(body), _CaptureSpy(failure=ValueError("conflict"))).handle(
        _request(body)
    )

    assert response.status == 202
    assert spy.calls[0]["capture_why"] == "Keep this source"
    assert conflict == type(conflict)(409, b'{"code":"immutable_conflict"}')


def test_captures_accepts_four_families_with_created_replay_and_conflict(tmp_path: Path) -> None:
    application = open_local_engine(compile_single_user_local(tmp_path / "brain"))
    token = "synthetic-token"
    payloads = (
        {"family": "text", "text": "Synthetic text"},
        {"family": "reference_or_file", "kind": "reference", "url": "https://example.test/ref"},
        {"family": "event", "event_type": "synthetic.event", "attributes": {"key": "value"}},
        {"family": "measurement", "value": "7", "unit": "count", "dimensions": {"key": "value"}},
    )
    responses = []
    for index, payload in enumerate(payloads):
        body = json.dumps({"delivery_id": f"http.{index}", "payload": payload}).encode()
        responses.append(
            ShareHttpHandler(
                expected_bearer_token=token,
                capture=application.capture,
                clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
                body_reader=lambda _maximum, _timeout, body=body: body,
            ).handle(
                HttpRequest(
                    "POST",
                    "/captures",
                    (
                        ("Authorization", "Bearer " + token),
                        ("Content-Length", str(len(body))),
                        ("Content-Type", "application/json"),
                    ),
                )
            )
        )

    replay_body = json.dumps({"delivery_id": "http.0", "payload": payloads[0]}).encode()
    replay = ShareHttpHandler(
        expected_bearer_token=token,
        capture=application.capture,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        body_reader=lambda _maximum, _timeout: replay_body,
    ).handle(
        HttpRequest(
            "POST",
            "/captures",
            (
                ("Authorization", "Bearer " + token),
                ("Content-Length", str(len(replay_body))),
                ("Content-Type", "application/json"),
            ),
        )
    )

    assert [response.status for response in responses] == [201, 201, 201, 201]
    assert replay.status == 200
    assert json.loads(responses[0].body)["capture_id"] == json.loads(replay.body)["capture_id"]
