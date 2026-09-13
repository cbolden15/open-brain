from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import LockScope
from open_brain_engine.storage.filesystem import (
    DuplicateConflictError,
    RootConfinementError,
    StorageError,
    WriteState,
    atomic_write_new,
    read_confined,
)

from .models import DeploymentTarget, HostRole
from .now import NowBuildResult, NowItem, NowProjectionInput, NowRoots, build_now, check_now
from .writer_jobs import (
    EffectCommand,
    EffectParameter,
    EffectReceipt,
    EffectRecord,
    PreparedEffect,
    ScheduledEffect,
    WriterJobError,
    WriterJobInvocation,
)

_OPAQUE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_REPLAY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_GENERATION_ID = re.compile(r"now_[0-9a-f]{64}")
_ITEM_FIELDS = frozenset({"title", "source_ref", "priority", "privacy_tier"})
_PROJECTION_FIELDS = frozenset({"focus", "queue", "life_os", "messages"})
_RECEIPT_FIELDS = frozenset(
    {
        "job_id",
        "replay_key",
        "request_digest_sha256",
        "effect",
        "effect_digest_sha256",
        "records",
        "review_item_ids",
        "approval_bindings",
        "parameters",
    }
)
_POINTER_FIELDS = frozenset({"version", "effect_digest_sha256"})
_RESERVATION_FIELDS = frozenset(
    {
        "version",
        "receipt",
        "projection",
        "generation_id",
        "work_item_count",
        "filtered_item_count",
    }
)
_MAX_POINTER_BYTES = 4 * 1024
_MAX_RESERVATION_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class SharedWriterAuthority:
    scope: LockScope

    def __post_init__(self) -> None:
        if self.scope is not LockScope.SHARED_WRITER:
            raise WriterJobError("shared writer authority mismatch")


