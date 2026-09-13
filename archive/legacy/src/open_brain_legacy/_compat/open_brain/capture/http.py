# Private legacy compatibility snapshot; excluded from every shipping artifact.
from __future__ import annotations

import base64
import json
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from typing import Protocol, cast
from urllib.parse import urlsplit

from open_brain_engine.engine import (
    CaptureAction,
    CapturePipeline,
    CaptureReceipt,
    CaptureWhyOrigin,
    ContentOrigin,
    EventPayload,
    FilePayload,
    MeasurementPayload,
    Payload,
    Provenance,
    PublicJobCaptureSink,
    ReferencePayload,
    ShareRequest,
    ShareResponse,
    ShareStatus,
    TextPayload,
    classify_privacy,
)

from open_brain_legacy._compat.open_brain.capture.auth import BearerAuthenticator

BODY_LIMIT_BYTES = 100_000
REQUEST_TIMEOUT_SECONDS = 5.0
_YOUTUBE_HOSTS = {"youtube.com", "youtu.be"}
_SOCIAL_HOSTS = {
    "bsky.app",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "reddit.com",
    "tiktok.com",
    "twitter.com",
    "x.com",
}


class RequestReadTimeout(Exception):
    """The injected request reader exceeded the intake read deadline."""


class _FieldTooLarge(Exception):
    pass


class BodyReader(Protocol):
    def __call__(self, maximum_bytes: int, timeout_seconds: float) -> bytes: ...


class CaptureAcceptor(Protocol):
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
    ) -> CaptureReceipt: ...


class ShareQueue(Protocol):
    """Legacy protocol retained only for non-P2 service composition typing."""

    def enqueue(self, *args: object, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "application/json"),)


class ShareHttpHandler:
    """A framework-neutral, bounded HTTP boundary for POST /share."""

    def __init__(
        self,
        *,
        expected_bearer_token: str,
        capture: CaptureAcceptor | PublicJobCaptureSink,
        clock: Callable[[], datetime],
        body_reader: BodyReader,
    ) -> None:
        if not callable(clock) or not callable(body_reader):
            raise ValueError("invalid share handler dependencies")
        self._authenticator = BearerAuthenticator(expected_bearer_token)
        if not (
            callable(getattr(capture, "accept", None)) or isinstance(capture, PublicJobCaptureSink)
        ):
            raise ValueError("invalid share handler dependencies")
        self.capture = capture
        self._clock = clock
        self._body_reader = body_reader

    def handle(self, request: HttpRequest) -> HttpResponse:
        if not isinstance(request, HttpRequest) or (
            request.method != "POST" or request.path not in {"/share", "/captures"}
        ):
            return _error(400, "invalid_request")

        headers = _headers(request.headers)
        if headers is None:
            return _error(400, "invalid_request")
        content_length = _content_length(headers)
        if content_length is None or content_length > BODY_LIMIT_BYTES:
            return _error(413, "request_too_large")
        if not _is_json_content_type(headers):
            return _error(415, "unsupported_media_type")
        if not self._authenticator.authenticate(headers.get("authorization", ())):
            return _error(401, "unauthorized")

        try:
            body = self._body_reader(BODY_LIMIT_BYTES, REQUEST_TIMEOUT_SECONDS)
        except RequestReadTimeout:
            return _error(408, "request_timeout")
        except Exception:
            return _error(400, "invalid_request")
        if not isinstance(body, bytes):
            return _error(400, "invalid_request")
        if len(body) > BODY_LIMIT_BYTES:
            return _error(413, "request_too_large")
        if len(body) != content_length:
            return _error(400, "invalid_request")

        if request.path == "/captures":
            if not callable(getattr(self.capture, "accept", None)):
                return _error(400, "invalid_request")
            try:
                delivery_id, payload, action, space_id, intent, capture_why, title = (
                    _parse_capture_request(body)
                )
            except _FieldTooLarge:
                return _error(413, "request_too_large")
            except ValueError:
                return _error(400, "invalid_request")
            try:
                receipt = cast(CaptureAcceptor, self.capture).accept(
                    payload,
                    delivery_id=delivery_id,
                    action=action,
                    space_id=space_id,
                    intent=intent,
                    capture_why=capture_why,
                    title=title,
                )
            except ValueError:
                return _error(409, "immutable_conflict")
            except Exception:
                return _error(500, "capture_unavailable")
            return HttpResponse(
                status=200 if receipt.duplicate else 201,
                body=_json_bytes(
                    {
                        "capture_id": receipt.capture_id,
                        "duplicate": receipt.duplicate,
                        "payload_family": receipt.payload_family,
                        "status": "accepted",
                    }
                ),
            )

        try:
            share_request = _parse_share_request(body)
        except _FieldTooLarge:
            return _error(413, "request_too_large")
        if share_request is None:
            return _error(400, "invalid_request")
        try:
            response = enqueue_share(request=share_request, capture=self.capture)
        except ValueError:
            return _error(409, "immutable_conflict")
        except Exception:
            return _error(500, "capture_unavailable")
        return HttpResponse(status=202, body=_json_bytes(response.to_dict()))


