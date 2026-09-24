from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import (
    CaptureSubmission,
    JournalEnvelope,
    ReceiptProtectionError,
    ReceiptProtectionRequest,
    TextPayload,
)
from open_brain_engine.engine.t03_contracts import EffectiveAuthority

from open_brain.profile import compile_single_user_local
from open_brain.services.receipt_protection import (
    ReceiptProtectionConfigurationError,
    load_receipt_protection_configuration,
)


def _request(tmp_path: Path) -> ReceiptProtectionRequest:
    profile = compile_single_user_local(tmp_path / "brain")
    brain_id = derive_brain_id(profile.tenant_id)
    authority = EffectiveAuthority(
        principal_id="receipt-protection-adapter-test",
        session_id="receipt-protection-adapter-session",
        capabilities=frozenset({"capture-submit"}),
        space_ids=None,
        allowed_capture_tiers=frozenset({PrivacyTier.WORK}),
        brain_id=brain_id,
        issuer_epoch=1,
    )
    submission = CaptureSubmission.for_destination_bound(
        profile=profile,
        authority=authority,
        payload=TextPayload("adapter replay body"),
        delivery_id="delivery.receipt-protection.adapter",
        requested_tier=PrivacyTier.WORK,
    )
    envelope = JournalEnvelope(submission, submission.privacy)
    return ReceiptProtectionRequest(
        brain_id=brain_id,
        issuer_epoch=1,
        delivery_id=submission.delivery_id,
        request_sha256=submission.request_sha256(),
        requested_tier=submission.requested_tier,
        final_admitted_tier=envelope.admitted_privacy.tier,
        replay_bytes=envelope.to_bytes(),
    )


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "protector.py"
    path.write_text(body, encoding="utf-8")
    return path


def _configuration(tmp_path: Path, script: Path, *, timeout: float = 1.0) -> Path:
    path = tmp_path / "receipt-protection.json"
    path.write_text(
        json.dumps(
            {
                "contract_version": "receipt-protection-command.v1",
                "argv": [sys.executable, str(script)],
                "timeout_seconds": timeout,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def test_owner_only_configuration_runs_a_bounded_foreground_protector(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "import json,sys\n"
        "from open_brain_engine.engine import ReceiptProtectionRequest,ProtectionAcknowledgement\n"
        "request=ReceiptProtectionRequest.from_dict(json.load(sys.stdin))\n"
        "ack=ProtectionAcknowledgement.for_request(request,protection_reference='test:durable',protected_at='2026-09-24T12:00:00Z')\n"
        "json.dump(ack.to_dict(),sys.stdout,sort_keys=True)\n",
    )
    configuration = load_receipt_protection_configuration(
        _configuration(tmp_path, script)
    )
    request = _request(tmp_path)

    acknowledgement = configuration.port.protect(
        request,
        timeout_seconds=configuration.timeout_seconds,
    )

    assert acknowledgement.commitment_sha256 == request.commitment_sha256
    assert acknowledgement.protection_reference == "test:durable"


def test_configuration_rejects_group_readable_files(tmp_path: Path) -> None:
    script = _script(tmp_path, "raise SystemExit(0)\n")
    path = _configuration(tmp_path, script)
    path.chmod(0o640)

    with pytest.raises(ReceiptProtectionConfigurationError, match="owner-only"):
        load_receipt_protection_configuration(path)


def test_protector_timeout_is_a_safe_retryable_code(tmp_path: Path) -> None:
    script = _script(tmp_path, "import time\ntime.sleep(1)\n")
    configuration = load_receipt_protection_configuration(
        _configuration(tmp_path, script, timeout=0.05)
    )

    with pytest.raises(ReceiptProtectionError) as raised:
        configuration.port.protect(
            _request(tmp_path),
            timeout_seconds=configuration.timeout_seconds,
        )

    assert raised.value.code == "protection_timeout"
    assert "adapter replay body" not in str(raised.value)


def test_protector_rejects_duplicate_acknowledgement_keys(tmp_path: Path) -> None:
    script = _script(tmp_path, "print('{\"status\":\"protected\",\"status\":\"protected\"}')\n")
    configuration = load_receipt_protection_configuration(
        _configuration(tmp_path, script)
    )

    with pytest.raises(ReceiptProtectionError) as raised:
        configuration.port.protect(
            _request(tmp_path),
            timeout_seconds=configuration.timeout_seconds,
        )

    assert raised.value.code == "protection_invalid_acknowledgement"
