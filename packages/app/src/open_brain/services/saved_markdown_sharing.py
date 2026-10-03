"""Owner-local CLI composition for exact saved-Markdown sharing requests."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from open_brain_engine.engine import EngineTaskSet
from open_brain_engine.engine.sharing_contracts import (
    SharingDecisionRequest,
    SharingError,
    SharingInspectRequest,
    SharingPreviewRequest,
    SharingRevokeRequest,
    parse_sharing_request,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

_REQUEST_LIMIT = 65_536


def read_sharing_request(path: str, action: str) -> object:
    kinds = {
        "preview": SharingPreviewRequest,
        "approve": SharingDecisionRequest,
        "reject": SharingDecisionRequest,
        "revoke": SharingRevokeRequest,
    }
    kind = kinds.get(action)
    if kind is None or not path:
        raise SharingError("invalid_arguments")
    try:
        if path == "-":
            raw = sys.stdin.buffer.read(_REQUEST_LIMIT + 1)
        else:
            with Path(path).open("rb") as stream:
                raw = stream.read(_REQUEST_LIMIT + 1)
        request: object = parse_sharing_request(raw, kind)
    except OSError, UnicodeError, ValueError:
        raise SharingError("invalid_arguments") from None
    if isinstance(request, SharingDecisionRequest) and request.decision != action:
        raise SharingError("invalid_arguments")
    return request


def invoke_sharing(
    tasks: EngineTaskSet,
    *,
    action: str,
    request: object,
    authority: EffectiveAuthority,
) -> dict[str, Any]:
    sharing = tasks.sharing
    if sharing is None:
        raise SharingError("operation_pending")
    if action == "preview" and isinstance(request, SharingPreviewRequest):
        return asdict(sharing.preview(request, authority=authority))
    if action == "inspect" and isinstance(request, SharingInspectRequest):
        return asdict(sharing.inspect(request, authority=authority))
    if action in {"approve", "reject"} and isinstance(request, SharingDecisionRequest):
        if request.decision != action:
            raise SharingError("invalid_arguments")
        return asdict(sharing.decide(request, authority=authority))
    if action == "revoke" and isinstance(request, SharingRevokeRequest):
        return asdict(sharing.revoke(request, authority=authority))
    raise SharingError("invalid_arguments")
