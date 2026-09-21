"""Connectors console script for the bounded offline outbox.

``open-brain-outbox`` is the owner-facing foreground CLI for the optional
connectors outbox: enqueue one JSON envelope request from stdin, run
exactly one bounded drain cycle through a deployment-supplied stdio
transport command, inspect metadata-only status, and resolve quarantined
items with the owner operations (retry, confirmed discard, conversion).
Every command prints metadata only and never echoes payload text; error
messages are the fixed labels of the underlying modules.

Exit codes: 0 success, 2 usage, 65 terminal refusals (owner-operation
refusals, store or contract failures, malformed requests, delivery
conflicts), 75 for ``drain_busy`` and ``outbox_full``.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from .contracts import DeliveryEnvelope, OutboxContractError
from .drain import DrainResult, DrainSummary, OutboxDrainError, run_drain_cycle
from .owner_ops import OwnerOperationError, owner_convert, owner_discard, owner_retry
from .stdio_transport import OutboxTransportError, StdioProcessTransport
from .store import EnqueueResult, OutboxStatus, OutboxStore, OutboxStoreError

__all__ = ["EXIT_OK", "EXIT_REFUSED", "EXIT_TEMPFAIL", "EXIT_USAGE", "main", "run_cli"]

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 65
EXIT_TEMPFAIL = 75

DEFAULT_MAX_ITEMS = 1000
DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_BATCH_ITEMS = 16
DEFAULT_MAX_BATCH_BYTES = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_POLICY_REF = "policy.cli-default-v1"
DEFAULT_RETRY_AGE_LIMIT_SECONDS = 86400
DEFAULT_RETRY_ATTEMPT_LIMIT = 8

_REQUEST_REQUIRED_KEYS = frozenset(
    {
        "destination_brain_id",
        "issuer_epoch",
        "tenant_id",
        "principal_id",
        "requested_tier",
        "text",
    }
)
_REQUEST_OPTIONAL_KEYS = frozenset(
    {
        "delivery_id",
        "policy_ref",
        "retry_age_limit_seconds",
        "retry_attempt_limit",
        "enqueued_at",
    }
)


class _CliUsageError(Exception):
    """A CLI input is unusable; the fixed message never echoes item content."""


class _CliRefusalError(Exception):
    """A request is refused terminally; the fixed message carries no content."""


def run_cli() -> None:
    raise SystemExit(main())


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "enqueue":
            return _command_enqueue(args)
        if args.command == "drain":
            return _command_drain(args)
        if args.command == "status":
            return _command_status(args)
        if args.command == "retry":
            return _command_retry(args)
        if args.command == "discard":
            return _command_discard(args)
        if args.command == "convert":
            return _command_convert(args)
        raise _CliUsageError("unknown command")
    except _CliUsageError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE
    except (
        _CliRefusalError,
        OwnerOperationError,
        OutboxStoreError,
        OutboxContractError,
        OutboxTransportError,
    ) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except OutboxDrainError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_TEMPFAIL


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-brain-outbox",
        description="Owner-facing foreground CLI for the connectors outbox (metadata-only output).",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    enqueue = subparsers.add_parser(
        "enqueue", help="enqueue one JSON envelope request read from stdin"
    )
    _add_store_options(enqueue)

    drain = subparsers.add_parser("drain", help="run exactly one bounded drain cycle")
    _add_store_options(drain)
    drain.add_argument(
        "--transport-argv",
        help="JSON array of strings: the deployment transport command",
    )
    drain.add_argument(
        "--transport-command-file",
        help="path to a file holding the transport argv as a JSON array",
    )
    drain.add_argument("--max-batch-items", type=_positive_int, default=DEFAULT_MAX_BATCH_ITEMS)
    drain.add_argument("--max-batch-bytes", type=_positive_int, default=DEFAULT_MAX_BATCH_BYTES)
    drain.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)

    status = subparsers.add_parser("status", help="print metadata-only outbox counts")
    _add_store_options(status)

    retry = subparsers.add_parser("retry", help="owner retry of one quarantined item")
    _add_store_options(retry)
    retry.add_argument("delivery_id")

    discard = subparsers.add_parser("discard", help="owner-confirmed terminal discard")
    _add_store_options(discard)
    discard.add_argument("delivery_id")
    discard.add_argument("--confirm", action="store_true", help="required confirmation")

    convert = subparsers.add_parser(
        "convert", help="convert one quarantined item to a new destination"
    )
    _add_store_options(convert)
    convert.add_argument("delivery_id")
    convert.add_argument("--destination-brain-id", required=True)
    convert.add_argument("--issuer-epoch", type=_positive_int, required=True)
    convert.add_argument("--new-delivery-id")

    return parser


def _add_store_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--outbox-dir", required=True, help="owner-supplied outbox directory")
    parser.add_argument("--max-items", type=_positive_int, default=DEFAULT_MAX_ITEMS)
    parser.add_argument("--max-bytes", type=_positive_int, default=DEFAULT_MAX_BYTES)
    parser.add_argument("--json", action="store_true", help="print one JSON document")


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be an integer") from error
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store(args: argparse.Namespace) -> OutboxStore:
    return OutboxStore(Path(args.outbox_dir), max_items=args.max_items, max_bytes=args.max_bytes)


def _emit(args: argparse.Namespace, payload: dict[str, object], human: str) -> None:
    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(human)


def _command_enqueue(args: argparse.Namespace) -> int:
    try:
        document = json.loads(sys.stdin.read())
    except json.JSONDecodeError as error:
        raise _CliRefusalError("invalid enqueue request document") from error
    if (
        not isinstance(document, dict)
        or not _REQUEST_REQUIRED_KEYS.issubset(document)
        or not document.keys() <= _REQUEST_REQUIRED_KEYS | _REQUEST_OPTIONAL_KEYS
    ):
        raise _CliRefusalError("invalid enqueue request fields")
    delivery_id = document.get("delivery_id")
    if not isinstance(delivery_id, str):
        delivery_id = str(uuid.uuid4())
    try:
        envelope = DeliveryEnvelope.create(
            destination_brain_id=document["destination_brain_id"],
            expected_issuer_epoch=document["issuer_epoch"],
            tenant_id=document["tenant_id"],
            principal_id=document["principal_id"],
            delivery_id=delivery_id,
            requested_tier=document["requested_tier"],
            policy_ref=document.get("policy_ref", DEFAULT_POLICY_REF),
            payload={"family": "text", "text": document["text"]},
            enqueued_at=document.get("enqueued_at", _now()),
            retry_age_limit_seconds=document.get(
                "retry_age_limit_seconds", DEFAULT_RETRY_AGE_LIMIT_SECONDS
            ),
            retry_attempt_limit=document.get("retry_attempt_limit", DEFAULT_RETRY_ATTEMPT_LIMIT),
        )
    except (OutboxContractError, TypeError, ValueError) as error:
        raise _CliRefusalError("invalid enqueue request values") from error
    result = _store(args).enqueue(envelope)
    _emit(
        args,
        {
            "delivery_id": envelope.delivery_id,
            "request_digest": envelope.request_digest,
            "result": result.value,
        },
        f"{result.value} delivery_id={envelope.delivery_id} "
        f"request_digest={envelope.request_digest}",
    )
    if result is EnqueueResult.OUTBOX_FULL:
        return EXIT_TEMPFAIL
    if result is EnqueueResult.DELIVERY_CONFLICT:
        return EXIT_REFUSED
    return EXIT_OK


def _command_drain(args: argparse.Namespace) -> int:
    argv = _transport_argv(args)
    if args.timeout_seconds <= 0:
        raise _CliUsageError("timeout must be positive")
    try:
        transport = StdioProcessTransport(argv, timeout_seconds=args.timeout_seconds)
    except OutboxTransportError as error:
        raise _CliUsageError(f"invalid drain transport: {error}") from error
    summary = run_drain_cycle(
        _store(args),
        transport,
        max_batch_items=args.max_batch_items,
        max_batch_bytes=args.max_batch_bytes,
    )
    payload = _summary_payload(summary)
    human = " ".join(f"{key}={value}" for key, value in payload.items())
    _emit(args, payload, human)
    if summary.result is DrainResult.DRAIN_BUSY:
        return EXIT_TEMPFAIL
    return EXIT_OK


def _command_status(args: argparse.Namespace) -> int:
    status = _store(args).status()
    payload = _status_payload(status)
    human = " ".join(f"{key}={value}" for key, value in payload.items())
    _emit(args, payload, human)
    return EXIT_OK


def _command_retry(args: argparse.Namespace) -> int:
    item = owner_retry(_store(args), args.delivery_id, now=_now())
    _emit(
        args,
        {"delivery_id": item.delivery_id, "result": "requeued", "state": item.state.value},
        f"requeued delivery_id={item.delivery_id}",
    )
    return EXIT_OK


def _command_discard(args: argparse.Namespace) -> int:
    item = owner_discard(_store(args), args.delivery_id, confirm=args.confirm, now=_now())
    _emit(
        args,
        {
            "delivery_id": item.delivery_id,
            "result": "discarded",
            "state": item.state.value,
        },
        f"discarded delivery_id={item.delivery_id}",
    )
    return EXIT_OK


def _command_convert(args: argparse.Namespace) -> int:
    conversion = owner_convert(
        _store(args),
        args.delivery_id,
        destination_brain_id=args.destination_brain_id,
        issuer_epoch=args.issuer_epoch,
        now=_now(),
        new_delivery_id=args.new_delivery_id,
    )
    _emit(
        args,
        {
            "new_delivery_id": conversion.new_delivery_id,
            "original_delivery_id": conversion.original_delivery_id,
            "result": conversion.result.value,
        },
        f"{conversion.result.value} original_delivery_id={conversion.original_delivery_id} "
        f"new_delivery_id={conversion.new_delivery_id}",
    )
    if conversion.result is EnqueueResult.OUTBOX_FULL:
        return EXIT_TEMPFAIL
    if conversion.result is EnqueueResult.DELIVERY_CONFLICT:
        return EXIT_REFUSED
    return EXIT_OK


def _transport_argv(args: argparse.Namespace) -> tuple[str, ...]:
    if (args.transport_argv is not None) == (args.transport_command_file is not None):
        raise _CliUsageError(
            "exactly one of --transport-argv or --transport-command-file is required"
        )
    raw = args.transport_argv
    if raw is None:
        try:
            raw = Path(args.transport_command_file).read_text(encoding="utf-8")
        except OSError as error:
            raise _CliUsageError("transport command file unreadable") from error
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise _CliUsageError("transport command is not a JSON array") from error
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(part, str) and part for part in value)
    ):
        raise _CliUsageError("transport command must be a nonempty JSON array of strings")
    return tuple(value)


def _summary_payload(summary: DrainSummary) -> dict[str, object]:
    return {
        "result": summary.result.value,
        "stale_lease_reclaimed": summary.stale_lease_reclaimed,
        "admitted_items": summary.admitted_items,
        "skipped_items": summary.skipped_items,
        "delivery_attempts": summary.delivery_attempts,
        "accepted": summary.accepted,
        "duplicate": summary.duplicate,
        "quarantined_age_exhausted": summary.quarantined_age_exhausted,
        "quarantined_attempts_exhausted": summary.quarantined_attempts_exhausted,
        "quarantined_receipt_mismatch": summary.quarantined_receipt_mismatch,
        "quarantined_refused": summary.quarantined_refused,
        "retried": summary.retried,
        "transport_errors": summary.transport_errors,
        "batch_too_large_items": summary.batch_too_large_items,
    }


def _status_payload(status: OutboxStatus) -> dict[str, object]:
    return {
        "max_items": status.max_items,
        "max_bytes": status.max_bytes,
        "headroom_bytes": status.headroom_bytes,
        "items": status.items,
        "size_bytes": status.size_bytes,
        "queued_items": status.queued_items,
        "queued_size_bytes": status.queued_size_bytes,
        "quarantined_items": status.quarantined_items,
        "quarantined_size_bytes": status.quarantined_size_bytes,
        "terminal_items": status.terminal_items,
        "terminal_size_bytes": status.terminal_size_bytes,
        "corrupt_items": status.corrupt_items,
        "corrupt_size_bytes": status.corrupt_size_bytes,
    }
