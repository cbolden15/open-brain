from __future__ import annotations

import ipaddress
import json
import re
import unicodedata
from hashlib import sha256
from typing import NewType, cast
from urllib.parse import SplitResult, urlsplit, urlunsplit

CaptureId = NewType("CaptureId", str)
AssetId = NewType("AssetId", str)
ReviewId = NewType("ReviewId", str)

_HEX = re.compile(r"^[0-9a-f]{64}$")
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


class IdentifierError(ValueError):
    """A value does not satisfy a deterministic identifier contract."""


class UrlError(ValueError):
    """A source URL is not a safe absolute HTTP(S) URL."""


def canonical_json_bytes(value: object) -> bytes:
    """Encode legacy normalized JSON.

    This function predates Portable Brain v1 and intentionally preserves its
    float and object-key coercion behavior for existing callers.
    """
    return json.dumps(
        _normalize_legacy_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def portable_canonical_json_bytes(value: object) -> bytes:
    """Encode the strict canonical JSON representation required by Portable v1."""
    return json.dumps(
        _normalize_portable_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def capture_id_for(identity: dict[str, object]) -> CaptureId:
    return CaptureId("cap_" + sha256(canonical_json_bytes(identity)).hexdigest())


def asset_id_for(data: bytes) -> AssetId:
    return AssetId("asset_" + sha256(data).hexdigest())


def review_id_for(capture_id: CaptureId, intent: str) -> ReviewId:
    return ReviewId(
        "review_"
        + sha256(
            canonical_json_bytes(
                {"identity_version": 1, "capture_id": str(capture_id), "intent": intent}
            )
        ).hexdigest()
    )


def approved_intent_record_id_for(review_id: ReviewId, intent: str) -> str:
    return (
        "intent_"
        + sha256(
            canonical_json_bytes(
                {"identity_version": 1, "review_id": str(review_id), "intent": intent}
            )
        ).hexdigest()
    )


def validate_identifier(value: str, *, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or not value.startswith(prefix)
        or not _HEX.fullmatch(value[len(prefix) :])
    ):
        raise IdentifierError("invalid identifier")
    return value


def canonicalize_source_url(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(character) < 32 for character in value):
        raise UrlError("invalid URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as error:
        raise UrlError("invalid URL") from error
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise UrlError("invalid URL")
    host = _canonical_host(parts.hostname)
    authority_host = f"[{host}]" if ":" in host else host
    scheme = parts.scheme.lower()
    netloc = (
        authority_host
        if port is None or (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        else f"{authority_host}:{port}"
    )
    path = _uppercase_percent_escapes(parts.path) or "/"
    query = _uppercase_percent_escapes(parts.query)
    normalized = SplitResult(scheme, netloc, path, query, "")
    return urlunsplit(normalized)


def _canonical_host(host: str) -> str:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise UrlError("invalid URL") from error
    return address.compressed.lower()


def _uppercase_percent_escapes(value: str) -> str:
    return _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), value)


def _normalize_legacy_json(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, list | tuple):
        return [_normalize_legacy_json(item) for item in value]
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _normalize_legacy_json(item)
            for key, item in value.items()
        }
    return value


def _nfc(text: str) -> str:
    # ASCII text is already NFC; skipping the call keeps the result identical.
    return text if text.isascii() else unicodedata.normalize("NFC", text)


def _normalize_portable_json(value: object) -> object:
    """Normalize Portable JSON: NFC strings, list containers, string keys, no floats.

    Historical verification canonicalizes millions of values per Brain open, so exact
    str, int, bool and None children are handled inline instead of through one call
    each. Every other value follows the same checks in the same order as before.
    """
    kind = type(value)
    if kind is str:
        return _nfc(cast(str, value))
    if kind is int or kind is bool or value is None:
        return value
    if kind is list or kind is tuple:
        sequence = True
    elif kind is dict:
        sequence = False
    else:
        if isinstance(value, float):
            raise ValueError("Portable canonical JSON rejects floats")
        if isinstance(value, str):
            return unicodedata.normalize("NFC", value)
        if isinstance(value, list | tuple):
            sequence = True
        elif isinstance(value, dict):
            sequence = False
        else:
            return value
    if sequence:
        items: list[object] = []
        for item in cast(list[object], value):
            item_kind = type(item)
            if item_kind is str:
                items.append(_nfc(cast(str, item)))
            elif item_kind is int or item_kind is bool or item is None:
                items.append(item)
            else:
                items.append(_normalize_portable_json(item))
        return items
    normalized: dict[str, object] = {}
    for key, item in cast(dict[object, object], value).items():
        if not isinstance(key, str):
            raise ValueError("canonical JSON requires string object keys")
        normalized_key = _nfc(key) if type(key) is str else unicodedata.normalize("NFC", key)
        if normalized_key in normalized:
            raise ValueError("canonical JSON rejects normalized key collision")
        item_kind = type(item)
        if item_kind is str:
            normalized[normalized_key] = _nfc(cast(str, item))
        elif item_kind is int or item_kind is bool or item is None:
            normalized[normalized_key] = item
        else:
            normalized[normalized_key] = _normalize_portable_json(item)
    return normalized
