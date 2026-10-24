"""Portable18 carries follow-up items, evidence positions and decisions; never quote text."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from open_brain_engine.core.ids import portable_canonical_json_bytes as canonical
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import TextPayload, local_schema, paging
from open_brain_engine.engine.followup_contracts import (
    DecideRequest,
    DecisionAction,
    EvidenceRef,
    EvidenceRole,
    FollowupItem,
    FollowupKind,
    ListFilter,
    OwnerKind,
    ProposeRequest,
)
from open_brain_engine.engine.followup_schema import FOLLOWUP_MIGRATION
from open_brain_engine.engine.local import BrainEngine
from open_brain_engine.engine.local_schema_catalog import LOCAL_MIGRATIONS
from open_brain_engine.engine.records import RecordProjector
from open_brain_engine.engine.t03_contracts import EffectiveAuthority
from open_brain_engine.portable.followups_v18 import FOLLOWUPS_CONTRACT, FOLLOWUPS_PATH
from open_brain_engine.portable.v1 import PortableValidationError
from open_brain_engine.portable.v17 import validated_portable_snapshot_v17
from open_brain_engine.portable.v18_catalog import PORTABLE_V18_SCHEMA_CATALOG
from open_brain_engine.portable.versioned import validated_portable_snapshot

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services.t03_adapters import owner_authority
from packages.app.tests.unit.engine.test_followups import _new_source, _revise_source

QUOTE_TEXT = "PRIVATE_SYNTHETIC_QUOTE promise to deliver the synthetic report"


def _engine(root: Path) -> BrainEngine:
    root.parent.chmod(0o700)
    return BrainEngine.open(compile_single_user_local(root, starter_spaces=()))


def _owner(engine: BrainEngine) -> EffectiveAuthority:
    return owner_authority(engine.tasks, session_id="owner-test")


def _export(engine: BrainEngine, root: Path, *, version: int = 18) -> dict[str, bytes]:
    receipt = engine.tasks.portability.export(root, export_id="export_" + str(uuid4()))
    assert receipt.schema_version == version
    return {
        path: raw
        for path, raw in validated_portable_snapshot(root).files.items()
        if path != "portable-manifest.json"
    }


def _restore(engine: BrainEngine, archive: Path, destination: Path) -> BrainEngine:
    engine.tasks.portability.import_clean(archive, destination, import_id="import_" + str(uuid4()))
    return BrainEngine.open(open_existing_single_user_local(destination))


def _capture(engine: BrainEngine, text: str) -> str:
    return engine.capture.accept(
        TextPayload(text), delivery_id="followup." + str(uuid4()), privacy_tier=PrivacyTier.WORK
    ).capture_id


def _ref(
    engine: BrainEngine, capture_id: str, role: EvidenceRole = EvidenceRole.ORIGIN
) -> EvidenceRef:
    with closing(engine._store.connect()) as connection:
        text = (
            RecordProjector(engine.profile, connection, _owner(engine))
            .source(capture_id, expected=capture_id, history=True)
            .text
        )
    end = min(23, len(text))
    return EvidenceRef(
        capture_id, capture_id, 0, end, sha256(text[:end].encode("utf-8")).hexdigest(), role
    )


def _propose(
    engine: BrainEngine, *refs: EvidenceRef, summary: str = "Synthetic summary"
) -> FollowupItem:
    return engine.followups.propose(
        ProposeRequest(
            operation_id="op-" + str(uuid4()),
            kind=FollowupKind.COMMITMENT,
            summary=summary,
            project="synthetic",
            owner=OwnerKind.SELF,
            owner_name=None,
            due=None,
            due_evidence_index=None,
            evidence=refs,
        ),
        authority=_owner(engine),
    )


def _connect(engine: BrainEngine) -> sqlite3.Connection:
    connection = sqlite3.connect(engine.profile.root / local_schema.PHASE1_STATE_DATABASE)
    connection.row_factory = sqlite3.Row
    return connection


def _rehash(root: Path) -> None:
    path = root / "portable-manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"] = [
        {"path": file.relative_to(root).as_posix(), "sha256": sha256(file.read_bytes()).hexdigest()}
        for file in sorted(root.rglob("*"))
        if file.is_file() and file != path
    ]
    path.write_bytes(canonical(manifest))


def test_empty_family_catalog_and_predecessor_reader_refusal(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    files = _export(engine, tmp_path / "archive")
    assert json.loads(files[FOLLOWUPS_PATH]) == {
        "contract": FOLLOWUPS_CONTRACT,
        "items": [],
        "evidence": [],
        "decisions": [],
    }
    assert files[FOLLOWUPS_PATH] == canonical(json.loads(files[FOLLOWUPS_PATH]))
    assert PORTABLE_V18_SCHEMA_CATALOG["migration23_sha256"] == FOLLOWUP_MIGRATION.checksum
    assert PORTABLE_V18_SCHEMA_CATALOG["state_schema_version"] == 23
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot_v17(tmp_path / "archive")


def test_export_carries_positions_and_hashes_never_quote_text(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    origin = _capture(engine, QUOTE_TEXT)
    support = _capture(engine, "Synthetic supporting evidence text for the same promise")
    refs = (_ref(engine, origin), _ref(engine, support, EvidenceRole.SUPPORTING))
    item = _propose(engine, *refs)
    files = _export(engine, tmp_path / "archive")
    payload = json.loads(files[FOLLOWUPS_PATH])
    assert payload["contract"] == FOLLOWUPS_CONTRACT
    assert [row["item_id"] for row in payload["items"]] == [item.item_id]
    exported = payload["items"][0]
    assert exported["summary"] == "Synthetic summary"
    assert exported["evidence_retracted"] is False
    assert exported["privacy_tier"] == "work"
    assert {
        (row["capture_id"], row["quote_start"], row["quote_end"], row["quote_sha256"], row["role"])
        for row in payload["evidence"]
    } == {
        (ref.capture_id, ref.quote_start, ref.quote_end, ref.quote_sha256, ref.role.value)
        for ref in refs
    }
    assert [row["evidence_id"] for row in payload["evidence"]] == sorted(
        row["evidence_id"] for row in payload["evidence"]
    )
    assert len(payload["decisions"]) >= 1
    assert [row["sequence"] for row in payload["decisions"]] == list(
        range(1, len(payload["decisions"]) + 1)
    )
    assert "PRIVATE_SYNTHETIC_QUOTE" not in files[FOLLOWUPS_PATH].decode("utf-8")
    assert "promise" not in files[FOLLOWUPS_PATH].decode("utf-8")


def test_retracted_item_exports_flagged_with_summary_omitted(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    gone = _capture(engine, QUOTE_TEXT)
    kept = _capture(engine, "Synthetic kept evidence text for the same promise")
    item = _propose(
        engine,
        _ref(engine, gone),
        _ref(engine, kept, EvidenceRole.SUPPORTING),
        summary="PRIVATE_SYNTHETIC_SUMMARY to withhold",
    )
    owner = _owner(engine)
    preview = engine.lifecycle.preview_retraction(capture_id=gone, authority=owner)
    engine.lifecycle.commit_retraction(str(preview["preview_id"]), str(uuid4()), authority=owner)
    files = _export(engine, tmp_path / "archive")
    payload = json.loads(files[FOLLOWUPS_PATH])
    exported = payload["items"][0]
    assert exported["item_id"] == item.item_id
    assert exported["evidence_retracted"] is True
    assert exported["summary"] is None
    assert len(payload["evidence"]) == 2
    assert "PRIVATE_SYNTHETIC" not in files[FOLLOWUPS_PATH].decode("utf-8")


def _followup_counts(engine: BrainEngine) -> tuple[int, int, int]:
    with _connect(engine) as connection:
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("followup_items", "followup_evidence", "followup_decisions")
        )


def test_round_trip_restores_items_and_withheld_summary_stays_withheld(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    origin = _capture(engine, QUOTE_TEXT)
    gone = _capture(engine, "PRIVATE_SYNTHETIC_GONE evidence text for another promise")
    live = _propose(engine, _ref(engine, origin))
    retracted = _propose(engine, _ref(engine, gone), summary="PRIVATE_SYNTHETIC_SUMMARY")
    owner = _owner(engine)
    preview = engine.lifecycle.preview_retraction(capture_id=gone, authority=owner)
    engine.lifecycle.commit_retraction(str(preview["preview_id"]), str(uuid4()), authority=owner)
    files = _export(engine, tmp_path / "archive")
    restored = _restore(engine, tmp_path / "archive", tmp_path / "restored")
    with _connect(restored) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 23
    assert _followup_counts(restored) == _followup_counts(engine)
    items, _cursor = restored.followups.list(ListFilter(), authority=_owner(restored))
    by_id = {item.item_id: item for item in items}
    assert by_id[live.item_id].summary == "Synthetic summary"
    assert by_id[live.item_id].evidence_retracted is False
    assert by_id[retracted.item_id].summary is None
    assert by_id[retracted.item_id].evidence_retracted is True
    shown, evidence, _decisions = restored.followups.show(live.item_id, authority=_owner(restored))
    assert shown.item_id == live.item_id
    assert evidence[0].quote == QUOTE_TEXT[:23]
    assert _export(restored, tmp_path / "again") == files
    with _connect(restored) as connection:
        stored = connection.execute(
            "SELECT summary FROM followup_items WHERE item_id=?", (retracted.item_id,)
        ).fetchone()[0]
    assert stored is None


def test_superseded_item_restores_before_or_after_its_successor(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    first = _propose(engine, _ref(engine, _capture(engine, "Synthetic first promise text body")))
    second = _propose(engine, _ref(engine, _capture(engine, "Synthetic second promise text")))
    # Supersede the item whose id sorts first, so the export lists it before its successor
    # and restore must insert the successor first (foreign keys are on).
    older, newer = sorted((first, second), key=lambda item: item.item_id)
    engine.followups.decide(
        DecideRequest(
            "op-" + str(uuid4()),
            older.item_id,
            older.version,
            DecisionAction.SUPERSEDE,
            superseded_by=newer.item_id,
        ),
        authority=_owner(engine),
    )
    files = _export(engine, tmp_path / "archive")
    restored = _restore(engine, tmp_path / "archive", tmp_path / "restored")
    shown, _evidence, history = restored.followups.show(older.item_id, authority=_owner(restored))
    assert shown.superseded_by == newer.item_id
    assert [entry.sequence for entry in history] == [1, 2]
    assert _export(restored, tmp_path / "again") == files


def _decide_owner(
    engine: BrainEngine, item: FollowupItem, action: DecisionAction, **fields: Any
) -> FollowupItem:
    return engine.followups.decide(
        DecideRequest("op-" + str(uuid4()), item.item_id, item.version, action, **fields),
        authority=_owner(engine),
    )


def test_round_trip_keeps_decided_items_citing_an_edited_source(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    source = _new_source(engine, "Synthetic promise: send the deck by 2026-12-01")
    cited = _ref(engine, source.head)
    item = _propose(engine, cited)
    _shown, (origin,), _history = engine.followups.show(item.item_id, authority=_owner(engine))
    item = _decide_owner(
        engine,
        item,
        DecisionAction.CORRECT,
        corrections={
            "owner": "other",
            "owner_name": "Synthetic Dana",
            "due": "2026-12-01",
            "due_evidence_id": origin.evidence_id,
        },
    )
    item = _decide_owner(
        engine,
        item,
        DecisionAction.LINK,
        ticket_key="SYN-1",
        ticket_url="https://example.invalid/t/1",
    )
    item = _decide_owner(engine, item, DecisionAction.SNOOZE, snoozed_until=date(2099, 1, 1))
    resolved = _propose(engine, _ref(engine, _capture(engine, "Synthetic resolved promise")))
    resolved = _decide_owner(engine, resolved, DecisionAction.RESOLVE, resolution="owner_statement")
    # The source moves on after the decisions; the evidence keeps citing the first revision.
    head = _revise_source(
        engine, source, "Edited intro. Synthetic promise: send the deck by 2026-12-01"
    )
    files = _export(engine, tmp_path / "archive")
    exported = {row["item_id"]: row for row in json.loads(files[FOLLOWUPS_PATH])["items"]}
    assert exported[item.item_id]["state"] == "snoozed"
    assert exported[item.item_id]["owner"] == "other"
    assert exported[item.item_id]["due"] == "2026-12-01"
    assert exported[item.item_id]["ticket_key"] == "SYN-1"
    assert exported[resolved.item_id]["resolution"] == "owner_statement"
    assert {row["revision_id"] for row in json.loads(files[FOLLOWUPS_PATH])["evidence"]} >= {
        cited.revision_id
    }
    assert cited.revision_id != head

    restored = _restore(engine, tmp_path / "archive", tmp_path / "restored")
    assert _export(restored, tmp_path / "again") == files
    client = EffectiveAuthority(
        "synthetic-client-principal",
        "synthetic-client-session",
        frozenset({"search", "content-read", "followup-read"}),
        None,
        allowed_read_tiers=frozenset({PrivacyTier.WORK}),
    )
    shown, (view,), history = restored.followups.show(item.item_id, authority=client)
    assert (view.status, view.head_revision_id) == ("moved", head)
    assert (shown.state.value, shown.owner_name, shown.ticket_key) == (
        "snoozed",
        "Synthetic Dana",
        "SYN-1",
    )
    assert shown.due == date(2026, 12, 1) and shown.due_evidence_id == origin.evidence_id
    assert [entry.sequence for entry in history] == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload | {"contract": "ob.followups.v2"},
        lambda payload: payload | {"items": payload["items"][:-1]},
        lambda payload: payload | {"evidence": []},
        lambda payload: (
            payload
            | {"items": [payload["items"][0] | {"summary": "x" * 281}] + payload["items"][1:]}
        ),
        lambda payload: (
            payload
            | {"items": [payload["items"][0] | {"evidence_retracted": True}] + payload["items"][1:]}
        ),
        lambda payload: (
            payload
            | {
                "evidence": [payload["evidence"][0] | {"quote_sha256": "zz"}]
                + payload["evidence"][1:]
            }
        ),
        lambda payload: (
            payload
            | {"decisions": [payload["decisions"][0] | {"sequence": 7}] + payload["decisions"][1:]}
        ),
        # v1 evidence cites source captures only: revision_id must equal capture_id.
        lambda payload: (
            payload
            | {
                "evidence": [payload["evidence"][0] | {"revision_id": "capture_other"}]
                + payload["evidence"][1:]
            }
        ),
    ],
)
def test_tampered_followups_refuse_before_destination_mutation(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    engine = _engine(tmp_path / "brain")
    _propose(engine, _ref(engine, _capture(engine, QUOTE_TEXT)))
    _export(engine, tmp_path / "archive")
    target = tmp_path / "archive" / FOLLOWUPS_PATH
    target.write_bytes(canonical(mutate(json.loads(target.read_bytes()))))
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")
    with pytest.raises((PortableValidationError, ValueError)):
        engine.tasks.portability.import_clean(
            tmp_path / "archive", tmp_path / "restored", import_id="import_" + str(uuid4())
        )
    assert not (tmp_path / "restored").exists()


def test_non_canonical_or_missing_family_refuses(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "brain")
    _export(engine, tmp_path / "archive")
    target = tmp_path / "archive" / FOLLOWUPS_PATH
    target.write_bytes(json.dumps(json.loads(target.read_bytes()), indent=2).encode("utf-8"))
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")
    target.unlink()
    _rehash(tmp_path / "archive")
    with pytest.raises(PortableValidationError):
        validated_portable_snapshot(tmp_path / "archive")


def test_portable17_input_restores_with_empty_followups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with monkeypatch.context() as predecessor:
        predecessor.setattr(local_schema, "PHASE1_STATE_SCHEMA_VERSION", 22)
        predecessor.setattr(local_schema, "LOCAL_MIGRATIONS", LOCAL_MIGRATIONS[:22])
        predecessor.setattr(paging, "PHASE1_STATE_SCHEMA_VERSION", 22)
        old = _engine(tmp_path / "brain")
        _capture(old, "Synthetic predecessor work record")
        files = _export(old, tmp_path / "archive", version=17)
    assert FOLLOWUPS_PATH not in files
    current = _engine(tmp_path / "current")
    restored = _restore(current, tmp_path / "archive", tmp_path / "restored")
    with _connect(restored) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 23
    assert _followup_counts(restored) == (0, 0, 0)
    again = _export(restored, tmp_path / "again")
    assert json.loads(again[FOLLOWUPS_PATH])["items"] == []
