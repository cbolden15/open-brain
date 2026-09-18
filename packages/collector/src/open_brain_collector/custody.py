"""Durable per-item collector custody shared by legacy and live capture paths."""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict
from typing import cast

from open_brain_engine.engine import PrivacyDecision

from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey

MAX_RETAINED_ITEMS = 256
MAX_RETAINED_UTF8_BYTES = 2_097_152
MAX_RESOLVED_RECEIPTS = 512
_FILE = "custody.json"
_OUTCOMES = {"pending", "captured", "duplicate", "quarantined", "history_only"}
_TERMINAL = _OUTCOMES - {"pending"}
_EVIDENCE = {None, "capture_id", "injected_sink", "acceleration_cache", "conflict"}
_ID = re.compile(r"custody:[0-9a-f]{64}")
_HEX = re.compile(r"[0-9a-f]{64}")
_RECEIPT_KEYS = {
    "receipt_id",
    "source_id",
    "binding",
    "generation",
    "control_epoch",
    "item_id",
    "revision_identity",
    "intake_digest",
    "intake",
    "retained_bytes",
    "outcome",
    "capture_id",
    "reason_code",
    "evidence",
    "sequence",
}


def intake_dict(intake: SourceRecordIntake) -> dict[str, object]:
    return {
        "key": asdict(intake.key),
        "url": intake.url,
        "text": intake.text,
        "title": intake.title,
        "privacy": intake.privacy.to_dict(),
    }


def intake_from_dict(value: object) -> SourceRecordIntake:
    if not isinstance(value, dict) or set(value) != {"key", "url", "text", "title", "privacy"}:
        raise LiveSourceError("collector_invalid_custody")
    try:
        key = value["key"]
        if not isinstance(key, dict):
            raise TypeError
        return SourceRecordIntake(
            key=SourceRecordKey(**key),
            url=cast(str, value["url"]),
            text=cast(str, value["text"]),
            title=cast(str | None, value["title"]),
            privacy=PrivacyDecision.from_dict(value["privacy"]),
        )
    except TypeError, ValueError, KeyError:
        raise LiveSourceError("collector_invalid_custody") from None


def intake_digest(intake: SourceRecordIntake) -> str:
    return hashlib.sha256(bounded_json(intake_dict(intake), 1_048_576)).hexdigest()


def _preimage(receipt: dict[str, object]) -> dict[str, object]:
    return {
        key: receipt[key]
        for key in (
            "source_id",
            "binding",
            "generation",
            "control_epoch",
            "item_id",
            "revision_identity",
            "intake_digest",
        )
    }


def _identity(preimage: dict[str, object]) -> str:
    return "custody:" + hashlib.sha256(bounded_json(preimage, 4096)).hexdigest()


def _unresolved(receipts: dict[str, object]) -> dict[str, object]:
    return {
        key: item
        for key, item in receipts.items()
        if isinstance(item, dict) and item.get("intake") is not None
    }


def _reserved_size(receipts: dict[str, object]) -> int:
    reserved: dict[str, object] = {}
    for key, value in _unresolved(receipts).items():
        item = dict(cast(dict[str, object], value))
        item.update(
            outcome="quarantined",
            capture_id="x" * 128,
            reason_code="source_revision_conflict",
            evidence="acceleration_cache",
        )
        reserved[key] = item
    return len(bounded_json({"receipts": reserved}, 4_194_304))