@dataclass(frozen=True, slots=True)
class NowProjectionSnapshot:
    projection: NowProjectionInput
    generation_id: str
    work_item_count: int
    filtered_item_count: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.projection, NowProjectionInput)
            or not isinstance(self.generation_id, str)
            or _GENERATION_ID.fullmatch(self.generation_id) is None
            or not isinstance(self.work_item_count, int)
            or isinstance(self.work_item_count, bool)
            or self.work_item_count < 0
            or not isinstance(self.filtered_item_count, int)
            or isinstance(self.filtered_item_count, bool)
            or self.filtered_item_count < 0
        ):
            raise WriterJobError("invalid NOW projection snapshot")
        body, work_item_count, filtered_item_count = _render_body(self.projection)
        if (
            self.generation_id != "now_" + _sha256_utf8(body)
            or self.work_item_count != work_item_count
            or self.filtered_item_count != filtered_item_count
        ):
            raise WriterJobError("invalid NOW projection snapshot")

    @classmethod
    def from_projection(cls, projection: NowProjectionInput) -> NowProjectionSnapshot:
        if not isinstance(projection, NowProjectionInput):
            raise WriterJobError("invalid NOW projection input")
        body, work_item_count, filtered_item_count = _render_body(projection)
        return cls(
            projection=projection,
            generation_id="now_" + _sha256_utf8(body),
            work_item_count=work_item_count,
            filtered_item_count=filtered_item_count,
        )

    def prepared_effect(self) -> PreparedEffect:
        return PreparedEffect(
            effect=ScheduledEffect.NOW_PROJECTION,
            records=(
                EffectRecord(
                    record_id=self.generation_id,
                    digest_sha256=self.generation_id[4:],
                ),
            ),
            parameters=(
                EffectParameter(
                    name="filtered_item_count",
                    value=str(self.filtered_item_count),
                ),
                EffectParameter(
                    name="work_item_count",
                    value=str(self.work_item_count),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class NowRuntimeApplication:
    projection: NowProjectionInput

    def __post_init__(self) -> None:
        if not isinstance(self.projection, NowProjectionInput):
            raise WriterJobError("invalid NOW runtime application")

    def prepare(self, invocation: WriterJobInvocation) -> PreparedEffect:
        if (
            not isinstance(invocation, WriterJobInvocation)
            or invocation.job_id != "JOB-022"
            or invocation.effect is not ScheduledEffect.NOW_PROJECTION
            or invocation.approved_records
            or invocation.approval_bindings
            or invocation.apply_review_decisions is not False
        ):
            raise WriterJobError("invalid NOW runtime invocation")
        return NowProjectionSnapshot.from_projection(self.projection).prepared_effect()


class NowEffectCapability:
    effect = ScheduledEffect.NOW_PROJECTION
    local_only = True
    dry_run = False

    def __init__(
        self,
        *,
        root: Path,
        projection: NowProjectionInput,
        roots: NowRoots,
        authority: SharedWriterAuthority,
    ) -> None:
        if (
            not isinstance(root, Path)
            or not isinstance(projection, NowProjectionInput)
            or not isinstance(roots, NowRoots)
            or not isinstance(authority, SharedWriterAuthority)
        ):
            raise WriterJobError("invalid NOW effect capability")
        self.root = root
        self._snapshot = NowProjectionSnapshot.from_projection(projection)
        self._roots = roots
        self._authority = authority

    def recover(self, job_id: str, replay_key: str) -> EffectReceipt | None:
        reservation, _pointer = _paths(job_id, replay_key)
        payload = _read_payload(
            self.root,
            reservation,
            missing_ok=True,
            error_message="NOW effect reservation conflict",
        )
        if payload is None:
            return None
        receipt, snapshot = _reservation_from_bytes(payload)
        self._validate_snapshot(snapshot)
        return receipt

    def reserve(self, command: EffectCommand) -> EffectReceipt:
        self._validate_command(command)
        receipt = EffectReceipt.from_command(command)
        reservation, _pointer = _paths(command.job_id, command.replay_key)
        try:
            state = atomic_write_new(
                root=self.root,
                relative=reservation,
                data=_reservation_bytes(receipt, self._snapshot),
            )
        except DuplicateConflictError:
            raise WriterJobError("NOW effect reservation conflict") from None
        except (RootConfinementError, StorageError):
            raise WriterJobError("NOW effect reservation conflict") from None
        if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
            raise WriterJobError("NOW effect reservation conflict")
        recovered = self.recover(command.job_id, command.replay_key)
        if recovered != receipt:
            raise WriterJobError("NOW effect reservation conflict")
        return receipt

    def apply(self, command: EffectCommand, receipt: EffectReceipt) -> None:
        self._validate_command(command)
        expected = EffectReceipt.from_command(command)
        if receipt != expected or self.recover(command.job_id, command.replay_key) != receipt:
            raise WriterJobError("NOW effect reservation conflict")
        result = _build_snapshot(self._roots, self._snapshot, self._authority)
        if (
            result.generation_id != self._snapshot.generation_id
            or result.work_item_count != self._snapshot.work_item_count
            or result.filtered_item_count != self._snapshot.filtered_item_count
        ):
            raise WriterJobError("NOW durable output conflict")
        _reservation, pointer = _paths(command.job_id, command.replay_key)
        _write_applied_pointer(self.root, pointer, receipt.effect_digest_sha256)

    def read(self, receipt: EffectReceipt) -> PreparedEffect | None:
        if not isinstance(receipt, EffectReceipt) or receipt.effect is not self.effect:
            raise WriterJobError("invalid NOW effect receipt")
        reservation, pointer = _paths(receipt.job_id, receipt.replay_key)
        payload = _read_payload(
            self.root,
            reservation,
            missing_ok=False,
            error_message="NOW effect reservation conflict",
        )
        assert payload is not None
        stored_receipt, snapshot = _reservation_from_bytes(payload)
        self._validate_snapshot(snapshot)
        if stored_receipt != receipt:
            raise WriterJobError("NOW effect reservation conflict")
        pointer_payload = _read_payload(
            self.root,
            pointer,
            missing_ok=True,
            error_message="NOW applied pointer conflict",
        )
        if pointer_payload is None:
            return None
        if _pointer_from_bytes(pointer_payload) != receipt.effect_digest_sha256:
            raise WriterJobError("NOW applied pointer conflict")
        if _observe_now_generation(self._roots) != snapshot.generation_id:
            raise WriterJobError("NOW durable read-back conflict")
        return snapshot.prepared_effect()

    def _validate_command(self, command: EffectCommand) -> None:
        if (
            not isinstance(command, EffectCommand)
            or command.job_id != "JOB-022"
            or command.prepared != self._snapshot.prepared_effect()
        ):
            raise WriterJobError("invalid NOW effect command")

    def _validate_snapshot(self, snapshot: NowProjectionSnapshot) -> None:
        if snapshot != self._snapshot:
            raise WriterJobError("NOW effect replay conflict")


class _NoopSharedWriterContext:
    def __enter__(self) -> None:
        return None

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        tb: object,
    ) -> Literal[False]:
        return False


class _NoopSharedWriterLease:
    def __init__(self, authority: SharedWriterAuthority) -> None:
        self._authority = authority

    def acquire(self, scope: LockScope) -> _NoopSharedWriterContext:
        if (
            self._authority.scope is not LockScope.SHARED_WRITER
            or scope is not LockScope.SHARED_WRITER
        ):
            raise WriterJobError("shared writer authority mismatch")
        return _NoopSharedWriterContext()


def _build_snapshot(
    roots: NowRoots,
    snapshot: NowProjectionSnapshot,
    authority: SharedWriterAuthority,
) -> NowBuildResult:
    try:
        return build_now(
            target=DeploymentTarget.CANONICAL_WRITER,
            host_role=HostRole.WRITER,
            roots=roots,
            lease=_NoopSharedWriterLease(authority),
            projection=snapshot.projection,
        )
    except Exception:
        raise WriterJobError("NOW durable output conflict") from None


def _observe_now_generation(roots: NowRoots) -> str:
    try:
        check = check_now(target=DeploymentTarget.CANONICAL_WRITER, roots=roots)
    except Exception:
        raise WriterJobError("NOW durable read-back conflict") from None
    if (
        check.available is not True
        or check.marker_valid is not True
        or not isinstance(check.generation_id, str)
        or _GENERATION_ID.fullmatch(check.generation_id) is None
    ):
        raise WriterJobError("NOW durable read-back conflict")
    body = _render_body_from_path(check.output_path)
    if "now_" + _sha256_utf8(body) != check.generation_id:
        raise WriterJobError("NOW durable read-back conflict")
    return check.generation_id


def _paths(job_id: str, replay_key: str) -> tuple[PurePosixPath, PurePosixPath]:
    identity = _sha256_bytes(
        canonical_json_bytes({"job_id": job_id, "replay_key": replay_key})
    )
    base = PurePosixPath("operations/effects/now") / identity
    return base.with_suffix(".json"), base.with_suffix(".applied.json")


def _reservation_bytes(receipt: EffectReceipt, snapshot: NowProjectionSnapshot) -> bytes:
    return canonical_json_bytes(
        {
            "version": 1,
            "receipt": _receipt_to_dict(receipt),
            "projection": _projection_to_dict(snapshot.projection),
            "generation_id": snapshot.generation_id,
            "work_item_count": snapshot.work_item_count,
            "filtered_item_count": snapshot.filtered_item_count,
        }
    )


def _reservation_from_bytes(payload: bytes) -> tuple[EffectReceipt, NowProjectionSnapshot]:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_RESERVATION_BYTES:
        raise WriterJobError("invalid NOW effect reservation")
    try:
        value = json.loads(payload.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != _RESERVATION_FIELDS
            or value["version"] != 1
            or not isinstance(value["receipt"], dict)
            or set(value["receipt"]) != _RECEIPT_FIELDS
            or not isinstance(value["projection"], dict)
            or set(value["projection"]) != _PROJECTION_FIELDS
        ):
            raise ValueError
        receipt = _receipt_from_dict(value["receipt"])
        snapshot = NowProjectionSnapshot(
            projection=_projection_from_dict(value["projection"]),
            generation_id=_require_generation_id(value["generation_id"]),
            work_item_count=_require_count(value["work_item_count"]),
            filtered_item_count=_require_count(value["filtered_item_count"]),
        )
        if _reservation_bytes(receipt, snapshot) != payload:
            raise ValueError
        return receipt, snapshot
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("invalid NOW effect reservation") from None


def _receipt_to_dict(receipt: EffectReceipt) -> dict[str, object]:
    return {
        "job_id": receipt.job_id,
        "replay_key": receipt.replay_key,
        "request_digest_sha256": receipt.request_digest_sha256,
        "effect": receipt.effect.value,
        "effect_digest_sha256": receipt.effect_digest_sha256,
        "records": [record.to_dict() for record in receipt.records],
        "review_item_ids": list(receipt.review_item_ids),
        "approval_bindings": [],
        "parameters": [parameter.to_dict() for parameter in receipt.parameters],
    }


def _receipt_from_dict(value: dict[str, object]) -> EffectReceipt:
    if value["approval_bindings"] != []:
        raise ValueError
    return EffectReceipt(
        job_id=_require_opaque_id(value["job_id"]),
        replay_key=_require_replay_key(value["replay_key"]),
        request_digest_sha256=_require_sha256(value["request_digest_sha256"]),
        effect=_require_effect(value["effect"]),
        effect_digest_sha256=_require_sha256(value["effect_digest_sha256"]),
        records=tuple(_record_from_dict(item) for item in _require_list(value["records"])),
        review_item_ids=tuple(
            _require_opaque_id(item) for item in _require_list(value["review_item_ids"])
        ),
        approval_bindings=(),
        parameters=tuple(
            EffectParameter(
                name=_require_parameter_name(item["name"]),
                value=_require_parameter_value(item["value"]),
            )
            for item in _require_parameter_dicts(value["parameters"])
        ),
    )


def _record_from_dict(value: object) -> EffectRecord:
    if (
        not isinstance(value, dict)
        or set(value) != {"record_id", "digest_sha256", "approval"}
        or value["approval"] is not None
    ):
        raise ValueError
    return EffectRecord(
        record_id=_require_opaque_id(value["record_id"]),
        digest_sha256=_require_sha256(value["digest_sha256"]),
    )


def _projection_to_dict(projection: NowProjectionInput) -> dict[str, object]:
    return {
        "focus": [_item_to_dict(item) for item in projection.focus],
        "queue": [_item_to_dict(item) for item in projection.queue],
        "life_os": None
        if projection.life_os is None
        else [_item_to_dict(item) for item in projection.life_os],
        "messages": None
        if projection.messages is None
        else [_item_to_dict(item) for item in projection.messages],
    }


def _projection_from_dict(value: dict[str, object]) -> NowProjectionInput:
    return NowProjectionInput(
        focus=tuple(_item_from_dict(item) for item in _require_list(value["focus"])),
        queue=tuple(_item_from_dict(item) for item in _require_list(value["queue"])),
        life_os=_optional_items(value["life_os"]),
        messages=_optional_items(value["messages"]),
    )


def _item_to_dict(item: NowItem) -> dict[str, object]:
    return {
        "title": item.title,
        "source_ref": item.source_ref,
        "priority": item.priority,
        "privacy_tier": item.privacy_tier.value,
    }


def _item_from_dict(value: object) -> NowItem:
    if not isinstance(value, dict) or set(value) != _ITEM_FIELDS:
        raise ValueError
    return NowItem(
        title=value["title"],
        source_ref=value["source_ref"],
        priority=value["priority"],
        privacy_tier=_require_privacy_tier(value["privacy_tier"]),
    )


def _optional_items(value: object) -> tuple[NowItem, ...] | None:
    if value is None:
        return None
    return tuple(_item_from_dict(item) for item in _require_list(value))


def _write_applied_pointer(root: Path, relative: PurePosixPath, effect_digest_sha256: str) -> None:
    try:
        state = atomic_write_new(
            root=root,
            relative=relative,
            data=canonical_json_bytes(
                {"version": 1, "effect_digest_sha256": effect_digest_sha256}
            ),
        )
    except DuplicateConflictError:
        raise WriterJobError("NOW applied pointer conflict") from None
    except (RootConfinementError, StorageError):
        raise WriterJobError("NOW applied pointer conflict") from None
    if state not in {WriteState.CREATED, WriteState.ALREADY_EXISTS}:
        raise WriterJobError("NOW applied pointer conflict")


def _pointer_from_bytes(payload: bytes) -> str:
    if not isinstance(payload, bytes) or not payload or len(payload) > _MAX_POINTER_BYTES:
        raise WriterJobError("NOW applied pointer conflict")
    try:
        value = json.loads(payload.decode("utf-8"))
        if (
            not isinstance(value, dict)
            or set(value) != _POINTER_FIELDS
            or value["version"] != 1
        ):
            raise ValueError
        effect_digest = _require_sha256(value["effect_digest_sha256"])
        if (
            canonical_json_bytes(
                {"version": 1, "effect_digest_sha256": effect_digest}
            )
            != payload
        ):
            raise ValueError
        return effect_digest
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        raise WriterJobError("NOW applied pointer conflict") from None


def _read_payload(
    root: Path,
    relative: PurePosixPath,
    *,
    missing_ok: bool,
    error_message: str,
) -> bytes | None:
    try:
        payload = read_confined(root=root, relative=relative)
    except (RootConfinementError, StorageError):
        raise WriterJobError(error_message) from None
    if payload is None and not missing_ok:
        raise WriterJobError(error_message)
    return payload


def _render_body(projection: NowProjectionInput) -> tuple[str, int, int]:
    from .now import _render_body as render_body

    return render_body(projection)


def _render_body_from_path(path: Path) -> str:
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise WriterJobError("NOW durable read-back conflict") from None
    marker = "<!-- open-brain-now-generation:"
    if not payload.startswith("# NOW\n\n") or marker not in payload:
        raise WriterJobError("NOW durable read-back conflict")
    head, tail = payload.split("-->\n\n", 1)
    if marker not in head:
        raise WriterJobError("NOW durable read-back conflict")
    return "# NOW\n\n" + tail


def _require_effect(value: object) -> ScheduledEffect:
    if value != ScheduledEffect.NOW_PROJECTION.value:
        raise ValueError
    return ScheduledEffect.NOW_PROJECTION


def _require_privacy_tier(value: object) -> PrivacyTier:
    if not isinstance(value, str):
        raise ValueError
    return PrivacyTier(value)


def _require_opaque_id(value: object) -> str:
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise ValueError
    return value


def _require_replay_key(value: object) -> str:
    if not isinstance(value, str) or _REPLAY_KEY.fullmatch(value) is None:
        raise ValueError
    return value


def _require_sha256(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError
    return value


def _require_generation_id(value: object) -> str:
    if not isinstance(value, str) or _GENERATION_ID.fullmatch(value) is None:
        raise ValueError
    return value


def _require_count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError
    return value


def _require_list(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError
    return value


def _require_parameter_dicts(value: object) -> list[dict[str, object]]:
    raw = _require_list(value)
    result: list[dict[str, object]] = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != {"name", "value"}:
            raise ValueError
        result.append(item)
    return result


def _require_parameter_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _require_parameter_value(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return value


def _sha256_utf8(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    from hashlib import sha256

    return sha256(value).hexdigest()
