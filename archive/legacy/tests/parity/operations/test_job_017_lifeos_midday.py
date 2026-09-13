from datetime import date

from open_brain_legacy.integrations.life_os import LifePlanRequest, ReviewGatedActionCandidate
from open_brain_legacy.operations.models import JobState
from open_brain_legacy.operations.optional_jobs import compose_lifeos_midday_job


def test_job_017_composes_enabled_idempotent_midday_nudge_argv() -> None:
    request = LifePlanRequest(
        plan_date=date(2026, 8, 14),
        action_candidates=(
            ReviewGatedActionCandidate(
                candidate_id="candidate_fixture",
                review_id="review_fixture",
            ),
        ),
    )

    first = compose_lifeos_midday_job(request)
    replay = compose_lifeos_midday_job(request)

    assert first == replay
    assert first.id == "JOB-017"
    assert first.state is JobState.ENABLED
    assert first.command == (
        "open-brain",
        "lifeos",
        "nudge",
        "midday",
        "--date=2026-08-14",
        "--json",
    )
    assert "candidate_fixture" not in repr(first.command)
    assert "review_fixture" not in repr(first.command)
