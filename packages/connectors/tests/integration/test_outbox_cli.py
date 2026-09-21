from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from open_brain_engine.core.access_contracts import derive_brain_id

from open_brain_connectors.outbox.store import DEFAULT_MAX_ITEM_BYTES

TENANT_ID = "tenant_123e4567-e89b-42d3-a456-426614174000"
BRAIN_ID = derive_brain_id(TENANT_ID)
OTHER_BRAIN_ID = derive_brain_id("tenant_00000000-0000-4000-8000-000000000000")
PRINCIPAL_ID = "synthetic-outbox-cli-principal"
PAYLOAD_TEXT = "CLI-PRIVATE-BODY-3d1f-canary"

# The receipt-script fixture copies the O4 stdio synthetic stand-in
# (tests/unit/outbox/test_stdio_transport.py): the child derives the
# destination-bound digest from the request document itself.
_RECEIPT_SCRIPT = (
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
    "digest = destination_bound_request_sha256(\n"
    "    destination_brain_id=document['destination_brain_id'],\n"
    "    issuer_epoch=document['issuer_epoch'],\n"
    "    tenant_id=document['tenant_id'],\n"
    "    principal_id=document['principal_id'],\n"
    "    payload=TextPayload(document['payload']['text']),\n"
    "    requested_tier=document['requested_tier'],\n"
    ")\n"
    "result = {\n"
    "    'capture_id': 'cap_cli_synthetic',\n"
    "    'delivery_id': document['delivery_id'],\n"
    "    'destination_brain_id': document['destination_brain_id'],\n"
    "    'duplicate': False,\n"
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

# A terminal admission refusal: exit 65 makes the drain quarantine the item.
_REFUSAL_SCRIPT = (
    "import json\n"
    "import sys\n"
    "\n"
    "document = json.loads(sys.stdin.buffer.read())\n"
    "assert document['delivery_id']\n"
    "failure = {\n"
    "    'error': {'code': 'cli_terminal_refusal', 'message': 'synthetic refusal'},\n"
    "    'retryable': False,\n"
    "    'status': 'failed',\n"
    "}\n"
    "sys.stdout.write(json.dumps(failure, sort_keys=True) + '\\n')\n"
    "sys.exit(65)\n"
)


def _script() -> str:
    script = Path(sys.executable).parent / "open-brain-outbox"
    assert script.exists(), (
        f"console script missing: {script} (run `uv sync --frozen --group dev` to install it)"
    )
    return str(script)


def _run(outbox: Path, *args: str, request: str | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [_script(), *args, "--outbox-dir", str(outbox)],
        input=request,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert PAYLOAD_TEXT not in completed.stdout, completed.stdout
    assert PAYLOAD_TEXT not in completed.stderr, completed.stderr
    return completed


def _write_script(tmp_path: Path, name: str, source: str) -> tuple[str, ...]:
    script = tmp_path / f"{name}.py"
    script.write_text(source, encoding="utf-8")
    return (sys.executable, "-I", str(script))


def _request(delivery_id: str = "delivery.cli-001") -> str:
    return json.dumps(
        {
            "destination_brain_id": BRAIN_ID,
            "issuer_epoch": 7,
            "tenant_id": TENANT_ID,
            "principal_id": PRINCIPAL_ID,
            "requested_tier": "work",
            "text": PAYLOAD_TEXT,
            "delivery_id": delivery_id,
        }
    )


def _enqueue(outbox: Path, delivery_id: str) -> subprocess.CompletedProcess[str]:
    return _run(outbox, "enqueue", "--json", request=_request(delivery_id))


def _drain_argv(
    outbox: Path, argv: tuple[str, ...], *extra: str
) -> subprocess.CompletedProcess[str]:
    return _run(
        outbox,
        "drain",
        "--json",
        "--transport-argv",
        json.dumps(list(argv)),
        "--max-batch-items",
        "4",
        "--max-batch-bytes",
        str(1024 * 1024),
        "--timeout-seconds",
        "60",
        *extra,
    )


def _status_json(outbox: Path) -> dict[str, object]:
    completed = _run(outbox, "status", "--json")
    assert completed.returncode == 0, completed.stderr
    document = json.loads(completed.stdout)
    assert isinstance(document, dict)
    return document


def test_enqueue_drain_status_round_trip_json_mode(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    argv = _write_script(tmp_path, "receipt", _RECEIPT_SCRIPT)

    enqueued = _enqueue(outbox, "delivery.cli-001")
    assert enqueued.returncode == 0, enqueued.stderr
    document = json.loads(enqueued.stdout)
    assert document["result"] == "queued"
    assert document["delivery_id"] == "delivery.cli-001"
    assert len(str(document["request_digest"])) == 64
    assert _status_json(outbox)["queued_items"] == 1

    drained = _drain_argv(outbox, argv)
    assert drained.returncode == 0, drained.stderr
    summary = json.loads(drained.stdout)
    assert summary["result"] == "completed"
    assert summary["accepted"] == 1
    assert _status_json(outbox)["terminal_items"] == 1


def test_enqueue_human_output_mode_is_metadata_only(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"

    enqueued = _run(outbox, "enqueue", request=_request("delivery.cli-002"))
    assert enqueued.returncode == 0, enqueued.stderr
    assert "queued" in enqueued.stdout
    assert not enqueued.stdout.startswith("{")

    status = _run(outbox, "status")
    assert status.returncode == 0, status.stderr
    assert "queued_items=1" in status.stdout


def test_enqueue_rejects_malformed_requests(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"

    bad_json = _run(outbox, "enqueue", "--json", request="{not json")
    assert bad_json.returncode == 65

    missing_key = _request()
    document = json.loads(missing_key)
    del document["tenant_id"]
    refused = _run(outbox, "enqueue", "--json", request=json.dumps(document))
    assert refused.returncode == 65
    assert _status_json(outbox)["items"] == 0


def test_usage_errors_exit_2(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"

    no_command = _run(outbox)
    assert no_command.returncode == 2

    unknown = _run(outbox, "bogus")
    assert unknown.returncode == 2


def test_outbox_full_exits_75(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0

    full = _run(
        outbox, "enqueue", "--json", "--max-items", "1", request=_request("delivery.cli-002")
    )
    assert full.returncode == 75
    assert json.loads(full.stdout)["result"] == "outbox_full"


def test_enqueue_item_too_large_exits_65_without_writing(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    request = json.loads(_request("delivery.cli-001"))
    # Four-byte UTF-8 characters keep the text inside the engine's 65,536
    # character payload cap while pushing the serialized envelope over the
    # store's per-item byte limit.
    request["text"] = "\U0010ffff" * (DEFAULT_MAX_ITEM_BYTES // 4 + 64)

    refused = _run(outbox, "enqueue", "--json", request=json.dumps(request))

    assert refused.returncode == 65
    assert json.loads(refused.stdout)["result"] == "item_too_large"
    assert os.listdir(outbox) == []
    assert _status_json(outbox)["items"] == 0


def test_drain_busy_exits_75(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0
    argv = _write_script(tmp_path, "receipt", _RECEIPT_SCRIPT)
    from datetime import UTC, datetime

    lease = outbox / ".drain-lease.json"
    lease.write_text(
        json.dumps(
            {
                "acquired_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "lease_version": "outbox.drain-lease.v1",
                "pid": os.getpid(),
            }
        ),
        encoding="utf-8",
    )

    busy = _drain_argv(outbox, argv)
    assert busy.returncode == 75
    assert json.loads(busy.stdout)["result"] == "drain_busy"
    assert _status_json(outbox)["queued_items"] == 1


def test_owner_retry_requeues_a_quarantined_item(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    refusal = _write_script(tmp_path, "refusal", _REFUSAL_SCRIPT)
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0
    assert _drain_argv(outbox, refusal).returncode == 0
    assert _status_json(outbox)["quarantined_items"] == 1

    retry_queued = _run(outbox, "retry", "--json", "delivery.cli-001")
    assert retry_queued.returncode == 0, retry_queued.stderr
    assert json.loads(retry_queued.stdout)["result"] == "requeued"
    assert _status_json(outbox)["queued_items"] == 1

    retry_again = _run(outbox, "retry", "--json", "delivery.cli-001")
    assert retry_again.returncode == 65
    retry_missing = _run(outbox, "retry", "--json", "delivery.absent-001")
    assert retry_missing.returncode == 65


def test_owner_discard_requires_confirmation(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    refusal = _write_script(tmp_path, "refusal", _REFUSAL_SCRIPT)
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0
    assert _drain_argv(outbox, refusal).returncode == 0

    unconfirmed = _run(outbox, "discard", "--json", "delivery.cli-001")
    assert unconfirmed.returncode == 65
    assert _status_json(outbox)["quarantined_items"] == 1

    confirmed = _run(outbox, "discard", "--json", "--confirm", "delivery.cli-001")
    assert confirmed.returncode == 0, confirmed.stderr
    assert json.loads(confirmed.stdout)["result"] == "discarded"
    assert _status_json(outbox)["terminal_items"] == 1


def test_owner_convert_enqueues_lineage_and_keeps_the_original(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    refusal = _write_script(tmp_path, "refusal", _REFUSAL_SCRIPT)
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0
    assert _drain_argv(outbox, refusal).returncode == 0

    converted = _run(
        outbox,
        "convert",
        "--json",
        "--destination-brain-id",
        OTHER_BRAIN_ID,
        "--issuer-epoch",
        "9",
        "--new-delivery-id",
        "delivery.converted-001",
        "delivery.cli-001",
    )
    assert converted.returncode == 0, converted.stderr
    document = json.loads(converted.stdout)
    assert document["result"] == "queued"
    assert document["original_delivery_id"] == "delivery.cli-001"
    assert document["new_delivery_id"] == "delivery.converted-001"
    status = _status_json(outbox)
    assert status["quarantined_items"] == 1
    assert status["queued_items"] == 1


def test_drain_accepts_a_transport_command_file(tmp_path: Path) -> None:
    outbox = tmp_path / "outbox"
    argv = _write_script(tmp_path, "receipt", _RECEIPT_SCRIPT)
    command_file = tmp_path / "transport-command.json"
    command_file.write_text(json.dumps(list(argv)), encoding="utf-8")
    assert _enqueue(outbox, "delivery.cli-001").returncode == 0

    drained = _run(
        outbox,
        "drain",
        "--json",
        "--transport-command-file",
        str(command_file),
        "--max-batch-items",
        "4",
        "--max-batch-bytes",
        str(1024 * 1024),
        "--timeout-seconds",
        "60",
    )
    assert drained.returncode == 0, drained.stderr
    assert json.loads(drained.stdout)["accepted"] == 1