class CustodyStore:
    """One aggregate quota and durable receipt document for an owned collector root."""

    def __init__(self, store: PrivateJsonStore) -> None:
        self._store = store

    @staticmethod
    def _valid_evidence(receipt: dict[str, object]) -> bool:
        outcome, evidence = receipt["outcome"], receipt["evidence"]
        if outcome == "pending":
            return (
                evidence is None
                and receipt["capture_id"] is None
                and receipt["reason_code"] is None
            )
        if outcome == "quarantined":
            return (
                evidence == "conflict"
                and receipt["reason_code"] == "source_revision_conflict"
                and receipt["capture_id"] is None
            )
        if outcome in {"captured", "duplicate", "history_only"}:
            if receipt["reason_code"] is not None:
                return False
            if evidence == "capture_id":
                return type(receipt["capture_id"]) is str and bool(receipt["capture_id"])
            return (
                evidence in {"injected_sink", "acceleration_cache"}
                and receipt["capture_id"] is None
            )
        return False

    def _load(self) -> dict[str, object]:
        value = self._store.read(_FILE)
        if value is None:
            return {"schema_version": 1, "next_sequence": 0, "receipts": {}}
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "next_sequence", "receipts"}
            or value.get("schema_version") != 1
            or type(value.get("next_sequence")) is not int
            or cast(int, value["next_sequence"]) < 0
            or not isinstance(value.get("receipts"), dict)
        ):
            raise LiveSourceError("collector_invalid_custody")
        receipts = cast(dict[str, object], value["receipts"])
        for receipt_id, raw in receipts.items():
            if not isinstance(raw, dict):
                raise LiveSourceError("collector_invalid_custody")
            receipt = cast(dict[str, object], raw)
            if (
                type(receipt_id) is not str
                or _ID.fullmatch(receipt_id) is None
                or set(receipt) != _RECEIPT_KEYS
                or receipt["receipt_id"] != receipt_id
                or _identity(_preimage(receipt)) != receipt_id
                or receipt["outcome"] not in _OUTCOMES
                or receipt["evidence"] not in _EVIDENCE
                or type(receipt["source_id"]) is not str
                or type(receipt["binding"]) is not str
                or type(receipt["generation"]) is not str
                or _HEX.fullmatch(receipt["generation"]) is None
                or type(receipt["control_epoch"]) is not int
                or receipt["control_epoch"] < 0
                or type(receipt["item_id"]) is not str
                or type(receipt["revision_identity"]) is not str
                or _HEX.fullmatch(receipt["revision_identity"]) is None
                or type(receipt["intake_digest"]) is not str
                or _HEX.fullmatch(receipt["intake_digest"]) is None
                or type(receipt["retained_bytes"]) is not int
                or receipt["retained_bytes"] < 0
                or type(receipt["sequence"]) is not int
                or receipt["sequence"] < 0
                or (
                    receipt["capture_id"] is not None
                    and (
                        type(receipt["capture_id"]) is not str
                        or not receipt["capture_id"]
                        or len(receipt["capture_id"]) > 128
                    )
                )
                or (
                    receipt["reason_code"] is not None
                    and receipt["reason_code"] != "source_revision_conflict"
                )
            ):
                raise LiveSourceError("collector_invalid_custody")
            intake_value = receipt["intake"]
            if intake_value is not None:
                intake = intake_from_dict(intake_value)
                if (
                    intake_digest(intake) != receipt["intake_digest"]
                    or intake.key.delivery_id() != receipt["item_id"]
                    or intake.key.revision_identity() != receipt["revision_identity"]
                    or len(bounded_json(intake_value, 1_048_576)) != receipt["retained_bytes"]
                ):
                    raise LiveSourceError("collector_invalid_custody")
            elif receipt["outcome"] in {"pending", "quarantined"} or receipt["retained_bytes"] != 0:
                raise LiveSourceError("collector_invalid_custody")
            if not self._valid_evidence(receipt):
                raise LiveSourceError("collector_invalid_custody")
        return cast(dict[str, object], value)

    def stage(
        self,
        *,
        source_id: str,
        binding: str,
        generation: str,
        control_epoch: int,
        intakes: tuple[SourceRecordIntake, ...],
    ) -> tuple[str, ...]:
        if not intakes:
            return ()
        with self._store.lock("custody"):
            state = self._load()
            receipts = cast(dict[str, object], state["receipts"])
            proposed: dict[str, dict[str, object]] = {}
            ids: list[str] = []
            sequence = cast(int, state["next_sequence"])
            for intake in intakes:
                item, digest = intake_dict(intake), intake_digest(intake)
                partial: dict[str, object] = {
                    "source_id": source_id,
                    "binding": binding,
                    "generation": generation,
                    "control_epoch": control_epoch,
                    "item_id": intake.key.delivery_id(),
                    "revision_identity": intake.key.revision_identity(),
                    "intake_digest": digest,
                }
                receipt_id = _identity(partial)
                ids.append(receipt_id)
                existing = receipts.get(receipt_id)
                if isinstance(existing, dict) and existing["intake"] is None:
                    existing.update(
                        intake=item,
                        retained_bytes=len(bounded_json(item, 1_048_576)),
                        outcome="pending",
                        capture_id=None,
                        reason_code=None,
                        evidence=None,
                    )
                if receipt_id in receipts or receipt_id in proposed:
                    continue
                proposed[receipt_id] = {
                    "receipt_id": receipt_id,
                    **partial,
                    "intake": item,
                    "retained_bytes": len(bounded_json(item, 1_048_576)),
                    "outcome": "pending",
                    "capture_id": None,
                    "reason_code": None,
                    "evidence": None,
                    "sequence": sequence,
                }
                sequence += 1
            unresolved = _unresolved(receipts)
            unresolved.update(proposed)
            size = _reserved_size(unresolved)
            if len(unresolved) > MAX_RETAINED_ITEMS or size > MAX_RETAINED_UTF8_BYTES:
                raise LiveSourceError("collector_custody_quota_exceeded")
            receipts.update(proposed)
            state["next_sequence"] = sequence
            self._store.write(_FILE, state)
            return tuple(ids)

    def receipt(self, receipt_id: str) -> dict[str, object]:
        if type(receipt_id) is not str or _ID.fullmatch(receipt_id) is None:
            raise LiveSourceError("collector_invalid_custody_id")
        with self._store.lock("custody"):
            value = cast(dict[str, object], self._load()["receipts"]).get(receipt_id)
            if not isinstance(value, dict):
                raise LiveSourceError("collector_custody_not_found")
            return dict(value)

    def intake(self, receipt_id: str) -> SourceRecordIntake:
        value = self.receipt(receipt_id)
        if value["intake"] is None:
            raise LiveSourceError("collector_custody_not_replayable")
        return intake_from_dict(value["intake"])

    def outcome(
        self,
        receipt_id: str,
        outcome: str,
        *,
        capture_id: str | None = None,
        reason_code: str | None = None,
        evidence: str | None = None,
    ) -> None:
        if outcome not in _TERMINAL:
            raise LiveSourceError("collector_invalid_custody")
        evidence = evidence or (
            "conflict"
            if outcome == "quarantined"
            else "capture_id"
            if capture_id is not None
            else "injected_sink"
        )
        with self._store.lock("custody"):
            state = self._load()
            receipt = cast(dict[str, object], state["receipts"]).get(receipt_id)
            if not isinstance(receipt, dict) or receipt["intake"] is None:
                raise LiveSourceError("collector_custody_not_found")
            receipt.update(
                outcome=outcome, capture_id=capture_id, reason_code=reason_code, evidence=evidence
            )
            if not self._valid_evidence(receipt):
                raise LiveSourceError("collector_invalid_custody")
            if _reserved_size(cast(dict[str, object], state["receipts"])) > MAX_RETAINED_UTF8_BYTES:
                receipt.update(outcome="pending", capture_id=None, reason_code=None, evidence=None)
                raise LiveSourceError("collector_custody_quota_exceeded")
            self._store.write(_FILE, state)

    def validate_terminal(self, receipt_ids: tuple[str, ...]) -> None:
        with self._store.lock("custody"):
            receipts = cast(dict[str, object], self._load()["receipts"])
            for receipt_id in receipt_ids:
                receipt = receipts.get(receipt_id)
                if (
                    not isinstance(receipt, dict)
                    or receipt["outcome"] not in _TERMINAL
                    or receipt["intake"] is None
                    or not self._valid_evidence(receipt)
                ):
                    raise LiveSourceError("collector_incomplete_custody")

    def release_completed(self, receipt_ids: tuple[str, ...]) -> None:
        with self._store.lock("custody"):
            state = self._load()
            receipts = cast(dict[str, object], state["receipts"])
            for receipt_id in receipt_ids:
                receipt = receipts.get(receipt_id)
                if (
                    not isinstance(receipt, dict)
                    or receipt["outcome"] not in _TERMINAL
                    or receipt["intake"] is None
                    or not self._valid_evidence(receipt)
                ):
                    raise LiveSourceError("collector_incomplete_custody")
                if receipt["outcome"] != "quarantined":
                    receipt.update(intake=None, retained_bytes=0)
            resolved = sorted(
                (
                    item
                    for item in receipts.values()
                    if isinstance(item, dict) and item["intake"] is None
                ),
                key=lambda item: cast(int, item["sequence"]),
            )
            for receipt in resolved[:-MAX_RESOLVED_RECEIPTS]:
                del receipts[cast(str, receipt["receipt_id"])]
            self._store.write(_FILE, state)

    def status(self, source_id: str | None = None) -> dict[str, object]:
        with self._store.lock("custody"):
            receipts = cast(dict[str, object], self._load()["receipts"])
            values = [
                item
                for item in receipts.values()
                if isinstance(item, dict) and (source_id is None or item["source_id"] == source_id)
            ]
            retained = {
                cast(str, item["receipt_id"]): item for item in values if item["intake"] is not None
            }
            return {
                "schema_version": 1,
                "counts": {
                    outcome: sum(item["outcome"] == outcome for item in values)
                    for outcome in _OUTCOMES
                },
                "receipt_ids": sorted(retained),
                "retained_items": len(retained),
                "retained_utf8_bytes": len(bounded_json({"receipts": retained}, 4_194_304)),
            }

    def inspect(self, receipt_id: str) -> dict[str, object]:
        value = self.receipt(receipt_id)
        return {
            key: value[key]
            for key in (
                "receipt_id",
                "source_id",
                "item_id",
                "revision_identity",
                "intake_digest",
                "outcome",
                "capture_id",
                "reason_code",
                "control_epoch",
            )
        }
