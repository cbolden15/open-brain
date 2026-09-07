"""Cross-field Brain Protocol v1 constraints that JSON Schema cannot express."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime
from typing import Final, cast

from .canonical import (
    LEDGER_HISTORY_COMMITMENT_PREFIX,
    canonical_json_bytes,
)
from .freeze import CLOCK_POLICY, RESOURCE_LIMITS


class ProtocolContractError(ValueError):
    """A structurally valid document violates a frozen protocol constraint."""


SIGNATURE_FIELDS: Final = {
    "cold-transfer-certificate": "owner_signature",
    "grant": "issuer_signature",
    "node-epoch-certificate": "owner_signature",
    "owner-key-certificate": "owner_signature",
    "receipt": "node_signature",
    "sequencer-stop-proof": "node_signature",
}


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProtocolContractError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolContractError(f"{label} must be an integer")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ProtocolContractError(f"{label} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProtocolContractError(f"{label} must be a timestamp") from error
    if parsed.tzinfo is None:
        raise ProtocolContractError(f"{label} must include an offset")
    return parsed


def decode_base64url(value: object, *, expected_bytes: int, label: str) -> bytes:
    """Decode canonical unpadded base64url and require an exact byte length."""
    if not isinstance(value, str) or "=" in value:
        raise ProtocolContractError(f"{label} must be unpadded base64url")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, TypeError) as error:
        raise ProtocolContractError(f"{label} must be unpadded base64url") from error
    canonical = base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=")
    if len(decoded) != expected_bytes or canonical != value:
        raise ProtocolContractError(f"{label} must encode exactly {expected_bytes} bytes")
    return decoded


def _validate_ledger_head(value: object, label: str) -> None:
    head = _object(value, label)
    commitment = head.get("history_commitment")
    if not isinstance(commitment, str) or not commitment.startswith(
        LEDGER_HISTORY_COMMITMENT_PREFIX
    ):
        raise ProtocolContractError(f"{label}.history_commitment has an invalid version")
    decode_base64url(
        commitment.removeprefix(LEDGER_HISTORY_COMMITMENT_PREFIX),
        expected_bytes=32,
        label=f"{label}.history_commitment",
    )


def signed_payload_bytes(contract: str, document: Mapping[str, object]) -> bytes:
    """Return the exact RFC 8785 bytes signed by a frozen signed contract."""
    try:
        signature_field = SIGNATURE_FIELDS[contract]
    except KeyError as error:
        raise ProtocolContractError(f"{contract} is not a signed contract") from error
    if signature_field not in document:
        raise ProtocolContractError(f"{contract} is missing {signature_field}")
    payload = dict(document)
    del payload[signature_field]
    return canonical_json_bytes(payload)


def request_binding_from_envelope(envelope: Mapping[str, object]) -> Mapping[str, object]:
    """Project the exact five-field principal-signature input from an envelope."""
    return _object(envelope.get("binding"), "request envelope binding")


def validate_protocol_semantics(contract: str, document: Mapping[str, object]) -> None:
    """Enforce portable constraints that Draft 2020-12 cannot compare or byte-count."""
    signature_field = SIGNATURE_FIELDS.get(contract)
    if signature_field is not None:
        decode_base64url(document.get(signature_field), expected_bytes=64, label=signature_field)

    if contract == "request-envelope":
        decode_base64url(
            document.get("principal_signature"),
            expected_bytes=64,
            label="principal_signature",
        )

    if contract == "owner-key-certificate":
        decode_base64url(
            document.get("owner_public_key"),
            expected_bytes=32,
            label="owner_public_key",
        )
        epoch = _integer(document.get("owner_epoch"), "owner_epoch")
        owner_key_id = document.get("owner_key_id")
        previous_key_id = document.get("previous_owner_key_id")
        certifying_key_id = document.get("certifying_owner_key_id")
        if epoch == 1:
            if previous_key_id is not None or certifying_key_id != owner_key_id:
                raise ProtocolContractError("the genesis owner key must be self-signed")
        elif (
            previous_key_id is None
            or previous_key_id != certifying_key_id
            or previous_key_id == owner_key_id
        ):
            raise ProtocolContractError("a rotated owner key must be certified by its predecessor")
        valid_from = _timestamp(document.get("valid_from"), "valid_from")
        retired_at = document.get("retired_at")
        if retired_at is not None and _timestamp(retired_at, "retired_at") <= valid_from:
            raise ProtocolContractError("retired_at must be later than valid_from")

    if contract == "query":
        if document.get("kind") == "request":
            literal = document.get("literal_text")
            if (
                isinstance(literal, str)
                and len(literal.encode("utf-8")) > RESOURCE_LIMITS.query_text_bytes
            ):
                raise ProtocolContractError("query literal exceeds 4096 UTF-8 bytes")
        elif document.get("kind") == "page":
            page_brain_id = document.get("brain_id")
            results = document.get("results")
            if isinstance(results, list):
                for result_index, result in enumerate(results):
                    result_value = _object(result, f"results[{result_index}]")
                    evidence_values = result_value.get("evidence")
                    if isinstance(evidence_values, list):
                        for evidence_index, evidence in enumerate(evidence_values):
                            label = f"results[{result_index}].evidence[{evidence_index}]"
                            evidence_value = _object(evidence, label)
                            validate_protocol_semantics("query-evidence", evidence_value)
                            if evidence_value.get("brain_id") != page_brain_id:
                                raise ProtocolContractError(
                                    f"{label} crosses the query page Brain boundary"
                                )

    if contract == "cold-transfer-certificate":
        decode_base64url(
            document.get("next_node_public_key"),
            expected_bytes=32,
            label="next_node_public_key",
        )
        previous = _integer(document.get("previous_epoch"), "previous_epoch")
        following = _integer(document.get("next_epoch"), "next_epoch")
        if following != previous + 1:
            raise ProtocolContractError("next_epoch must immediately follow previous_epoch")
        _validate_ledger_head(document.get("prior_ledger_head"), "prior_ledger_head")

    if contract == "node-epoch-certificate":
        decode_base64url(
            document.get("node_public_key"),
            expected_bytes=32,
            label="node_public_key",
        )
        epoch = _integer(document.get("sequencer_epoch"), "sequencer_epoch")
        prior = document.get("prior_ledger_head")
        if (epoch == 1) != (prior is None):
            raise ProtocolContractError("only the genesis Node epoch may omit a prior ledger head")
        if prior is not None:
            _validate_ledger_head(prior, "prior_ledger_head")

    if contract == "sequencer-stop-proof":
        _validate_ledger_head(document.get("last_ledger_head"), "last_ledger_head")

    if contract == "commit-batch":
        brain_id = document.get("brain_id")
        items = document.get("items")
        if isinstance(items, list):
            for index, item in enumerate(items):
                child = _object(item, f"items[{index}]")
                if child.get("brain_id") != brain_id:
                    raise ProtocolContractError(f"items[{index}] crosses the batch Brain boundary")
                kind = child.get("kind")
                if isinstance(kind, str):
                    validate_protocol_semantics(kind.replace("_", "-"), child)
                provenance = child.get("provenance")
                if provenance is not None and _object(
                    provenance,
                    f"items[{index}].provenance",
                ).get("brain_id") != brain_id:
                    raise ProtocolContractError(
                        f"items[{index}].provenance crosses the batch Brain boundary"
                    )

    if contract == "grant":
        decode_base64url(
            document.get("principal_public_key"),
            expected_bytes=32,
            label="principal_public_key",
        )
        issued = _timestamp(document.get("issued_at"), "issued_at")
        expires = _timestamp(document.get("expires_at"), "expires_at")
        ttl = _integer(document.get("ttl_seconds"), "ttl_seconds")
        if ttl < CLOCK_POLICY.grant_minimum_seconds or ttl > CLOCK_POLICY.grant_maximum_seconds:
            raise ProtocolContractError("grant TTL is outside the frozen range")
        if (expires - issued).total_seconds() != ttl:
            raise ProtocolContractError("grant timestamps do not match ttl_seconds")

    if contract == "receipt":
        certificate = _object(document.get("node_epoch_certificate"), "node_epoch_certificate")
        validate_protocol_semantics("node-epoch-certificate", certificate)
        if certificate.get("brain_id") != document.get("brain_id"):
            raise ProtocolContractError("receipt and Node certificate Brain IDs differ")
        if certificate.get("node_key_id") != document.get("node_key_id"):
            raise ProtocolContractError("receipt and Node certificate key IDs differ")
        if certificate.get("sequencer_epoch") != document.get("sequencer_epoch"):
            raise ProtocolContractError("receipt and Node certificate epochs differ")
        history = document.get("owner_key_history")
        if not isinstance(history, list):
            raise ProtocolContractError("owner_key_history must be an array")
        expected_previous_key_id: object = None
        expected_epoch = 1
        seen_key_ids: set[object] = set()
        previous_valid_from: datetime | None = None
        previous_retired_at: datetime | None = None
        certifying_owner_certificate: Mapping[str, object] | None = None
        certifying_owner_key_id = certificate.get("owner_key_id")
        for index, entry in enumerate(history):
            owner_certificate = _object(entry, f"owner_key_history[{index}]")
            validate_protocol_semantics("owner-key-certificate", owner_certificate)
            if owner_certificate.get("brain_id") != document.get("brain_id"):
                raise ProtocolContractError("owner key history crosses the receipt Brain boundary")
            if owner_certificate.get("owner_epoch") != expected_epoch:
                raise ProtocolContractError(
                    "owner key history epochs must be contiguous and ordered"
                )
            if owner_certificate.get("previous_owner_key_id") != expected_previous_key_id:
                raise ProtocolContractError("owner key history predecessor link is broken")
            current_key_id = owner_certificate.get("owner_key_id")
            if current_key_id in seen_key_ids:
                raise ProtocolContractError("owner key history repeats a key identifier")
            valid_from = _timestamp(
                owner_certificate.get("valid_from"),
                f"owner_key_history[{index}].valid_from",
            )
            if previous_valid_from is not None:
                if valid_from <= previous_valid_from:
                    raise ProtocolContractError(
                        "owner key history valid_from values must strictly increase"
                    )
                if previous_retired_at is None:
                    raise ProtocolContractError(
                        "an owner key must be retired before its successor becomes valid"
                    )
                if valid_from < previous_retired_at:
                    raise ProtocolContractError(
                        "successive owner key validity intervals must not overlap"
                    )
            retired_at_value = owner_certificate.get("retired_at")
            retired_at = (
                None
                if retired_at_value is None
                else _timestamp(
                    retired_at_value,
                    f"owner_key_history[{index}].retired_at",
                )
            )
            seen_key_ids.add(current_key_id)
            expected_previous_key_id = current_key_id
            expected_epoch += 1
            previous_valid_from = valid_from
            previous_retired_at = retired_at
            if current_key_id == certifying_owner_key_id:
                certifying_owner_certificate = owner_certificate
        if certifying_owner_certificate is None:
            raise ProtocolContractError("receipt history does not contain the certifying owner key")
        certificate_issued_at = _timestamp(
            certificate.get("issued_at"),
            "node_epoch_certificate.issued_at",
        )
        certifier_valid_from = _timestamp(
            certifying_owner_certificate.get("valid_from"),
            "certifying owner valid_from",
        )
        certifier_retired_at_value = certifying_owner_certificate.get("retired_at")
        if certificate_issued_at < certifier_valid_from:
            raise ProtocolContractError(
                "Node epoch certificate predates its certifying owner validity interval"
            )
        if certifier_retired_at_value is not None and certificate_issued_at >= _timestamp(
            certifier_retired_at_value,
            "certifying owner retired_at",
        ):
            raise ProtocolContractError(
                "Node epoch certificate is outside its certifying owner validity interval"
            )
        if _timestamp(document.get("issued_at"), "issued_at") < certificate_issued_at:
            raise ProtocolContractError("receipt predates its Node epoch certificate")

    if contract == "commit-result" and document.get("kind") in {"accepted", "replayed"}:
        validate_protocol_semantics(
            "receipt",
            _object(document.get("receipt"), "receipt"),
        )

    if contract in {"record", "proposal", "revision", "effect-receipt", "query-evidence"}:
        provenance = _object(document.get("provenance"), "provenance")
        if provenance.get("brain_id") != document.get("brain_id"):
            raise ProtocolContractError(f"{contract} provenance crosses its Brain boundary")


def validate_cold_transfer_pair(
    certificate: Mapping[str, object],
    stop_proof: Mapping[str, object],
) -> None:
    """Bind an owner-signed transfer certificate to its Node-signed stop proof."""
    validate_protocol_semantics("cold-transfer-certificate", certificate)
    validate_protocol_semantics("sequencer-stop-proof", stop_proof)
    matching_fields = {
        "brain_id": "brain_id",
        "from_node_id": "node_id",
        "from_machine_instance": "machine_instance",
        "previous_epoch": "epoch",
        "prior_ledger_head": "last_ledger_head",
        "stop_proof_id": "stop_proof_id",
    }
    for certificate_field, proof_field in matching_fields.items():
        if certificate.get(certificate_field) != stop_proof.get(proof_field):
            raise ProtocolContractError(
                f"cold transfer {certificate_field} does not match stop proof {proof_field}"
            )
    if _timestamp(certificate.get("issued_at"), "issued_at") < _timestamp(
        stop_proof.get("stopped_at"),
        "stopped_at",
    ):
        raise ProtocolContractError("cold transfer predates the stop proof")
