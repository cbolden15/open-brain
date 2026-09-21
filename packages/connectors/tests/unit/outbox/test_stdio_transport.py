from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest
from open_brain_engine.core.access_contracts import derive_brain_id
from open_brain_engine.core.models import PrivacyTier

from open_brain_connectors.outbox.contracts import (
    DeliveryEnvelope,
    TerminalReceipt,
    TerminalReceiptStatus,
)
from open_brain_connectors.outbox.drain import DeliveryFailure
from open_brain_connectors.outbox.stdio_transport import (
    OutboxTransportError,
    StdioProcessTransport,
)

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
PRINCIPAL_ID = "synthetic-stdio-principal"
EPOCH = 7
PAYLOAD_TEXT = "Stdio transport body"

# The child interpreter injects a couple of its own variables on some
# platforms (macOS adds __CF_USER_TEXT_ENCODING and LC_CTYPE), so isolation
# is asserted on variables that would truly be inherited from the parent.
_EMPTY_ENVIRONMENT_ASSERT = (
    "import os\nassert 'PATH' not in os.environ and 'HOME' not in os.environ, os.environ\n"
)


def _envelope(*, delivery_id: str = "delivery.stdio-001") -> DeliveryEnvelope:
    return DeliveryEnvelope.create(
        destination_brain_id=BRAIN_ID,
        expected_issuer_epoch=EPOCH,
        tenant_id=TENANT_ID,
        principal_id=PRINCIPAL_ID,
        delivery_id=delivery_id,
        requested_tier=PrivacyTier.WORK,
        policy_ref="policy.synthetic-v1",
        payload={"family": "text", "text": PAYLOAD_TEXT},
        enqueued_at="2026-09-21T12:00:00Z",
        retry_age_limit_seconds=86400,
        retry_attempt_limit=8,
    )


def _write_script(tmp_path: Path, name: str, source: str) -> tuple[str, ...]:
    script = tmp_path / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    return (sys.executable, "-I", str(script))


def _receipt_script(*, duplicate: bool, environment_assert: str = _EMPTY_ENVIRONMENT_ASSERT) -> str:
    """A capture-submit stand-in that derives its own digest from the request."""
    return (
        "import json\n"
        "import sys\n"
        "\n"
        "from open_brain_engine.engine.contracts import (\n"
        "    TextPayload,\n"
        "    destination_bound_request_sha256,\n"
        ")\n"
        "\n"
        "document = json.loads(sys.stdin.buffer.read())\n"
        "assert document['payload']['family'] == 'text'\n"
        + environment_assert
        + "digest = destination_bound_request_sha256(\n"
        "    destination_brain_id=document['destination_brain_id'],\n"
        "    issuer_epoch=document['issuer_epoch'],\n"
        "    tenant_id=document['tenant_id'],\n"
        "    principal_id=document['principal_id'],\n"
        "    payload=TextPayload(document['payload']['text']),\n"
        "    requested_tier=document['requested_tier'],\n"
        ")\n"
        "result = {\n"
        "    'capture_id': 'cap_stdio_synthetic',\n"
        "    'delivery_id': document['delivery_id'],\n"
        "    'destination_brain_id': document['destination_brain_id'],\n"
        f"    'duplicate': {'True' if duplicate else 'False'},\n"
        "    'final_admitted_tier': document['requested_tier'],\n"
        "    'issuer_epoch': document['issuer_epoch'],\n"
        "    'payload_family': 'text',\n"
        "    'request_sha256': digest,\n"
        "    'requested_tier': document['requested_tier'],\n"
        "    'state': 'processed',\n"
        "    'status': 'captured',\n"
        "}\n"
        "sys.stdout.write(json.dumps(result, sort_keys=True) + '\\n')\n"
    )


def _failure_script(*, code: str, exit_code: int, retryable: bool) -> str:
    return (
        "import json\n"
        "import sys\n"
        "\n"
        "document = json.loads(sys.stdin.buffer.read())\n"
        "assert document['delivery_id']\n"
        "failure = {\n"
        "    'error': {'code': '" + code + "', 'message': '" + code + "'},\n"
        "    'retryable': " + ("True" if retryable else "False") + ",\n"
        "    'status': 'failed',\n"
        "}\n"
        "sys.stdout.write(json.dumps(failure, sort_keys=True) + '\\n')\n"
        "sys.exit(" + str(exit_code) + ")\n"
    )


def _no_new_threads(call: object) -> object:
    before = threading.enumerate()
    try:
        return call
    finally:
        assert threading.enumerate() == before


