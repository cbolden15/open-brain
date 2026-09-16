"""One durable preview/apply path for manual and scheduled priority sources."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Protocol, cast

from open_brain_engine.engine import PrivacyDecision, ReferencePayload

from open_brain_connectors.runtime.live_common import (
    LiveBatch,
    LiveSourceError,
    bounded_json,
    local_source_privacy,
)
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.source_host import existing_source_profile
from open_brain_connectors.runtime.source_intake import SourceRecordIntake, SourceRecordKey
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

_PROVIDERS = {
    "gmail": "mail_label",
    "google_drive": "drive_file",
    "slack": "channel",
    "agent_session": "local_project",
}
_MAX_COMMITTED = 2048


class LiveRuntime(Protocol):
    def acknowledge(
        self, selection: SourceResourceSelection, checkpoint: dict[str, object]
    ) -> None: ...

    def fetch(
        self,
        selection: SourceResourceSelection,
        options: dict[str, object],
        checkpoint: dict[str, object] | None,
    ) -> LiveBatch: ...


def _digest(value: object) -> str:
    return hashlib.sha256(bounded_json(value, 4_194_304)).hexdigest()


def _binding(root: Path) -> str:
    try:
        profile = existing_source_profile(root)
    except Exception:
        raise LiveSourceError("source_brain_unavailable") from None
    return _digest([str(profile.root), profile.root_identity, profile.tenant_id])


def _source_name(source_id: str) -> str:
    if type(source_id) is not str or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9._:-]{0,95}", source_id
    ):
        raise LiveSourceError("source_invalid_id")
    return "source-" + hashlib.sha256(source_id.encode()).hexdigest() + ".json"


def _selection(value: object) -> SourceResourceSelection:
    if not isinstance(value, dict) or set(value) != {
        "connector_name",
        "connection_id",
        "resource_id",
        "resource_type",
    }:
        raise LiveSourceError("source_invalid_selection")
    try:
        result = SourceResourceSelection(**value)
        if _PROVIDERS.get(result.connector_name) != result.resource_type:
            raise ValueError
        return result
    except TypeError, ValueError:
        raise LiveSourceError("source_invalid_selection") from None


def _intake_to_dict(intake: SourceRecordIntake) -> dict[str, object]:
    return {
        "key": asdict(intake.key),
        "url": intake.url,
        "text": intake.text,
        "title": intake.title,
        "privacy": intake.privacy.to_dict(),
    }


def _intake_from_dict(value: object) -> SourceRecordIntake:
    if not isinstance(value, dict) or set(value) != {"key", "url", "text", "title", "privacy"}:
        raise LiveSourceError("source_invalid_preview")
    try:
        result = SourceRecordIntake(
            key=SourceRecordKey(**value["key"]),
            url=value["url"],
            text=value["text"],
            title=value["title"],
            privacy=PrivacyDecision.from_dict(value["privacy"]),
        )
        if result.privacy != local_source_privacy():
            raise ValueError
        return result
    except TypeError, ValueError, KeyError:
        raise LiveSourceError("source_invalid_preview") from None


class LiveCaptureService:
    """Private source state, cancellation and acknowledged provider checkpoints.

    The existing collector process owns scheduling and its process lease. This
    service owns the new source records; both foreground imports and that process
    call apply(). A per-source run lock prevents duplicate fetch/apply, while a
    short state lock makes pause acknowledgement a real capture barrier.
    """

    def __init__(
        self,
        root: Path,
        brain_root: Path,
        *,
        runtime: LiveRuntime,
        sink: Callable[[SourceRecordIntake], None] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._brain_root = brain_root
        self._brain_binding = _binding(brain_root)
        self._store = PrivateJsonStore(root)
        self._runtime = runtime
        self._sink = sink
        self._clock = clock or (lambda: int(time.time()))
        with self._store.lock("state"):
            marker = self._store.read("brain.json")
            expected = {"schema_version": 1, "binding": self._brain_binding}
            if marker is None:
                self._store.write("brain.json", expected)
            elif marker != expected:
                raise LiveSourceError("source_brain_mismatch")

    def _check_brain(self) -> None:
        if _binding(self._brain_root) != self._brain_binding:
            raise LiveSourceError("source_brain_mismatch")

    def _load(self, source_id: str) -> dict[str, object]:
        self._check_brain()
        value = self._store.read(_source_name(source_id))
        keys = {
            "schema_version",
            "source_id",
            "binding",
            "selection",
            "options",
            "generation",
            "status",
            "control_epoch",
            "interval_seconds",
            "next_run_epoch",
            "checkpoint",
            "committed",
            "pending",
            "last_run",
            "pause_ack_epoch",
        }
        if (
            not isinstance(value, dict)
            or set(value) != keys
            or value["schema_version"] != 1
            or value["source_id"] != source_id
            or value["binding"] != self._brain_binding
            or type(value["status"]) is not str
            or value["status"] not in {"disabled", "enabled", "paused"}
            or type(value["control_epoch"]) is not int
            or type(value["interval_seconds"]) is not int
            or not 1 <= value["interval_seconds"] <= 31_536_000
            or not isinstance(value["options"], dict)
            or not isinstance(value["committed"], dict)
            or any(
                type(key) is not str or type(item) is not str
                for key, item in value["committed"].items()
            )
            or any(
                value[key] is not None and type(value[key]) is not int
                for key in ("next_run_epoch", "pause_ack_epoch")
            )
            or (value["status"] == "enabled" and value["next_run_epoch"] is None)
            or (value["last_run"] is not None and not isinstance(value["last_run"], dict))
            or (
                value["pending"] is not None
                and (
                    type(value["pending"]) is not str
                    or not re.fullmatch(r"preview:[0-9a-f]{64}", value["pending"])
                )
            )
            or (value["checkpoint"] is not None and not isinstance(value["checkpoint"], dict))
        ):
            raise LiveSourceError("source_invalid_state")
        _selection(value["selection"])
        if value["generation"] != _digest([value["selection"], value["options"], value["binding"]]):
            raise LiveSourceError("source_invalid_state")
        return cast(dict[str, object], value)

    def _save(self, entry: dict[str, object]) -> None:
        self._store.write(_source_name(cast(str, entry["source_id"])), entry)

    def configure(
        self,
        source_id: str,
        selection: SourceResourceSelection,
        options: dict[str, object],
        *,
        reset: bool = False,
        disable: bool = False,
    ) -> dict[str, object]:
        self._check_brain()
        selected = _selection(asdict(selection))
        if type(options) is not dict or any(
            word in str(key).lower() for key in options for word in ("token", "secret", "password")
        ):
            raise LiveSourceError("source_invalid_options")
        bounded_json(options, 16_384)
        generation = _digest([asdict(selected), options, self._brain_binding])
        with self._store.lock("state"):
            existing = self._store.read(_source_name(source_id))
            epoch = 0
            old_pending = None
            if existing is not None:
                old = self._load(source_id)
                if old["generation"] == generation:
                    if disable:
                        old_pending = old["pending"]
                        old.update(
                            status="disabled",
                            next_run_epoch=None,
                            pending=None,
                            pause_ack_epoch=None,
                            control_epoch=cast(int, old["control_epoch"]) + 1,
                        )
                        self._save(old)
                        if isinstance(old_pending, str):
                            self._store.delete(old_pending.replace(":", "-") + ".json")
                    return self._summary(old)
                if not reset:
                    raise LiveSourceError("source_selection_reset_required")
                epoch = cast(int, old["control_epoch"]) + 1
                old_pending = old["pending"]
            entry: dict[str, object] = {
                "schema_version": 1,
                "source_id": source_id,
                "binding": self._brain_binding,
                "selection": asdict(selected),
                "options": options,
                "generation": generation,
                "status": "disabled",
                "control_epoch": epoch,
                "interval_seconds": 900,
                "next_run_epoch": None,
                "checkpoint": None,
                "committed": {},
                "pending": None,
                "last_run": None,
                "pause_ack_epoch": None,
            }
            self._save(entry)
            if isinstance(old_pending, str):
                self._store.delete(old_pending.replace(":", "-") + ".json")
            return self._summary(entry)

    def status(self, source_id: str | None = None) -> dict[str, object]:
        self._check_brain()
        if source_id is not None:
            return self._summary(self._load(source_id))
        sources = []
        for name in self._store.names("source-"):
            value = self._store.read(name)
            if not isinstance(value, dict) or type(value.get("source_id")) is not str:
                raise LiveSourceError("source_invalid_state")
            sources.append(self._summary(self._load(value["source_id"])))
        return {"schema_version": 1, "sources": sources}

    @staticmethod
    def _summary(entry: Mapping[str, object]) -> dict[str, object]:
        return {
            key: entry[key]
            for key in (
                "source_id",
                "selection",
                "options",
                "status",
                "interval_seconds",
                "next_run_epoch",
                "pause_ack_epoch",
                "last_run",
            )
        }

    def control(
        self, source_id: str, action: str, *, interval_seconds: int | None = None
    ) -> dict[str, object]:
        if action not in {"enable", "pause", "resume", "disable", "schedule", "sync_now"}:
            raise LiveSourceError("source_invalid_control")
        if interval_seconds is not None and (
            type(interval_seconds) is not int or not 1 <= interval_seconds <= 31_536_000
        ):
            raise LiveSourceError("source_invalid_interval")
        with self._store.lock("state"):
            entry = self._load(source_id)
            if action in {"enable", "resume"}:
                entry.update(status="enabled", pause_ack_epoch=None, next_run_epoch=self._clock())
            elif action == "pause":
                entry.update(status="paused", pause_ack_epoch=self._clock(), next_run_epoch=None)
            elif action == "disable":
                entry.update(status="disabled", pause_ack_epoch=None, next_run_epoch=None)
            elif action == "sync_now":
                if entry["status"] != "enabled":
                    raise LiveSourceError("source_not_enabled")
                entry["next_run_epoch"] = self._clock()
            elif interval_seconds is None:
                raise LiveSourceError("source_invalid_interval")
            if interval_seconds is not None:
                entry["interval_seconds"] = interval_seconds
            # A disable or pause cancels any import already in progress, including manual imports.
            if action in {"disable", "pause"}:
                entry["control_epoch"] = cast(int, entry["control_epoch"]) + 1
                pending = entry["pending"]
                entry["pending"] = None
                self._save(entry)
                if isinstance(pending, str):
                    self._store.delete(pending.replace(":", "-") + ".json")
            self._save(entry)
            return self._summary(entry)

    def _pending(self, entry: Mapping[str, object]) -> dict[str, object] | None:
        pending = entry["pending"]
        if pending is None:
            return None
        if type(pending) is not str or not re.fullmatch(r"preview:[0-9a-f]{64}", pending):
            raise LiveSourceError("source_invalid_preview")
        value = self._store.read(pending.replace(":", "-") + ".json")
        if (
            not isinstance(value, dict)
            or value.get("generation") != entry["generation"]
            or value.get("binding") != self._brain_binding
            or value.get("source_id") != entry["source_id"]
            or value.get("before") != _digest(entry["checkpoint"])
            or "preview_id" not in value
        ):
            raise LiveSourceError("source_stale_preview")
        content = {key: item for key, item in value.items() if key != "preview_id"}
        if pending != "preview:" + _digest(content) or value["preview_id"] != pending:
            raise LiveSourceError("source_invalid_preview")
        return cast(dict[str, object], value)

    def preview(self, source_id: str) -> dict[str, object]:
        with self._store.lock("run-" + _source_name(source_id), timeout_seconds=0):
            entry = self._load(source_id)
            pending = self._pending(entry)
            if pending is None:
                self._acknowledge(entry)
                selected = _selection(entry["selection"])
                batch = self._runtime.fetch(
                    selected,
                    cast(dict[str, object], entry["options"]),
                    cast(dict[str, object] | None, entry["checkpoint"]),
                )
                if any(
                    item.key.connector_name != selected.connector_name
                    or item.key.connection_id != selected.connection_id
                    or item.key.resource_id != selected.resource_id
                    or item.privacy != local_source_privacy()
                    for item in batch.intakes
                ):
                    raise LiveSourceError("source_selection_mismatch")
                pending = {
                    "schema_version": 1,
                    "source_id": source_id,
                    "binding": self._brain_binding,
                    "generation": entry["generation"],
                    "before": _digest(entry["checkpoint"]),
                    "intakes": [_intake_to_dict(item) for item in batch.intakes],
                    "checkpoint": batch.checkpoint,
                    "has_more": batch.has_more,
                    "notices": list(batch.notices),
                }
                preview_id = "preview:" + _digest(pending)
                pending["preview_id"] = preview_id
                with self._store.lock("state"):
                    latest = self._load(source_id)
                    if (
                        latest["generation"] != entry["generation"]
                        or latest["control_epoch"] != entry["control_epoch"]
                    ):
                        raise LiveSourceError("source_changed_during_fetch")
                    self._store.write(preview_id.replace(":", "-") + ".json", pending)
                    latest["pending"] = preview_id
                    self._save(latest)
            values = cast(list[object], pending["intakes"])
            intakes = [_intake_from_dict(item) for item in values]
            return {
                "source_id": source_id,
                "preview_id": pending["preview_id"],
                "count": len(intakes),
                "has_more": pending["has_more"],
                "notices": pending["notices"],
                "records": [
                    {"title": item.title, "source_reference": item.url, "trust": "unverified"}
                    for item in intakes
                ],
            }

    def _acknowledge(self, entry: Mapping[str, object]) -> None:
        checkpoint = entry["checkpoint"]
        if isinstance(checkpoint, dict):
            self._runtime.acknowledge(_selection(entry["selection"]), checkpoint)

    def _submit(self, intake: SourceRecordIntake) -> bool:
        if self._sink is not None:
            self._sink(intake)
            return False
        from open_brain_collector.runner import collector_capture_sink

        # Metadata-only provider revisions must also change the engine's content digest.
        text = f"Source revision: {intake.key.revision_identity()}\n\n{intake.text}"
        if len(text) > 65_536:
            raise LiveSourceError("source_content_too_large")
        sink = collector_capture_sink(self._brain_root)
        kwargs = intake.capture_kwargs()
        kwargs["payload"] = ReferencePayload(url=intake.url, supplied_text=text)
        return sink.submit(**kwargs).duplicate  # type: ignore[arg-type]

    def apply(
        self, source_id: str, preview_id: str, *, automatic: bool = False
    ) -> dict[str, object]:
        with self._store.lock("run-" + _source_name(source_id), timeout_seconds=0):
            entry = self._load(source_id)
            last_run = entry["last_run"]
            if (
                isinstance(last_run, dict)
                and last_run.get("preview_id") == preview_id
                and last_run.get("outcome") == "completed"
                and entry["pending"] is None
            ):
                self._acknowledge(entry)
                return {"source_id": source_id, **last_run}
            pending = self._pending(entry)
            if pending is None or pending["preview_id"] != preview_id:
                raise LiveSourceError("source_stale_preview")
            intakes = tuple(
                _intake_from_dict(item) for item in cast(list[object], pending["intakes"])
            )
            protected = {item.key.delivery_id() for item in intakes}
            captured = duplicates = 0
            for intake in intakes:
                with self._store.lock("state"):
                    latest = self._load(source_id)
                    self._require_current(entry, latest, automatic)
                    committed = cast(dict[str, str], latest["committed"])
                    delivery, revision = intake.key.delivery_id(), intake.key.revision_identity()
                    if committed.get(delivery) == revision:
                        duplicates += 1
                        continue
                    duplicate = self._submit(intake)
                    committed[delivery] = revision
                    if len(committed) > _MAX_COMMITTED:
                        # This is an acceleration cache. The engine remains the
                        # durable duplicate authority after historical cache eviction.
                        keep = protected | set(
                            sorted(set(committed) - protected)[-(_MAX_COMMITTED - len(protected)) :]
                        )
                        latest["committed"] = {
                            key: value for key, value in committed.items() if key in keep
                        }
                    captured += int(not duplicate)
                    duplicates += int(duplicate)
                    self._save(latest)
            with self._store.lock("state"):
                latest = self._load(source_id)
                self._require_current(entry, latest, automatic)
                last: dict[str, object] = {
                    "preview_id": preview_id,
                    "outcome": "completed",
                    "captured_count": captured,
                    "duplicate_count": duplicates,
                    "finished_epoch": self._clock(),
                    "has_more": pending["has_more"],
                    "notices": pending["notices"],
                    "failure_code": None,
                }
                latest.update(checkpoint=pending["checkpoint"], pending=None, last_run=last)
                if latest["status"] == "enabled":
                    latest["next_run_epoch"] = self._clock() + (
                        1 if pending["has_more"] else cast(int, latest["interval_seconds"])
                    )
                self._save(latest)
                self._store.delete(preview_id.replace(":", "-") + ".json")
                try:
                    self._acknowledge(latest)
                except LiveSourceError, OSError:
                    # Capture and checkpoint are already durable. Cleanup is retried
                    # before the next fetch; it must not turn this into a failed import.
                    last["notices"] = [
                        *cast(list[str], last["notices"]),
                        "source_queue_cleanup_pending",
                    ]
                    self._save(latest)
                return {"source_id": source_id, **last}

    @staticmethod
    def _require_current(
        before: Mapping[str, object], current: Mapping[str, object], automatic: bool
    ) -> None:
        if (
            current["generation"] != before["generation"]
            or current["control_epoch"] != before["control_epoch"]
            or current["pending"] != before["pending"]
            or (automatic and current["status"] != "enabled")
        ):
            raise LiveSourceError("source_import_cancelled")

    def sync_due(self) -> list[dict[str, object]]:
        results = []
        for summary in cast(list[dict[str, object]], self.status()["sources"]):
            if (
                summary["status"] != "enabled"
                or cast(int, summary["next_run_epoch"]) > self._clock()
            ):
                continue
            source_id = cast(str, summary["source_id"])
            try:
                preview = self.preview(source_id)
                results.append(
                    self.apply(source_id, cast(str, preview["preview_id"]), automatic=True)
                )
            except Exception as error:
                code = error.code if isinstance(error, LiveSourceError) else "source_capture_failed"
                retry = error.retry_after_seconds if isinstance(error, LiveSourceError) else None
                last = {"outcome": "failed", "failure_code": code, "finished_epoch": self._clock()}
                with self._store.lock("state"):
                    entry = self._load(source_id)
                    entry["last_run"] = last
                    if entry["status"] == "enabled":
                        entry["next_run_epoch"] = self._clock() + (retry or 60)
                    self._save(entry)
                results.append({"source_id": source_id, **last})
        return results
