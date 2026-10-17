"""Actual process death leaves accepted custody recoverable without publication."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.engine import BrainEngine, CaptureFault

from open_brain.profile import compile_single_user_local
from packages.engine.tests.test_consumer_intake_engine import (
    _conversation,
    _counts,
    _owner,
    _plan,
    _rows,
)
from packages.engine.tests.test_consumer_intake_engine import (
    active_profiles as active_profiles,
)

_CRASH = """
import json, os, sys
from pathlib import Path
from open_brain.profile import open_existing_single_user_local
from open_brain_engine.engine import BrainEngine, CaptureFault
from open_brain_engine.engine.consumer_plans import ConsumerImportPlan
from open_brain_engine.engine.consumer_profiles import ConsumerProfileRecord
from open_brain_engine.engine import consumer_profiles
from packages.engine.tests.test_consumer_intake_engine import _owner
data=json.load(sys.stdin)
plan=ConsumerImportPlan.from_bytes(bytes.fromhex(data['root']),
    {key:bytes.fromhex(raw) for key,raw in data['objects'].items()})
consumer_profiles._ACTIVE_PROFILES={plan.profile_id:
    ConsumerProfileRecord(plan.provider,plan.profile_id,'1'*64)}
engine=BrainEngine.open(open_existing_single_user_local(Path(sys.argv[1])))
point=CaptureFault(sys.argv[2])
target=int(sys.argv[3])
count=0
original=engine._fault
def crash(actual):
    global count
    if actual==point:
        count+=1
        if count==target:
            os._exit(83)
    original(actual)
engine._fault=crash
preview=engine.consumer_imports.preview_consumer_import(plan,authority=_owner(engine))
engine.consumer_imports.commit_consumer_import(preview['preview_id'],plan.operation_id,
    plan,authority=_owner(engine))
raise AssertionError('requested crash boundary was not reached')
"""


@pytest.mark.parametrize("provider", ["chatgpt", "claude"])
@pytest.mark.parametrize(
    ("point", "ordinal"),
    [
        (CaptureFault.AFTER_CONSUMER_INTENT, 1),
        (CaptureFault.AFTER_CONSUMER_CUSTODY, 1),
        (CaptureFault.AFTER_CONSUMER_CUSTODY, 2),
        (CaptureFault.AFTER_CAPTURE_RESERVATION, 1),
        (CaptureFault.AFTER_CONSUMER_SEGMENT, 1),
        (CaptureFault.AFTER_CONSUMER_SEGMENT, 2),
        (CaptureFault.CONSUMER_READY, 1),
        (CaptureFault.AFTER_CONSUMER_ACTIVATION, 1),
    ],
)
def test_process_exit_at_each_durable_boundary(
    tmp_path: Path,
    active_profiles: None,
    provider: str,
    point: CaptureFault,
    ordinal: int,
) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    engine = BrainEngine.open(profile)
    plan = _plan(
        provider,
        (
            _conversation("first", "killintake first " + "x" * 60010),
            _conversation("second", "killintake second"),
        ),
    )
    payload = json.dumps(
        {
            "root": plan.to_bytes().hex(),
            "objects": {key: raw.hex() for key, raw in plan.objects().items()},
        }
    )
    child = subprocess.run(
        [sys.executable, "-c", _CRASH, str(profile.root), point.value, str(ordinal)],
        input=payload,
        text=True,
        capture_output=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[3],
        check=False,
    )
    assert child.returncode == 83, child.stderr
    events = _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events")
    captures = _rows(engine, "SELECT capture_id,stage FROM captures ORDER BY capture_id")
    engine = BrainEngine.open(profile)
    # Startup/status cannot resume accepted consumer work, including stage-zero
    # reservations left behind by a real process exit.
    assert _rows(engine, "SELECT capture_id,stage FROM captures ORDER BY capture_id") == captures
    status = engine.consumer_imports.consumer_import_status(
        plan.operation_id, authority=_owner(engine)
    )
    assert _rows(engine, "SELECT sequence,event_sha256 FROM lifecycle_journal_events") == events
    assert _counts(status)["pending"] > 0
    for (capture,) in _rows(
        engine,
        "SELECT c.capture_id FROM captures c WHERE NOT EXISTS("
        "SELECT 1 FROM logical_sources s WHERE s.head_capture_id=c.capture_id "
        "AND s.historical_only=0)",
    ):
        assert engine.capture.get(cast(str, capture)) is None
    result = engine.consumer_imports.recover_consumer_import(
        plan.operation_id, authority=_owner(engine)
    )
    assert result["status"] == "committed" and _counts(result)["committed"] == 2
    assert _counts(result)["segments_materialized"] == 3
    assert _rows(engine, "SELECT count(*) FROM captures") == [(3,)]
    assert _rows(engine, "SELECT count(*) FROM logical_sources") == [(3,)]
    assert len(engine.retrieval.search("killintake")) == 2
    assert (
        engine.consumer_imports.recover_consumer_import(plan.operation_id, authority=_owner(engine))
        == result
    )
