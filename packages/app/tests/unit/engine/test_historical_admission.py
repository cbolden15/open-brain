"""Historical witnesses must match genuine stored state, not just well-typed JSON."""

from pathlib import Path

import pytest
from open_brain_engine.engine import TextPayload, open_local_engine
from open_brain_engine.engine.historical_admission import verify_historical_source_cas
from open_brain_engine.engine.historical_contracts import HistoricalSourceCAS
from open_brain_engine.engine.local_schema import open_local_database_read_only
from open_brain_engine.engine.sharing_contracts import SharingError

from open_brain.profile import compile_single_user_local


@pytest.mark.parametrize(
    "field",
    [
        None,
        "source_id",
        "expected_head",
        "expected_head_version",
        "expected_route_version",
        "expected_lifecycle_version",
        "expected_control_epoch",
        "expected_lifecycle",
        "expected_availability",
        "expected_historical_only",
    ],
)
def test_exact_cas_against_real_source_state(tmp_path: Path, field: str | None) -> None:
    profile = compile_single_user_local(tmp_path / "brain")
    tasks = open_local_engine(profile)
    receipt = tasks.capture.accept(
        TextPayload("synthetic retained owner"), delivery_id="synthetic.retained.owner"
    )
    with open_local_database_read_only(profile) as connection:
        row = connection.execute(
            "SELECT s.*,l.lifecycle_version,g.control_epoch FROM logical_sources s "
            "JOIN source_lifecycle_state l USING(source_id) CROSS JOIN engine_generations g "
            "WHERE s.head_capture_id=? AND g.singleton=1",
            (receipt.capture_id,),
        ).fetchone()
        assert row is not None
        witness = HistoricalSourceCAS(
            source_id=row["source_id"],
            expected_head=row["head_capture_id"],
            expected_head_version=row["head_version"],
            expected_route_version=row["route_version"],
            expected_lifecycle_version=row["lifecycle_version"],
            expected_control_epoch=row["control_epoch"],
            expected_lifecycle=row["lifecycle"],
            expected_availability=row["availability"],
            expected_historical_only=bool(row["historical_only"]),
        )
        if field is None:
            verify_historical_source_cas(connection, witness)
        else:
            changes: dict[str, object] = {
                "source_id": "source_other",
                "expected_head": "capture_other",
                "expected_head_version": witness.expected_head_version + 1,
                "expected_route_version": witness.expected_route_version + 1,
                "expected_lifecycle_version": witness.expected_lifecycle_version + 1,
                "expected_control_epoch": witness.expected_control_epoch + 1,
                "expected_lifecycle": "retired",
                "expected_availability": "missing",
                "expected_historical_only": not witness.expected_historical_only,
            }
            changed = HistoricalSourceCAS.from_value(
                dict(witness.value(), **{field: changes[field]})
            )
            with pytest.raises(SharingError, match="revision_changed"):
                verify_historical_source_cas(connection, changed)
        assert connection.execute("SELECT count(*) FROM captures").fetchone()[0] == 1
