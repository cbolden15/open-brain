from __future__ import annotations

import json

import pytest
from open_brain_engine.core.ids import canonical_json_bytes

from open_brain.services.appliance_daemon import (
    MAXIMUM_CONTROL_ENVELOPE_BYTES,
    ApplianceControlProtocolError,
    ControlReceipt,
    ControlRequest,
)


def test_control_request_requires_canonical_bounded_known_envelope() -> None:
    request = ControlRequest(
        delivery_id="delivery.appliance.control.request",
        text="Synthetic control request",
    )

    assert ControlRequest.from_bytes(request.to_bytes()) == request

    with pytest.raises(ApplianceControlProtocolError, match="canonical"):
        ControlRequest.from_bytes(
            json.dumps(
                {
                    "text": request.text,
                    "action": request.action,
                    "delivery_id": request.delivery_id,
                    "schema_version": request.schema_version,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )

    with pytest.raises(ApplianceControlProtocolError, match="request envelope"):
        ControlRequest.from_bytes(
            canonical_json_bytes({**request.to_dict(), "unknown": True})
        )

    with pytest.raises(ApplianceControlProtocolError, match="unsupported action"):
        ControlRequest.from_bytes(
            canonical_json_bytes({**request.to_dict(), "action": "capture.reject.text"})
        )

    oversized = canonical_json_bytes(
        {
            "action": request.action,
            "delivery_id": request.delivery_id,
            "schema_version": 1,
            "text": "x" * MAXIMUM_CONTROL_ENVELOPE_BYTES,
        }
    )
    with pytest.raises(ApplianceControlProtocolError, match="too large"):
        ControlRequest.from_bytes(oversized)


def test_control_receipt_requires_canonical_metadata_only_bounded_envelope() -> None:
    receipt = ControlReceipt(
        delivery_id="delivery.appliance.control.receipt",
        capture_id="capture_123e4567-e89b-42d3-a456-426614174100",
        state="inbox",
    )

    assert ControlReceipt.from_bytes(receipt.to_bytes()) == receipt
    assert "Synthetic control request" not in receipt.to_bytes().decode("utf-8")

    with pytest.raises(ApplianceControlProtocolError, match="canonical"):
        ControlReceipt.from_bytes(
            json.dumps(
                {
                    "state": receipt.state,
                    "capture_id": receipt.capture_id,
                    "delivery_id": receipt.delivery_id,
                    "status": receipt.status,
                    "action": receipt.action,
                    "schema_version": receipt.schema_version,
                },
                separators=(",", ":"),
            ).encode("utf-8")
        )

    with pytest.raises(ApplianceControlProtocolError, match="receipt envelope"):
        ControlReceipt.from_bytes(
            canonical_json_bytes({**receipt.to_dict(), "text": "forbidden"})
        )

    oversized = canonical_json_bytes(
        {
            "action": receipt.action,
            "capture_id": receipt.capture_id,
            "delivery_id": "x" * MAXIMUM_CONTROL_ENVELOPE_BYTES,
            "schema_version": 1,
            "state": receipt.state,
            "status": receipt.status,
        }
    )
    with pytest.raises(ApplianceControlProtocolError, match="too large"):
        ControlReceipt.from_bytes(oversized)
