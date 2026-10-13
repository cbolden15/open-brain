"""Strict reconstruction of frozen observed v2 evidence for owner reconciliation.

Decoding proves representation integrity, never current profile authority or
publication eligibility. Admission must independently validate those facts.
"""

from typing import Any

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .contracts import _submission_from_journal_value
from .sharing_contracts import SharingError
from .source_intake import (
    SourceRevisionBinding,
    SourceRevisionObservedDelivery,
    SourceRevisionSubmission,
)
from .source_observation import SourceRevisionObservation


def _closed(value: object, keys: set[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        raise SharingError("invalid_arguments")
    return value


def decode_historical_observation(value: object) -> SourceRevisionObservedDelivery:
    """Return exactly represented public-job evidence; reject lossy decoding."""
    try:
        data = _closed(
            value,
            {
                "dto_version",
                "binding",
                "submission",
                "expected_lifecycle_version",
                "delivery_id",
                "observation",
            },
        )
        raw = portable_canonical_json_bytes(data)
        if len(raw) > 65_536 or type(data["dto_version"]) is not int or data["dto_version"] != 2:
            raise SharingError("invalid_arguments")
        binding_value = _closed(
            data["binding"],
            {
                "destination_brain_id",
                "issuer_epoch",
                "root_fingerprint",
                "accepted_source_id",
                "namespace",
            },
        )
        namespace_keys = {"connector_name", "connection_id", "resource_id", "external_id"}
        _closed(binding_value["namespace"], namespace_keys)
        binding = SourceRevisionBinding(**binding_value)
        submission = _closed(
            data["submission"],
            {
                "dto_version",
                "namespace",
                "revision_key",
                "canonical_sha256",
                "expected_head",
                "ordering",
                "expected_control_epoch",
                "capture",
                "delivery_id",
                "file_bytes_base64",
            },
        )
        if type(submission["dto_version"]) is not int or submission["dto_version"] != 1:
            raise SharingError("invalid_arguments")
        _closed(submission["namespace"], namespace_keys)
        if type(submission["ordering"]) is not dict:
            raise SharingError("invalid_arguments")
        capture = _closed(
            submission["capture"],
            {
                "actor_id",
                "capture_why",
                "capture_why_origin",
                "schema_version",
                "intent",
                "payload",
                "privacy",
                "provenance",
                "role_claim",
                "source_origin",
                "source_reference",
                "space_id",
                "tenant_id",
                "title",
            },
        )
        if type(capture["payload"]) is not dict:
            raise SharingError("invalid_arguments")
        payload = dict(capture["payload"])
        file_payload = (
            payload.get("family") == "reference_or_file" and payload.get("kind") == "file"
        )
        if file_payload:
            if "data_base64" in payload:
                raise SharingError("invalid_arguments")
            payload["data_base64"] = submission["file_bytes_base64"]
        elif submission["file_bytes_base64"] is not None:
            raise SharingError("invalid_arguments")
        journal = dict(
            capture,
            payload=payload,
            delivery_id=submission["delivery_id"],
            action="quick",
            occurrence_at=payload.get("occurrence_at")
            if payload.get("family") in ("event", "measurement")
            else None,
            submission_path="public_job",
            destination_brain_id=None,
            issuer_epoch=None,
        )
        restored_capture = _submission_from_journal_value(journal)
        restored = SourceRevisionSubmission(
            **{
                key: item
                for key, item in submission.items()
                if key not in {"capture", "delivery_id", "file_bytes_base64"}
            },
            capture=restored_capture,
        )
        observed = SourceRevisionObservedDelivery(
            binding=binding,
            submission=restored,
            expected_lifecycle_version=data["expected_lifecycle_version"],
            delivery_id=data["delivery_id"],
            observation=SourceRevisionObservation.from_value(data["observation"]),
        )
        if observed.custody_bytes() != raw:
            raise SharingError("invalid_arguments")
        return observed
    except TypeError, ValueError, UnicodeError, OverflowError:
        raise SharingError("invalid_arguments") from None
