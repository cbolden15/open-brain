"""Owner-only bounded enumeration of every capture assigned to one space (W5 P3 gap)."""

from __future__ import annotations

from pathlib import Path

import pytest
from open_brain_engine.engine import BrainEngine, TextPayload
from open_brain_engine.engine.t03_contracts import EffectiveAuthority, T03Error

from open_brain.profile import compile_single_user_local


def test_space_capture_ids_are_bounded_and_owner_only(tmp_path: Path) -> None:
    engine = BrainEngine.open(compile_single_user_local(tmp_path / "brain"))
    dedicated = engine.inbox.create_space("Dedicated", delivery_id="synthetic.dedicated")
    other = engine.inbox.create_space("Other", delivery_id="synthetic.other")
    assert engine.inbox.space_capture_ids(dedicated.space_id, limit=10) == ((), False)
    first = engine.capture.accept(
        TextPayload("synthetic one"), delivery_id="synthetic.1", space_id=dedicated.space_id
    )
    second = engine.capture.accept(
        TextPayload("synthetic two"), delivery_id="synthetic.2", space_id=dedicated.space_id
    )
    engine.capture.accept(
        TextPayload("synthetic elsewhere"), delivery_id="synthetic.3", space_id=other.space_id
    )
    ids, truncated = engine.inbox.space_capture_ids(dedicated.space_id, limit=10)
    assert sorted(ids) == sorted([first.capture_id, second.capture_id]) and not truncated
    bounded, truncated = engine.inbox.space_capture_ids(dedicated.space_id, limit=1)
    assert len(bounded) == 1 and truncated
    with pytest.raises(T03Error, match="not_found"):
        engine.inbox.space_capture_ids("space_00000000-0000-4000-8000-000000000000", limit=10)
    with pytest.raises(T03Error, match="invalid_arguments"):
        engine.inbox.space_capture_ids(dedicated.space_id, limit=0)
    agent = EffectiveAuthority(
        "synthetic-agent", "synthetic-session", frozenset({"search"}), None, owner=False
    )
    with pytest.raises(T03Error, match="unsupported_capability"):
        engine.inbox.with_authority(agent).space_capture_ids(dedicated.space_id, limit=10)
