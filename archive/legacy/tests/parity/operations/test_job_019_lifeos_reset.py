from datetime import date

from open_brain_legacy._compat.open_brain.integrations import Capability, IntegrationConfig
from open_brain_legacy.integrations.life_os import (
    InMemoryLifePlanStore,
    LifeOSIntegration,
    LifePlanRequest,
    LifeResetDisposition,
    LifeResetRequest,
)
from open_brain_legacy.operations.models import JobState
from open_brain_legacy.operations.optional_jobs import compose_lifeos_reset_job


def test_job_019_composes_enabled_reset_and_preserves_reset_replay() -> None:
    store = InMemoryLifePlanStore()
    request = LifeResetRequest(plan_date=date(2026, 8, 14))
    job = compose_lifeos_reset_job(request)

    assert job.state is JobState.ENABLED
    assert job.command == (
        "open-brain",
        "lifeos",
        "reset",
        "--date=2026-08-14",
        "--json",
    )
    disabled = LifeOSIntegration(store=store).reset(request)
    assert disabled.disposition is LifeResetDisposition.DISABLED

    integration = LifeOSIntegration(
        config=IntegrationConfig(live_adapters=frozenset({Capability.LIFE_OS})),
        store=store,
    )
    integration.plan(LifePlanRequest(plan_date=request.plan_date))

    first = integration.reset(request)
    replay = integration.reset(request)

    assert first.disposition is LifeResetDisposition.RESET
    assert replay.disposition is LifeResetDisposition.DUPLICATE
    assert first.plan_key == replay.plan_key
    assert store.plans == ()
