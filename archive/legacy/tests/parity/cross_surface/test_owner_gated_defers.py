from __future__ import annotations

import json

import pytest

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.main import main
from open_brain_legacy.cli.social import SocialCompatibilityAction, compatibility
from open_brain_legacy.integrations.hooks import (
    HookCompatibilityAction,
    HookCompatibilityDisposition,
    TemporaryHookPlanner,
)
from packages.legacy.tests.parity.cross_surface._preflight import (
    AUTHORITATIVE_ROW_CLASSIFICATIONS,
    OWNER_GATED_DEFER_ROW_IDS,
    RowDisposition,
)

_EXPECTED_AUTHORITATIVE_ROW_IDS = frozenset(
    f"{prefix}-{index:03d}"
    for prefix, count in (
        ("CLI", 15),
        ("LED", 9),
        ("INT", 14),
        ("CAP", 11),
        ("JOB", 30),
        ("HOOK", 2),
        ("EXT", 2),
    )
    for index in range(1, count + 1)
)
_EXPECTED_OWNER_GATED_DEFER_ROW_IDS: frozenset[str] = frozenset()


def test_authoritative_row_classifications_cover_exact_allocation_without_defers() -> None:
    row_ids = tuple(row.row_id for row in AUTHORITATIVE_ROW_CLASSIFICATIONS)
    defer_row_ids = frozenset(
        row.row_id
        for row in AUTHORITATIVE_ROW_CLASSIFICATIONS
        if row.disposition is not RowDisposition.OPEN_BRAIN_LIVE
    )

    assert len(row_ids) == 83
    assert len(set(row_ids)) == 83
    assert frozenset(row_ids) == _EXPECTED_AUTHORITATIVE_ROW_IDS
    assert OWNER_GATED_DEFER_ROW_IDS == _EXPECTED_OWNER_GATED_DEFER_ROW_IDS
    assert defer_row_ids == _EXPECTED_OWNER_GATED_DEFER_ROW_IDS


def test_cli_005_review_is_no_longer_intercepted_as_an_owner_gated_defer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["review", "edit", "review_synthetic", "--json"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code is ExitCode.FAILURE
    assert output["status"] == "unavailable"
    assert "owner_gated" not in output
    assert not {"cutover_ready", "live_healthy", "parity_ready"}.intersection(output)


@pytest.mark.parametrize(
    "action",
    [SocialCompatibilityAction.RETAIN],
)
def test_led_009_social_compatibility_is_implemented_without_a_defer(
    action: SocialCompatibilityAction,
) -> None:
    result = compatibility(action=action, dry_run=True)

    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope["status"] == "implementation-ready"
    assert "required_evidence" not in result.envelope
    assert not {"cutover_ready", "live_healthy", "parity_ready"}.intersection(
        result.envelope
    )


@pytest.mark.parametrize(
    "action",
    [HookCompatibilityAction.RETAIN],
)
def test_hook_002_compatibility_is_implemented_without_a_defer(
    action: HookCompatibilityAction,
) -> None:
    result = TemporaryHookPlanner().compatibility(action)

    assert result.disposition is HookCompatibilityDisposition.IMPLEMENTATION_READY
