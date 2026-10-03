"""Explicit provider identity and order admission; private values never enter read DTOs."""
# ruff: noqa: E501

from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from open_brain_engine.core.ids import portable_canonical_json_bytes

from .contracts import CaptureSubmission, CaptureSubmissionPath, FilePayload
from .source_observation import SourceRevisionObservation
from .t03_contracts import T03Error, _freeze, _thaw

if TYPE_CHECKING:
    from .historical_checkpoint import HistoricalBaselineDuplicate, HistoricalBaselineTemplate
    from .local import BrainEngine


def _identity(value: object) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 1024 or "\x00" in value:
        raise T03Error("invalid_arguments")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise T03Error("invalid_arguments") from None
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionSubmission:
    capture: CaptureSubmission
    namespace: Mapping[str, str]
    revision_key: str
    canonical_sha256: str
    expected_head: str | None
    ordering: Mapping[str, Any]
    expected_control_epoch: int
    dto_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.dto_version) is not int
            or self.dto_version != 1
            or not isinstance(self.capture, CaptureSubmission)
            or self.capture.submission_path is not CaptureSubmissionPath.PUBLIC_JOB
            or self.capture.request_sha256() != self.canonical_sha256
            or type(self.expected_control_epoch) is not int
            or not 0 <= self.expected_control_epoch <= 9007199254740991
        ):
            raise T03Error("invalid_arguments")
        if set(self.namespace) != {"connector_name", "connection_id", "resource_id", "external_id"}:
            raise T03Error("invalid_arguments")
        namespace = {key: _identity(value) for key, value in self.namespace.items()}
        _identity(self.revision_key)
        if self.expected_head is not None:
            from .normalization import _portable_id

            _portable_id(self.expected_head, "capture")
        order = dict(self.ordering)
        kind = order.get("kind")
        if kind == "predecessor":
            if set(order) != {"kind", "revision_key"}:
                raise T03Error("invalid_arguments")
            _identity(order["revision_key"])
        elif kind == "monotonic":
            if set(order) != {"kind", "provider_namespace", "epoch", "sequence"}:
                raise T03Error("invalid_arguments")
            _identity(order["provider_namespace"])
            _identity(order["epoch"])
            if type(order["sequence"]) is not int or not 0 <= order["sequence"] <= 9007199254740991:
                raise T03Error("invalid_arguments")
        elif kind != "unordered" or set(order) != {"kind"}:
            raise T03Error("invalid_arguments")
        object.__setattr__(self, "namespace", _freeze(namespace))
        object.__setattr__(self, "ordering", _freeze(order))

    def namespace_bytes(self) -> bytes:
        return portable_canonical_json_bytes(_thaw(self.namespace))

    def custody_bytes(self) -> bytes:
        return portable_canonical_json_bytes(
            {
                "dto_version": 1,
                "namespace": _thaw(self.namespace),
                "revision_key": self.revision_key,
                "canonical_sha256": self.canonical_sha256,
                "expected_head": self.expected_head,
                "ordering": _thaw(self.ordering),
                "expected_control_epoch": self.expected_control_epoch,
                "capture": self.capture.request_value(),
                "delivery_id": self.capture.delivery_id,
                "file_bytes_base64": (
                    base64.b64encode(self.capture.payload.data).decode("ascii")
                    if isinstance(self.capture.payload, FilePayload)
                    else None
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class SourceRevisionReceipt:
    source_id: str | None
    capture_id: str | None
    outcome: str
    control_epoch: int
    custody_id: str | None = None


def revision_order_decision(
    head: sqlite3.Row | Mapping[str, Any] | None, ordering: Mapping[str, Any]
) -> tuple[bool, str | None, bool]:
    """One shared promotion/predecessor/conflict decision for source admission."""
    if head is None:
        return True, None, ordering["kind"] == "predecessor"
    previous = json.loads(head["ordering_json"] or "null")
    if ordering["kind"] == "predecessor":
        promote = ordering["revision_key"] == head["revision_key"]
        return promote, head["capture_id"], not promote
    if ordering["kind"] == "monotonic" and previous is not None:
        comparable = all(
            ordering[key] == previous.get(key) for key in ("kind", "provider_namespace", "epoch")
        )
        conflict = not comparable or ordering["sequence"] == previous.get("sequence")
        promote = comparable and ordering["sequence"] > previous["sequence"]
        return promote, None, conflict
    return False, None, True


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionBinding:
    """Path-free, collector-visible binding for one managed logical item."""

    destination_brain_id: str
    issuer_epoch: int
    root_fingerprint: str
    accepted_source_id: str
    namespace: Mapping[str, str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.destination_brain_id, str)
            or not self.destination_brain_id.startswith("brn_")
            or type(self.issuer_epoch) is not int
            or self.issuer_epoch < 1
            or any(
                not isinstance(value, str) or not value
                for value in (self.root_fingerprint, self.accepted_source_id)
            )
            or set(self.namespace)
            != {"connector_name", "connection_id", "resource_id", "external_id"}
        ):
            raise T03Error("invalid_arguments")
        object.__setattr__(
            self,
            "namespace",
            _freeze({key: _identity(value) for key, value in self.namespace.items()}),
        )

    def value(self) -> dict[str, object]:
        return {
            "destination_brain_id": self.destination_brain_id,
            "issuer_epoch": self.issuer_epoch,
            "root_fingerprint": self.root_fingerprint,
            "accepted_source_id": self.accepted_source_id,
            "namespace": _thaw(self.namespace),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionDelivery:
    binding: SourceRevisionBinding
    submission: SourceRevisionSubmission
    expected_lifecycle_version: int
    delivery_id: str
    dto_version: int = 1

    def __post_init__(self) -> None:
        if (
            type(self.dto_version) is not int
            or self.dto_version != 1
            or not isinstance(self.binding, SourceRevisionBinding)
            or not isinstance(self.submission, SourceRevisionSubmission)
            or dict(self.binding.namespace) != dict(self.submission.namespace)
            or type(self.expected_lifecycle_version) is not int
            or self.expected_lifecycle_version < 0
        ):
            raise T03Error("invalid_arguments")
        _identity(self.delivery_id)

    def custody_bytes(self) -> bytes:
        return portable_canonical_json_bytes(
            {
                "dto_version": 1,
                "binding": self.binding.value(),
                "submission": json.loads(self.submission.custody_bytes()),
                "expected_lifecycle_version": self.expected_lifecycle_version,
                "delivery_id": self.delivery_id,
            }
        )

    @property
    def envelope_sha256(self) -> str:
        return sha256(self.custody_bytes()).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class SourceRevisionObservedDelivery(SourceRevisionDelivery):
    """Distinct v2 envelope; the original v1 delivery bytes remain frozen."""

    observation: SourceRevisionObservation
    dto_version: int = 2

    def __post_init__(self) -> None:
        if type(self.dto_version) is not int or self.dto_version != 2:
            raise T03Error("invalid_arguments")
        SourceRevisionDelivery(
            binding=self.binding,
            submission=self.submission,
            expected_lifecycle_version=self.expected_lifecycle_version,
            delivery_id=self.delivery_id,
        )
        if not isinstance(self.observation, SourceRevisionObservation):
            raise T03Error("invalid_arguments")
        self.observation.validate_capture(self.submission.capture.request_value())

    def custody_bytes(self) -> bytes:
        value = json.loads(SourceRevisionDelivery.custody_bytes(self))
        value["dto_version"] = 2
        value["observation"] = self.observation.value()
        return portable_canonical_json_bytes(value)


@dataclass(frozen=True, slots=True)
class SourceRevisionHead:
    source_id: str | None
    capture_id: str | None
    revision_key: str | None
    lifecycle: str | None
    lifecycle_version: int
    control_epoch: int
    destination_brain_id: str
    issuer_epoch: int


@dataclass(frozen=True, slots=True)
class SourceRevisionDeliveryReceipt:
    delivery_id: str
    envelope_sha256: str
    destination_brain_id: str
    issuer_epoch: int
    source_receipt: SourceRevisionReceipt | None
    outcome: str


class PublicJobRevisionSink:
    """The collector's narrow revision-only capability.

    It persists whole-envelope replay evidence before the pre-existing source
    CAS task runs.  It deliberately exposes neither routing nor lifecycle
    authority.
    """

    def __init__(self, engine: BrainEngine, binding: SourceRevisionBinding) -> None:
        self._engine = engine
        self._binding = binding

    def baseline_template(self) -> HistoricalBaselineTemplate | None:
        """Return body-free historical coordinates, not an eligibility grant."""
        from open_brain_engine.storage.filesystem import StorageError

        from .historical_checkpoint import historical_baseline_template
        from .sharing_contracts import SharingError

        connection = self._engine._store.connect()
        try:
            return historical_baseline_template(connection, self._engine.profile, self._binding)
        except SharingError, StorageError:
            raise T03Error("revision_changed") from None
        finally:
            connection.close()

    def lookup_baseline(
        self, delivery: SourceRevisionObservedDelivery, *, selection_generation: str
    ) -> HistoricalBaselineDuplicate | None:
        """Resolve an exact active baseline without reserving normal custody."""
        from open_brain_engine.storage.filesystem import StorageError

        from .historical_checkpoint import lookup_historical_baseline
        from .sharing_contracts import SharingError

        if (
            type(delivery) is not SourceRevisionObservedDelivery
            or delivery.binding != self._binding
        ):
            raise T03Error("invalid_arguments")
        connection = self._engine._store.connect()
        try:
            return lookup_historical_baseline(
                connection,
                self._engine.profile,
                delivery,
                selection_generation=selection_generation,
            )
        except SharingError, StorageError:
            raise T03Error("revision_changed") from None
        finally:
            connection.close()

    @contextmanager
    def baseline_page_checkpoint(
        self,
        entries: tuple[tuple[SourceRevisionObservedDelivery, HistoricalBaselineDuplicate], ...],
        *,
        selection_generation: str,
    ) -> Iterator[None]:
        """Revalidate a bounded historical page under one canonical writer fence.

        Every entry must belong to this selected root/resource and destination.
        The caller still holds its selection barrier, proves page completeness,
        and retains sender bodies. No nested per-item writer leases are needed.
        """
        from .historical_checkpoint import HistoricalBaselineDuplicate
        from .sharing_contracts import SharingError

        if type(entries) is not tuple or not 1 <= len(entries) <= 25:
            raise T03Error("invalid_arguments")
        seen: set[bytes] = set()
        for entry in entries:
            if type(entry) is not tuple or len(entry) != 2:
                raise T03Error("invalid_arguments")
            delivery, result = entry
            if (
                type(delivery) is not SourceRevisionObservedDelivery
                or type(result) is not HistoricalBaselineDuplicate
                or delivery.binding.destination_brain_id != self._binding.destination_brain_id
                or delivery.binding.issuer_epoch != self._binding.issuer_epoch
                or delivery.binding.root_fingerprint != self._binding.root_fingerprint
                or delivery.binding.accepted_source_id != self._binding.accepted_source_id
                or any(
                    delivery.binding.namespace[key] != self._binding.namespace[key]
                    for key in ("connector_name", "connection_id", "resource_id")
                )
            ):
                raise T03Error("invalid_arguments")
            namespace = delivery.submission.namespace_bytes()
            if namespace in seen:
                raise T03Error("invalid_arguments")
            seen.add(namespace)
        try:
            with self._engine._writer_lease.acquire_shared_writer():
                for delivery, result in entries:
                    sink = PublicJobRevisionSink(self._engine, delivery.binding)
                    current = sink.lookup_baseline(
                        delivery, selection_generation=selection_generation
                    )
                    if current is None or current != result:
                        raise T03Error("revision_changed")
                yield
        except SharingError:
            raise T03Error("revision_changed") from None

    @contextmanager
    def baseline_checkpoint(
        self,
        delivery: SourceRevisionObservedDelivery,
        result: HistoricalBaselineDuplicate,
        *,
        selection_generation: str,
    ) -> Iterator[None]:
        """Hold canonical writer exclusion through the caller's checkpoint write.

        The caller additionally owns the selection barrier and page completeness
        checks. This grants no body release, publication or owner mutation.
        """
        from .historical_checkpoint import HistoricalBaselineDuplicate
        from .sharing_contracts import SharingError

        if type(result) is not HistoricalBaselineDuplicate:
            raise T03Error("invalid_arguments")
        try:
            with self._engine._writer_lease.acquire_shared_writer():
                current = self.lookup_baseline(delivery, selection_generation=selection_generation)
                if current is None or current != result:
                    raise T03Error("revision_changed")
                yield
        except SharingError:
            raise T03Error("revision_changed") from None

    def inspect_head(self) -> SourceRevisionHead:
        namespace_sha = sha256(
            portable_canonical_json_bytes(_thaw(self._binding.namespace))
        ).hexdigest()
        connection = self._engine._store.connect()
        try:
            if not connection.in_transaction:
                connection.execute("BEGIN")
            from .historical_source import historical_baseline_for_namespace, historical_source_row

            baseline = historical_baseline_for_namespace(
                connection, self._engine.profile, namespace_sha, binding=self._binding
            )
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
            ).fetchone()
            if identity is None or (identity["brain_id"], identity["issuer_epoch"]) != (
                self._binding.destination_brain_id,
                self._binding.issuer_epoch,
            ):
                raise T03Error("revision_changed")
            source = connection.execute(
                "SELECT s.*,coalesce(l.lifecycle_version,0) lifecycle_version FROM source_namespaces n "
                "JOIN logical_sources s USING(source_id) LEFT JOIN source_lifecycle_state l USING(source_id) "
                "WHERE n.namespace_sha256=?",
                (namespace_sha,),
            ).fetchone()
            if source is None and baseline is not None:
                source = historical_source_row(connection, baseline)
            epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
            revision_key = None
            if source is not None:
                row = connection.execute(
                    "SELECT revision_key FROM source_revisions WHERE capture_id=?",
                    (source["head_capture_id"],),
                ).fetchone()
                revision_key = None if row is None else row["revision_key"]
                if (
                    baseline is not None
                    and source["head_capture_id"] == baseline.retained_original.capture_id
                ):
                    if revision_key is not None:
                        raise T03Error("revision_changed")
                    revision_key = baseline.observed_delivery.submission.revision_key
        finally:
            connection.close()
        return SourceRevisionHead(
            source_id=None if source is None else source["source_id"],
            capture_id=None if source is None else source["head_capture_id"],
            revision_key=revision_key,
            lifecycle=None if source is None else source["lifecycle"],
            lifecycle_version=0 if source is None else source["lifecycle_version"],
            control_epoch=epoch,
            destination_brain_id=self._binding.destination_brain_id,
            issuer_epoch=self._binding.issuer_epoch,
        )

    def submit(self, delivery: SourceRevisionDelivery) -> SourceRevisionDeliveryReceipt:
        if not isinstance(delivery, SourceRevisionDelivery) or delivery.binding != self._binding:
            raise T03Error("invalid_arguments")
        delivery.submission.capture.validate_profile(self._engine.profile)
        terminal_intake: SourceRevisionReceipt | None = None
        with (
            self._engine._writer_lease.acquire_shared_writer(),
            self._engine._store.transaction() as connection,
        ):
            identity = connection.execute(
                "SELECT brain_id,issuer_epoch FROM brain_identity WHERE singleton=1"
            ).fetchone()
            if identity is None or (identity["brain_id"], identity["issuer_epoch"]) != (
                self._binding.destination_brain_id,
                self._binding.issuer_epoch,
            ):
                raise T03Error("revision_changed")
            existing = connection.execute(
                "SELECT * FROM managed_source_deliveries WHERE delivery_id=?",
                (delivery.delivery_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["envelope_sha256"] != delivery.envelope_sha256
                    or existing["envelope_bytes"] != delivery.custody_bytes()
                ):
                    raise T03Error("invalid_arguments")
                if existing["receipt_json"] is not None:
                    retained = json.loads(existing["receipt_json"])
                    source = SourceRevisionReceipt(**retained["source_receipt"])
                    return SourceRevisionDeliveryReceipt(
                        delivery.delivery_id,
                        delivery.envelope_sha256,
                        self._binding.destination_brain_id,
                        self._binding.issuer_epoch,
                        source,
                        source.outcome,
                    )
                intake = connection.execute(
                    "SELECT submission_json,receipt_json FROM source_intakes "
                    "WHERE namespace_sha256=? AND revision_key=?",
                    (
                        sha256(delivery.submission.namespace_bytes()).hexdigest(),
                        delivery.submission.revision_key,
                    ),
                ).fetchone()
                if intake is not None and intake["receipt_json"] is not None:
                    if bytes(intake["submission_json"]) != delivery.submission.custody_bytes():
                        raise T03Error("invalid_arguments")
                    terminal_intake = SourceRevisionReceipt(**json.loads(intake["receipt_json"]))
            else:
                control_epoch = connection.execute(
                    "SELECT control_epoch FROM engine_generations"
                ).fetchone()[0]
                if control_epoch != delivery.submission.expected_control_epoch:
                    raise T03Error("revision_changed")
                if (
                    connection.execute(
                        "SELECT 1 FROM managed_source_deliveries WHERE source_delivery_id=?",
                        (delivery.submission.capture.delivery_id,),
                    ).fetchone()
                    is not None
                ):
                    raise T03Error("invalid_arguments")
                namespace_sha = sha256(delivery.submission.namespace_bytes()).hexdigest()
                if (
                    connection.execute(
                        "SELECT 1 FROM source_intakes WHERE namespace_sha256=? AND receipt_json IS NULL",
                        (namespace_sha,),
                    ).fetchone()
                    is not None
                ):
                    raise T03Error("operation_pending")
                if (
                    connection.execute(
                        "SELECT 1 FROM managed_source_deliveries WHERE receipt_json IS NULL "
                        "AND json_extract(CAST(envelope_bytes AS TEXT),'$.submission.namespace')=?",
                        (delivery.submission.namespace_bytes().decode("utf-8"),),
                    ).fetchone()
                    is not None
                ):
                    raise T03Error("operation_pending")
                intake = connection.execute(
                    "SELECT submission_json,receipt_json FROM source_intakes WHERE namespace_sha256=? AND revision_key=?",
                    (namespace_sha, delivery.submission.revision_key),
                ).fetchone()
                if (
                    intake is not None
                    and bytes(intake["submission_json"]) != delivery.submission.custody_bytes()
                ):
                    raise T03Error("invalid_arguments")
                current = connection.execute(
                    "SELECT s.source_id,s.lifecycle,s.availability,s.historical_only,s.head_capture_id,l.lifecycle_version "
                    "FROM source_namespaces n JOIN logical_sources s USING(source_id) "
                    "JOIN source_lifecycle_state l USING(source_id) WHERE n.namespace_sha256=?",
                    (namespace_sha,),
                ).fetchone()
                from .historical_source import (
                    historical_baseline_for_namespace,
                    historical_revision_head,
                    historical_source_row,
                )

                baseline = historical_baseline_for_namespace(
                    connection, self._engine.profile, namespace_sha, binding=self._binding
                )
                if current is None and baseline is not None:
                    current = historical_source_row(connection, baseline)
                if baseline is not None:
                    if (
                        delivery.submission.revision_key
                        == baseline.observed_delivery.submission.revision_key
                    ):
                        raise T03Error("invalid_arguments")
                    if current is not None and (
                        current["availability"] != "available" or current["historical_only"]
                    ):
                        raise T03Error("revision_changed")
                if current is None:
                    if (
                        delivery.expected_lifecycle_version != 0
                        or delivery.submission.expected_head is not None
                    ):
                        raise T03Error("revision_changed")
                elif (
                    current["lifecycle"] != "active"
                    or current["lifecycle_version"] != delivery.expected_lifecycle_version
                    or current["head_capture_id"] != delivery.submission.expected_head
                ):
                    raise T03Error("revision_changed")
                head = (
                    None
                    if current is None
                    else connection.execute(
                        "SELECT * FROM source_revisions WHERE capture_id=?",
                        (current["head_capture_id"],),
                    ).fetchone()
                )
                head = historical_revision_head(head, baseline)
                # Exact completed intake is reconciliation of retained custody,
                # not a new revision competing with the now-current head.
                # Full envelope equality and head/lifecycle CAS were checked above;
                # SourceTasks still returns its independently retained receipt.
                if (intake is None or intake["receipt_json"] is None) and revision_order_decision(
                    head, delivery.submission.ordering
                )[2]:
                    raise T03Error("source_revision_conflict")
                connection.execute(
                    "INSERT INTO managed_source_deliveries("
                    "delivery_id,envelope_sha256,envelope_bytes,source_id,destination_brain_id,"
                    "issuer_epoch,expected_head,expected_lifecycle_version,source_delivery_id,"
                    "receipt_json) VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        delivery.delivery_id,
                        delivery.envelope_sha256,
                        delivery.custody_bytes(),
                        None if current is None else current["source_id"],
                        self._binding.destination_brain_id,
                        self._binding.issuer_epoch,
                        delivery.submission.expected_head,
                        delivery.expected_lifecycle_version,
                        delivery.submission.capture.delivery_id,
                    ),
                )
        try:
            receipt = (
                terminal_intake
                if terminal_intake is not None
                else self._engine.sources.submit_revision(delivery.submission)
            )
        except T03Error as error:
            if error.code == "operation_pending":
                return SourceRevisionDeliveryReceipt(
                    delivery.delivery_id,
                    delivery.envelope_sha256,
                    self._binding.destination_brain_id,
                    self._binding.issuer_epoch,
                    None,
                    "operation_pending",
                )
            raise
        with self._engine._store.transaction() as connection:
            source = connection.execute(
                "SELECT lifecycle_version FROM source_lifecycle_state WHERE source_id=?",
                (receipt.source_id,),
            ).fetchone()
            # Pending managed admission blocks withdrawal until receipt linkage
            # completes. Completed exact replay returns above, even if retired.
            if receipt.outcome != "quarantined" and (
                source is None or source["lifecycle_version"] != delivery.expected_lifecycle_version
            ):
                raise T03Error("revision_changed")
            retained = {
                "source_receipt": {
                    "source_id": receipt.source_id,
                    "capture_id": receipt.capture_id,
                    "outcome": receipt.outcome,
                    "control_epoch": receipt.control_epoch,
                    "custody_id": receipt.custody_id,
                }
            }
            connection.execute(
                "UPDATE managed_source_deliveries SET source_id=?,receipt_json=? WHERE delivery_id=?",
                (receipt.source_id, json.dumps(retained, sort_keys=True), delivery.delivery_id),
            )
        return SourceRevisionDeliveryReceipt(
            delivery.delivery_id,
            delivery.envelope_sha256,
            self._binding.destination_brain_id,
            self._binding.issuer_epoch,
            receipt,
            receipt.outcome,
        )

    def verify_receipt(
        self, delivery: SourceRevisionDelivery, receipt: SourceRevisionDeliveryReceipt
    ) -> None:
        """Validate terminal evidence against immutable custody, not today's head."""
        if (
            not isinstance(delivery, SourceRevisionDelivery)
            or delivery.binding != self._binding
            or not isinstance(receipt, SourceRevisionDeliveryReceipt)
            or receipt.delivery_id != delivery.delivery_id
            or receipt.envelope_sha256 != delivery.envelope_sha256
            or receipt.destination_brain_id != self._binding.destination_brain_id
            or type(receipt.issuer_epoch) is not int
            or receipt.issuer_epoch != self._binding.issuer_epoch
            or receipt.source_receipt is None
            or type(receipt.source_receipt.control_epoch) is not int
            or receipt.outcome != receipt.source_receipt.outcome
        ):
            raise T03Error("invalid_arguments")
        connection = self._engine._store.connect()
        try:
            row = connection.execute(
                "SELECT envelope_bytes,receipt_json FROM managed_source_deliveries WHERE delivery_id=?",
                (delivery.delivery_id,),
            ).fetchone()
        finally:
            connection.close()
        if (
            row is None
            or bytes(row["envelope_bytes"]) != delivery.custody_bytes()
            or row["receipt_json"] is None
            or SourceRevisionReceipt(**json.loads(row["receipt_json"])["source_receipt"])
            != receipt.source_receipt
        ):
            raise T03Error("invalid_arguments")


def quarantine_stale_intakes(engine: BrainEngine) -> None:
    """Retain prior durable evidence, but never write new source/blob bytes after fencing."""
    from open_brain_engine.portable.v1 import validate_portable_write
    from open_brain_engine.storage.filesystem import read_confined

    from .normalization import _dated_path, _timestamp

    with engine._store.transaction() as connection:
        epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
        for intake in list(
            connection.execute("SELECT * FROM source_intakes WHERE receipt_json IS NULL")
        ):
            plan = json.loads(intake["plan_json"])
            if plan["control_epoch"] == epoch:
                continue
            custody_id = "custody_" + str(uuid4())
            source = connection.execute(
                "SELECT * FROM logical_sources WHERE source_id=?", (intake["source_id"],)
            ).fetchone()
            source_id = source["source_id"] if source is not None else None
            capture = connection.execute(
                "SELECT * FROM captures WHERE delivery_id=?", (intake["delivery_id"],)
            ).fetchone()
            retained_id = None
            if capture is not None:
                if capture["stage"] >= 3:
                    raise T03Error("operation_pending")
                path = _dated_path(
                    "sources/captures", capture["accepted_at"], capture["capture_id"]
                )
                raw = read_confined(
                    root=engine.profile.root,
                    relative=path,
                    expected_root_identity=engine.profile.root_identity,
                )
                if raw is not None:
                    validate_portable_write(path, raw, engine.profile.tenant_id)
                    if raw != portable_canonical_json_bytes(engine._capture_record(capture)):
                        raise T03Error("operation_pending")
                    retained_id = capture["capture_id"]
                    if source_id is None:
                        source_id = "source_" + str(uuid4())
                        connection.execute(
                            "INSERT INTO logical_sources VALUES(?,?,1,?,0,1,'active','available')",
                            (source_id, retained_id, capture["space_id"]),
                        )
                    sequence = connection.execute(
                        "SELECT coalesce(max(sequence),0)+1 FROM source_revisions "
                        "WHERE source_id=?",
                        (source_id,),
                    ).fetchone()[0]
                    connection.execute(
                        "INSERT INTO source_revisions VALUES(?,?,?,NULL,?,?,?,NULL,NULL,NULL,?,"
                        "'ungrouped_legacy_history')",
                        (
                            retained_id,
                            source_id,
                            sequence,
                            path,
                            sha256(raw).hexdigest(),
                            raw,
                            capture["accepted_at"],
                        ),
                    )
                    connection.execute(
                        "UPDATE captures SET source_path=?,stage=3 WHERE capture_id=?",
                        (path, retained_id),
                    )
                    connection.execute(
                        "DELETE FROM search_documents WHERE capture_id=? AND record_type='source'",
                        (retained_id,),
                    )
                else:
                    # This unacknowledged reservation has no public capture evidence. Its
                    # complete payload and metadata remain in durable private custody.
                    connection.execute(
                        "DELETE FROM captures WHERE capture_id=?", (capture["capture_id"],)
                    )
            connection.execute(
                "INSERT INTO source_quarantine VALUES(?,?,?,?,?,?)",
                (
                    custody_id,
                    source_id,
                    intake["revision_key"],
                    intake["request_sha256"],
                    intake["submission_json"],
                    _timestamp(engine._clock()),
                ),
            )
            receipt = dict(
                source_id=source_id,
                capture_id=retained_id,
                outcome="quarantined",
                control_epoch=epoch,
                custody_id=custody_id,
            )
            connection.execute(
                "UPDATE source_intakes SET receipt_json=? WHERE delivery_id=?",
                (json.dumps(receipt, sort_keys=True), intake["delivery_id"]),
            )
            # A fenced source revision may have been accepted into the
            # ingress journal before its canonical stage faulted.  Its source
            # custody is now explicitly quarantined, so it must not later
            # re-enter canonical materialization as an ordinary queued item.
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 10:
                journal = connection.execute(
                    "SELECT 1 FROM capture_ingestion_items WHERE delivery_id=?",
                    (intake["delivery_id"],),
                ).fetchone()
                if journal is not None:
                    engine.ingestion._append_event(
                        connection,
                        intake["delivery_id"],
                        "quarantined",
                        0,
                        {"status": "quarantined", "reason": "source_fenced"},
                    )


def register_intake(
    connection: sqlite3.Connection, capture: sqlite3.Row, payload: bytes, intake: sqlite3.Row
) -> None:
    """Complete the source linkage in the same commit as capture stage three."""
    plan = json.loads(intake["plan_json"])
    epoch = connection.execute("SELECT control_epoch FROM engine_generations").fetchone()[0]
    if epoch != plan["control_epoch"]:
        raise T03Error("operation_pending")
    source_id = intake["source_id"]
    source = connection.execute(
        "SELECT * FROM logical_sources WHERE source_id=?", (source_id,)
    ).fetchone()
    if source is None:
        connection.execute(
            "INSERT INTO logical_sources VALUES(?,?,0,?,0,1,'active','available')",
            (source_id, capture["capture_id"], capture["space_id"]),
        )
        connection.execute(
            "INSERT INTO source_namespaces VALUES(?,?,?)",
            (intake["namespace_sha256"], plan["namespace_json"], source_id),
        )
    elif source["head_capture_id"] != plan["expected_head"]:
        raise T03Error("revision_changed")
    elif source["lifecycle"] != "active":
        # A returning file or a journal replay is not an authorization to undo
        # an explicit withdrawal.  Retain the caller's exact custody envelope
        # for owner resolution instead of silently resurrecting the source.
        raise T03Error("revision_changed")
    if source is not None and connection.execute("PRAGMA user_version").fetchone()[0] >= 13:
        # Reservation is not current eligibility. Recovery must repeat this
        # check before binding the namespace or advancing a retained source.
        if connection.execute(
            "SELECT 1 FROM historical_baselines WHERE source_id=?", (source_id,)
        ).fetchone() is not None and (
            source["availability"] != "available" or source["historical_only"]
        ):
            raise T03Error("revision_changed")
        namespace = connection.execute(
            "SELECT source_id FROM source_namespaces WHERE namespace_sha256=?",
            (intake["namespace_sha256"],),
        ).fetchone()
        if namespace is None:
            baseline = connection.execute(
                "SELECT 1 FROM historical_baselines WHERE source_id=? AND namespace_sha256=?",
                (source_id, intake["namespace_sha256"]),
            ).fetchone()
            if baseline is None:
                raise T03Error("revision_changed")
            connection.execute(
                "INSERT INTO source_namespaces VALUES(?,?,?)",
                (intake["namespace_sha256"], plan["namespace_json"], source_id),
            )
        elif namespace["source_id"] != source_id:
            raise T03Error("revision_changed")
    sequence = connection.execute(
        "SELECT coalesce(max(sequence),0)+1 FROM source_revisions WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    # Link an initial managed reservation in the same commit as accepted source
    # evidence, so withdrawal cannot pass while its receipt is still pending.
    if connection.execute("PRAGMA user_version").fetchone()[0] >= 11:
        connection.execute(
            "UPDATE managed_source_deliveries SET source_id=? WHERE receipt_json IS NULL "
            "AND source_delivery_id=? "
            "AND json_extract(CAST(envelope_bytes AS TEXT),'$.submission.namespace')=? "
            "AND json_extract(CAST(envelope_bytes AS TEXT),'$.submission.revision_key')=? "
            "AND json_extract(CAST(envelope_bytes AS TEXT),'$.submission.canonical_sha256')=?",
            (
                source_id,
                plan["legacy_delivery_id"],
                plan["namespace_json"],
                intake["revision_key"],
                intake["request_sha256"],
            ),
        )
    connection.execute(
        "INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)",
        (
            capture["capture_id"],
            source_id,
            sequence,
            plan["predecessor_capture_id"],
            capture["source_path"],
            sha256(payload).hexdigest(),
            payload,
            intake["request_sha256"],
            intake["revision_key"],
            json.dumps(plan["ordering"], sort_keys=True),
            capture["accepted_at"],
        ),
    )
    if source is not None:
        # A new revision inherits the current route; captured evidence remains unchanged.
        connection.execute(
            "UPDATE captures SET space_id=? WHERE capture_id=?",
            (source["space_id"], capture["capture_id"]),
        )
        connection.execute(
            "UPDATE search_documents SET space_id=? WHERE record_type='source' AND capture_id=?",
            (source["space_id"], capture["capture_id"]),
        )
        if plan["promote"]:
            connection.execute(
                "UPDATE logical_sources SET head_capture_id=?,head_version=head_version+1 "
                "WHERE source_id=?",
                (capture["capture_id"], source_id),
            )
    connection.execute(
        "INSERT OR IGNORE INTO source_aliases VALUES(?,?,?)",
        (plan["legacy_delivery_id"], source_id, intake["request_sha256"]),
    )
    receipt = {
        "source_id": source_id,
        "capture_id": capture["capture_id"],
        "outcome": "captured" if plan["promote"] else "history_only",
        "control_epoch": epoch,
    }
    connection.execute(
        "UPDATE source_intakes SET receipt_json=? WHERE delivery_id=?",
        (json.dumps(receipt, sort_keys=True), intake["delivery_id"]),
    )
