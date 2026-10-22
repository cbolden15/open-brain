"""Crash, lost-acknowledgement and race recovery for every P3 owner write (RECOVERY.md).

Each write is interrupted before the authority call, after the authority
committed but before its result reached the guide, and after the result but
before the journal recorded it. create-destination, route, consent, select,
release-preview and approve replay their exact saved request through the
real authority and must apply exactly once; policy-apply, setup-preview and
setup-apply halt and require a fresh preview. A refused replay halts.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from open_brain_engine.engine.local_schema import open_local_database_read_only

from open_brain.profile import compile_single_user_local, open_existing_single_user_local
from open_brain.services import source_guide_release
from open_brain.services.source_guide_journal import WorkflowJournal
from open_brain.services.source_guide_release import ReleaseFlow
from packages.app.tests.integration.services.test_source_guide import Guide
from packages.app.tests.integration.services.test_source_guide_recovery import (
    _Crash,
    _crash,
    _settled,
)
from packages.app.tests.integration.services.test_source_guide_release import (
    approve,
    approved_one,
    destination,
    inspected,
    journal,
    new_release_guide,
    policy_applied,
    released,
    routed,
    selected_and_consented,
    setup_previewed,
    sources,
)

TEXT = "Decision: keep onboarding invite-only."
POSITIONS = ("before_authority", "after_commit_before_result", "after_result_before_journal")
REPLAYABLE = ("create-destination", "route", "consent", "select", "release-preview", "approve")
REPREVIEW = {
    "policy-apply": "policy-preview",
    "setup-preview": "setup-preview",
    "setup-apply": "setup-preview",
}


@pytest.fixture
def guide(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> Guide:
    return new_release_guide(tmp_path, capsys, monkeypatch)


def _stage(guide: Guide, action: str) -> dict[str, object]:
    """Advance a fresh workflow to just before `action` and return its arguments."""
    if action == "create-destination":
        inspected(guide, [TEXT])
        return {"name": "Guide release"}
    if action == "route":
        receipts = inspected(guide, [TEXT])
        destination(guide)
        return {"receipt_id": receipts[0]}
    if action in {"select", "consent"}:
        receipts = routed(guide, [TEXT])
        if action == "select":
            return {"member_receipt_ids": receipts}
        assert guide.confirm("select", {"member_receipt_ids": receipts})[0] == 0
        return {"provider_id": "openai"}
    if action in {"release-preview", "approve"}:
        receipts, _selection = selected_and_consented(guide, [TEXT])
        if action == "release-preview":
            return {"receipt_id": receipts[0], "provider_ids": ["openai"]}
        return {"preview_id": released(guide, receipts[0])["preview_id"]}
    approved_one(guide)
    if action == "policy-apply":
        code, preview = guide.advance("policy-preview")
        assert code == 0, preview
        return {"policy_sha256": preview["data"]["payload_sha256"]}
    policy_applied(guide)
    if action == "setup-preview":
        return {}
    return {"preview_id": setup_previewed(guide)["preview_id"]}


def _interrupt(monkeypatch: pytest.MonkeyPatch, position: str) -> None:
    if position == "after_result_before_journal":
        original_write = WorkflowJournal.write

        def write(self: WorkflowJournal, document: Any, **keywords: Any) -> Any:
            if keywords.get("reserve_bytes") == 0:
                raise _Crash
            return original_write(self, document, **keywords)

        monkeypatch.setattr(WorkflowJournal, "write", write)
        return
    original = ReleaseFlow._call

    def call(self: ReleaseFlow, action: str, request: Any) -> object:
        if position == "before_authority":
            raise _Crash
        original(self, action, request)
        raise _Crash

    monkeypatch.setattr(ReleaseFlow, "_call", call)


def _counts(guide: Guide) -> dict[str, int]:
    """Authority-side effect counters; each P3 write must move exactly one of them once."""
    from open_brain.services.session_consent import DurableProviderConsentStore

    profile = open_existing_single_user_local(guide.brain)
    with closing(open_local_database_read_only(profile)) as connection:
        counts = {
            "create-destination": connection.execute(
                "SELECT count(*) FROM spaces WHERE name='Guide release'"
            ).fetchone()[0],
            "route": connection.execute(
                "SELECT coalesce(sum(route_version),0) FROM logical_sources "
                "WHERE source_id IN (SELECT source_id FROM source_namespaces)"
            ).fetchone()[0],
            "select": connection.execute(
                "SELECT count(*) FROM slack_recall_selections"
            ).fetchone()[0],
            "release-preview": connection.execute(
                "SELECT count(*) FROM sharing_previews"
            ).fetchone()[0],
            "approve": connection.execute("SELECT count(*) FROM sharing_decisions").fetchone()[0],
        }
    consent = guide.root / "consent" / "state.json"
    binding = json.loads(guide.journal.read_bytes())["binding"]
    counts["consent"] = (
        len(DurableProviderConsentStore(
            consent, brain_id=binding["brain_id"], issuer_epoch=binding["issuer_epoch"]
        ).load().records)
        if consent.exists() else 0
    )
    return counts


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("action", REPLAYABLE)
def test_replayable_write_reconciles_its_exact_saved_request_once(
    guide: Guide, monkeypatch: pytest.MonkeyPatch, action: str, position: str
) -> None:
    arguments = _stage(guide, action)
    before = _counts(guide)
    with monkeypatch.context() as patch:
        _interrupt(patch, position)
        sequence = _crash(guide, action, arguments)
    _settled(guide, sequence)
    saved = journal(guide)["operations"][sequence]

    code, proposed = guide.advance("reconcile", {"operation_sequence": sequence})
    assert code == 3, proposed
    assert json.loads(proposed["intent"]["canonical_request_utf8"]) == {
        "operation_sequence": sequence, "request_sha256": saved["request_sha256"]
    }
    code, replayed = guide.advance(
        "reconcile", {"operation_sequence": sequence}, confirmation=approve(proposed["intent"])
    )
    assert (code, replayed["outcome"]) == (0, "completed"), replayed
    after = _counts(guide)
    expected = dict(before)
    expected[action] += 1
    assert after == expected
    original = journal(guide)["operations"][sequence]
    # Byte-exact saved request and intent; evidence only from the authority receipt.
    assert original["canonical_request_utf8"] == saved["canonical_request_utf8"]
    assert original["canonical_intent_utf8"] == saved["canonical_intent_utf8"]
    assert original["state"] == "completed" and original["evidence"] is not None
    if action == "create-destination":
        assert journal(guide)["choices"]["destination_space_id"] == original["evidence"][
            "space_id"
        ]
    # The workflow continues from the reconciled receipt with no duplicate write.
    code, again = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, again["code"]) == (2, "invalid_request")


@pytest.mark.parametrize("position", POSITIONS)
@pytest.mark.parametrize("action", tuple(REPREVIEW))
def test_policy_and_setup_ambiguity_halts_and_repreviews(
    guide: Guide, monkeypatch: pytest.MonkeyPatch, action: str, position: str
) -> None:
    arguments = _stage(guide, action)
    policy_path = guide.root / "policy" / "policy.json"
    config = guide.project / ".mcp.json"
    with monkeypatch.context() as patch:
        _interrupt(patch, position)
        sequence = _crash(guide, action, arguments)
    _settled(guide, sequence)
    observed = (
        policy_path.read_bytes() if policy_path.exists() else None,
        config.read_bytes() if config.exists() else None,
    )
    code, halted = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, halted["outcome"], halted["code"]) == (
        78, "unknown_outcome", "operation_unknown"
    ), halted
    assert halted["next_actions"] == [REPREVIEW[action]]
    # Halt only: no authority call, no replay or reconstruction of the saved request.
    assert observed == (
        policy_path.read_bytes() if policy_path.exists() else None,
        config.read_bytes() if config.exists() else None,
    )
    assert journal(guide)["operations"][sequence]["state"] == "unknown"
    if action == "policy-apply":
        code, preview = guide.advance("policy-preview")
        assert code == 0, preview
        reused = observed[0] is not None
        assert json.loads(journal(guide)["operations"][-1]["canonical_request_utf8"])[
            "expected_absent"
        ] is (not reused)
        code, applied = guide.confirm(
            "policy-apply", {"policy_sha256": preview["data"]["payload_sha256"]}
        )
        assert code == 0, applied
        assert policy_path.read_text() == preview["data"]["payload_utf8"]
        return
    previewed = setup_previewed(guide)
    code, done = guide.confirm("setup-apply", {"preview_id": previewed["preview_id"]})
    assert (code, done["outcome"]) == (0, "completed"), done
    assert list(json.loads(config.read_text())["mcpServers"]) == ["open-brain"]
    assert not (guide.root / "launched").exists()


def test_kill_after_first_setup_file_write_halts_and_repreviews(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain.services import agent_setup

    arguments = _stage(guide, "setup-apply")
    config = guide.project / ".mcp.json"
    original = agent_setup._atomic_apply
    writes: list[Path] = []

    def dying(path: Path, data: bytes | None, *, expected: bytes | None) -> None:
        # The first owned file is written, then the process dies: no rollback runs.
        if not writes:
            original(path, data, expected=expected)
        writes.append(path)
        raise _Crash

    with monkeypatch.context() as patch:
        patch.setattr(agent_setup, "_atomic_apply", dying)
        code, proposed = guide.advance("setup-apply", arguments)
        assert code == 3
        code, failed = guide.advance(
            "setup-apply", arguments, confirmation=approve(proposed["intent"])
        )
    assert (code, failed["outcome"], failed["code"]) == (
        78, "unknown_outcome", "operation_unknown"
    ), failed
    assert writes and writes[0] == config and config.exists()
    sequence = journal(guide)["operations"][-1]["sequence"]
    assert journal(guide)["operations"][-1]["state"] == "unknown"
    code, stale = guide.advance("setup-apply", arguments)
    assert (code, stale["code"]) == (78, "operation_unknown")
    code, halted = guide.advance("reconcile", {"operation_sequence": sequence})
    assert (code, halted["next_actions"]) == (78, ["setup-preview"])
    # The stale preview is never replayed; a fresh preview reflects the partial write.
    code, refused = guide.advance("setup-apply", arguments)
    assert (code, refused["code"], refused["next_actions"]) == (
        78, "revision_conflict", ["setup-preview"]
    )
    previewed = setup_previewed(guide)
    assert previewed["preview_id"] != arguments["preview_id"]
    code, done = guide.confirm("setup-apply", {"preview_id": previewed["preview_id"]})
    assert code == 0, done
    assert list(json.loads(config.read_text())["mcpServers"]) == ["open-brain"]
    assert not (guide.root / "launched").exists()


def test_route_replay_after_owner_withdrawal_returns_the_committed_receipt(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import open_local_engine
    from open_brain_engine.engine.source_lifecycle_contracts import SourceWithdrawRequest

    from open_brain.services.t03_adapters import owner_authority

    arguments = _stage(guide, "route")
    with monkeypatch.context() as patch:
        _interrupt(patch, "after_commit_before_result")
        sequence = _crash(guide, "route", arguments)
    _settled(guide, sequence)
    (source_id, row), = sources(guide).items()
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    binding = journal(guide)["binding"]
    tasks.sources.withdraw(
        SourceWithdrawRequest(
            operation_id="withdraw.synthetic", source_id=source_id,
            expected_head=row["head_capture_id"],
            expected_lifecycle_version=row["lifecycle_version"],
            brain_id=binding["brain_id"], issuer_epoch=binding["issuer_epoch"],
            reason_code="owner_withdrawn",
        ),
        authority=owner_authority(tasks, session_id="other-owner"),
    )
    code, replay = guide.confirm("reconcile", {"operation_sequence": sequence})
    assert (code, replay["outcome"]) == (0, "completed"), replay
    assert sources(guide)[source_id]["route_version"] == row["route_version"]
    # The withdrawn source cannot progress: its current head is no longer active.
    code, refused = guide.advance("select", {"member_receipt_ids": [arguments["receipt_id"]]})
    assert code == 78 and refused["outcome"] == "refused", refused


def test_unsafe_consent_store_during_replay_stays_unknown_then_replays_once(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stage(guide, "consent")
    with monkeypatch.context() as patch:
        _interrupt(patch, "after_commit_before_result")
        sequence = _crash(guide, "consent", arguments)
    _settled(guide, sequence)
    consent = guide.root / "consent" / "state.json"
    consent.chmod(0o644)
    code, replay = guide.confirm("reconcile", {"operation_sequence": sequence})
    # The store may fail after its atomic replace: unknown, never a proof of non-commit.
    assert (code, replay["outcome"], replay["code"]) == (78, "unknown_outcome", "operation_unknown")
    assert journal(guide)["status"] == "active"
    assert journal(guide)["operations"][sequence]["state"] == "unknown"
    consent.chmod(0o600)
    code, replayed = guide.confirm("reconcile", {"operation_sequence": sequence})
    assert (code, replayed["outcome"]) == (0, "completed"), replayed
    assert _counts(guide)["consent"] == 1


def test_foreign_consent_state_on_replay_halts_because_refusal_proves_nothing(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.core.models import PrivacyTier

    from open_brain.services.session_consent import DurableProviderConsentStore

    arguments = _stage(guide, "consent")
    with monkeypatch.context() as patch:
        _interrupt(patch, "after_commit_before_result")
        sequence = _crash(guide, "consent", arguments)
    _settled(guide, sequence)
    consent = guide.root / "consent" / "state.json"
    (guide.root / "foreign").mkdir(mode=0o700)
    foreign = guide.root / "foreign" / "state.json"
    DurableProviderConsentStore(foreign, brain_id="brn_" + "b" * 26, issuer_epoch=1).grant(
        provider_id="openai", allowed_tiers=frozenset({PrivacyTier.WORK}),
        operation_id="grant.foreign", decided_at="2026-10-22T12:00:00Z",
    )
    consent.write_bytes(foreign.read_bytes())
    committed = consent.read_bytes()
    code, replay = guide.confirm("reconcile", {"operation_sequence": sequence})
    assert (code, replay["outcome"], replay["code"]) == (78, "refused", "authority_refused")
    state = journal(guide)
    # The grant may have committed (it did): the refused replay halts the workflow.
    assert state["status"] == "halted"
    assert state["operations"][sequence]["state"] == "unknown"
    assert state["operations"][sequence]["evidence"] is None
    assert replay["next_actions"] == ["cancel"]
    assert consent.read_bytes() == committed
    code, blocked = guide.advance("release-preview", {"receipt_id": "custody:x",
                                                      "provider_ids": ["openai"]})
    assert (code, blocked["code"], blocked["next_actions"]) == (
        78, "receipt_unresolved", ["cancel"]
    )
    code, cancelled = guide.advance("cancel")
    assert (code, cancelled["outcome"]) == (0, "cancelled")
    assert journal(guide)["operations"][sequence]["state"] == "unknown"
    assert consent.read_bytes() == committed


def test_concurrent_owner_route_wins_the_authority_cas(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    from open_brain_engine.engine import open_local_engine
    from open_brain_engine.engine.t03_contracts import SourceRouteRequest

    from open_brain.services.t03_adapters import owner_authority

    arguments = _stage(guide, "route")
    (source_id, row), = sources(guide).items()
    tasks: Any = open_local_engine(compile_single_user_local(guide.brain))
    other = tasks.inbox.create_space("Other session", delivery_id="synthetic.other").space_id
    original = ReleaseFlow._call

    def raced(self: ReleaseFlow, action: str, request: Any) -> object:
        # A second owner session commits first, between confirmation and the guide's call.
        tasks.sources.route(
            SourceRouteRequest(
                source_id=source_id, space_id=other, expected_head=row["head_capture_id"],
                expected_route_version=row["route_version"],
                operation_id="operation_00000000-0000-4000-8000-0000000000bb",
            ),
            authority=owner_authority(tasks, session_id="other-owner"),
        )
        return original(self, action, request)

    code, proposed = guide.advance("route", arguments)
    assert code == 3
    with monkeypatch.context() as patch:
        patch.setattr(ReleaseFlow, "_call", raced)
        code, refused = guide.advance(
            "route", arguments, confirmation=approve(proposed["intent"])
        )
    assert (code, refused["code"]) == (78, "revision_conflict"), refused
    current = sources(guide)[source_id]
    assert current["space_id"] == other
    assert current["route_version"] == row["route_version"] + 1
    # The stale confirmation is spent; a new intent shows the other session's version.
    code, proposed_again = guide.advance("route", arguments)
    assert code == 3
    versions = proposed_again["intent"]["expected_versions"]
    assert versions[1]["version"] == str(row["route_version"] + 1)


def test_expired_release_preview_never_auto_authorizes_approval(
    guide: Guide, monkeypatch: pytest.MonkeyPatch
) -> None:
    arguments = _stage(guide, "approve")

    class _Later(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            return datetime.now(UTC) + timedelta(hours=1)

    monkeypatch.setattr(source_guide_release, "datetime", _Later)
    code, refused = guide.advance("approve", arguments)
    assert (code, refused["code"], refused["next_actions"]) == (
        78, "authority_refused", ["release-preview"]
    )
    assert _counts(guide)["approve"] == 0