def enqueue_share(
    *,
    request: ShareRequest,
    capture: CaptureAcceptor | PublicJobCaptureSink,
) -> ShareResponse:
    """Submit one validated share as an owner reference through engine durability."""
    if not isinstance(request, ShareRequest):
        raise ValueError("invalid share submission")
    delivery_id = "share." + sha256(request.canonical_bytes()).hexdigest()
    if isinstance(capture, PublicJobCaptureSink):
        receipt = capture.submit(
            ReferencePayload(request.url, request.text or None),
            delivery_id=delivery_id,
            source_origin=ContentOrigin.THIRD_PARTY,
            source_reference=request.url,
            provenance=Provenance.create(
                source_ref=request.url,
                content_origin=ContentOrigin.THIRD_PARTY,
                owner_context=CaptureWhyOrigin.AUTOMATION_ABSENT,
            ),
            privacy=classify_privacy(
                request.privacy_tier,
                policy_version="public-job-share-v1",
            ),
        )
    elif callable(getattr(capture, "accept", None)):
        receipt = capture.accept(
            ReferencePayload(request.url, request.text or None),
            delivery_id=delivery_id,
            action=CaptureAction.QUICK,
            capture_why=request.why,
        )
    else:
        raise ValueError("invalid share submission")
    if not hasattr(receipt, "capture_id") or not isinstance(receipt.capture_id, str):
        raise ValueError("invalid share queue result")
    return ShareResponse.create(
        capture_id=receipt.capture_id,
        pipeline=_classify_pipeline(request.url),
        duplicate=receipt.duplicate,
        status=ShareStatus.DUPLICATE if receipt.duplicate else ShareStatus.QUEUED,
    )


def _headers(value: object) -> dict[str, tuple[str, ...]] | None:
    if not isinstance(value, tuple):
        return None
    normalized: dict[str, list[str]] = {}
    for header in value:
        if (
            not isinstance(header, tuple)
            or len(header) != 2
            or not isinstance(header[0], str)
            or not isinstance(header[1], str)
            or not header[0].isascii()
        ):
            return None
        normalized.setdefault(header[0].lower(), []).append(header[1])
    return {key: tuple(values) for key, values in normalized.items()}


def _content_length(headers: Mapping[str, tuple[str, ...]]) -> int | None:
    values = headers.get("content-length", ())
    if len(values) != 1 or not values[0] or not values[0].isascii() or not values[0].isdigit():
        return None
    return int(values[0])


def _is_json_content_type(headers: Mapping[str, tuple[str, ...]]) -> bool:
    values = headers.get("content-type", ())
    return len(values) == 1 and values[0].strip().lower() == "application/json"


