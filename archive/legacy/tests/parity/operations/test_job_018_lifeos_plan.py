from datetime import date

import pytest

from open_brain_legacy._compat.open_brain.integrations import Capability, IntegrationConfig
from open_brain_legacy.integrations.life_os import (
    InMemoryLifePlanStore,
    LifeOSIntegration,
    LifePlanDisposition,
    LifePlanRequest,
    ReviewGatedActionCandidate,
)
from open_brain_legacy.operations.models import JobState
from open_brain_legacy.operations.optional_jobs import compose_lifeos_plan_job


def test_job_018_composes_one_review_gated_generic_plan_per_date() -> None:
    store = InMemoryLifePlanStore()
    request = LifePlanRequest(
        plan_date=date(2026, 8, 14),
        action_candidates=(
            ReviewGatedActionCandidate("candidate_fixture", "review_fixture"),
        ),
    )
    job = compose_lifeos_plan_job(request)

    assert job.state is JobState.ENABLED
    assert job.command == (
        "open-brain",
        "lifeos",
        "plan",
        "--date=2026-08-14",
        "--generic-titles",
        "--json",
    )
    assert "candidate_fixture" not in repr(job.command)
    assert LifeOSIntegration(store=store).plan(request).disposition is LifePlanDisposition.DISABLED
    assert store.plans == ()

    integration = LifeOSIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.LIFE_OS})),
        store=store,
    )
    first = integration.plan(request)
    replay = integration.plan(request)

    assert first.disposition is LifePlanDisposition.PLANNED
    assert replay.disposition is LifePlanDisposition.DUPLICATE
    assert replay.plan == first.plan
    with pytest.raises(ValueError, match="conflicting LifeOS plan replay"):
        integration.plan(
            LifePlanRequest(
                plan_date=request.plan_date,
                action_candidates=(
                    ReviewGatedActionCandidate("candidate_other", "review_other"),
                ),
            )
        )
