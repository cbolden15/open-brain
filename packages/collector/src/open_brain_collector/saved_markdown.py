"""Collector bridge for the reusable saved-Markdown selected-root adapter."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
from typing import cast

from open_brain_engine.core.ids import portable_canonical_json_bytes
from open_brain_engine.engine import SourceRevisionDeliveryReceipt

from open_brain_collector.custody import intake_dict, intake_from_dict
from open_brain_collector.lifecycle import (
    CollectorRunPage,
    SavedMarkdownBaseline,
    SavedMarkdownDelivery,
)
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.live_common import LiveSourceError, bounded_json
from open_brain_connectors.runtime.live_storage import PrivateJsonStore
from open_brain_connectors.runtime.saved_markdown import (
    SavedMarkdownAbsenceCandidate,
    SavedMarkdownRootAdapter,
    SavedMarkdownScan,
    SavedMarkdownScanEpoch,
    SavedMarkdownScanPage,
)
from open_brain_connectors.runtime.source_registry import SourceResourceSelection

__all__ = ["SavedMarkdownCollectorRuntime"]


class SavedMarkdownCollectorRuntime:
    """Expose one caller-bound saved-Markdown scan to collector custody.

    Refusals stay local in the returned scan. They are never turned into an
    intake, and an incomplete scan carries no disappearance semantics.
    """

    def __init__(
        self, adapter: SavedMarkdownRootAdapter, *, store: PrivateJsonStore | None = None,
        selection_generation: str = "initial",
    ) -> None:
        if not isinstance(adapter, SavedMarkdownRootAdapter):
            raise ConnectorContractError("invalid saved markdown runtime")
        self._adapter = adapter
        self._store = store
        self._generation = selection_generation
        self._name = "saved-scan-" + sha256(portable_canonical_json_bytes(
            asdict(adapter.selection))).hexdigest() + ".json"
        self._memory: dict[str, object] | None = None
        self.last_scan: SavedMarkdownScan | None = None

    def _load(self) -> dict[str, object]:
        value = self._store.read(self._name) if self._store is not None else self._memory
        if value is None:
            return {"epoch": None, "page": None, "known": {}, "absence": [], "recovery": None}
        if not isinstance(value, dict) or set(value) not in (
            {"epoch", "page", "known", "absence"},
            {"epoch", "page", "known", "absence", "recovery"},
        ):
            raise ConnectorContractError("invalid saved markdown scan state")
        state = dict(value)
        state.setdefault("recovery", None)
        recovery = state["recovery"]
        if recovery is not None and (
            not isinstance(recovery, dict)
            or set(recovery) != {"request_sha256", "epoch_sha256"}
            or any(type(item) is not str or len(item) != 64
                   or any(character not in "0123456789abcdef" for character in item)
                   for item in recovery.values())
        ):
            raise ConnectorContractError("invalid saved markdown recovery state")
        return state

    def _save(self, value: dict[str, object]) -> None:
        if self._store is not None:
            self._store.write(self._name, value)
        else:
            self._memory = value

    def acknowledge_page(self) -> None:
        state = self._load()
        state["page"] = None
        self._save(state)

    def validate_page_checkpoint(self) -> None:
        """Refuse failed inventory, including a cached page after restart."""
        state = self._load()
        page = state["page"]
        if state["epoch"] is None or not isinstance(page, dict):
            raise LiveSourceError("collector_scan_incomplete")
        epoch = SavedMarkdownScanEpoch.from_value(state["epoch"])
        if epoch.state["errors"] or (not epoch.state["complete"] and page["next_cursor"] is None):
            raise LiveSourceError("collector_scan_incomplete")

    def restart_failed_inventory(
        self, *, selection: SourceResourceSelection, cursor: str | None,
        retained_intakes: tuple[object, ...], recovery_id: str,
    ) -> None:
        """Discard failed inventory only after the controller retains its exact page.

        Known terminal sources remain unchanged. No absence, checkpoint success,
        capture or independent-protection grant is inferred from this reset.
        """
        from open_brain_connectors.runtime.source_intake import SourceRecordIntake

        if (selection != self._adapter.selection or type(recovery_id) is not str
                or len(recovery_id) != 64
                or any(character not in "0123456789abcdef" for character in recovery_id)
                or not isinstance(retained_intakes, tuple) or len(retained_intakes) > 25
                or any(type(item) is not SourceRecordIntake for item in retained_intakes)):
            raise ConnectorContractError("invalid saved markdown recovery")
        intakes = cast(tuple[SourceRecordIntake, ...], retained_intakes)
        request_sha = sha256(bounded_json({
            "selection": asdict(selection), "cursor": cursor, "recovery_id": recovery_id,
            "intakes": [intake_dict(item) for item in intakes],
        }, 4_194_304)).hexdigest()
        state = self._load()
        recovery = state["recovery"]
        if isinstance(recovery, dict) and recovery["request_sha256"] == request_sha:
            if state["epoch"] is not None or state["page"] is not None:
                raise LiveSourceError("collector_recovery_stale")
            return
        page = state["page"]
        if state["epoch"] is None or not isinstance(page, dict):
            raise LiveSourceError("collector_recovery_stale")
        epoch = SavedMarkdownScanEpoch.from_value(state["epoch"])
        if not epoch.state["errors"] and (
            epoch.state["complete"] or page["next_cursor"] is not None
        ):
            raise LiveSourceError("collector_scan_not_failed")
        cached_intakes = tuple(intake_from_dict(value)
                               for value in cast(list[object], page["intakes"]))
        if page["cursor"] != cursor or cached_intakes != intakes:
            raise LiveSourceError("collector_incomplete_custody")
        state["recovery"] = {
            "request_sha256": request_sha,
            "epoch_sha256": sha256(bounded_json(epoch.value(), 4_194_304)).hexdigest(),
        }
        state["epoch"], state["page"], state["absence"] = None, None, []
        self._save(state)

    def record_terminal(self, intake: object, receipt: object) -> None:
        lifecycle_version = 0
        if isinstance(receipt, SavedMarkdownBaseline):
            lifecycle_version = receipt.result.source_cas.expected_lifecycle_version
            source_id, capture_id = receipt.result.source_id, receipt.result.capture_id
        else:
            if isinstance(receipt, SavedMarkdownDelivery):
                lifecycle_version = receipt.delivery.expected_lifecycle_version
                receipt = receipt.receipt
            if not isinstance(receipt, SourceRevisionDeliveryReceipt):
                return
            source = receipt.source_receipt
            if source is None or source.source_id is None or source.capture_id is None:
                return
            source_id, capture_id = source.source_id, source.capture_id
        from open_brain_connectors.runtime.source_intake import SourceRecordIntake

        if not isinstance(intake, SourceRecordIntake):
            raise ConnectorContractError("invalid saved markdown receipt")
        state = self._load()
        known = cast(dict[str, dict[str, object]], state["known"])
        item_id = intake.key.external_id.removeprefix("item:")
        # Lifecycle version belongs to the exact submitted envelope. The sink
        # provides it after validating the terminal receipt.
        known[item_id] = {"source_id": source_id, "head": capture_id,
                          "lifecycle_version": lifecycle_version}
        state["absence"] = [candidate for candidate in cast(list[dict[str, object]],
                                                           state["absence"])
                            if candidate["item_id"] != item_id]
        self._save(state)

    @property
    def absence_candidates(self) -> tuple[SavedMarkdownAbsenceCandidate, ...]:
        return tuple(SavedMarkdownAbsenceCandidate(
            item_id=cast(str, candidate["item_id"]), source_id=cast(str, candidate["source_id"]),
            expected_head=cast(str, candidate["expected_head"]),
            lifecycle_version=cast(int, candidate["lifecycle_version"]),
            epoch_id=cast(str, candidate["epoch_id"]),
            evidence_sha256=cast(str, candidate["evidence_sha256"]),
            destination_identity=cast(str, candidate["destination_identity"]),
            selection_generation=cast(str, candidate["selection_generation"]),
        ) for candidate in cast(list[dict[str, object]], self._load()["absence"]))

    def fetch_page(
        self,
        selection: SourceResourceSelection,
        cursor: str | None,
    ) -> CollectorRunPage:
        if selection != self._adapter.selection:
            raise ConnectorContractError("invalid saved markdown page")
        state = self._load()
        cached = state["page"]
        if isinstance(cached, dict) and cached["cursor"] == cursor:
            return CollectorRunPage(selection, tuple(intake_from_dict(value) for value in
                cast(list[object], cached["intakes"])), cast(str | None, cached["next_cursor"]))
        if cached is not None and (not isinstance(cached, dict)
                                   or cached["next_cursor"] != cursor):
            raise ConnectorContractError("invalid saved markdown continuation")
        saved = state["epoch"]
        epoch = SavedMarkdownScanEpoch.from_value(saved) if saved is not None else None
        if cursor is None and (epoch is None or epoch.state["complete"]
                               or epoch.state["errors"]):
            epoch = self._adapter.begin_epoch(generation=self._generation)
            state["absence"] = []
        expected_cursor = (epoch.epoch_id + ":" + sha256(portable_canonical_json_bytes(
            epoch.value())).hexdigest()) if epoch is not None else None
        if epoch is None or (cursor is not None and cursor != expected_cursor):
            raise ConnectorContractError("invalid saved markdown continuation")
        page = self._adapter.scan_page(epoch, generation=self._generation)
        next_cursor = None if page.complete or page.epoch.state["errors"] else (
            epoch.epoch_id + ":" + sha256(portable_canonical_json_bytes(
                page.epoch.value())).hexdigest())
        self.last_scan = SavedMarkdownScan(page.candidates, page.complete, next_cursor)
        state["epoch"] = page.epoch.value()
        present = cast(dict[str, str], page.epoch.state["seen"])
        state["absence"] = [candidate for candidate in cast(list[dict[str, object]],
                                                           state["absence"])
                            if candidate["item_id"] not in present]
        if page.complete:
            self._propose_absence(state, page)
        result = CollectorRunPage(
            selection=selection,
            intakes=tuple(
                candidate.intake for candidate in page.candidates if candidate.intake is not None
            ),
            next_cursor=next_cursor,
        )
        state["page"] = {"cursor": cursor, "next_cursor": next_cursor,
                         "intakes": [intake_dict(intake) for intake in result.intakes]}
        self._save(state)
        return result

    def _propose_absence(self, state: dict[str, object], page: SavedMarkdownScanPage) -> None:
        evidence = page.evidence_sha256
        assert evidence is not None
        seen = cast(dict[str, str], page.epoch.state["seen"])
        state["absence"] = [asdict(SavedMarkdownAbsenceCandidate(
            item_id=item_id, source_id=cast(str, known["source_id"]),
            expected_head=cast(str, known["head"]),
            lifecycle_version=cast(int, known["lifecycle_version"]),
            epoch_id=page.epoch.epoch_id, evidence_sha256=evidence,
            destination_identity=self._adapter.destination_identity,
            selection_generation=self._generation,
        )) for item_id, known in cast(dict[str, dict[str, object]], state["known"]).items()
            if item_id not in seen]
