"""Historical denial precedes ordinary policy and retains owner history."""

from pathlib import Path

import pytest
from open_brain_engine.core.models import PrivacyTier
from open_brain_engine.engine import BrainEngine
from open_brain_engine.engine.historical_fence import HistoricalPendingFence
from open_brain_engine.engine.historical_recovery import (
    _historical_transaction,
    recover_historical_pending,
)
from open_brain_engine.engine.runtime_admission import exclusive_runtime_admission
from open_brain_engine.engine.sharing import EligibilityMode, sharing_eligible
from open_brain_engine.engine.t03_contracts import (
    EffectiveAuthority,
    RecordReadRequest,
    SearchPageRequest,
    T03Error,
)

from open_brain.profile import compile_single_user_local
from packages.app.tests.unit.engine.test_historical_projection import _claim_transition
from packages.app.tests.unit.engine.test_paging import (
    capture_work,
    external_authority,
    scoped_authority,
    wire,
)


def test_pending_fence_denies_before_projection_and_owner_retains_history(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    capture_id = record.receipt.original_capture_id
    with _historical_transaction(engine.profile, lambda: None) as connection:
        assert sharing_eligible(
            connection, capture_id, mode=EligibilityMode.EXTERNAL_READ, profile=engine.profile
        )
        assert not sharing_eligible(connection, capture_id, mode=EligibilityMode.EXTERNAL_READ)
    fence = HistoricalPendingFence(engine.profile.root, engine.profile.root_identity)
    fence.prepare(record)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        assert not sharing_eligible(
            connection, capture_id, mode=EligibilityMode.EXTERNAL_READ, profile=engine.profile
        )
        assert not sharing_eligible(
            connection, capture_id, mode=EligibilityMode.LOCAL_HISTORY, profile=engine.profile
        )
        assert sharing_eligible(
            connection, capture_id, mode=EligibilityMode.OWNER_HISTORY, profile=engine.profile
        )
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(engine.profile) as admission:
        recover_historical_pending(engine, authority=owner, admission=admission)
    with _historical_transaction(engine.profile, lambda: None) as connection:
        assert not sharing_eligible(
            connection, capture_id, mode=EligibilityMode.EXTERNAL_READ, profile=engine.profile
        )
        assert sharing_eligible(
            connection, capture_id, mode=EligibilityMode.OWNER_HISTORY, profile=engine.profile
        )


@pytest.mark.parametrize("external_reader", [False, True])
def test_pending_fence_removes_ordinary_candidates_before_search_and_content_read(
    tmp_path: Path,
    external_reader: bool,
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    target = capture_work(
        engine,
        delivery_id="ordinary.safe",
        text="historical barrier nebula",
        external_egress=True,
    )
    external = external_authority(engine) if external_reader else scoped_authority(PrivacyTier.WORK)
    search = SearchPageRequest(query="historical barrier nebula", limit=10)
    before = wire(engine.retrieval.search_page(search, authority=external))
    assert [row["record_id"] for row in before["results"]] == [target]
    current = RecordReadRequest(record_id=target, expected_revision_id=target)
    engine.retrieval.read_record(current, authority=external)
    HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).prepare(record)
    assert engine.retrieval.search_page(search, authority=external).to_wire()["results"] == []
    with pytest.raises(T03Error, match="not_found"):
        engine.retrieval.read_record(current, authority=external)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    engine.retrieval.read_record(current, authority=owner)
    with exclusive_runtime_admission(engine.profile) as admission:
        recover_historical_pending(engine, authority=owner, admission=admission)
    assert [
        row["record_id"]
        for row in wire(engine.retrieval.search_page(search, authority=external))["results"]
    ] == [target]
    engine.retrieval.read_record(current, authority=external)


@pytest.mark.parametrize("missing", ["registry", "fence", "claim"])
def test_missing_required_evidence_never_becomes_ordinary_policy(
    tmp_path: Path, missing: str
) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    record = _claim_transition(engine)
    HistoricalPendingFence(engine.profile.root, engine.profile.root_identity).prepare(record)
    owner = EffectiveAuthority("synthetic-owner", "session", frozenset(), None, owner=True)
    with exclusive_runtime_admission(engine.profile) as admission:
        recover_historical_pending(engine, authority=owner, admission=admission)
    if missing != "claim":
        name = "historical-claims.v1.json" if missing == "registry" else "historical-fence.v1.json"
        path = engine.profile.root / ".open-brain" / "historical-authority" / name
        path.rename(path.with_suffix(".retained"))
    with (
        pytest.raises(RuntimeError, match="discard synthetic damage"),
        _historical_transaction(engine.profile, lambda: None) as connection,
    ):
        if missing == "claim":
            connection.execute("DROP TRIGGER historical_claims_delete_immutable")
            connection.execute("DELETE FROM historical_claims")
        assert not sharing_eligible(
            connection,
            record.receipt.original_capture_id,
            mode=EligibilityMode.EXTERNAL_READ,
            profile=engine.profile,
        )
        assert sharing_eligible(
            connection,
            record.receipt.original_capture_id,
            mode=EligibilityMode.OWNER_HISTORY,
            profile=engine.profile,
        )
        raise RuntimeError("discard synthetic damage")