def _parse_share_request(body: bytes) -> ShareRequest | None:
    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        required_fields = {"url", "why"}
        optional_fields = {"text", "privacy"}
        if (
            not isinstance(decoded, dict)
            or not required_fields.issubset(decoded)
            or not set(decoded).issubset(required_fields | optional_fields)
        ):
            return None
        url = decoded["url"]
        why = decoded["why"]
        text = decoded.get("text", "")
        privacy = decoded.get("privacy")
        if (
            not isinstance(url, str)
            or not isinstance(why, str)
            or not isinstance(text, str)
            or (privacy is not None and not isinstance(privacy, str))
        ):
            return None
        if len(unicodedata.normalize("NFC", url).encode("utf-8")) > 2_048:
            raise _FieldTooLarge
        return ShareRequest.create(
            url=url,
            why=why,
            text=text,
            privacy_tier=privacy,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None


def _parse_capture_request(
    body: bytes,
) -> tuple[
    str,
    TextPayload | ReferencePayload | FilePayload | EventPayload | MeasurementPayload,
    CaptureAction,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    try:
        value = json.loads(body.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid capture request") from error
    if not isinstance(value, dict):
        raise ValueError("invalid capture request")
    required = {"delivery_id", "payload"}
    optional = {"action", "capture_why", "intent", "space_id", "title"}
    if not required.issubset(value) or not set(value).issubset(required | optional):
        raise ValueError("invalid capture request")
    delivery_id = _string(value["delivery_id"])
    payload = _capture_payload(value["payload"])
    action = CaptureAction(value.get("action", "quick"))
    return (
        delivery_id,
        payload,
        action,
        _optional_string(value, "space_id"),
        _optional_string(value, "intent"),
        _optional_string(value, "capture_why"),
        _optional_string(value, "title"),
    )


def _capture_payload(
    value: object,
) -> TextPayload | ReferencePayload | FilePayload | EventPayload | MeasurementPayload:
    if not isinstance(value, dict):
        raise ValueError("invalid capture payload")
    family = _string(value.get("family"))
    if family == "text" and set(value) == {"family", "text"}:
        return TextPayload(_string(value.get("text")))
    if family == "reference_or_file":
        kind = _string(value.get("kind"))
        if (
            kind == "reference"
            and set(value).issubset({"family", "kind", "supplied_text", "url"})
            and {"family", "kind", "url"}.issubset(value)
        ):
            return ReferencePayload(
                _string(value.get("url")), _optional_string(value, "supplied_text")
            )
        if kind == "file" and set(value) == {
            "data_base64",
            "family",
            "file_name",
            "kind",
            "media_type",
        }:
            return FilePayload(
                _string(value.get("file_name")),
                _string(value.get("media_type")),
                base64.b64decode(_string(value.get("data_base64")), validate=True),
            )
    if (
        family == "event"
        and set(value).issubset({"attributes", "event_type", "family", "occurrence_at"})
        and {"attributes", "event_type", "family"}.issubset(value)
    ):
        return EventPayload(
            _string(value.get("event_type")),
            _optional_string(value, "occurrence_at"),
            _string_mapping(value.get("attributes")),
        )
    if (
        family == "measurement"
        and set(value).issubset({"dimensions", "family", "occurrence_at", "unit", "value"})
        and {"dimensions", "family", "unit", "value"}.issubset(value)
    ):
        return MeasurementPayload(
            _string(value.get("value")),
            _string(value.get("unit")),
            _optional_string(value, "occurrence_at"),
            _string_mapping(value.get("dimensions")),
        )
    raise ValueError("invalid capture payload")


def _string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid string")
    return value


def _optional_string(value: Mapping[str, object], key: str) -> str | None:
    result = value.get(key)
    if result is None:
        return None
    return _string(result)


def _string_mapping(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError("invalid string mapping")
    return dict(value)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _classify_pipeline(url: str) -> CapturePipeline:
    host = urlsplit(url).hostname or ""
    if _matches_host(host, _YOUTUBE_HOSTS):
        return CapturePipeline.YOUTUBE
    if _matches_host(host, _SOCIAL_HOSTS):
        return CapturePipeline.SOCIAL
    return CapturePipeline.WEB


def _matches_host(host: str, allowed_hosts: set[str]) -> bool:
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _error(status: int, code: str) -> HttpResponse:
    return HttpResponse(status=status, body=_json_bytes({"code": code}))


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