def test_exit_zero_receipt_maps_to_a_bound_terminal_receipt(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "receipt", _receipt_script(duplicate=False))
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, TerminalReceipt)
    assert outcome.status is TerminalReceiptStatus.ACCEPTED
    assert outcome.brain_id == BRAIN_ID
    assert outcome.issuer_epoch == EPOCH
    assert outcome.delivery_id == "delivery.stdio-001"
    assert outcome.request_digest == _envelope().request_digest
    assert outcome.final_admitted_tier is PrivacyTier.WORK
    assert outcome.protection_acknowledgement is None


def test_exit_zero_duplicate_receipt_maps_to_duplicate(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "duplicate", _receipt_script(duplicate=True))
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, TerminalReceipt)
    assert outcome.status is TerminalReceiptStatus.DUPLICATE


def test_exit_seventy_five_maps_to_a_retryable_reported_code(tmp_path: Path) -> None:
    argv = _write_script(
        tmp_path,
        "retryable",
        _failure_script(code="rate_limited", exit_code=75, retryable=True),
    )
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "rate_limited"
    assert outcome.retryable is True


def test_exit_sixty_five_maps_to_a_terminal_reported_code(tmp_path: Path) -> None:
    argv = _write_script(
        tmp_path,
        "refused",
        _failure_script(code="tier_not_permitted", exit_code=65, retryable=False),
    )
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "tier_not_permitted"
    assert outcome.retryable is False


def test_exit_seventy_eight_maps_to_terminal_policy_mismatch(tmp_path: Path) -> None:
    argv = _write_script(
        tmp_path, "policy", _failure_script(code="stale_policy", exit_code=78, retryable=False)
    )
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "policy_mismatch"
    assert outcome.retryable is False


def test_exit_two_maps_to_terminal_transport_misuse(tmp_path: Path) -> None:
    argv = _write_script(
        tmp_path,
        "usage",
        "import sys\n"
        "sys.stderr.write('usage: capture-submit TEXT --policy PATH\\n')\n"
        "sys.exit(2)\n",
    )
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "transport_misuse"
    assert outcome.retryable is False


def test_timeout_kills_the_group_and_reports_retryable_transport_error(
    tmp_path: Path,
) -> None:
    argv = _write_script(tmp_path, "sleeper", "import time\ntime.sleep(60)\n")
    transport = StdioProcessTransport(argv, timeout_seconds=0.5)
    started = time.monotonic()

    outcome = _no_new_threads(transport(_envelope()))

    assert time.monotonic() - started < 15
    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "transport_error"
    assert outcome.retryable is True


def test_malformed_output_is_a_retryable_transport_error(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "malformed", "import sys\nsys.stdout.write('not json\\n')\n")
    transport = StdioProcessTransport(argv)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "transport_error"
    assert outcome.retryable is True


def test_oversize_stdout_is_a_retryable_transport_error(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "chatter", "import sys\nsys.stdout.write('x' * 8192 + '\\n')\n")
    transport = StdioProcessTransport(argv, max_stdout_bytes=4096)

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "transport_error"
    assert outcome.retryable is True


def test_missing_command_is_a_retryable_transport_error(tmp_path: Path) -> None:
    transport = StdioProcessTransport((str(tmp_path / "does-not-exist"),))

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, DeliveryFailure)
    assert outcome.code == "transport_error"
    assert outcome.retryable is True


def test_environment_allowlist_is_the_only_inherited_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OUTBOX_STDIO_TEST", "allowlisted")
    monkeypatch.setenv("OUTBOX_STDIO_SECRET", "never-inherited")
    environment_assert = (
        "import os\n"
        "assert os.environ.get('OUTBOX_STDIO_TEST') == 'allowlisted', os.environ\n"
        "assert 'OUTBOX_STDIO_SECRET' not in os.environ, os.environ\n"
        "assert 'PATH' not in os.environ and 'HOME' not in os.environ, os.environ\n"
    )
    argv = _write_script(
        tmp_path,
        "allowlisted",
        _receipt_script(duplicate=False, environment_assert=environment_assert),
    )
    transport = StdioProcessTransport(argv, environment_allowlist=("OUTBOX_STDIO_TEST",))

    outcome = _no_new_threads(transport(_envelope()))

    assert isinstance(outcome, TerminalReceipt)


def test_invalid_constructor_inputs_are_refused(tmp_path: Path) -> None:
    argv = _write_script(tmp_path, "receipt", _receipt_script(duplicate=False))
    with pytest.raises(OutboxTransportError):
        StdioProcessTransport(())
    with pytest.raises(OutboxTransportError):
        StdioProcessTransport(argv, timeout_seconds=0)
    with pytest.raises(OutboxTransportError):
        StdioProcessTransport(argv, max_stdout_bytes=0)
    with pytest.raises(OutboxTransportError):
        StdioProcessTransport(argv, environment_allowlist=("OUTBOX_STDIO_TEST", "BAD NAME"))
